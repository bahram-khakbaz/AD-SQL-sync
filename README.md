# DigiExpress QuantumSync Monitor

**DigiExpress QuantumSync Monitor** is a production-ready synchronization service between **Rahkaran SQL Server** and **Active Directory (LDAP)**. It reads employee data from Rahkaran, updates matching AD users, writes a local PostgreSQL sync cache, and exposes a real-time Flask monitoring dashboard.

This version includes the production fixes validated during deployment:

- Automatic AD `company` update with the exact value `Digi Express`.
- Safer LDAP modify handling; failed AD writes are detected and logged.
- Cache-first behavior after initial validation to avoid unnecessary AD load.
- Optional manual cache rebuild using `RESET_SYNC_CACHE_ON_START`.
- Correct Docker bind mount for `/app/main.py`, so code changes are actually loaded by the running container.

---

## Architecture

```text
Rahkaran SQL Server
        |
        v
Python Sync Engine / Flask Monitor
        |
        +--> Active Directory via LDAP
        |
        +--> PostgreSQL sync_cache
        |
        +--> Web Dashboard :8080 / host port 80
```

---

## What this service syncs

| Rahkaran field | Active Directory attribute / usage |
| --- | --- |
| `EmailAddress` | Used to find AD user by `mail` or `userPrincipalName` |
| `PerssonelCode` | `employeeID` |
| `ContractType` | `contractType` |
| `CostCenterDlTitle` | `costCenterDlTitle` |
| `MobileNumber` | `empMob` |
| `Fix_Var` | `extensionAttribute7` |
| `DepartmentTitleLastLevel` | `department` |
| `PostTitleLastLevel` | `title` |
| `ReportToEmail` | Finds manager DN and updates `manager` |
| `FirstName` + `LastName` | `faDisplayName` |
| Fixed value | `company = your company` |

---

## Production behavior

### Company attribute

Every synced AD user should have:

```text
company = Digi Express
```

The value is configurable:

```env
AD_COMPANY_VALUE=Digi Express
```

If the variable is missing, the application uses `your comany` by default.

### Sync cache behavior

The PostgreSQL table `sync_cache` is used to reduce unnecessary AD traffic.

Normal flow:

1. Fetch employees from Rahkaran.
2. Normalize Rahkaran values.
3. Compare normalized values with `sync_cache`.
4. If nothing changed, skip the user without querying AD.
5. If the user is new or changed, query AD, calculate AD changes, apply LDAP modifications, then update `sync_cache` only after success.

### Manual cache rebuild

Use this only when you intentionally want to validate all records against AD again:

```env
RESET_SYNC_CACHE_ON_START=true
```

After one successful rebuild, set it back to:

```env
RESET_SYNC_CACHE_ON_START=false
```

Do not keep it enabled permanently, because every container restart will truncate and rebuild `sync_cache`.

---

## Zero-to-production setup

### 1. System update

```bash
sudo apt update && sudo apt upgrade -y
```

### 2. Install Docker and Docker Compose

```bash
sudo apt install ca-certificates curl gnupg -y
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y
```

### 3. Create project directory

```bash
mkdir -p /opt/digiexpress-sync/static
cd /opt/digiexpress-sync
chmod -R 755 /opt/digiexpress-sync
```

### 4. Static files

Place dashboard static assets in:

```text
/opt/digiexpress-sync/static
```

Required files include:

```text
bootstrap.rtl.min.css
all.min.css
Orbitron-Regular.ttf
Vazirmatn-Regular.woff2
static/webfonts/fa-solid-900.woff2
```

---

## docker-compose.yml

Use this version so local `main.py` is mounted directly into `/app/main.py`. This prevents the common issue where `/opt/digiexpress-sync/main.py` is edited on the host but the container still runs the old image copy.

