# app/tamu.py
import csv
import io
import logging
import urllib.request
from datetime import datetime
from typing import List, Dict, Any
from app.config import GOOGLE_SHEET_TAMU_CSV_URL

logger = logging.getLogger("ManifestAPI.Tamu")

def fetch_active_tamu_from_csv(target_date: str = None) -> List[Dict[str, Any]]:
    """
    Menarik dan memfilter data tamu dari Google Sheet CSV Export (Tab ke-3).
    Mendukung format layout khusus dengan header di baris ke-5.
    """
    if not GOOGLE_SHEET_TAMU_CSV_URL or "GANTI_DENGAN_GID" in GOOGLE_SHEET_TAMU_CSV_URL:
        logger.warning("URL Google Sheet Tamu belum dikonfigurasi dengan benar di .env.")
        return []

    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    
    # Berbagai pola pencocokan tanggal (antisipasi format di sheet: 11/18/2026 atau 11/18/26)
    date_patterns = [
        target_date,                                      # 2026-11-18
        target_dt.strftime("%m/%d/%Y"),                   # 11/18/2026
        target_dt.strftime("%m/%d/%y"),                   # 11/18/26
        f"{target_dt.month}/{target_dt.day}/",            # "11/18/" untuk antisipasi tahun terpotong
        target_dt.strftime("%d/%m/%Y"),                   # 18/11/2026
    ]

    active_tamu_list = []

    try:
        # Tarik data CSV secara in-memory (timeout 5 detik)
        req = urllib.request.Request(
            GOOGLE_SHEET_TAMU_CSV_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        
        with urllib.request.urlopen(req, timeout=5) as response:
            csv_content = response.read().decode('utf-8', errors='ignore')

        csv_reader = csv.reader(io.StringIO(csv_content))
        rows = list(csv_reader)

        # Pastikan data cukup (minimal harus ada 6 baris: 4 baris judul/kosong + 1 header + 1 data)
        if not rows or len(rows) < 6:
            logger.warning("Data CSV Google Sheet kosong atau tidak lengkap.")
            return []

        # =========================================================================
        # PROSES DETEKSI HEADER PADA BARIS KE-5 (Indeks ke-4)
        # =========================================================================
        raw_header = rows[4]
        header = [h.strip().upper() for h in raw_header]

        col_idx = {}
        for idx, col_name in enumerate(header):
            if "TANGGAL KUNJUNGAN" in col_name:
                col_idx["visit_date"] = idx
            elif "NAMA TAMU" in col_name:
                col_idx["name"] = idx
            elif "NAMA PERUSAHAAN" in col_name:
                col_idx["company"] = idx
            elif "NOMOR KARTU" in col_name:
                col_idx["id"] = idx
            elif "PIC" in col_name:
                col_idx["pic"] = idx

        # Fallback pencocokan fleksibel jika ada penulisan kolom yang berubah sedikit
        if "visit_date" not in col_idx:
            for idx, col_name in enumerate(header):
                if "TANGGAL" in col_name: col_idx["visit_date"] = idx
        if "name" not in col_idx:
            for idx, col_name in enumerate(header):
                if "NAMA" in col_name: col_idx["name"] = idx

        # Pastikan kolom esensial berhasil dideteksi
        if "visit_date" not in col_idx or "name" not in col_idx:
            logger.error(f"Gagal mendeteksi kolom tanggal/nama. Header terbaca: {header}")
            return []

        # =========================================================================
        # PARSING BARIS DATA (Mulai dari Baris ke-6 / Indeks ke-5)
        # =========================================================================
        for row_idx, row in enumerate(rows[5:], start=6):
            if not row or len(row) <= 1:
                continue

            def get_val(key):
                idx = col_idx.get(key)
                if idx is not None and idx < len(row):
                    return row[idx].strip()
                return ""

            nama_tamu = get_val("name")
            tgl_kunjungan = get_val("visit_date")
            id_tamu = get_val("id") or "-"
            perusahaan = get_val("company")
            pic = get_val("pic")

            if not nama_tamu:
                continue

            # 1. Cocokkan Tanggal Kunjungan
            is_matched_date = False
            for pattern in date_patterns:
                if pattern in tgl_kunjungan:
                    is_matched_date = True
                    break

            if not is_matched_date:
                continue

            # Set tampilan waktu masuk (In-Site)
            clock_in_str = target_dt.strftime('%d-%b-%Y') + " (In)"

            # Bangun format tampilan Nama + Perusahaan + PIC
            display_name = f"{nama_tamu} ({perusahaan})" if perusahaan else nama_tamu
            if pic:
                display_name = f"{display_name} - PIC: {pic}"

            active_tamu_list.append({
                'department': 'TAMU',
                'id': id_tamu if id_tamu != "-" else f"TAMU-{len(active_tamu_list)+1:03d}",
                'name': display_name.upper(),
                'clock_in': clock_in_str
            })

        logger.info(f"Berhasil memproses {len(active_tamu_list)} tamu aktif On-Site dari Google Sheet CSV Tab 3.")
        return active_tamu_list

    except Exception as e:
        logger.error(f"Gagal mengambil/parse CSV Google Sheet: {str(e)}", exc_info=True)
        return []
