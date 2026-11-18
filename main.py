from fastapi import FastAPI, Request, Query, status, BackgroundTasks, Form, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime
import uvicorn
import logging
import os

from app.config import (APP_HOST, APP_PORT, SECRET_KEY, ADMIN_USER, ADMIN_USER_HASH, UPDATE_INTERVAL_MINUTES)
from app.database import fetch_active_clocking_data, engine
from app.generator import build_txt_manifest_content
from app.tasks import generate_and_save_manifest_job, STORAGE_DIR 
from app.security import verify_password
from app.evacuation import save_evacuation_data, get_evacuation_history_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ManifestAPI")

scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Menyalakan Enterprise Task Scheduler...")
    scheduler.add_job(
        generate_and_save_manifest_job,
        trigger="cron",
        hour="*",          # Setiap jam
        minute=0,          # Tepat di menit ke-0
        coalesce=True,     # Gabungkan eksekusi jika ada keterlambatan sistem
        misfire_grace_time=300,
        id="auto_manifest_job_cron",
        replace_existing=True
    )
    scheduler.start()
    yield
    logger.info("Mematikan Task Scheduler dengan aman...")
    scheduler.shutdown(wait=False)

app = FastAPI(title="Live Clocking Manifest", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

# ============================================================================
# ENTERPRISE SECURITY LAYER (SESSION MIDDLEWARE)
# ============================================================================
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="manifest_session",
    max_age=28800  # 8 jam sesi aktif
)