```yaml
version: '3.8'

services:
  postgres-db:
    image: mirror2.chabokan.net/postgres:15-alpine
    container_name: digiexpress_internal_db
    environment:
      POSTGRES_USER: admin
      POSTGRES_PASSWORD: MySecretPostgresPass123
      POSTGRES_DB: sync_storage
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U admin -d sync_storage"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: always

  sync-script:
    build: .
    container_name: rahkaran_ad_sync
    depends_on:
      postgres-db:
        condition: service_healthy
    ports:
      - "80:8080"
    environment:
      PG_CONN_STR: postgresql://admin:MySecretPostgresPass123@postgres-db:5432/sync_storage
      SQL_CONN_STR: "DRIVER={ODBC Driver 18 for SQL Server};SERVER=YOUR_SQL_SERVER;DATABASE=YOUR_DATABASE;UID=YOUR_SQL_USER;PWD=YOUR_SQL_PASSWORD;TrustServerCertificate=yes;"
      LDAP_SERVER: ldap://YOUR_DC:389
      LDAP_USER: YOUR_DOMAIN\\YOUR_LDAP_USER
      LDAP_PASSWORD: YOUR_LDAP_PASSWORD
      AD_SEARCH_BASE: DC=digikala,DC=com
      AD_COMPANY_VALUE: Digi Express
      RESET_SYNC_CACHE_ON_START: "false"
    volumes:
      - ./main.py:/app/main.py:ro
      - ./static:/app/static:ro
    restart: always

volumes:
  pgdata:
```

> Replace all placeholder values before production deployment. Do not commit real passwords to GitHub.

---

## Dockerfile

```dockerfile
FROM mirror2.chabokan.net/library/python:3.11-slim

RUN apt-get update && apt-get install -y unixodbc unixodbc-dev g++ curl gnupg2 \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

---

## requirements.txt

```text
Flask
ldap3
psycopg2-binary
pyodbc
```

---

## main.py

```python
import os
import logging
import time
import threading
from datetime import datetime, timedelta
import psycopg2
import pyodbc
from ldap3 import Server, Connection, ALL, MODIFY_REPLACE
from ldap3.utils.conv import escape_filter_chars
from flask import Flask, jsonify, render_template_string, send_from_directory

# --- تنظیمات لاگین برای مانیتورینگ در داکر ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- راه‌اندازی Flask برای مانیتورینگ آنلاین پنل وب ---
app = Flask(__name__)
sync_status = {
    "status": "Initializing",
    "last_sync": "Never",
    "updated_count": 0,
    "skipped_count": 0,
    "next_sync_eta": "Calculating...",
    "errors": [],
    "changes_log": []  # لیست تغییرات اخیر
}

# --- بارگذاری متغیرهای محیطی از داکرکومپوز ---
PG_CONN_STR = os.getenv("PG_CONN_STR", "postgresql://admin:MySecretPostgresPass123@postgres-db:5432/sync_storage")
SQL_CONN_STR = os.getenv("SQL_CONN_STR")

AD_SERVER = os.getenv("LDAP_SERVER", "ldap://DN2-DC01.digikala.com")
AD_USER = os.getenv("LDAP_USER")
AD_PASSWORD = os.getenv("LDAP_PASSWORD")
AD_SEARCH_BASE = os.getenv("AD_SEARCH_BASE", "DC=digikala,DC=com")
COMPANY_VALUE = os.getenv("AD_COMPANY_VALUE", "Digi Express")
RESET_SYNC_CACHE_ON_START = os.getenv("RESET_SYNC_CACHE_ON_START", "false").strip().lower() in ("1", "true", "yes", "y")

