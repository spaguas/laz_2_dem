from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .job_store import JobRecord, JobStore
from .job_worker import JobWorker

BASE_DIR = Path(__file__).resolve().parent.parent
NUVENS_DIR = BASE_DIR / "app" / "nuvens"
OUTPUT_DIR = BASE_DIR / "app" / "outputs"
DATA_DIR = BASE_DIR / "app" / "data"

for directory in (NUVENS_DIR, OUTPUT_DIR, DATA_DIR):
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
# A segunda letra do prefixo (ex.: o "F" em "SF-22-") pode variar; quem define o EPSG
# é apenas o número (22 ou 23), então o padrão aceita qualquer letra nessa posição.
GRID_PREFIX_PATTERN = re.compile(r"^S[A-Z]-(22|23)-")
GRID_NUMBER_SRS = {
    "22": "EPSG:31982",
    "23": "EPSG:31983",
}


def list_laz_grids() -> list[str]:
    return sorted(
        path.name
        for path in NUVENS_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".laz"
        and GRID_PREFIX_PATTERN.match(path.name.upper())
    )


def get_grid_path(grid_name: str) -> Path:
    safe_name = Path(grid_name).name
    if safe_name != grid_name:
        raise HTTPException(status_code=400, detail=f"Grid inválido: {grid_name}")

    grid_path = NUVENS_DIR / safe_name
    if grid_path.suffix.lower() != ".laz" or not grid_path.is_file():
        raise HTTPException(status_code=400, detail=f"Grid não encontrado: {grid_name}")
    return grid_path


def infer_source_srs(grid_name: str) -> str:
    match = GRID_PREFIX_PATTERN.match(grid_name.upper())
    if match:
        return GRID_NUMBER_SRS[match.group(1)]
    raise HTTPException(
        status_code=400,
        detail=f"Não foi possível inferir a projeção de origem para o grid: {grid_name}",
    )


def find_processed_grids(grid_names: list[str]) -> list[str]:
    selected = set(grid_names)
    processed = set()
    for job in store.list_jobs():
        if job.original_filename not in selected:
            continue
        if job.status == "completed" and Path(job.output_path).exists():
            processed.add(job.original_filename)
    return sorted(processed)


@app.on_event("startup")
def startup_event() -> None:
    worker.start()


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    jobs = store.list_jobs()[:10]
    grids = list_laz_grids()
    static_version = str((BASE_DIR / "app" / "static" / "app.js").stat().st_mtime_ns)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "jobs": jobs,
            "grids": grids,
            "projections": PROJECTION_OPTIONS,
            "grid_prefix_pattern": GRID_PREFIX_PATTERN.pattern,
            "grid_number_srs": GRID_NUMBER_SRS,
            "static_version": static_version,
        },
    )


