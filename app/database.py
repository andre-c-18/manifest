from sqlalchemy import text
from typing import Dict, Any
from app.config import engine
from datetime import datetime
import logging

logger = logging.getLogger("ManifestAPI.Database")

def fetch_active_clocking_data(tag: str = "manifest", target_date: str = None) -> Dict[str, Any]:
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    try:
        # =========================================================
        # ZERO-ZOMBIE POLICY: Raw Query menggunakan Context Manager
        # Koneksi langsung dihancurkan / dikembalikan setelah 'with' selesai
        # =========================================================
        with engine.connect() as conn:
            
            # =========================================================
            # STEP 1: FETCH KARYAWAN UNMATCHED / ACTIVE
            # Menggunakan vw_manifest (sudah terfilter terminal khusus manifest di DB)
            # Agregasi log vertikal: 0 = In, 1 = Out
            # =========================================================
            q_trans = text("""
                SELECT 
                    card_id,
                    MIN(CASE WHEN direction = 0 THEN clocking_time END) AS clock_in_time,
                    MAX(CASE WHEN direction = 1 THEN clocking_time END) AS clock_out_time
                FROM `db-it-andreas`.VW_TACTIVITIES_STAGING_VALID
                WHERE clocking_date = :target_date
                GROUP BY card_id, clocking_date
                HAVING clock_in_time IS NOT NULL 
                   AND clock_out_time IS NULL
            """)
            
            trans_data = conn.execute(q_trans, {"target_date": target_date}).fetchall()
            
            if not trans_data:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            active_cards = {}
            card_list = []
            
            for row in trans_data:
                c_id = str(row.card_id).strip() if row.card_id else ""
                
                if c_id and c_id not in active_cards:
                    card_list.append(c_id)
                    
                    c_in_val = row.clock_in_time
                    if isinstance(c_in_val, datetime):
                        c_in_str = c_in_val.strftime('%d-%b-%Y %H:%M')
                    else:
                        c_in_str = str(c_in_val) if c_in_val else ""
                        
                    active_cards[c_id] = {
                        'clock_in': c_in_str,
                        'matched_in_master': False
                    }

            if not card_list:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            # =========================================================
            # STEP 2: HYBRID FILTERING & PRE-SELECTION (Via card_id)
            # Mencegah JOIN tabel raksasa (SLA < 3 detik)
            # =========================================================
            card_tuple = tuple(card_list)

            # 2A. Cek Master OS (vw_master_os_active)
            q_os = text("""
                SELECT employee_code, employee_name, cost_center_id, card_number 
                FROM vw_master_os_active 
                WHERE card_number IN :c_list
            """)
            for os_emp in conn.execute(q_os, {"c_list": card_tuple}).fetchall():
                c_id = str(os_emp.card_number).strip() if os_emp.card_number else ""
                if c_id in active_cards:
                    safe_cc_val = str(os_emp.cost_center_id).strip() if os_emp.cost_center_id is not None else ""
                    active_cards[c_id]['emp_id'] = str(os_emp.employee_code).strip() if os_emp.employee_code else ""
                    active_cards[c_id]['emp_name'] = os_emp.employee_name
                    active_cards[c_id]['master_ref_val'] = safe_cc_val
                    active_cards[c_id]['matched_in_master'] = True 

            # 2B. Cek Master Karyawan Tetap (vw_master_karyawan)
            q_karyawan = text("""
                SELECT employee_id, employee_name, cost_center, card_no 
                FROM vw_master_karyawan 
                WHERE card_no IN :c_list
            """)
            for kar_emp in conn.execute(q_karyawan, {"c_list": card_tuple}).fetchall():
                c_id = str(kar_emp.card_no).strip() if kar_emp.card_no else ""
                if c_id in active_cards:
                    safe_cc_val = str(kar_emp.cost_center).strip() if kar_emp.cost_center is not None else ""
                    active_cards[c_id]['emp_id'] = str(kar_emp.employee_id).strip() if kar_emp.employee_id else ""
                    active_cards[c_id]['emp_name'] = kar_emp.employee_name
                    active_cards[c_id]['master_ref_val'] = safe_cc_val
                    active_cards[c_id]['matched_in_master'] = True 

            # =========================================================
            # STEP 3: UNIFIED DUAL-MAPPING POOL (FILTER TOP vs TOP SOETA)
            # =========================================================
            all_ref_ids = {
                emp['master_ref_val'] for emp in active_cards.values() 
                if emp.get('matched_in_master') and emp.get('master_ref_val') != ""
            }
            
            cc_map = {}
            if all_ref_ids:
                ref_tuple = tuple(all_ref_ids)

                # Prioritas: 'TOP' reguler dibanding 'TOP SOETA'
                q_cc = text("""
                    SELECT cost_center, org_name 
                    FROM org_cost_center 
                    WHERE cost_center IN :ref_list
                    ORDER BY CASE WHEN UPPER(org_name) LIKE '%SOETA%' THEN 1 ELSE 0 END ASC
                """)
                for cc_row in conn.execute(q_cc, {"ref_list": ref_tuple}).fetchall():
                    key = str(cc_row.cost_center).strip()
                    if key not in cc_map or "SOETA" not in str(cc_row.org_name).upper():
                        cc_map[key] = cc_row.org_name

                q_org = text("""
                    SELECT org_id, org_name 
                    FROM org_cost_center 
                    WHERE org_id IN :ref_list
                    ORDER BY CASE WHEN UPPER(org_name) LIKE '%SOETA%' THEN 1 ELSE 0 END ASC
                """)
                for org_row in conn.execute(q_org, {"ref_list": ref_tuple}).fetchall():
                    org_id_str = str(org_row.org_id).strip()
                    if org_id_str not in cc_map:
                        cc_map[org_id_str] = org_row.org_name
                    elif "SOETA" in str(cc_map[org_id_str]).upper() and "SOETA" not in str(org_row.org_name).upper():
                        cc_map[org_id_str] = org_row.org_name

        # =========================================================
        # STEP 4: GROUPING DATA IN-MEMORY (Pencocokan Akhir)
        # =========================================================
        grouped: Dict[str, Dict[str, Any]] = {}
        for c_id, emp_data in active_cards.items():
            
            if not emp_data.get('matched_in_master'): 
                continue
            
            ref_val = str(emp_data.get('master_ref_val', '')).strip()
            org_name = cc_map.get(ref_val, "UNASSIGNED")
            
            # Sanitasi UI fallback jika 'SOETA' masih terikut
            if org_name != "UNASSIGNED" and "SOETA" in org_name.upper():
                org_name = org_name.upper().replace("SOETA", "").strip()

            if ref_val not in grouped:
                grouped[ref_val] = {'cost_center_id': ref_val, 'cost_center_name': org_name, 'employees': []}
            
            grouped[ref_val]['employees'].append({
                'department': org_name,
                'id': emp_data['emp_id'],
                'name': emp_data['emp_name'],
                'clock_in': emp_data['clock_in']
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