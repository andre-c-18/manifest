import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from typing import Dict, Any
from sqlalchemy import text
from app.config import engine
from app.tamu import fetch_active_tamu_from_csv

# ============================================================================
# 1. SETUP LOGGER & DEDICATED LOG FILE (OPSI A)
# ============================================================================
logger = logging.getLogger("ManifestAPI.Database")

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

auto_expire_logger = logging.getLogger("ManifestAPI.AutoExpire")
auto_expire_logger.setLevel(logging.WARNING)

if not auto_expire_logger.handlers:
    log_file_path = os.path.join(LOG_DIR, "auto_expired.log")
    rf_handler = RotatingFileHandler(
        log_file_path, 
        maxBytes=5 * 1024 * 1024, 
        backupCount=5, 
        encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    rf_handler.setFormatter(formatter)
    auto_expire_logger.addHandler(rf_handler)

_logged_auto_expired_cache = set()

STAGING_NODE_ALIASES: Dict[str, str] = {
    "SY-112": "FING-112",
    "SY-113": "FING-113",
    "SY-114": "CRS-114",
}

MAX_STAY_HOURS = 14.0


# ============================================================================
# 2. FUNGSI UTAMA PENARIKAN DATA MANIFEST
# ============================================================================
def fetch_active_clocking_data(tag: str = "manifest", target_date: str = None) -> Dict[str, Any]:
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")

    if not target_date:
        target_date = today_str

    is_today = (target_date == today_str)

    try:
        current_dt = datetime.strptime(target_date, "%Y-%m-%d")
        prev_date = (current_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # Jendela Shift 3 hanya aktif jika melihat HARI INI dan masih dini hari (00:00 - 08:30)
        include_prev_shift3 = is_today and (now_dt.hour < 8 or (now_dt.hour == 8 and now_dt.minute <= 30))
        shift3_start_threshold = f"{prev_date} 20:00:00"

        with engine.connect() as conn:
            # =========================================================================
            # TAHAP 1: PRE-SELECTION TERMINAL MASTER
            # =========================================================================
            q_term = text("""
                SELECT 
                    UPPER(TRIM(COALESCE(server_loc, ''))) AS server_loc,
                    UPPER(TRIM(COALESCE(node_id, ''))) AS node_id,
                    UPPER(TRIM(COALESCE(direction, ''))) AS direction
                FROM `db-hr`.`terminal_master`
                WHERE company_id = '1111' 
                  AND tags LIKE '%manifest%' COLLATE utf8mb4_general_ci
                  AND UPPER(TRIM(COALESCE(direction, ''))) IN ('IN', 'OUT')
                ORDER BY id ASC
            """)
            term_result = conn.execute(q_term).mappings().fetchall()

            if not term_result:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            term_map: Dict[str, str] = {}
            for r in term_result:
                srv = str(r.get('server_loc') or '').strip()
                n_id = str(r.get('node_id') or '').strip()
                direction = str(r.get('direction') or '').strip()

                if not n_id or direction not in ('IN', 'OUT'):
                    continue

                full_node_id = f"{srv}-{n_id}" if srv else n_id
                if full_node_id not in term_map:
                    term_map[full_node_id] = direction

            if not term_map:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            query_nodes = set(term_map.keys())
            for staging_alias, master_node in STAGING_NODE_ALIASES.items():
                if master_node in term_map:
                    query_nodes.add(staging_alias)

            term_in_clause = ", ".join(f"'{k.replace(chr(39), chr(39)*2)}'" for k in sorted(query_nodes))

            # =========================================================================
            # TAHAP 2: AMBIL TRANSAKSI (MENDUKUNG HARI INI & BACKDATE)
            # =========================================================================
            if include_prev_shift3:
                date_filter_sql = """
                    (clocking_date = :target_date 
                     OR (clocking_date = :prev_date AND clocking_time >= :shift3_threshold))
                """
            else:
                date_filter_sql = "clocking_date = :target_date"

            q_trans = text(f"""
                SELECT 
                    card_id,
                    employee_id,
                    clocking_time AS clock_in_time,
                    node_id
                FROM `db-webapps`.`TBL_TACTIVITIES_STAGING_NON_ATTENDANCE`
                WHERE company_id = 1111
                  AND {date_filter_sql}
                  AND node_id IN ({term_in_clause})
                ORDER BY clocking_time ASC
            """)

            trans_result = conn.execute(q_trans, {
                "target_date": target_date,
                "prev_date": prev_date,
                "shift3_threshold": shift3_start_threshold
            }).mappings().fetchall()

            if not trans_result:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            # =========================================================================
            # RESOLUSI ARAH DARI MEMORI PYTHON
            # =========================================================================
            valid_taps = []
            card_set = set()

            for row in trans_result:
                raw_node = str(row.get('node_id') or '').strip().upper()
                node_str = STAGING_NODE_ALIASES.get(raw_node, raw_node)

                direction = term_map.get(node_str)
                if direction not in ('IN', 'OUT'):
                    continue

                tap_time = row['clock_in_time']
                if isinstance(tap_time, str):
                    try:
                        tap_time = datetime.strptime(tap_time[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                elif not isinstance(tap_time, datetime):
                    continue

                card_str = str(row.get('card_id') or '').strip()
                if not card_str:
                    continue

                card_set.add(card_str)
                valid_taps.append({
                    'card_id': card_str,
                    'clock_in_time': tap_time,
                    'direction': direction
                })

            if not valid_taps or not card_set:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            card_in_clause = ", ".join(f"'{c.replace(chr(39), chr(39)*2)}'" for c in card_set)

            # =========================================================================
            # TAHAP 3: AMBIL MASTER COST CENTER & KARYAWAN (TETAP + OS)
            # =========================================================================
            q_occ = text("""
                SELECT id, cost_center, org_name 
                FROM `db-hr`.`org_cost_center`
                ORDER BY id ASC
            """)
            occ_rows = conn.execute(q_occ).mappings().fetchall()

            cc_by_id = {}
            cc_by_code = {}

            for r in occ_rows:
                r_id = str(r['id']).strip() if r.get('id') is not None else ""
                r_code = str(r['cost_center']).strip() if r.get('cost_center') is not None else ""
                r_name = str(r['org_name']).strip() if r.get('org_name') else ""

                if r_id and r_name:
                    cc_by_id[r_id] = r_name
                if r_code and r_code not in cc_by_code and r_name:
                    cc_by_code[r_code] = r_name

            q_tetap = text(f"""
                SELECT 
                    card_no, 
                    employee_id, 
                    employee_name, 
                    cost_center, 
                    'CRS' AS sub_company_name,
                    'TETAP' AS worker_category
                FROM `vw_master_karyawan`
                WHERE card_no IN ({card_in_clause})
            """)
            tetap_rows = conn.execute(q_tetap).mappings().fetchall()

            q_os = text(f"""
                SELECT 
                    card_number AS card_no, 
                    CAST(employee_code AS CHAR) AS employee_id, 
                    employee_name, 
                    org_cc_id,
                    cost_center_id AS cost_center, 
                    sub_company_name,
                    'OS' AS worker_category
                FROM `db-hr`.`vw_master_os_active`
                WHERE card_number IN ({card_in_clause})
            """)
            os_rows = conn.execute(q_os).mappings().fetchall()

            emp_map = {}
            for r in tetap_rows:
                if r.get('card_no'):
                    emp_map[str(r['card_no']).strip()] = dict(r)

            for r in os_rows:
                if r.get('card_no'):
                    emp_map[str(r['card_no']).strip()] = dict(r)

            # =========================================================================
            # TAHAP 4: FILTER KEHADIRAN (HARI INI VS BACKDATE)
            # =========================================================================
            emp_taps: Dict[str, Dict[str, Any]] = {}

            for tap in valid_taps:
                c_id = tap['card_id']
                emp_data = emp_map.get(c_id)
                if not emp_data:
                    continue

                emp_id = str(emp_data.get('employee_id') or c_id).strip()
                tap_time = tap['clock_in_time']
                direction = tap['direction']

                if emp_id not in emp_taps:
                    emp_taps[emp_id] = {
                        'emp_data': emp_data,
                        'min_in': None,
                        'max_in': None,
                        'max_out': None
                    }

                if direction == 'IN':
                    if emp_taps[emp_id]['min_in'] is None or tap_time < emp_taps[emp_id]['min_in']:
                        emp_taps[emp_id]['min_in'] = tap_time
                    if emp_taps[emp_id]['max_in'] is None or tap_time > emp_taps[emp_id]['max_in']:
                        emp_taps[emp_id]['max_in'] = tap_time

                elif direction == 'OUT':
                    if emp_taps[emp_id]['max_out'] is None or tap_time > emp_taps[emp_id]['max_out']:
                        emp_taps[emp_id]['max_out'] = tap_time

            emp_active_on_site: Dict[str, Dict[str, Any]] = {}

            if is_today:
                # -------------------------------------------------------------
                # 🌟 KONDISI A: HARI INI (LIVE REAL-TIME ON-SITE)
                # -------------------------------------------------------------
                for emp_id, data in emp_taps.items():
                    min_in = data['min_in']
                    latest_in = data['max_in']
                    latest_out = data['max_out']

                    # Syarat On-Site Hari Ini:
                    # 1. Pernah clock-in dan status terakhir BUKAN clock-out
                    # 2. Tidak melampaui MAX_STAY_HOURS (14 jam)
                    if latest_in is not None and (latest_out is None or latest_in > latest_out):
                        duration_hours = (now_dt - latest_in).total_seconds() / 3600.0

                        if duration_hours <= MAX_STAY_HOURS:
                            emp_active_on_site[emp_id] = {
                                'emp_data': data['emp_data'],
                                'clock_in_time': min_in
                            }
                        else:
                            # Catat overstay hari ini ke file logs/auto_expired.log
                            cache_key = f"{target_date}_{emp_id}_{latest_in.strftime('%Y%m%d%H%M%S')}"
                            if cache_key not in _logged_auto_expired_cache:
                                _logged_auto_expired_cache.add(cache_key)
                                emp_name = data['emp_data'].get('employee_name', 'UNKNOWN')
                                cc_name = data['emp_data'].get('cost_center', '-')
                                worker_cat = data['emp_data'].get('worker_category', '-')

                                auto_expire_logger.warning(
                                    f"ACTION=AUTO_EXPIRE | NRP={emp_id} | Name={emp_name} | "
                                    f"Type={worker_cat} | CC={cc_name} | Last_Clock_In={latest_in.strftime('%Y-%m-%d %H:%M:%S')} | "
                                    f"Stay_Duration={duration_hours:.2f}h | Reason=No Tap-Out > {MAX_STAY_HOURS}h"
                                )
            else:
                # -------------------------------------------------------------
                # 🌙 KONDISI B: BACKDATE (ON-SITE AKHIR HARI / EOD 23:59:59)
                # -------------------------------------------------------------
                # Hanya mengambil karyawan yang status tapping terakhirnya pada tanggal
                # tersebut adalah IN (belum pernah tapping OUT hingga hari berganti)
                for emp_id, data in emp_taps.items():
                    min_in = data['min_in']
                    latest_in = data['max_in']
                    latest_out = data['max_out']

                    if latest_in is not None and (latest_out is None or latest_in > latest_out):
                        emp_active_on_site[emp_id] = {
                            'emp_data': data['emp_data'],
                            'clock_in_time': min_in
                        }

            # =========================================================================
            # TAHAP 5: GROUPING PER COST CENTER (KARYAWAN TETAP + OS)
            # =========================================================================
            grouped: Dict[str, Dict[str, Any]] = {}

            for emp_id, data in emp_active_on_site.items():
                emp_data = data['emp_data']
                tap_time = data['clock_in_time']
                worker_type = emp_data.get('worker_category', 'TETAP')

                raw_cc = str(emp_data.get('cost_center') or '').strip()
                raw_org_cc_id = str(emp_data.get('org_cc_id') or '').strip()
                sub_company = str(emp_data.get('sub_company_name') or '').strip()

                matched_name = ""

                if worker_type == 'OS':
                    if raw_org_cc_id and raw_org_cc_id in cc_by_id:
                        matched_name = cc_by_id[raw_org_cc_id]
                    elif raw_cc and raw_cc in cc_by_code:
                        matched_name = cc_by_code[raw_cc]
                else:
                    if raw_cc and raw_cc in cc_by_code:
                        matched_name = cc_by_code[raw_cc]

                if matched_name:
                    clean_name = matched_name
                elif sub_company and sub_company.lower() not in ('none', 'null', '', '-'):
                    clean_name = sub_company
                elif raw_cc:
                    clean_name = raw_cc
                else:
                    clean_name = "UNASSIGNED"

                group_key = clean_name
                c_in_str = tap_time.strftime('%d-%b-%Y %H:%M')

                if group_key not in grouped:
                    grouped[group_key] = {
                        'cost_center_id': group_key,
                        'cost_center_code': raw_cc,
                        'cost_center_name': group_key,
                        'employees': []
                    }

                grouped[group_key]['employees'].append({
                    'department': group_key,
                    'id': emp_id,
                    'name': str(emp_data.get('employee_name') or "UNKNOWN"),
                    'clock_in': c_in_str
                })

            # =========================================================================
            # TAHAP 5B: GABUNGKAN DATA TAMU DARI GOOGLE SHEET CSV (COST CENTER: TAMU)
            # =========================================================================
            tamu_list = fetch_active_tamu_from_csv(target_date=target_date)

            if tamu_list:
                grouped['TAMU'] = {
                    'cost_center_id': 'TAMU',
                    'cost_center_code': 'TAMU',
                    'cost_center_name': 'TAMU',
                    'employees': tamu_list
                }

            for cc in grouped.values():
                cc['employees'].sort(key=lambda x: str(x['id']))

            sorted_grouped_list = sorted(list(grouped.values()), key=lambda x: x['cost_center_name'])

            return {'data': sorted_grouped_list, 'source': 'database+gsheet_tamu', 'error': None, 'date': target_date}


    except Exception as e:
        logger.error(f"DB Fetch Error: {str(e)}", exc_info=True)
        return {'data': [], 'source': 'database_error', 'error': str(e), 'date': target_date}
