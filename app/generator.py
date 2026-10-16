from typing import List, Dict, Any

def build_txt_manifest_content(data_list: list) -> str:
    if not data_list:
        return "Tidak ada data manifest yang sesuai.\n"

    lines = []
    
    # 1. Hitung Grand Total dari seluruh grup Cost Center
    grand_total = sum(len(group['employees']) for group in data_list)
    
    # 2. Header Utama Dokumen
    lines.append("MANIFEST KARYAWAN ON-SITE")
    lines.append(f"Total Keseluruhan: {grand_total} Orang")
    lines.append("=" * 70)
    lines.append("") # Spasi kosong

    # 3. Looping per Cost Center
    for group in data_list:
        lines.append(f"Cost Center : {group['cost_center_id']} - {group['cost_center_name']}")
        lines.append(f"Sub-Total   : {len(group['employees'])} Orang")
        lines.append("-" * 70)
        lines.append("NRP       | NAMA KARYAWAN                  | CLOCK IN")
        lines.append("-" * 70)
        
        for emp in group['employees']:
            id_pad = str(emp['id']).ljust(9)
            name_pad = str(emp['name'])[:30].ljust(30)
            lines.append(f"{id_pad} | {name_pad} | {emp['clock_in']}")
        
        lines.append("=" * 70)
        
        # 🌟 Karakter \f (Form Feed): Memaksa Notepad / Printer fisik ganti halaman 🌟
        lines.append("\f")
        
    return "\n".join(lines)