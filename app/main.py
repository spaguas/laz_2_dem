from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .job_store import JobRecord, JobStore
from .job_worker import JobWorker

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "app" / "uploads"
OUTPUT_DIR = BASE_DIR / "app" / "outputs"
DATA_DIR = BASE_DIR / "app" / "data"

for directory in (UPLOAD_DIR, OUTPUT_DIR, DATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)

store = JobStore(DATA_DIR / "jobs.json")
worker = JobWorker(store)

app = FastAPI(title="Processador LAZ para GeoTIFF")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

PROJECTION_OPTIONS = [
    {"value": "EPSG:4674", "label": "SIRGAS 2000 (EPSG:4674)"},
    {"value": "EPSG:4326", "label": "WGS84 (EPSG:4326)"},
    {"value": "EPSG:31982", "label": "SIRGAS 2000 / UTM 22S (EPSG:31982)"},
    {"value": "EPSG:31983", "label": "SIRGAS 2000 / UTM 23S (EPSG:31983)"},
    {"value": "EPSG:31984", "label": "SIRGAS 2000 / UTM 24S (EPSG:31984)"},
    {"value": "EPSG:31985", "label": "SIRGAS 2000 / UTM 25S (EPSG:31985)"},
]
ALLOWED_PROJECTIONS = {item["value"] for item in PROJECTION_OPTIONS}


@app.on_event("startup")
def startup_event() -> None:
    worker.start()


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    jobs = store.list_jobs()[:10]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "jobs": jobs,
            "projections": PROJECTION_OPTIONS,
        },
    )


@app.post("/api/jobs")
def create_job(
    file: UploadFile = File(...),
    source_srs: str = Form(...),
    target_srs: str = Form(...),
) -> JSONResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix != ".laz":
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .laz")
    if source_srs not in ALLOWED_PROJECTIONS:
        raise HTTPException(status_code=400, detail="Projeção de origem inválida.")
    if target_srs not in ALLOWED_PROJECTIONS:
        raise HTTPException(status_code=400, detail="Projeção de destino inválida.")

    job_id = str(uuid.uuid4())
    safe_name = Path(file.filename).name
    input_path = UPLOAD_DIR / f"{job_id}_{safe_name}"
    output_path = OUTPUT_DIR / f"{job_id}.tif"

    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    now = datetime.now(tz=timezone.utc).isoformat()
    store.create_job(
        JobRecord(
            id=job_id,
            original_filename=safe_name,
            input_path=str(input_path),
            output_path=str(output_path),
            status="queued",
            progress=0,
            message="Job criado e aguardando processamento.",
            created_at=now,
            updated_at=now,
            source_srs=source_srs,
            target_srs=target_srs,
        )
    )

    worker.enqueue(job_id)
    return JSONResponse(
        {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Upload concluído. Job agendado para execução.",
            "source_srs": source_srs,
            "target_srs": target_srs,
        },
        status_code=202,
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    return JSONResponse(
        {
            "id": job.id,
            "filename": job.original_filename,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "source_srs": job.source_srs,
            "target_srs": job.target_srs,
            "download_url": f"/api/jobs/{job.id}/download" if job.status == "completed" else None,
        }
    )


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str) -> FileResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Resultado ainda não disponível")

    return FileResponse(
        path=output_path,
        media_type="image/tiff",
        filename=f"{Path(job.original_filename).stem}_{job.target_srs.lower().replace(':', '_')}.tif",
    )
