from sqlalchemy import text
from typing import Dict, Any
from app.config import engine
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("ManifestAPI.Database")

def fetch_active_clocking_data(tag: str = "manifest", target_date: str = None) -> Dict[str, Any]:
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    try:
        with engine.connect() as conn:
            target_date_obj = datetime.strptime(target_date, '%Y-%m-%d')    
            start_dt = target_date_obj.strftime('%Y-%m-%d 00:00:00')
            end_dt = (target_date_obj + timedelta(days=1)).strftime('%Y-%m-%d 08:59:59')

            # =========================================================================
            # TAHAP 1: AMBIL DATA TERMINAL DULU
            # =========================================================================
            q_term = text("""
                SELECT node_id, UPPER(TRIM(COALESCE(direction, ''))) AS direction
                FROM `db-hr`.`terminal_master`
                WHERE company_id = '1111' 
                  AND tags LIKE '%manifest%' COLLATE utf8mb4_general_ci
            """)
            term_result = conn.execute(q_term).mappings().fetchall()
            
            if not term_result:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}
                
            term_map = {str(r['node_id']).strip(): r['direction'] for r in term_result if r.get('node_id')}
            term_in_clause = ", ".join(f"'{k}'" for k in term_map.keys())

            # =========================================================================
            # TAHAP 2: AMBIL PURE TBL_TACTIVITIES
            # =========================================================================
            q_trans = text(f"""
                SELECT 
                    CARD_ID AS card_id,
                    CLOCKING_DATE AS clock_in_time,
                    TERMINAL_ID AS terminal_id
                FROM `db-worksync`.`TBL_TACTIVITIES`
                WHERE CLOCKING_DATE >= :start_dt 
                  AND CLOCKING_DATE <= :end_dt
                  AND TERMINAL_ID IN ({term_in_clause})
            """)

            trans_result = conn.execute(q_trans, {
                "start_dt": start_dt, 
                "end_dt": end_dt
            }).mappings().fetchall()
            
            if not trans_result:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            # =========================================================================
            # LOGIKA SHIFT MALAM DI PYTHON
            # =========================================================================
            valid_taps = []
            card_set = set()
            
            for row in trans_result:
                term_id = str(row['terminal_id']).strip()
                direction = term_map.get(term_id, '')
                tap_time = row['clock_in_time']
                
                if direction == 'OUT' and tap_time.hour <= 8:
                    calc_date = (tap_time - timedelta(days=1)).strftime('%Y-%m-%d')
                else:
                    calc_date = tap_time.strftime('%Y-%m-%d')
                    
                if calc_date == target_date:
                    card_str = str(row['card_id']).strip()
                    card_set.add(card_str)
                    valid_taps.append({
                        'card_id': card_str,
                        'clock_in_time': tap_time,
                        'direction': direction
                    })

            if not valid_taps:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            card_in_clause = ", ".join(f"'{c}'" for c in card_set)

            # =========================================================================
            # TAHAP 3: AMBIL MASTER KARYAWAN (GABUNGAN TETAP & OS)
            # =========================================================================
            # 1. Karyawan Tetap
            q_tetap = text(f"""
                SELECT 
                    card_no, 
                    employee_id, 
                    employee_name, 
                    cost_center, 
                    dept_name,
                    'CRS' AS sub_company_name
                FROM `vw_master_karyawan`
                WHERE card_no IN ({card_in_clause})
            """)
            tetap_rows = conn.execute(q_tetap).mappings().fetchall()

            # 2. Karyawan OS
            q_os = text(f"""
                SELECT 
                    card_number AS card_no, 
                    CAST(employee_code AS CHAR) AS employee_id, 
                    employee_name, 
                    cost_center_id AS cost_center, 
                    cc_name AS dept_name,
                    sub_company_name
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
            # TAHAP 4: IN-MEMORY MERGE & GROUPING FORMAT API
            # =========================================================================
            emp_taps: Dict[str, Dict[str, Any]] = {}

            for tap in valid_taps:
                c_id = tap['card_id']
                emp_data = emp_map.get(c_id)
                
                # Syarat 1: Wajib ada di master data
                if not emp_data:
                    continue
                
                emp_id = str(emp_data.get('employee_id') or c_id).strip()
                tap_time = tap['clock_in_time']
                direction = tap.get('direction', '')

                if emp_id not in emp_taps:
                    emp_taps[emp_id] = {
                        'emp_data': emp_data,
                        'max_in': None,
                        'max_out': None
                    }

                # Catat waktu tap IN dan OUT terbesar (terbaru)
                if direction == 'IN':
                    if emp_taps[emp_id]['max_in'] is None or tap_time > emp_taps[emp_id]['max_in']:
                        emp_taps[emp_id]['max_in'] = tap_time
                elif direction == 'OUT':
                    if emp_taps[emp_id]['max_out'] is None or tap_time > emp_taps[emp_id]['max_out']:
                        emp_taps[emp_id]['max_out'] = tap_time

            # 2. Filter: Hanya karyawan yang punya IN dan belum OUT (atau IN > OUT)
            emp_active_on_site: Dict[str, Dict[str, Any]] = {}

            for emp_id, data in emp_taps.items():
                latest_in = data['max_in']
                latest_out = data['max_out']

                # Syarat utama: Punya IN, dan (tidak pernah OUT atau tap IN terakhir lebih baru dari OUT terakhir)
                if latest_in is not None:
                    if latest_out is None or latest_in > latest_out:
                        emp_active_on_site[emp_id] = {
                            'emp_data': data['emp_data'],
                            'clock_in_time': latest_in
                        }

            # 3. Format struktur grouping departemen / cost center untuk UI
            grouped: Dict[str, Dict[str, Any]] = {}

            for emp_id, data in emp_active_on_site.items():
                emp_data = data['emp_data']
                tap_time = data['clock_in_time']
                
                raw_cc = emp_data.get('cost_center')
                raw_dept = emp_data.get('dept_name')
                sub_company = emp_data.get('sub_company_name')

                # Logika Fallback Cost Center / Department
                cc_valid = raw_cc and str(raw_cc).strip().lower() not in ('', 'none', 'null', 'nan', '-')
                dept_valid = raw_dept and str(raw_dept).strip().lower() not in ('', 'none', 'null', 'nan', '-')
                sub_valid = sub_company and str(sub_company).strip().lower() not in ('', 'none', 'null', 'nan', '-')

                if dept_valid:
                    final_dept_name = str(raw_dept).strip()
                    final_cc_id = str(raw_cc).strip() if cc_valid else final_dept_name
                elif sub_valid:
                    final_dept_name = str(sub_company).strip()
                    final_cc_id = final_dept_name
                else:
                    final_dept_name = "UNASSIGNED"
                    final_cc_id = "UNASSIGNED"

                c_in_str = tap_time.strftime('%d-%b-%Y %H:%M') if isinstance(tap_time, datetime) else str(tap_time)

                if final_cc_id not in grouped:
                    grouped[final_cc_id] = {
                        'cost_center_id': final_cc_id, 
                        'cost_center_name': final_dept_name, 
                        'employees': []
                    }
                
                grouped[final_cc_id]['employees'].append({
                    'department': final_dept_name,
                    'id': emp_id,
                    'name': str(emp_data.get('employee_name') or "UNKNOWN"),
                    'clock_in': c_in_str
                })

            for cc in grouped.values():
                cc['employees'].sort(key=lambda x: x['id'])

            sorted_grouped_list = sorted(list(grouped.values()), key=lambda x: x['cost_center_id'])

            return {'data': sorted_grouped_list, 'source': 'database', 'error': None, 'date': target_date}

    except Exception as e:
        logger.error(f"DB Fetch Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {'data': [], 'source': 'database_error', 'error': str(e), 'date': target_date}