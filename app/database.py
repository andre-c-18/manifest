from sqlalchemy import text
from typing import Dict, Any
from app.config import engine
from datetime import datetime
import logging

logger = logging.getLogger("ManifestAPI.Database")

def fetch_active_clocking_data(tag: str = "manifest", target_date: str = None) -> Dict[str, Any]:
    # Jika tidak ada target_date, gunakan hari ini
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    try:
        # =========================================================
        # ZERO-ZOMBIE POLICY: Gunakan Context Manager
        # =========================================================
        with engine.connect() as conn:
            
            # Query langsung tembak ke view yang sudah kita buat
            q_manifest = text("""
                SELECT 
                    emp_id,
                    emp_name,
                    clock_in_time,
                    cost_center_id,
                    department_name
                FROM `db-hr`.vw_manifest
                WHERE work_date = :target_date
            """)
            
            # Execute dengan bind parameter untuk cegah SQL Injection
            result = conn.execute(q_manifest, {"target_date": target_date}).fetchall()
            
            if not result:
                return {'data': [], 'source': 'database', 'error': None, 'date': target_date}

            # =========================================================
            # GROUPING DATA IN-MEMORY (Format Response API)
            # =========================================================
            grouped: Dict[str, Dict[str, Any]] = {}
            
            for row in result:
                cc_id = str(row.cost_center_id).strip() if row.cost_center_id else "UNASSIGNED"
                dept_name = str(row.department_name).strip() if row.department_name else row.cost_center_id
                
                # Format jam masuk
                c_in_val = row.clock_in_time
                if isinstance(c_in_val, datetime):
                    c_in_str = c_in_val.strftime('%d-%b-%Y %H:%M')
                else:
                    c_in_str = str(c_in_val) if c_in_val else ""

                # Buat dictionary key jika cost center belum ada
                if cc_id not in grouped:
                    grouped[cc_id] = {
                        'cost_center_id': cc_id, 
                        'cost_center_name': dept_name, 
                        'employees': []
                    }
                
                # Masukkan data karyawan ke dalam list departemennya
                grouped[cc_id]['employees'].append({
                    'department': dept_name,
                    'id': str(row.emp_id).strip() if row.emp_id else "",
                    'name': row.emp_name,
                    'clock_in': c_in_str
                })

            # Sorting internal agar datanya rapi saat dilempar ke Frontend
            for cc in grouped.values():
                cc['employees'].sort(key=lambda x: x['id'])

            sorted_grouped_list = sorted(list(grouped.values()), key=lambda x: x['cost_center_id'])

            return {'data': sorted_grouped_list, 'source': 'database', 'error': None, 'date': target_date}

    except Exception as e:
        logger.error(f"DB Fetch Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {'data': [], 'source': 'database_error', 'error': str(e), 'date': target_date}