# 🌟 MIDDLEWARE HIT-AND-RUN CLEANUP (Mencegah Zombie Connection / Sleep di MySQL)
@app.middleware("http")
async def db_teardown_middleware(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    finally:
        # Menjamin seluruh koneksi fisik SQLAlchemy dilepaskan setelah HTTP Request selesai
        engine.dispose()

def check_html_auth(request: Request) -> bool:
    """Helper untuk proteksi halaman HTML (Redirect jika belum login)"""
    return bool(request.session.get("authenticated"))

def require_api_auth(request: Request) -> bool:
    """Dependency untuk proteksi Endpoint API (Return 401 jika belum login)"""
    return bool(request.session.get("authenticated"))

# ============================================================================
# HELPER: In-Memory Filtering 
# ============================================================================
def filter_manifest_memory(data_list: list, cc_id: str = None, search: str = None) -> list:
    if not cc_id and not search:
        return data_list

    filtered_result = []
    search_query = search.strip().lower() if search else ""
    safe_cc_id = str(cc_id).strip().upper() if cc_id else ""

    for group in data_list:
        group_cc_id = str(group.get("cost_center_id") or "").strip().upper()
        if safe_cc_id and group_cc_id != safe_cc_id:
            continue

        if search_query:
            matched_employees = [
                emp for emp in group.get("employees", [])
                if search_query in emp.get("name", "").lower()
                or search_query in str(emp.get("id", "")).lower()
                or search_query in emp.get("department", "").lower()
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
# ROUTER / ENDPOINTS UTAMA (AUTH & PAGES)
# ============================================================================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if check_html_auth(request):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and ADMIN_USER_HASH and verify_password(password, ADMIN_USER_HASH):        
        request.session.clear()         
        request.session["authenticated"] = True
        request.session["username"] = username
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse("login.html", {
        "request": request, 
        "error": "Username atau Password salah!"
    })

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

@app.get("/", response_class=HTMLResponse)
def index(request: Request, tag: str = Query("manifest"), date: str = Query(None)):
    if not check_html_auth(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    result = fetch_active_clocking_data(tag=tag, target_date=date)
    selected_date = result.get("date")
    safe_date_display = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%d %b %Y") if selected_date else ""
    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": result,
        "selected_date": selected_date,
        "display_date": safe_date_display,
        "current_date": datetime.now().strftime("%Y%m%d"),
        "now_formatted": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "username": request.session.get("username", "Admin"),
        "refresh_interval_seconds": UPDATE_INTERVAL_MINUTES * 60
    })

# ============================================================================
# ENDPOINTS API (Terlindungi)
# ============================================================================
@app.get("/api/data")
def api_data(request: Request, tag: str = Query("manifest"), date: str = Query(None)):
    if not require_api_auth(request): 
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    http_status = status.HTTP_200_OK if not result.get("error") else status.HTTP_206_PARTIAL_CONTENT
    return JSONResponse(status_code=http_status, content={
        "status": http_status, 
        "source": result.get("source"),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "data": result.get("data")
    })

@app.get("/download_txt")
def download_txt(request: Request, tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    if not require_api_auth(request): 
        return HTMLResponse("Unauthorized", status_code=401)
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result.get("data", []), cc_id, search)
    txt_content = build_txt_manifest_content(filtered_data)
    safe_date_file = result.get('date', '').replace('-', '')
    return Response(
        content=txt_content, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="manifest_karyawan_{safe_date_file}.txt"'}
    )

@app.get("/raw_txt", response_class=PlainTextResponse)
def raw_txt(request: Request, tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    if not require_api_auth(request): 
        return PlainTextResponse("Unauthorized", status_code=401)
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result.get("data", []), cc_id, search)
    return build_txt_manifest_content(filtered_data)

# ============================================================================
# ENDPOINT MANAJEMEN BACKGROUND TASKS & ARSIP (Terlindungi)
# ============================================================================
@app.post("/api/trigger-manifest")
def trigger_manifest_generation(request: Request, background_tasks: BackgroundTasks):
    if not require_api_auth(request): 
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    background_tasks.add_task(generate_and_save_manifest_job)
    return JSONResponse(status_code=202, content={"message": "Proses pembentukan manifest fisik berjalan di background."})

@app.get("/api/download-arsip")
def download_arsip(request: Request, date: str = Query(..., description="Format YYYY-MM-DD")):
    if not require_api_auth(request): 
        return HTMLResponse("Unauthorized", status_code=401)
    safe_date_file = date.replace('-', '')
    filename = f"manifest_karyawan_{safe_date_file}.txt"
    filepath = os.path.join(STORAGE_DIR, filename)
    
    if os.path.exists(filepath): 
        return FileResponse(path=filepath, filename=filename, media_type='text/plain')
    return JSONResponse(status_code=404, content={"error": f"File {filename} belum dibentuk oleh sistem."})

@app.get("/api/arsip-list")
def list_arsip_files(request: Request):
    """Membaca daftar file fisik .txt berformat YYYYMMDD_HHMM di folder storage/manifests"""
    if not require_api_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    os.makedirs(STORAGE_DIR, exist_ok=True)
    files_data = []
    
    for fname in sorted(os.listdir(STORAGE_DIR), reverse=True):
        if fname.endswith(".txt") and fname.startswith("manifest_karyawan_"):
            fpath = os.path.join(STORAGE_DIR, fname)
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")
            raw_str = fname.replace("manifest_karyawan_", "").replace(".txt", "")
            parts = raw_str.split("_")
            formatted_date = ""
            file_time = ""
            if len(parts) >= 1 and len(parts[0]) == 8:
                d = parts[0]
                formatted_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            if len(parts) == 2 and len(parts[1]) == 4:
                t = parts[1]
                file_time = f"{t[:2]}:{t[2:]}"
            files_data.append({
                "filename": fname,
                "date": formatted_date,
                "time": file_time,
                "last_modified": mtime
            })
    
    return JSONResponse(status_code=200, content={"files": files_data})

@app.get("/api/download-arsip-direct")
def download_arsip_direct(request: Request, filename: str = Query(...)):
    """Mengunduh file arsip spesifik berdasarkan nama file (termasuk jam)"""
    if not require_api_auth(request):
        return HTMLResponse("Unauthorized", status_code=401)
    if not filename.endswith(".txt") or ".." in filename or "/" in filename or "\\" in filename:
        return JSONResponse(status_code=403, content={"error": "Akses dilarang. Nama file tidak valid."})
    filepath = os.path.join(STORAGE_DIR, filename)
    
    if os.path.exists(filepath):
        return FileResponse(path=filepath, filename=filename, media_type="text/plain")
    return JSONResponse(status_code=404, content={"error": f"File {filename} tidak ditemukan di server."})

# =========================================================================
# 🌟 ENDPOINT API SIMPAN DATA EVAKUASI (SUPER FAST & ANTI-ZOMBIE)
# =========================================================================
@app.post("/api/evacuation/save")
async def api_save_evacuation(request: Request):
    """
    Endpoint simpan data evakuasi dengan optimasi performa tinggi (SLA < 3 detik).
    Mengabaikan validasi baris Pydantic untuk memproses 2.000+ data secara instan (< 200ms).
    """
    if not require_api_auth(request): 
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    officer = request.session.get("username") or "Admin Security"
    
    try:
        # 🌟 Langsung ambil JSON mentah dari body request tanpa validasi baris Pydantic
        payload = await request.json()
        duration_str = payload.get("duration", "00:00:00")
        duration_seconds = int(payload.get("duration_seconds", 0))
        details_list = payload.get("details", [])

        if not details_list:
            return JSONResponse(status_code=400, content={"error": "Data detail absensi evakuasi kosong."})

        # Kirim list mentah ke pemrosesan raw batch insert
        event_id = save_evacuation_data(
            duration_str=duration_str,
            duration_seconds=duration_seconds,
            officer=officer,
            details=details_list
        )
        return {"status": 200, "message": "Sesi evakuasi berhasil disimpan.", "event_id": event_id}
        
    except Exception as e:
        logger.error(f"Error menyimpan evakuasi: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": f"Gagal menyimpan data: {str(e)}"})

# =========================================================================
# ENDPOINT API AMBIL RIWAYAT EVAKUASI
# =========================================================================
@app.get("/api/evacuation/history")
async def api_get_evacuation_history(request: Request):
    if not require_api_auth(request): 
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    history_data = get_evacuation_history_list(limit=10)
    return {"status": 200, "history": history_data}

if __name__ == "__main__":
    # Gunakan reload=False agar background scheduler 3 jam tidak ter-reset
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=False)
