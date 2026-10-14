from typing import List, Dict, Any

def build_txt_manifest_content(cost_centers: List[Dict[str, Any]]) -> str:
    lines = []
    total_groups = len(cost_centers)

    for idx, group in enumerate(cost_centers):
        cc_id = group.get('cost_center_id', '')
        cc_name = group.get('cost_center_name', '')
        lines.append(f"Cost Center : {cc_id} - {cc_name}")
        lines.append(f"{'DEPARTMENT':<24}{'EMPLOYEE ID':<14}{'NAMA KARYAWAN':<26}{'CLOCKING IN'}")
        lines.append("")

        for emp in group.get('employees', []):
            dept = str(emp.get('department', ''))[:23]
            emp_id = str(emp.get('id', ''))[:13]
            name = str(emp.get('name', ''))[:25]
            clock_in = str(emp.get('clock_in', ''))
            
            lines.append(f"{dept:<24}{emp_id:<14}{name:<26}{clock_in}")

        if idx < total_groups - 1:
            lines.append("\r\n")

    return "\r\n".join(lines)