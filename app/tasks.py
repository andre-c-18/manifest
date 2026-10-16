import os
import logging
from datetime import datetime
from app.database import fetch_active_clocking_data
from app.generator import build_txt_manifest_content

logger = logging.getLogger("ManifestAPI.Tasks")

# Definisikan folder penyimpanan fisik di server (otomatis dibuat jika belum ada)
STORAGE_DIR = os.path.join(os.getcwd(), "storage", "manifests")
os.makedirs(STORAGE_DIR, exist_ok=True)

def generate_and_save_manifest_job():
    """
    Task Scheduler: Menarik data langsung dari MySQL (Tanpa Zombie Connection)
    lalu menyimpannya menjadi file fisik .txt di server.
    """
    logger.info("Memulai eksekusi background job: Generate Manifest TXT...")
    try:
        # 1. Gunakan fungsi yang sudah sangat teroptimasi (Hybrid Filtering)
        # Fungsi ini otomatis membuka & menutup koneksi DB dengan Context Manager
        result = fetch_active_clocking_data(tag="manifest")
        
        if result.get("error"):
            logger.error(f"Job dibatalkan karena error DB: {result['error']}")
            return

        # 2. Render teks
        txt_content = build_txt_manifest_content(result["data"])
        
        # 3. Tentukan nama file (Timestamps harian)
        # Format: manifest_karyawan_YYYYMMDD.txt (akan menimpa file di hari yang sama)
        safe_date_file = result['date'].replace('-', '')
        filename = f"manifest_karyawan_{safe_date_file}.txt"
        filepath = os.path.join(STORAGE_DIR, filename)

        # 4. Tulis ke file fisik (I/O Operation)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(txt_content)

        logger.info(f"Background Job Sukses! File tersimpan di: {filepath}")

    except Exception as e:
        logger.error(f"Gagal mengeksekusi background job: {str(e)}")
        import traceback
        traceback.print_exc()