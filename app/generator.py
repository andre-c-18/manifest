from typing import List, Dict, Any
from datetime import datetime

def build_txt_manifest_content(grouped_data: List[Dict[str, Any]]) -> str:
    """
    Menghasilkan file string text (.txt) dengan format monospace table.
    - Menambahkan nomor urut per Cost Center (NO).
    - Memisahkan Tanggal (DATE IN) dan Jam (TIME IN).
    - Menambahkan kolom kosong (CEK) untuk absensi manual.
    """
    lines = []
    
    print_date = datetime.now().strftime('%d/%m/%Y, %H.%M.%S')
    grand_total = sum(len(g.get('employees', [])) for g in grouped_data)
    
    lines.append(f"{'MANIFEST KARYAWAN ON-SITE':^100}")
    lines.append(f"{f'Tanggal Cetak: {print_date} | Total Keseluruhan: {grand_total} Orang':^100}")
    lines.append("-" * 100)
    lines.append("")

    if not grouped_data:
        lines.append("Tidak ada data karyawan on-site sesuai kriteria filter.")
        return "\n".join(lines)

    for group in grouped_data:
        cc_code = str(group.get('cost_center_code') or "").strip()
        cc_name = str(group.get('cost_center_name') or "UNASSIGNED").strip()
        
        cc_label = f"{cc_code} - {cc_name}" if (cc_code and cc_code != cc_name) else cc_name
        
        employees = group.get('employees', [])
        
        # Header Cost Center
        lines.append(f"Cost Center : {cc_label}")
        lines.append(f"Sub-Total   : {len(employees)} Orang")
        lines.append("=" * 100)
        
        # Header Tabel Data (Dengan kolom baru NO, DATE IN, TIME IN, dan CEK)
        lines.append(f"{'NO':<4} | {'NRP':<10} | {'NAMA KARYAWAN':<30} | {'DATE IN':<12} | {'TIME IN':<8} | {'CEKLIST':<5}")
        lines.append("-" * 100)
        
        # Isi Data Karyawan
        for idx, emp in enumerate(employees, start=1):
            emp_id = str(emp.get('id', '')).strip()
            emp_name = str(emp.get('name', '')).strip()
            
            safe_name = emp_name[:30].ljust(30)
            
            raw_clock_in = str(emp.get('clock_in', '')).strip()
            date_in = ""
            time_in = ""
            
            if raw_clock_in and " " in raw_clock_in:
                parts = raw_clock_in.split(" ", 1)
                date_in = parts[0]
                time_in = parts[1]
            else:
                date_in = raw_clock_in[:12] if raw_clock_in else "-"
                time_in = "-"
                
            # Formatting Baris
            # NO (4) | NRP (10) | NAMA (30) | DATE (12) | TIME (8) | CEK(kosong)
            line = (
                f"{str(idx).ljust(4)} | "
                f"{emp_id.ljust(10)} | "
                f"{safe_name} | "
                f"{date_in.ljust(12)} | "
                f"{time_in.ljust(8)} | "
                f"[   ]"
            )
            lines.append(line)
            
        lines.append("=" * 100)
        lines.append("")
        
    return "\n".join(lines)