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
            # 1. Ambil Master org_cost_center (Di-cache ke memori)
            q_occ = text("""
                SELECT id, cost_center, org_name 
                FROM `db-hr`.`org_cost_center`
                ORDER BY id ASC
            """)
            occ_rows = conn.execute(q_occ).mappings().fetchall()

            # Mapping O(1) di Python
            cc_by_id = {}
            cc_by_code = {}

            for r in occ_rows:
                r_id = str(r['id']).strip() if r.get('id') is not None else ""
                r_code = str(r['cost_center']).strip() if r.get('cost_center') is not None else ""
                r_name = str(r['org_name']).strip() if r.get('org_name') else ""

                if r_id and r_name:
                    cc_by_id[r_id] = r_name

                # Hanya ambil nilai pertama jika ada kode cost_center yang kembar
                if r_code and r_code not in cc_by_code and r_name:
                    cc_by_code[r_code] = r_name

            # 2. Ambil Master Karyawan Tetap
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

            # 3. Ambil Master Karyawan OS (Ambil org_cc_id dan cost_center_id)
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
            # TAHAP 4: FILTER ON-SITE (IN > OUT), RESOLUSI CC, & GROUPING
            # =========================================================================
            # 1. Kumpulkan histori tap per karyawan
            emp_taps: Dict[str, Dict[str, Any]] = {}

            for tap in valid_taps:
                c_id = tap['card_id']
                emp_data = emp_map.get(c_id)
                if not emp_data:
                    continue
                
                emp_id = str(emp_data.get('employee_id') or c_id).strip()
                tap_time = tap['clock_in_time']
                direction = tap.get('direction', '')

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

            for emp_id, data in emp_taps.items():
                min_in = data['min_in']
                latest_in = data['max_in']
                latest_out = data['max_out']

                if latest_in is not None:
                    if latest_out is None or latest_in > latest_out:
                        emp_active_on_site[emp_id] = {
                            'emp_data': data['emp_data'],
                            'clock_in_time': min_in
                        }

            # 3. Format struktur grouping dengan nama dari org_cost_center
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
                final_id = group_key
                final_name = clean_name

                c_in_str = tap_time.strftime('%d-%b-%Y %H:%M') if isinstance(tap_time, datetime) else str(tap_time)

                if group_key not in grouped:
                    grouped[group_key] = {
                        'cost_center_id': final_id,
                        'cost_center_code': raw_cc,
                        'cost_center_name': final_name,
                        'employees': []
                    }
                
                grouped[group_key]['employees'].append({
                    'department': final_name,
                    'id': emp_id,
                    'name': str(emp_data.get('employee_name') or "UNKNOWN"),
                    'clock_in': c_in_str
                })

            for cc in grouped.values():
                cc['employees'].sort(key=lambda x: x['id'])

            sorted_grouped_list = sorted(list(grouped.values()), key=lambda x: x['cost_center_name'])

            return {'data': sorted_grouped_list, 'source': 'database', 'error': None, 'date': target_date}

    except Exception as e:
        logger.error(f"DB Fetch Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {'data': [], 'source': 'database_error', 'error': str(e), 'date': target_date}