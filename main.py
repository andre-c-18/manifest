from fastapi import FastAPI, Request, Query, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime
import uvicorn
import logging

from app.config import APP_HOST, APP_PORT
from app.database import fetch_active_clocking_data
from app.generator import build_txt_manifest_content

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ManifestAPI")

app = FastAPI(title="Enterprise Live Clocking Manifest")
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
        # 1. Filter Dropdown Cost Center
        if cc_id and group["cost_center_id"] != cc_id:
            continue
        
        # 2. Filter Kolom Pencarian (Search Input)
        if search_query:
            matched_employees = [
                emp for emp in group["employees"]
                if search_query in emp["name"].lower() or 
                   search_query in emp["id"] or 
                   search_query in emp["department"].lower()
            ]
            
            # Jika tidak ada karyawan yang cocok di grup ini, buang grupnya
            if not matched_employees:
                continue
                
            # Copy instance agar data asli tidak termutasi
            filtered_group = group.copy()
            filtered_group["employees"] = matched_employees
            filtered_result.append(filtered_group)
        else:
            filtered_result.append(group)

    return filtered_result

# ============================================================================
# ROUTER / ENDPOINTS
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

if __name__ == "__main__":
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=True)