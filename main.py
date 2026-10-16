from fastapi import FastAPI, Request, Query, status, BackgroundTasks
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import uvicorn
import logging
import os

from app.config import APP_HOST, APP_PORT
from app.database import fetch_active_clocking_data
from app.generator import build_txt_manifest_content

# IMPORT MODUL TASKS BARU
from app.tasks import generate_and_save_manifest_job, STORAGE_DIR 

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ManifestAPI")

# ============================================================================
# SCHEDULER LIFESPAN (ENTERPRISE APP LIFECYCLE)
# ============================================================================
# Menggunakan BackgroundScheduler agar tidak memblokir Main Thread (SLA < 3s Terjaga)
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP EVENT ---
    logger.info("Menyalakan Enterprise Task Scheduler...")
    
    # Atur jadwal di sini. Contoh: Berjalan setiap 1 jam.
    # Anda bisa mengganti ke 'cron' untuk waktu spesifik (misal: akhir shift)
    # scheduler.add_job(generate_and_save_manifest_job, 'interval', hours=1)
    
    scheduler.start()
    yield # Aplikasi berjalan melayani request...
    
    # --- SHUTDOWN EVENT ---
    # Memastikan memori dibersihkan saat Uvicorn server dimatikan
    logger.info("Mematikan Task Scheduler dengan aman...")
    scheduler.shutdown()

# Daftarkan lifespan ke instance FastAPI
app = FastAPI(title="Enterprise Live Clocking Manifest", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

# ============================================================================
# HELPER: In-Memory Filtering (SLA < 3 Detik - Tanpa Query DB Tambahan)
# ============================================================================
def filter_manifest_memory(data_list: list, cc_id: str = None, search: str = None) -> list:
    if not cc_id and not search:
        return data_list

    filtered_result = []
    search_query = search.lower() if search else ""

    for group in data_list:
        if cc_id and group["cost_center_id"] != cc_id:
            continue
        
        if search_query:
            matched_employees = [
                emp for emp in group["employees"]
                if search_query in emp["name"].lower() or 
                   search_query in emp["id"] or 
                   search_query in emp["department"].lower()
            ]
            
            if not matched_employees:
                continue
                
            filtered_group = group.copy()
            filtered_group["employees"] = matched_employees
            filtered_result.append(filtered_group)
        else:
            filtered_result.append(group)

    return filtered_result

# ============================================================================
# ROUTER / ENDPOINTS UTAMA
# ============================================================================
@app.get("/", response_class=HTMLResponse)
def index(request: Request, tag: str = Query("manifest"), date: str = Query(None)):
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    selected_date = result.get("date")
    safe_date_display = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%d %b %Y")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": result,
        "selected_date": selected_date,
        "display_date": safe_date_display,
        "current_date": datetime.now().strftime("%Y%m%d"),
        "now_formatted": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

@app.get("/api/data")
def api_data(tag: str = Query("manifest"), date: str = Query(None)):
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    http_status = status.HTTP_200_OK if not result["error"] else status.HTTP_206_PARTIAL_CONTENT
    return JSONResponse(status_code=http_status, content={
        "status": http_status,
        "source": result["source"],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": result["data"]
    })

@app.get("/download_txt")
def download_txt(tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result["data"], cc_id, search)
    
    txt_content = build_txt_manifest_content(filtered_data)
    safe_date_file = result['date'].replace('-', '')
    filename = f"manifest_karyawan_{safe_date_file}.txt"

    return Response(
        content=txt_content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/raw_txt", response_class=PlainTextResponse)
def raw_txt(tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result["data"], cc_id, search)
    return build_txt_manifest_content(filtered_data)

# ============================================================================
# ENDPOINT MANAJEMEN BACKGROUND TASKS & ARSIP
# ============================================================================
@app.post("/api/trigger-manifest")
def trigger_manifest_generation(background_tasks: BackgroundTasks):
    """
    Memicu pembentukan file fisik TXT ke server sekarang juga tanpa memblokir response.
    """
    background_tasks.add_task(generate_and_save_manifest_job)
    return JSONResponse(status_code=202, content={"message": "Proses pembentukan manifest fisik berjalan di background."})

@app.get("/api/download-arsip")
def download_arsip(date: str = Query(..., description="Format YYYY-MM-DD")):
    """
    Mengunduh file fisik TXT yang sudah dibuat oleh Scheduler dari direktori storage.
    """
    safe_date_file = date.replace('-', '')
    filename = f"manifest_karyawan_{safe_date_file}.txt"
    filepath = os.path.join(STORAGE_DIR, filename)

    if os.path.exists(filepath):
        return FileResponse(path=filepath, filename=filename, media_type='text/plain')
    else:
        return JSONResponse(status_code=404, content={"error": f"File {filename} belum dibentuk oleh sistem atau tidak ditemukan di server."})

if __name__ == "__main__":
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=True)