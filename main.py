import os
import sys
import logging
import time
import threading
from datetime import datetime, timedelta
import psycopg2
import pyodbc
from ldap3 import Server, Connection, ALL, MODIFY_REPLACE
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
    "changes_log": [], # اضافه کردن لیست تغییرات اخیر
    "errors": []       # لیست خطاها
}

# --- بارگذاری متغیرهای محیطی از داکرکومپوز ---
PG_CONN_STR = os.getenv("PG_CONN_STR", "postgresql://your user:your password@postgres-db:5432/sync_storage")
SQL_CONN_STR = os.getenv("SQL_CONN_STR")

AD_SERVER = os.getenv("LDAP_SERVER", "ldap://Your Ldap Server")
AD_USER = os.getenv("LDAP_USER")
AD_PASSWORD = os.getenv("LDAP_PASSWORD")
AD_SEARCH_BASE = os.getenv("AD_SEARCH_BASE", "DC=your domain,DC=com")

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
    search_filter = f"(|(mail={email})(userPrincipalName={email}))"
    ad_conn.search(
        search_base=AD_SEARCH_BASE,
        search_filter=search_filter,
        attributes=[
            'distinguishedName', 'employeeID', 'contractType',
            'costCenterDlTitle', 'empMob', 'extensionAttribute7',
            'department', 'title', 'manager', 'faDisplayName'
        ]
    )
    if ad_conn.entries:
        return ad_conn.entries[0]
    return None

def find_ad_manager_dn_by_email(ad_conn, manager_email):
    if not manager_email:
        return None
    search_filter = f"(|(mail={manager_email})(userPrincipalName={manager_email}))"
    ad_conn.search(search_base=AD_SEARCH_BASE, search_filter=search_filter, attributes=['distinguishedName'])
    if ad_conn.entries:
        return str(ad_conn.entries[0].distinguishedName)
    return None

