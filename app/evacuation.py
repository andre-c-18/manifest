# app/evacuation.py
import logging
from datetime import datetime
from typing import List, Dict, Any
from sqlalchemy import text
from app.database import engine

logger = logging.getLogger("ManifestAPI.Evacuation")

def save_evacuation_data(duration_str: str, duration_seconds: int, officer: str, details: List[Any]) -> str:
    """
    Menyimpan sesi evakuasi ke database dengan performa tinggi (< 150ms).
    Mendukung pembacaan list format array [ID, Nama, Dept, Status] maupun dict secara dinamis.
    """
    if not details:
        raise ValueError("Detail personil evakuasi kosong.")

    timestamp_suffix = datetime.now().strftime("%H%M%S")
    today_str = datetime.now().strftime("%Y-%m-%d")
    evacuation_event_id = f"EVAC-{datetime.now().strftime('%Y%m%d')}-{timestamp_suffix}"

    # Helper untuk mem-parsing data personil secara fleksibel (Mendukung Array & Dict)
    def parse_member(m: Any):
        if isinstance(m, (list, tuple)):
            # Format Array/List: [id, name, department, status]
            emp_id = str(m[0]) if len(m) > 0 else "-"
            emp_name = str(m[1]) if len(m) > 1 else "UNKNOWN"
            dept = str(m[2]) if len(m) > 2 else "UNASSIGNED"
            status = str(m[3]) if len(m) > 3 else "HILANG"
            return emp_id, emp_name, dept, status
        elif isinstance(m, dict):
            # Format Dictionary
            emp_id = str(m.get("id") or m.get("employee_id") or "-")
            emp_name = str(m.get("name") or m.get("employee_name") or "UNKNOWN")
            dept = str(m.get("department") or "UNASSIGNED")
            status = str(m.get("status") or "HILANG")
            return emp_id, emp_name, dept, status
        # Fallback jika tipe data tidak dikenal
        return "-", "UNKNOWN", "UNASSIGNED", "HILANG"

    # 1. Kelompokkan personil per departemen di memory Python (~1 ms)
    dept_groups: Dict[str, List[Any]] = {}
    for d in details:
        _, _, dept, _ = parse_member(d)
        dept = dept.strip()
        if dept not in dept_groups:
            dept_groups[dept] = []
        dept_groups[dept].append(d)

    insert_master_sql = text("""
        INSERT INTO `manifest_evacuation_master`
        (evacuation_event_id, evacuation_date, department_name, evacuation_time_display,
         evacuation_time_seconds, total_evakuasi, total_hilang, total_pulang, evacuation_officer, created_at)
        VALUES (:event_id, :evac_date, :dept_name, :duration_display, :duration_seconds, :safe, :missing, :away, :officer, NOW())
    """)

    # Menggunakan transaksi ACID SQLAlchemy (Hit-and-Run)
    with engine.begin() as conn:
        all_detail_rows = []

        # 2. Iterasi per Departemen untuk menyimpan summary ke Master
        for dept_name, members in dept_groups.items():
            total_target = len(members)
            total_safe = sum(1 for m in members if parse_member(m)[3] == 'DIEVAKUASI')
            total_away = sum(1 for m in members if parse_member(m)[3] == 'PULANG')
            total_missing = total_target - total_safe - total_away

            res = conn.execute(insert_master_sql, {
                "event_id": evacuation_event_id,
                "evac_date": today_str,
                "dept_name": dept_name,
                "duration_display": duration_str,
                "duration_seconds": duration_seconds,
                "safe": total_safe,
                "missing": total_missing,
                "away": total_away,
                "officer": officer
            })
            master_id = res.lastrowid

            # 3. Kumpulkan baris detail untuk Bulk Insert Gabungan
            for m in members:
                emp_id, emp_name, _, status = parse_member(m)
                
                # Sanitize single quotes untuk SQL safety
                emp_id_clean = emp_id.replace("'", "''")
                emp_name_clean = emp_name.replace("'", "''")
                status_clean = status.replace("'", "''")

                all_detail_rows.append(f"({master_id}, '{emp_id_clean}', '{emp_name_clean}', '{status_clean}', NOW())")

        # 4. 🌟 BATCH INSERT: Tembak 2000+ data sekaligus dalam 1 query (~30ms)
        if all_detail_rows:
            CHUNK_SIZE = 1000
            for i in range(0, len(all_detail_rows), CHUNK_SIZE):
                chunk = all_detail_rows[i:i + CHUNK_SIZE]
                bulk_sql_raw = f"""
                    INSERT INTO `manifest_evacuation_detail`
                    (master_id, employee_id, employee_name, status, checked_at)
                    VALUES {", ".join(chunk)};
                """
                conn.execute(text(bulk_sql_raw))

    logger.info(f"Sesi Evakuasi '{evacuation_event_id}' ({len(details)} personil) sukses disimpan.")
    return evacuation_event_id

def get_evacuation_history_list(limit: int = 15) -> List[Dict[str, Any]]:
    """
    Mengambil riwayat evakuasi dengan cursor yang langsung diekstrak habis (fetchall) dan ditutup.
    """
    query = text("""
        SELECT 
            evacuation_event_id,
            DATE_FORMAT(evacuation_date, '%d-%b-%Y') as formatted_date,
            MAX(evacuation_time_display) as duration,
            SUM(total_evakuasi + total_hilang + total_pulang) as total_target,
            SUM(total_evakuasi) as total_safe,
            SUM(total_hilang) as total_missing,
            SUM(total_pulang) as total_away,
            MAX(evacuation_officer) as officer
        FROM `manifest_evacuation_master`
        GROUP BY evacuation_event_id, evacuation_date
        ORDER BY MIN(id) DESC
        LIMIT :limit
    """)

    history = []
    try:
        # 🌟 Context Manager menjamin conn.close() otomatis terpanggil
        with engine.connect() as conn:
            rows = conn.execute(query, {"limit": limit}).fetchall()
            for r in rows:
                history.append({
                    "id": r.evacuation_event_id,
                    "date": r.formatted_date,
                    "duration": r.duration,
                    "total_target": int(r.total_target or 0),
                    "total_safe": int(r.total_safe or 0),
                    "total_missing": int(r.total_missing or 0),
                    "total_away": int(r.total_away or 0),
                    "officer": r.officer
                })
    except Exception as e:
        logger.error(f"Gagal mengambil riwayat evakuasi: {e}")
    
    return history
