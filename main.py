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

from app.config import APP_HOST, APP_PORT, ADMIN_USER_HASH
from app.database import fetch_active_clocking_data
from app.generator import build_txt_manifest_content
from app.tasks import generate_and_save_manifest_job, STORAGE_DIR 
from app.security import verify_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ManifestAPI")

scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Menyalakan Enterprise Task Scheduler...")
    scheduler.start()
    yield
    logger.info("Mematikan Task Scheduler dengan aman...")
    scheduler.shutdown()

app = FastAPI(title="Enterprise Live Clocking Manifest", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")

# ============================================================================
# ENTERPRISE SECURITY LAYER (SESSION MIDDLEWARE)
# ============================================================================
app.add_middleware(SessionMiddleware, secret_key="super-secret-enterprise-key-manifest-2026")

def check_html_auth(request: Request):
    """Helper untuk proteksi halaman HTML (Redirect jika belum login)"""
    if not request.session.get("authenticated"):
        return False
    return True

def require_api_auth(request: Request):
    """Dependency untuk proteksi Endpoint API (Return 401 jika belum login)"""
    if not request.session.get("authenticated"):
        return False
    return True

# ============================================================================
# HELPER: In-Memory Filtering 
# ============================================================================
def filter_manifest_memory(data_list: list, cc_id: str = None, search: str = None) -> list:
    if not cc_id and not search:
        return data_list
    filtered_result = []
    search_query = search.lower() if search else ""
    for group in data_list:
        if cc_id and group["cost_center_id"] != cc_id: continue
        if search_query:
            matched_employees = [
                emp for emp in group["employees"]
                if search_query in emp["name"].lower() or search_query in emp["id"] or search_query in emp["department"].lower()
            ]
            if not matched_employees: continue
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
    """Menampilkan Halaman Login"""
    if request.session.get("authenticated"):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    
    if username == "admin" and ADMIN_USER_HASH and verify_password(password, ADMIN_USER_HASH):        
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
    # 🌟 PROTEKSI HALAMAN
    if not check_html_auth(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    result = fetch_active_clocking_data(tag=tag, target_date=date)
    selected_date = result.get("date")
    safe_date_display = datetime.strptime(selected_date, "%Y-%m-%d").strftime("%d %b %Y")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "result": result,
        "selected_date": selected_date,
        "display_date": safe_date_display,
        "current_date": datetime.now().strftime("%Y%m%d"),
        "now_formatted": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "username": request.session.get("username", "Admin")
    })

# ============================================================================
# ENDPOINTS API (Terlindungi)
# ============================================================================
@app.get("/api/data")
def api_data(request: Request, tag: str = Query("manifest"), date: str = Query(None)):
    if not require_api_auth(request): return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    http_status = status.HTTP_200_OK if not result["error"] else status.HTTP_206_PARTIAL_CONTENT
    return JSONResponse(status_code=http_status, content={
        "status": http_status, "source": result["source"],
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": result["data"]
    })

@app.get("/download_txt")
def download_txt(request: Request, tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    if not require_api_auth(request): return HTMLResponse("Unauthorized", status_code=401)
    
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result["data"], cc_id, search)
    txt_content = build_txt_manifest_content(filtered_data)
    safe_date_file = result['date'].replace('-', '')
    
    return Response(
        content=txt_content, media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="manifest_karyawan_{safe_date_file}.txt"'}
    )

@app.get("/raw_txt", response_class=PlainTextResponse)
def raw_txt(request: Request, tag: str = Query("manifest"), date: str = Query(None), cc_id: str = Query(None), search: str = Query(None)):
    if not require_api_auth(request): return PlainTextResponse("Unauthorized", status_code=401)
    
    result = fetch_active_clocking_data(tag=tag, target_date=date)
    filtered_data = filter_manifest_memory(result["data"], cc_id, search)
    return build_txt_manifest_content(filtered_data)

# ============================================================================
# ENDPOINT MANAJEMEN BACKGROUND TASKS & ARSIP (Terlindungi)
# ============================================================================
@app.post("/api/trigger-manifest")
def trigger_manifest_generation(request: Request, background_tasks: BackgroundTasks):
    if not require_api_auth(request): return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    background_tasks.add_task(generate_and_save_manifest_job)
    return JSONResponse(status_code=202, content={"message": "Proses pembentukan manifest fisik berjalan di background."})

@app.get("/api/download-arsip")
def download_arsip(request: Request, date: str = Query(..., description="Format YYYY-MM-DD")):
    if not require_api_auth(request): return HTMLResponse("Unauthorized", status_code=401)
    
    safe_date_file = date.replace('-', '')
    filename = f"manifest_karyawan_{safe_date_file}.txt"
    filepath = os.path.join(STORAGE_DIR, filename)

    if os.path.exists(filepath): return FileResponse(path=filepath, filename=filename, media_type='text/plain')
    return JSONResponse(status_code=404, content={"error": f"File {filename} belum dibentuk oleh sistem."})

if __name__ == "__main__":
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=True)