# --- قالب گرافیکی HTML داشبورد مانیتورینگ ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>DigiExpress Quantum Monitor v9.1</title>
    <link href="/static/bootstrap.rtl.min.css" rel="stylesheet">
    <link rel="stylesheet" href="/static/all.min.css">
    <style>
        /* لود فونت‌های دانلود شده از لوکال */
        @font-face {
            font-family: 'Orbitron';
            src: url('/static/Orbitron-Regular.ttf') format('truetype');
            font-weight: normal; font-style: normal;
        }
        @font-face {
            font-family: 'Vazirmatn';
            src: url('/static/Vazirmatn-Regular.woff2') format('woff2');
        }

        :root { --accent: #f43f5e; --accent-glow: rgba(244, 63, 94, 0.4); --glass: rgba(13, 17, 23, 0.6); }

        body {
            direction: ltr;
            background: radial-gradient(circle at top right, #1a1f35, #000000);
            background-color: #050505;
            /* پترن شبکه با CSS خالص (جایگزین grid.svg) */
            background-image: linear-gradient(rgba(244, 63, 94, 0.04) 1px, transparent 1px),
                              linear-gradient(90deg, rgba(244, 63, 94, 0.04) 1px, transparent 1px);
            background-size: 40px 40px;
            color: #e6edf3; font-family: 'Vazirmatn', sans-serif; min-height: 100vh;
        }

        @keyframes pulseGlow {
            0% { box-shadow: 0 0 5px rgba(255,255,255,0.02); border-color: rgba(255, 255, 255, 0.05); }
            50% { box-shadow: 0 0 20px var(--accent-glow); border-color: rgba(244, 63, 94, 0.3); }
            100% { box-shadow: 0 0 5px rgba(255,255,255,0.02); border-color: rgba(255, 255, 255, 0.05); }
        }

        .quantum-card {
            background: var(--glass);
            backdrop-filter: blur(25px);
            border: 2px solid rgba(255, 255, 255, 0.05);
            border-radius: 20px;
            padding: 30px;
            animation: pulseGlow 4s infinite ease-in-out;
            transition: 0.3s;
        }
        .quantum-card:hover {
            transform: translateY(-5px) scale(1.01);
            animation: none; /* توقف انیمیشن هنگام هوور */
            border-color: rgba(244, 63, 94, 0.5);
            box-shadow: 0 8px 32px var(--accent-glow);
        }

        .gradient-text { background: linear-gradient(to right, #ffffff, #a0a0a0); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-family: 'Orbitron', sans-serif; }
        .red-gradient-text { background: linear-gradient(to right, #f43f5e, #fb7185); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-family: 'Orbitron', sans-serif; }

        .value-display { font-size: 3rem; font-weight: 700; font-family: 'Orbitron', sans-serif; text-shadow: 0 0 10px rgba(255,255,255,0.5); }

        .log-panel {
            background: rgba(0, 0, 0, 0.5);
            border-radius: 15px; padding: 20px;
            max-height: 250px; overflow-y: auto;
            border: 1px solid rgba(255,255,255,0.05);
        }

        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: var(--accent); border-radius: 2px; }

        .status-badge {
            background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3);
            color: #22c55e; padding: 5px 15px; border-radius: 30px;
            font-size: 0.85rem; font-weight: 700; text-transform: uppercase;
        }
    </style>
</head>
<body class="p-5">
    <div class="container-fluid">
        <div class="d-flex justify-content-between align-items-center mb-5 pb-3 border-bottom border-secondary border-opacity-25">
            <h1 class="gradient-text mb-0 fs-2 fw-bold">DIGI<span class="red-gradient-text">EXPRESS</span> AD-RK Sync Monitor</h1>
            <div class="d-flex align-items-center gap-3">
                <span class= "small uppercase" style="color: white; font-family: 'Orbitron', sans-serif; letter-spacing: 1px;">Infrastructure Console</span>
            </div>
        </div>

        <div class="quantum-card mb-4 d-flex justify-content-between align-items-center">
            <div class="d-flex align-items-center gap-4">
                <i class="fa-solid fa-satellite-dish text-danger fs-3"></i>
                <div>
                    <div class="small uppercase text-secondary">Service status</div>
                    <div class="status-badge"><i class="fa-solid fa-atom fa-spin me-2"></i>{{ status_data.status }}</div>
                </div>
            </div>
            <div class="text-end">
                <div class="small uppercase text-secondary">Last Sync timestamp</div>
                <div class="h5 mb-0 text-light">{{ status_data.last_sync }}</div>
            </div>
        </div>

        <div class="row g-4 mb-5">
            <div class="col-md-4"><div class="quantum-card text-center"><div class="small uppercase text-secondary">Successful Updates</div><div class="value-display text-success">{{ status_data.updated_count }}</div></div></div>
            <div class="col-md-4"><div class="quantum-card text-center"><div class="small uppercase text-secondary">Skipped Records</div><div class="value-display text-info">{{ status_data.skipped_count }}</div></div></div>
            <div class="col-md-4"><div class="quantum-card text-center"><div class="small uppercase text-secondary">ETA until next cycle</div><div class="value-display text-warning" style="font-size: 2.2rem;">{{ status_data.next_sync_eta }}</div></div></div>
        </div>

        <div class="row g-4">
            <div class="col-md-6">
                <h6 class="text-success mb-3 fw-bold uppercase fs-6"><i class="fa-solid fa-wave-square me-2"></i>Recent Change Events</h6>
                <div class="log-panel quantum-card">
                    {% for log in status_data.changes_log %}<div class="mb-2 pb-2 border-bottom border-dark">{{ log }}</div>{% else %}No active events.{% endfor %}
                </div>
            </div>
            <div class="col-md-6">
                <h6 class="text-danger mb-3 fw-bold uppercase fs-6"><i class="fa-solid fa-heartbeat me-2"></i>Critical Error Logs</h6>
                <div class="log-panel quantum-card text-danger">
                    {% for err in status_data.errors %}<div class="mb-2 pb-2 border-bottom border-dark">{{ err }}</div>{% else %}All systems nominal.{% endfor %}
                </div>
            </div>
        </div>
    </div>
</body>
</html>
"""

def get_rahkaran_data():
    logger.info("Fetching active employee data from Rahkaran SQL Server...")
    query = """
        SELECT
            EmailAddress AS Email,
            PerssonelCode AS PersonnelCode,
            ContractType,
            CostCenterDlTitle,
            MobileNumber AS EmpMob,
            Fix_Var AS ExtensionAttribute7,
            DepartmentTitleLastLevel AS Department,
            PostTitleLastLevel AS JobTitle,
            ReportToEmail AS ManagerEmail,
            FirstName,
            LastName
        FROM DigiExpress.dbo.EmployeeTotalTable
        WHERE EmailAddress IS NOT NULL
          AND EmailAddress <> ''
          AND EmailAddress LIKE '%@%'
    """
    try:
        conn = pyodbc.connect(SQL_CONN_STR)
        cursor = conn.cursor()
        cursor.execute(query)
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        conn.close()
        logger.info(f"Successfully fetched {len(results)} records from Rahkaran.")
        return results
    except Exception as e:
        logger.error(f"Failed to fetch data from Rahkaran SQL Server: {e}")
        raise e

def find_ad_user_by_email(ad_conn, email):
    safe_email = escape_filter_chars(email)
    search_filter = f"(|(mail={safe_email})(userPrincipalName={safe_email}))"
    ad_conn.search(
        search_base=AD_SEARCH_BASE,
        search_filter=search_filter,
        attributes=[
            'distinguishedName', 'employeeID', 'contractType',
            'costCenterDlTitle', 'empMob', 'extensionAttribute7',
            'department', 'title', 'manager', 'faDisplayName',
            'company'
        ]
    )
    if ad_conn.entries:
        return ad_conn.entries[0]
    return None

def find_ad_manager_dn_by_email(ad_conn, manager_email):
    if not manager_email:
        return None
    safe_email = escape_filter_chars(manager_email)
    search_filter = f"(|(mail={safe_email})(userPrincipalName={safe_email}))"
    ad_conn.search(search_base=AD_SEARCH_BASE, search_filter=search_filter, attributes=['distinguishedName'])
    if ad_conn.entries:
        return str(ad_conn.entries[0].distinguishedName)
    return None

def main_loop(rebuild_cache=False):
    """
    rebuild_cache=True:
        - sync_cache را خالی می‌کند.
        - برای همه رکوردهای راهکاران با AD چک می‌کند.
        - اگر لازم بود AD را آپدیت می‌کند.
        - بعد از موفقیت، cache را از نو می‌سازد.

    rebuild_cache=False:
        - اول فقط راهکاران را با sync_cache مقایسه می‌کند.
        - اگر داده نسبت به cache تغییری نکرده باشد، اصلاً به AD وصل نمی‌شود.
        - فقط برای رکوردهای جدید/تغییرکرده به AD وصل می‌شود و آپدیت می‌زند.
    """
    global sync_status
    logger.info("Starting synchronization cycle... rebuild_cache=%s", rebuild_cache)

    try:
        employees = get_rahkaran_data()
    except Exception as e:
        sync_status["status"] = "Failed (Rahkaran Connection/Query Error)"
        sync_status["errors"].append(f"Rahkaran Error: {str(e)}")
        return

    if not employees:
        logger.warning("No data found from Rahkaran. Skipping this cycle.")
        sync_status["status"] = "Skipped (No Data)"
        return

    try:
        pg_conn = psycopg2.connect(PG_CONN_STR)
        pg_cursor = pg_conn.cursor()
        pg_cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_cache (
                email VARCHAR(255) PRIMARY KEY,
                employee_id VARCHAR(100),
                contract_type VARCHAR(100),
                cost_center_title VARCHAR(255),
                emp_mob VARCHAR(100),
                ext_attribute7 VARCHAR(100),
                department VARCHAR(255),
                job_title VARCHAR(255),
                manager_email VARCHAR(255),
                manager_dn TEXT,
                fa_display_name VARCHAR(255),
                company VARCHAR(255)
            )
        """)
        # برای سازگاری با جدول قبلی که manager_email نداشت
        pg_cursor.execute("ALTER TABLE sync_cache ADD COLUMN IF NOT EXISTS manager_email VARCHAR(255)")
        pg_conn.commit()

        if rebuild_cache:
            logger.warning("Initial rebuild requested. Truncating sync_cache before AD validation...")
            pg_cursor.execute("TRUNCATE TABLE sync_cache")
            pg_conn.commit()
    except Exception as e:
        logger.error(f"PostgreSQL connection/init failed: {e}")
        sync_status["status"] = "Failed (Local DB Cache Error)"
        sync_status["errors"].append(f"DB Error: {str(e)}")
        return

    ad_conn = None

    def ensure_ad_connection():
        nonlocal ad_conn
        if ad_conn is not None and ad_conn.bound:
            return ad_conn
        try:
            server = Server(AD_SERVER, get_info=ALL)
            ad_conn = Connection(server, user=AD_USER, password=AD_PASSWORD, auto_bind=True)
            logger.info("Connected successfully to Active Directory.")
            return ad_conn
        except Exception as e:
            logger.error(f"LDAP connection failed: {e}")
            raise Exception(f"AD Connection Error: {str(e)}")

    def get_ad_val_safe(user_obj, attr_name):
        attr = getattr(user_obj, attr_name, None)
        if attr is None:
            return ""
        if hasattr(attr, 'values') and attr.values:
            return str(attr.values[0]).strip()
        if hasattr(attr, 'value') and attr.value:
            if isinstance(attr.value, list):
                return str(attr.value[0]).strip()
            return str(attr.value).strip()
        val_str = str(attr).strip()
        return "" if val_str.startswith('[') or val_str.endswith(']') else val_str

    def upsert_cache(email, emp_id, contract_type, cost_center, emp_mob, ext_attr7,
                     department, job_title, manager_email, manager_dn, fa_display_name, company):
        pg_cursor.execute("""
            INSERT INTO sync_cache (
                email, employee_id, contract_type, cost_center_title, emp_mob,
                ext_attribute7, department, job_title, manager_email, manager_dn,
                fa_display_name, company
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET
                employee_id=EXCLUDED.employee_id,
                contract_type=EXCLUDED.contract_type,
                cost_center_title=EXCLUDED.cost_center_title,
                emp_mob=EXCLUDED.emp_mob,
                ext_attribute7=EXCLUDED.ext_attribute7,
                department=EXCLUDED.department,
                job_title=EXCLUDED.job_title,
                manager_email=EXCLUDED.manager_email,
                manager_dn=EXCLUDED.manager_dn,
                fa_display_name=EXCLUDED.fa_display_name,
                company=EXCLUDED.company
        """, (
            email, emp_id, contract_type, cost_center, emp_mob, ext_attr7,
            department, job_title, manager_email, manager_dn, fa_display_name, company
        ))
        pg_conn.commit()

    updated_count = 0
    skipped_count = 0

    for emp in employees:
        try:
            email = emp['Email'].strip().lower()
            emp_id = str(emp['PersonnelCode']).strip() if emp['PersonnelCode'] else ""
            contract_type = emp['ContractType'].strip() if emp['ContractType'] else ""
            cost_center = emp['CostCenterDlTitle'].strip() if emp['CostCenterDlTitle'] else ""
            emp_mob = str(emp['EmpMob']).strip() if emp['EmpMob'] else ""
            ext_attr7 = str(emp['ExtensionAttribute7']).strip() if emp['ExtensionAttribute7'] else ""
            department = emp['Department'].strip() if emp['Department'] else ""
            job_title = emp['JobTitle'].strip() if emp['JobTitle'] else ""
            manager_email = emp['ManagerEmail'].strip().lower() if emp['ManagerEmail'] else ""
            first_name = emp['FirstName'].strip() if emp['FirstName'] else ""
            last_name = emp['LastName'].strip() if emp['LastName'] else ""
            fa_display_name = f"{first_name} {last_name}".strip()
            company = COMPANY_VALUE

            # در سیکل‌های عادی، اول با cache مقایسه می‌کنیم تا بی‌دلیل به AD فشار نیاید.
            # manager_dn را اینجا مقایسه نمی‌کنیم چون برای ساختنش باید AD سرچ شود؛ manager_email را cache کرده‌ایم.
            if not rebuild_cache:
                pg_cursor.execute("""
                    SELECT employee_id, contract_type, cost_center_title, emp_mob,
                           ext_attribute7, department, job_title, manager_email,
                           fa_display_name, company
                    FROM sync_cache
                    WHERE email = %s
                """, (email,))
                cached = pg_cursor.fetchone()

                current_signature = (
                    emp_id, contract_type, cost_center, emp_mob, ext_attr7,
                    department, job_title, manager_email, fa_display_name, company
                )

                if cached and tuple("" if v is None else str(v) for v in cached) == current_signature:
                    skipped_count += 1
                    continue

            conn = ensure_ad_connection()
            ad_user = find_ad_user_by_email(conn, email)
            if not ad_user:
                skipped_count += 1
                logger.warning("AD user not found for email: %s", email)
                continue

            user_dn = str(ad_user.distinguishedName)
            manager_dn = ""
            if manager_email:
                manager_dn = find_ad_manager_dn_by_email(conn, manager_email) or ""

            ad_emp_id = get_ad_val_safe(ad_user, 'employeeID')
            ad_contract = get_ad_val_safe(ad_user, 'contractType')
            ad_cost_center = get_ad_val_safe(ad_user, 'costCenterDlTitle')
            ad_mob = get_ad_val_safe(ad_user, 'empMob')
            ad_ext7 = get_ad_val_safe(ad_user, 'extensionAttribute7')
            ad_dept = get_ad_val_safe(ad_user, 'department')
            ad_title = get_ad_val_safe(ad_user, 'title')
            ad_fa_name = get_ad_val_safe(ad_user, 'faDisplayName')
            ad_manager = get_ad_val_safe(ad_user, 'manager')
            ad_company = get_ad_val_safe(ad_user, 'company')

            changes = {}
            if ad_emp_id != emp_id: changes['employeeID'] = [(MODIFY_REPLACE, [emp_id])]
            if ad_contract != contract_type: changes['contractType'] = [(MODIFY_REPLACE, [contract_type])]
            if ad_cost_center != cost_center: changes['costCenterDlTitle'] = [(MODIFY_REPLACE, [cost_center])]
            if ad_mob != emp_mob: changes['empMob'] = [(MODIFY_REPLACE, [emp_mob])]
            if ad_ext7 != ext_attr7: changes['extensionAttribute7'] = [(MODIFY_REPLACE, [ext_attr7])]
            if ad_dept != department: changes['department'] = [(MODIFY_REPLACE, [department])]
            if ad_title != job_title: changes['title'] = [(MODIFY_REPLACE, [job_title])]
            if ad_fa_name != fa_display_name: changes['faDisplayName'] = [(MODIFY_REPLACE, [fa_display_name])]
            if ad_company != company: changes['company'] = [(MODIFY_REPLACE, [company])]
            if ad_manager.lower() != manager_dn.lower():
                changes['manager'] = [(MODIFY_REPLACE, [manager_dn])] if manager_dn else [(MODIFY_REPLACE, [])]

            if changes:
                logger.info(f"Applying AD changes for {email}: {list(changes.keys())}")
                ok = conn.modify(user_dn, changes)
                if not ok:
                    raise Exception(f"LDAP modify failed: {conn.result}")

                updated_count += 1
                change_info = f"Update: {email} | Fields: {list(changes.keys())}"
                sync_status["changes_log"].insert(0, change_info)
                if len(sync_status["changes_log"]) > 10:
                    sync_status["changes_log"].pop()
            else:
                skipped_count += 1

            # cache فقط بعد از validate واقعی AD و موفقیت modify/no-change نوشته می‌شود.
            upsert_cache(
                email, emp_id, contract_type, cost_center, emp_mob, ext_attr7,
                department, job_title, manager_email, manager_dn, fa_display_name, company
            )

        except Exception as e:
            err_msg = f"Failed to sync {emp.get('Email')}: {e}"
            logger.error(err_msg)
            if len(sync_status["errors"]) < 10:
                sync_status["errors"].insert(0, err_msg)

    try:
        pg_cursor.close()
        pg_conn.close()
    finally:
        if ad_conn is not None and ad_conn.bound:
            ad_conn.unbind()

    logger.info(f"Sync complete. Updated: {updated_count}, Skipped: {skipped_count}")
    sync_status.update({
        "status": "Success",
        "last_sync": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_count": updated_count,
        "skipped_count": skipped_count
    })

def sleep_until_midnight():
    global sync_status
    while True:
        now = datetime.now()
        tomorrow_midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        seconds_to_wait = (tomorrow_midnight - now).total_seconds()
        hours = int(seconds_to_wait // 3600)
        minutes = int((seconds_to_wait % 3600) // 60)
        sync_status["next_sync_eta"] = f"{hours}h {minutes}m"
        if seconds_to_wait <= 60: break
        time.sleep(60)

def sync_scheduler_thread():
    global sync_status
    logger.info("Running initial sync cycle. RESET_SYNC_CACHE_ON_START=%s", RESET_SYNC_CACHE_ON_START)
    sync_status["status"] = "Running Initial Cycle"
    main_loop(rebuild_cache=RESET_SYNC_CACHE_ON_START)
    while True:
        sync_status["status"] = "Waiting until midnight"
        sleep_until_midnight()
        logger.info("Midnight reached. Executing formal sync cycle...")
        sync_status["status"] = "Running Formal Sync Cycle"
        main_loop(rebuild_cache=False)
        time.sleep(60)

@app.route('/')
def get_status():
    return render_template_string(DASHBOARD_HTML, status_data=sync_status)

@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/api/json')
def get_json_api():
    return jsonify(sync_status)

@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

@app.route('/webfonts/<path:filename>')
def serve_webfonts(filename):
    # هر درخواستی که به /webfonts/ بیاید، به /static/webfonts/ هدایت می‌شود
    return send_from_directory('static/webfonts', filename)

if __name__ == "__main__":
    threading.Thread(target=sync_scheduler_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
```

---

## .gitignore

```text
.env
*.log
__pycache__/
*.pyc
*.db
sync_data.json
.env.*
!.env.example
```

---

## Deployment

### Build deployment

Use this when the server has access to all required images and packages:

```bash
cd /opt/digiexpress-sync
docker compose up -d --build
docker logs -f rahkaran_ad_sync
```

### Update Python code without internet

If the image already exists and you changed only `main.py`, either restart the service when using the recommended bind mount:

```bash
cd /opt/digiexpress-sync
docker restart rahkaran_ad_sync
docker logs -f rahkaran_ad_sync
```

Or copy the file directly into the running container:

```bash
docker cp /opt/digiexpress-sync/main.py rahkaran_ad_sync:/app/main.py
docker restart rahkaran_ad_sync
docker logs -f rahkaran_ad_sync
```

Verify the running container has the correct code:

```bash
docker exec rahkaran_ad_sync sh -lc "grep -n "Digi Express\|company\|RESET_SYNC_CACHE" /app/main.py | head -80"
```

Expected output should include:

```text
COMPANY_VALUE = os.getenv("AD_COMPANY_VALUE", "Digi Express")
```

---

## Manual cache rebuild procedure

1. Set in compose or environment:

```env
RESET_SYNC_CACHE_ON_START=true
```

2. Restart:

```bash
docker restart rahkaran_ad_sync
```

3. Watch logs:

```bash
docker logs -f rahkaran_ad_sync
```

4. After successful sync, set back:

```env
RESET_SYNC_CACHE_ON_START=false
```

5. Restart again:

```bash
docker restart rahkaran_ad_sync
```

---

## Useful PostgreSQL checks

Open psql:

```bash
docker exec -it digiexpress_internal_db psql -U admin -d sync_storage
```

Count cache rows:

```sql
SELECT COUNT(*) FROM sync_cache;
```

Check a user:

```sql
SELECT email, company
FROM sync_cache
WHERE email = 'vahid.khazaei@digikala.com';
```

Delete one user from cache for a targeted retest:

```sql
DELETE FROM sync_cache
WHERE email = 'vahid.khazaei@digikala.com';
```

Do not truncate the full table unless you intentionally want a full AD revalidation.

---

## Useful AD checks

Check the `company` attribute in Active Directory:

```powershell
Get-ADUser -LDAPFilter "(|(mail=vahid.khazaei@digikala.com)(userPrincipalName=vahid.khazaei@digikala.com))" -Properties company |
Select Name,SamAccountName,Company
```

Search by mail or UPN:

```powershell
Get-ADUser -LDAPFilter "(|(mail=user@domain.com)(userPrincipalName=user@domain.com))" -Properties mail,userPrincipalName,company |
Select Name,SamAccountName,mail,userPrincipalName,company
```

---

## Log examples

Successful company update:

```text
Applying AD changes for vahid.khazaei@digikala.com: ['company']
Sync complete. Updated: 413, Skipped: 2106
```

AD user not found:

```text
AD user not found for email: user@domain.com
```

LDAP permission or object-level issue:

```text
LDAP modify failed: {'result': 50, 'description': 'insufficientAccessRights'}
```

This can happen for stale/disabled/moved AD users, users whose email was removed, or attributes delegated differently across OUs.

---

## Troubleshooting

| Issue | Cause | Fix |
| --- | --- | --- |
| `company` does not update | Container still runs old `/app/main.py` | Run `docker exec ... grep` and use bind mount or `docker cp` |
| `company` is in cache but not AD | Old code updated cache without checking LDAP result | Use this version; delete one row from cache and retest |
| Lots of `AD user not found` | AD user has no `mail`/`userPrincipalName`, or user left organization | Expected for stale users; verify AD object if needed |
| `insufficientAccessRights` | LDAP account cannot write that object/attribute, or object is stale/moved | Check OU delegation and object status |
| Dashboard static 404 | Missing local static assets | Verify files in `/opt/digiexpress-sync/static` |
| Code changed but behavior did not | No bind mount and no rebuild/copy | Use `docker cp main.py rahkaran_ad_sync:/app/main.py` then restart |

---

## Git workflow

```bash
cd /opt/digiexpress-sync
git status
git add README.md main.py Dockerfile docker-compose.yml requirements.txt .gitignore
git commit -m "Update AD sync cache logic and company attribute handling"
git push
```

---

## Security notes

Never commit real values for:

- SQL username/password
- LDAP username/password
- production server IPs if internal policy forbids it
- `.env` files
- logs containing employee data

Use environment variables or a protected `.env` file on the server.