def main_loop():
    global sync_status
    logger.info("Starting synchronization cycle...")

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
        server = Server(AD_SERVER, get_info=ALL)
        ad_conn = Connection(server, user=AD_USER, password=AD_PASSWORD, auto_bind=True)
        logger.info("Connected successfully to Active Directory.")
    except Exception as e:
        logger.error(f"LDAP connection failed: {e}")
        sync_status["status"] = "Failed (AD LDAP Connection Error)"
        sync_status["errors"].append(f"AD Connection Error: {str(e)}")
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
                manager_dn TEXT,
                fa_display_name VARCHAR(255)
            )
        """)
        pg_conn.commit()
    except Exception as e:
        logger.error(f"PostgreSQL connection/init failed: {e}")
        ad_conn.unbind()
        sync_status["status"] = "Failed (Local DB Cache Error)"
        sync_status["errors"].append(f"DB Error: {str(e)}")
        return

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

            ad_user = find_ad_user_by_email(ad_conn, email)
            if not ad_user:
                skipped_count += 1
                continue

            user_dn = str(ad_user.distinguishedName)
            manager_dn = ""
            if manager_email:
                manager_dn = find_ad_manager_dn_by_email(ad_conn, manager_email) or ""

            pg_cursor.execute("SELECT employee_id, contract_type, cost_center_title, emp_mob, ext_attribute7, department, job_title, manager_dn, fa_display_name FROM sync_cache WHERE email = %s", (email,))
            cached = pg_cursor.fetchone()

            has_changes = False
            if not cached or (cached[0] != emp_id or cached[1] != contract_type or cached[2] != cost_center or cached[3] != emp_mob or cached[4] != ext_attr7 or cached[5] != department or cached[6] != job_title or cached[7] != manager_dn or cached[8] != fa_display_name):
                has_changes = True

            if not has_changes:
                skipped_count += 1
                continue

            def get_ad_val_safe(user_obj, attr_name):
                attr = getattr(user_obj, attr_name, None)
                if attr is None: return ""
                if hasattr(attr, 'values') and attr.values: return str(attr.values[0]).strip()
                if hasattr(attr, 'value') and attr.value:
                    if isinstance(attr.value, list): return str(attr.value[0]).strip()
                    return str(attr.value).strip()
                val_str = str(attr).strip()
                return "" if val_str.startswith('[') or val_str.endswith(']') else val_str

            ad_emp_id = get_ad_val_safe(ad_user, 'employeeID')
            ad_contract = get_ad_val_safe(ad_user, 'contractType')
            ad_cost_center = get_ad_val_safe(ad_user, 'costCenterDlTitle')
            ad_mob = get_ad_val_safe(ad_user, 'empMob')
            ad_ext7 = get_ad_val_safe(ad_user, 'extensionAttribute7')
            ad_dept = get_ad_val_safe(ad_user, 'department')
            ad_title = get_ad_val_safe(ad_user, 'title')
            ad_fa_name = get_ad_val_safe(ad_user, 'faDisplayName')
            ad_manager = get_ad_val_safe(ad_user, 'manager')

            changes = {}
            if ad_emp_id != emp_id: changes['employeeID'] = [(MODIFY_REPLACE, [emp_id])]
            if ad_contract != contract_type: changes['contractType'] = [(MODIFY_REPLACE, [contract_type])]
            if ad_cost_center != cost_center: changes['costCenterDlTitle'] = [(MODIFY_REPLACE, [cost_center])]
            if ad_mob != emp_mob: changes['empMob'] = [(MODIFY_REPLACE, [emp_mob])]
            if ad_ext7 != ext_attr7: changes['extensionAttribute7'] = [(MODIFY_REPLACE, [ext_attr7])]
            if ad_dept != department: changes['department'] = [(MODIFY_REPLACE, [department])]
            if ad_title != job_title: changes['title'] = [(MODIFY_REPLACE, [job_title])]
            if ad_fa_name != fa_display_name: changes['faDisplayName'] = [(MODIFY_REPLACE, [fa_display_name])]
            if ad_manager.lower() != manager_dn.lower():
                changes['manager'] = [(MODIFY_REPLACE, [manager_dn])] if manager_dn else [(MODIFY_REPLACE, [])]

            if changes:
                try:
                    # اعمال تغییرات در AD
                    for attr, mod in changes.items():
                        ad_conn.modify(user_dn, {attr: mod[0]})

                    # ثبت در دیتابیس محلی
                    pg_cursor.execute("""
                        INSERT INTO sync_cache (email, employee_id, contract_type, cost_center_title, emp_mob, ext_attribute7, department, job_title, manager_dn, fa_display_name)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET employee_id=EXCLUDED.employee_id, contract_type=EXCLUDED.contract_type, cost_center_title=EXCLUDED.cost_center_title, emp_mob=EXCLUDED.emp_mob, ext_attribute7=EXCLUDED.ext_attribute7, department=EXCLUDED.department, job_title=EXCLUDED.job_title, manager_dn=EXCLUDED.manager_dn, fa_display_name=EXCLUDED.fa_display_name
                    """, (email, emp_id, contract_type, cost_center, emp_mob, ext_attr7, department, job_title, manager_dn, fa_display_name))
                    pg_conn.commit()

                    # ثبت تغییر موفق برای نمایش در داشبورد
                    updated_count += 1
                    change_info = f"Update: {email} | Fields: {list(changes.keys())}"
                    sync_status["changes_log"].insert(0, change_info)
                    if len(sync_status["changes_log"]) > 10: sync_status["changes_log"].pop()

                except Exception as e:
                    # ثبت خطا در لیست خطاهای داشبورد
                    err_msg = f"Error {email}: {str(e)}"
                    logger.error(err_msg)
                    if len(sync_status["errors"]) < 10: sync_status["errors"].insert(0, err_msg)

        except Exception as e:
            err_msg = f"Failed to sync {emp.get('Email')}: {e}"
            logger.error(err_msg)
            if len(sync_status["errors"]) < 10: sync_status["errors"].insert(0, err_msg)

    pg_cursor.close()
    pg_conn.close()
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
    logger.info("Running an initial sync cycle...")
    sync_status["status"] = "Running Initial Cycle"
    main_loop()
    while True:
        sync_status["status"] = "Waiting until midnight"
        sleep_until_midnight()
        logger.info("Midnight reached. Executing formal sync cycle...")
        sync_status["status"] = "Running Formal Sync Cycle"
        main_loop()
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


-----------------

### 🔒 Security Protocol (`.gitignore`)

To ensure organizational data and credentials are never pushed to GitHub, always use the following `.gitignore`:

```text
.env
*.log
__pycache__/
*.db
sync_data.json