@app.post("/api/jobs")
def create_job(
    grids: list[str] = Form(...),
    approve_reprocess: Annotated[bool, Form()] = False,
) -> JSONResponse:
    if not grids:
        raise HTTPException(status_code=400, detail="Selecione ao menos um grid .laz.")

    selected_paths = [get_grid_path(grid) for grid in grids]
    if len({path.name for path in selected_paths}) != len(selected_paths):
        raise HTTPException(status_code=400, detail="A seleção contém grids duplicados.")

    inferred_sources = {path.name: infer_source_srs(path.name) for path in selected_paths}
    processed_grids = find_processed_grids([path.name for path in selected_paths])
    if processed_grids and not approve_reprocess:
        return JSONResponse(
            {
                "detail": "Um ou mais grids selecionados já foram processados.",
                "reprocess_required": True,
                "grids": processed_grids,
            },
            status_code=409,
        )

    now = datetime.now(tz=timezone.utc).isoformat()
    created_jobs = []
    for input_path in selected_paths:
        job_id = str(uuid.uuid4())
        source_srs = inferred_sources[input_path.name]
        target_srs = source_srs
        output_path = OUTPUT_DIR / f"{input_path.stem}.tiff"
        store.create_job(
            JobRecord(
                id=job_id,
                original_filename=input_path.name,
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
        created_jobs.append(
            {
                "id": job_id,
                "filename": input_path.name,
                "status": "queued",
                "progress": 0,
                "message": "Grid agendado para execução.",
                "source_srs": source_srs,
                "target_srs": target_srs,
            }
        )

    return JSONResponse(
        {
            "jobs": created_jobs,
            "status": "queued",
            "progress": 0,
            "message": f"{len(created_jobs)} grid(s) agendado(s) para execução.",
        },
        status_code=202,
    )


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    jobs = [
        {
            "id": job.id,
            "filename": job.original_filename,
            "status": job.status,
            "progress": job.progress,
            "message": job.message,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.updated_at if job.status == "completed" else None,
            "source_srs": job.source_srs,
            "target_srs": job.target_srs,
            "has_error_log": bool(job.error_log),
        }
        for job in store.list_jobs()
    ]
    return JSONResponse({"jobs": jobs})


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
            "completed_at": job.updated_at if job.status == "completed" else None,
            "source_srs": job.source_srs,
            "target_srs": job.target_srs,
            "error_log": job.error_log,
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
        filename=output_path.name,
    )


@app.get("/api/summary")
def get_summary() -> JSONResponse:
    return JSONResponse(store.get_overall_summary())


@app.get("/api/batch/{batch_id}/summary")
def get_batch_summary(batch_id: str) -> JSONResponse:
    return JSONResponse(store.get_batch_summary(batch_id))


@app.post("/api/batch")
async def create_batch(
    file: UploadFile = File(...),
    approve_reprocess: Annotated[bool, Form()] = False,
) -> JSONResponse:
    contents = await file.read()
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = contents.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    filenames: list[str] = []
    for row in reader:
        if not row:
            continue
        name = row[0].strip()
        if not name:
            continue
        if name.lower() in ("filename", "nome", "arquivo", "grid"):
            continue
        if not name.lower().endswith(".laz"):
            name += ".laz"
        filenames.append(name)

    if not filenames:
        raise HTTPException(status_code=400, detail="CSV vazio ou sem nomes de arquivo válidos.")

    # Validate all filenames before creating any job
    errors: list[dict] = []
    valid_paths: list[tuple[str, Path, str]] = []
    for name in filenames:
        safe_name = Path(name).name
        if safe_name != name:
            errors.append({"filename": name, "error": "Nome de arquivo inválido."})
            continue
        grid_path = NUVENS_DIR / safe_name
        if grid_path.suffix.lower() != ".laz" or not grid_path.is_file():
            errors.append({"filename": name, "error": "Arquivo não encontrado em app/nuvens."})
            continue
        try:
            source_srs = infer_source_srs(name)
        except HTTPException as exc:
            errors.append({"filename": name, "error": exc.detail})
            continue
        valid_paths.append((name, grid_path, source_srs))

    if not valid_paths:
        return JSONResponse(
            {"detail": "Nenhum arquivo válido encontrado.", "errors": errors},
            status_code=400,
        )

    valid_names = [name for name, _, _ in valid_paths]
    processed_grids = find_processed_grids(valid_names)
    skipped_grids: list[str] = []
    if processed_grids and not approve_reprocess:
        skipped_grids = processed_grids
        processed_set = set(processed_grids)
        valid_paths = [item for item in valid_paths if item[0] not in processed_set]

    if not valid_paths:
        return JSONResponse(
            {
                "detail": "Todos os grids do CSV já foram processados anteriormente.",
                "skipped": skipped_grids,
                "errors": errors,
            },
            status_code=200,
        )

    batch_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()
    created_jobs = []

    for name, input_path, source_srs in valid_paths:
        job_id = str(uuid.uuid4())
        output_path = OUTPUT_DIR / f"{input_path.stem}.tiff"
        store.create_job(
            JobRecord(
                id=job_id,
                original_filename=name,
                input_path=str(input_path),
                output_path=str(output_path),
                status="queued",
                progress=0,
                message="Job criado via CSV e aguardando processamento.",
                created_at=now,
                updated_at=now,
                source_srs=source_srs,
                target_srs=source_srs,
                batch_id=batch_id,
            )
        )
        worker.enqueue(job_id)
        created_jobs.append({"id": job_id, "filename": name, "status": "queued"})

    message = f"{len(created_jobs)} grid(s) agendado(s) de {len(filenames)} linha(s) no CSV."
    if skipped_grids:
        message += f" {len(skipped_grids)} grid(s) já processado(s) foram ignorado(s)."

    return JSONResponse(
        {
            "batch_id": batch_id,
            "total": len(filenames),
            "queued": len(created_jobs),
            "skipped": skipped_grids,
            "errors": errors,
            "message": message,
        },
        status_code=202,
    )
