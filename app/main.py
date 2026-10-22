from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import bacia_vetor, bacias, mdt_bacia
from .grid_srs import GRID_NUMBER_SRS, GRID_PREFIX_PATTERN, infer_srs_from_name
from .job_store import JobRecord, JobStore
from .job_worker import JobWorker
from .mdt_pipeline import log_path_for

BASE_DIR = Path(__file__).resolve().parent.parent
NUVENS_DIR = BASE_DIR / "app" / "nuvens"
OUTPUT_DIR = BASE_DIR / "app" / "outputs"
DATA_DIR = BASE_DIR / "app" / "data"
MDT_DIR = Path(os.environ.get("MDT_HOST_PATH") or (BASE_DIR / "app" / "mdt_tiles"))
MDT_BACIA_DIR = Path(os.environ.get("MDT_BACIA_HOST_PATH") or (BASE_DIR / "app" / "mdt_bacia"))
# Uma pasta por processamento de MDT por bacia (nunca sobrescreve saídas antigas).
MDT_JOBS_DIR = OUTPUT_DIR / "mdt_bacia"
UPLOAD_DIR = BASE_DIR / "app" / "uploads"
UPLOAD_MAX_AGE_SECONDS = 24 * 3600
UPLOAD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

for directory in (NUVENS_DIR, OUTPUT_DIR, DATA_DIR, MDT_DIR, MDT_BACIA_DIR, MDT_JOBS_DIR, UPLOAD_DIR):
    directory.mkdir(parents=True, exist_ok=True)

store = JobStore(DATA_DIR / "jobs.json")
worker = JobWorker(store, mdt_bacia_dir=MDT_BACIA_DIR)

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
    srs = infer_srs_from_name(grid_name)
    if srs:
        return srs
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
            "kind": job.kind,
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
            "log_url": f"/api/jobs/{job.id}/log" if log_path_for(Path(job.output_path)).is_file() else None,
            "kind": job.kind,
            "missing_tiles": (job.extra or {}).get("missing_tiles") if job.kind == "mdt_bacia" else None,
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


@app.get("/api/jobs/{job_id}/log")
def download_log(job_id: str) -> FileResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    log_path = log_path_for(Path(job.output_path))
    if not log_path.is_file():
        raise HTTPException(status_code=404, detail="Log ainda não disponível")

    return FileResponse(path=log_path, media_type="text/plain; charset=utf-8", filename=log_path.name)


def _bacia_response(info: bacias.BaciaInfo) -> dict:
    tile_names = bacias.resolve_tile_names_for_uc(info.uc)
    found, missing = mdt_bacia.resolve_tile_files(tile_names, MDT_DIR)
    epsg, _areas = bacia_vetor.escolher_zona_utm(info.geometry)
    return {
        "uc": info.uc,
        "upst_area": info.upst_area,
        "tile_count": len(tile_names),
        "tiles_found": len(found),
        "missing_tiles": missing,
        "crs_destino": f"EPSG:{epsg}",
    }


@app.get("/api/bacias/by-uc/{uc}")
def bacia_by_uc(uc: int) -> JSONResponse:
    info = bacias.get_bacia_by_uc(uc)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Bacia não encontrada para o código UC {uc}.")
    return JSONResponse(_bacia_response(info))


@app.get("/api/bacias/by-point")
def bacia_by_point(lat: float, lon: float) -> JSONResponse:
    info = bacias.get_bacia_by_point(lat, lon)
    if info is None:
        raise HTTPException(
            status_code=404,
            detail="Nenhuma bacia hidrográfica encontrada para essa coordenada.",
        )
    return JSONResponse(_bacia_response(info))


def _find_existing_mdt_bacia(filename: str) -> Optional[Path]:
    server_path = MDT_BACIA_DIR / filename
    if server_path.is_file():
        return server_path
    output_path = OUTPUT_DIR / filename
    if output_path.is_file():
        return output_path
    anteriores = sorted(MDT_JOBS_DIR.glob(f"*/{filename}"), key=lambda p: p.stat().st_mtime, reverse=True)
    return anteriores[0] if anteriores else None


def _new_mdt_job_dir(base: str, job_id: str) -> Path:
    """Pasta própria do processamento (nomes só ASCII, sem espaços)."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = MDT_JOBS_DIR / f"{bacia_vetor.nome_ascii(base, max_len=50)}_{stamp}_{job_id[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _validate_resolution(resolution: float) -> None:
    if not (0.1 <= resolution <= 100):
        raise HTTPException(status_code=400, detail="Resolução espacial deve estar entre 0.1 e 100 metros.")


def _parse_barragem(lat: Optional[float], lon: Optional[float]) -> Optional[list[float]]:
    if lat is None and lon is None:
        return None
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Coordenada da barragem inválida (informe latitude e longitude).")
    return [lon, lat]


def _schedule_mdt_job(
    *,
    filename: str,
    folder: Path,
    job_id: str,
    epsg: int,
    tile_files: list[Path],
    missing_tiles: list[str],
    extra: dict,
) -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    target_srs = f"EPSG:{epsg}"
    store.create_job(
        JobRecord(
            id=job_id,
            original_filename=filename,
            input_path=str(MDT_DIR),
            output_path=str(folder / filename),
            status="queued",
            progress=0,
            message=f"Job de MDT criado ({len(tile_files)} folha(s), {len(missing_tiles)} ausente(s)).",
            created_at=now,
            updated_at=now,
            source_srs=target_srs,
            target_srs=target_srs,
            kind="mdt_bacia",
            extra={
                **extra,
                "epsg": epsg,
                "target_srs": target_srs,
                "tile_files": [str(p) for p in tile_files],
                "missing_tiles": missing_tiles,
            },
        )
    )
    worker.enqueue(job_id)


@app.get("/api/mdt-bacia/check")
def check_mdt_bacia(uc: int, resolution: float = 1.0) -> JSONResponse:
    filename = mdt_bacia.mdt_bacia_filename(uc, resolution)
    existing_path = _find_existing_mdt_bacia(filename)
    if existing_path is None:
        return JSONResponse({"exists": False})
    return JSONResponse({"exists": True, "filename": filename, "path": str(existing_path)})


@app.post("/api/mdt-bacia/jobs")
def create_mdt_bacia_job(
    uc: Annotated[int, Form()],
    resolution: Annotated[float, Form()] = 1.0,
    save_to_server: Annotated[bool, Form()] = False,
    approve_overwrite: Annotated[bool, Form()] = False,
    merge_mode: Annotated[str, Form()] = "fast",
    barragem_lat: Annotated[Optional[float], Form()] = None,
    barragem_lon: Annotated[Optional[float], Form()] = None,
) -> JSONResponse:
    _validate_resolution(resolution)

    if merge_mode not in ("fast", "blend"):
        raise HTTPException(status_code=400, detail="merge_mode deve ser 'fast' ou 'blend'.")

    barragem = _parse_barragem(barragem_lat, barragem_lon)

    info = bacias.get_bacia_by_uc(uc)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Bacia não encontrada para o código UC {uc}.")

    tile_names = bacias.resolve_tile_names_for_uc(info.uc)
    if not tile_names:
        raise HTTPException(
            status_code=400,
            detail=f"Nenhuma lista de tiles encontrada para a bacia UC {uc} (folhas_{uc}.csv ausente).",
        )

    tile_files, missing_tiles = mdt_bacia.resolve_tile_files(tile_names, MDT_DIR)
    if not tile_files:
        raise HTTPException(
            status_code=400,
            detail="Nenhum tile MDT foi encontrado no servidor para os tiles dessa bacia.",
        )

    filename = mdt_bacia.mdt_bacia_filename(uc, resolution)

    existing_path = _find_existing_mdt_bacia(filename)
    if existing_path is not None and not approve_overwrite:
        return JSONResponse(
            {
                "detail": "Já existe um arquivo MDT_BACIA para essa bacia e resolução.",
                "reprocess_required": True,
                "filename": filename,
                "path": str(existing_path),
            },
            status_code=409,
        )

    # CRS da bacia: SIRGAS 2000 / UTM da zona que contém a maior parte da área.
    epsg, areas = bacia_vetor.escolher_zona_utm(info.geometry)
    bacia_utm = bacia_vetor.gpd.GeoSeries([info.geometry], crs="EPSG:4326").to_crs(epsg).iloc[0]

    job_id = str(uuid.uuid4())
    folder = _new_mdt_job_dir(f"UC_{uc}", job_id)
    bacia_path = folder / "bacia.gpkg"
    bacia_vetor.salvar_geometria(bacia_utm, epsg, bacia_path)

    _schedule_mdt_job(
        filename=filename,
        folder=folder,
        job_id=job_id,
        epsg=epsg,
        tile_files=tile_files,
        missing_tiles=missing_tiles,
        extra={
            "origem": "uc",
            "uc": uc,
            "resolution": resolution,
            "save_to_server": save_to_server,
            "merge_mode": merge_mode,
            "bacia_path": str(bacia_path),
            "area_zona_km2": areas,
            "barragem": barragem,
        },
    )

    return JSONResponse(
        {
            "id": job_id,
            "filename": filename,
            "status": "queued",
            "progress": 0,
            "message": "MDT da bacia agendado para execução.",
            "kind": "mdt_bacia",
            "tile_count": len(tile_files),
            "missing_tiles": missing_tiles,
            "target_srs": f"EPSG:{epsg}",
        },
        status_code=202,
    )


# --- MDT de bacia enviada como arquivo vetorial -------------------------------
def _cleanup_old_uploads() -> None:
    limite = time.time() - UPLOAD_MAX_AGE_SECONDS
    for folder in UPLOAD_DIR.iterdir():
        if folder.is_dir() and UPLOAD_ID_PATTERN.match(folder.name) and folder.stat().st_mtime < limite:
            shutil.rmtree(folder, ignore_errors=True)


def _upload_folder(upload_id: str) -> Path:
    if not UPLOAD_ID_PATTERN.match(upload_id or ""):
        raise HTTPException(status_code=400, detail="Identificador de upload inválido.")
    folder = UPLOAD_DIR / upload_id
    if not (folder / "meta.json").is_file():
        raise HTTPException(status_code=404, detail="Bacia enviada não encontrada ou expirada. Envie o arquivo novamente.")
    return folder


@app.post("/api/bacias/upload")
async def upload_bacia(files: list[UploadFile] = File(...)) -> JSONResponse:
    _cleanup_old_uploads()
    arquivos = [(f.filename or "", await f.read()) for f in files]

    upload_id = uuid.uuid4().hex
    folder = UPLOAD_DIR / upload_id
    raw_dir = folder / "original"
    try:
        vector_path = bacia_vetor.salvar_upload(arquivos, raw_dir)
        nome = bacia_vetor.nome_ascii(vector_path.stem, max_len=40)
        bacia = bacia_vetor.preparar_bacia(vector_path, nome)
    except bacia_vetor.BaciaVetorError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Não foi possível ler a bacia: {exc}") from exc

    tile_names = bacia_vetor.folhas_por_geometria(bacia.geometry_wgs84)
    if not tile_names:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail="Nenhuma folha MDT do índice de articulação intersecta a bacia enviada.",
        )
    found, missing = mdt_bacia.resolve_tile_files(tile_names, MDT_DIR)

    bacia_vetor.salvar_geometria(bacia.geometry_utm, bacia.epsg, folder / "bacia.gpkg")
    meta = {
        "nome": bacia.nome,
        "arquivo_enviado": vector_path.name,
        "epsg": bacia.epsg,
        "crs_original": bacia.crs_original,
        "reprojetada": bacia.reprojetada,
        "n_feicoes": bacia.n_feicoes,
        "area_km2": bacia.area_km2,
        "area_zona_km2": bacia.area_zona_km2,
        "tile_names": tile_names,
    }
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return JSONResponse(
        {
            "upload_id": upload_id,
            "nome": bacia.nome,
            "arquivo": vector_path.name,
            "crs_original": bacia.crs_original,
            "crs_destino": f"EPSG:{bacia.epsg}",
            "reprojetada": bacia.reprojetada,
            "n_feicoes": bacia.n_feicoes,
            "area_km2": bacia.area_km2,
            "area_zona_km2": bacia.area_zona_km2,
            "tile_count": len(tile_names),
            "tiles_found": len(found),
            "missing_tiles": missing,
        }
    )


@app.post("/api/mdt-bacia/upload-jobs")
def create_mdt_bacia_upload_job(
    upload_id: Annotated[str, Form()],
    resolution: Annotated[float, Form()] = 5.0,
    barragem_lat: Annotated[Optional[float], Form()] = None,
    barragem_lon: Annotated[Optional[float], Form()] = None,
) -> JSONResponse:
    _validate_resolution(resolution)
    barragem = _parse_barragem(barragem_lat, barragem_lon)
    upload = _upload_folder(upload_id)
    meta = json.loads((upload / "meta.json").read_text(encoding="utf-8"))

    tile_files, missing_tiles = mdt_bacia.resolve_tile_files(meta["tile_names"], MDT_DIR)
    if not tile_files:
        raise HTTPException(
            status_code=400,
            detail="Nenhuma folha MDT da bacia enviada foi encontrada no servidor.",
        )

    job_id = str(uuid.uuid4())
    folder = _new_mdt_job_dir(meta["nome"], job_id)
    bacia_path = folder / "bacia.gpkg"
    shutil.copy2(upload / "bacia.gpkg", bacia_path)
    filename = mdt_bacia.mdt_bacia_filename(meta["nome"], resolution)

    _schedule_mdt_job(
        filename=filename,
        folder=folder,
        job_id=job_id,
        epsg=int(meta["epsg"]),
        tile_files=tile_files,
        missing_tiles=missing_tiles,
        extra={
            "origem": "vetor",
            "nome_bacia": meta["nome"],
            "arquivo_enviado": meta["arquivo_enviado"],
            "crs_original": meta["crs_original"],
            "reprojetada": meta["reprojetada"],
            "area_zona_km2": meta["area_zona_km2"],
            "resolution": resolution,
            # Bacia enviada pelo usuário: nunca copia para a pasta do servidor.
            "save_to_server": False,
            "merge_mode": "fast",
            "bacia_path": str(bacia_path),
            "barragem": barragem,
        },
    )

    return JSONResponse(
        {
            "id": job_id,
            "filename": filename,
            "status": "queued",
            "progress": 0,
            "message": "MDT da bacia enviada agendado para execução.",
            "kind": "mdt_bacia",
            "tile_count": len(tile_files),
            "missing_tiles": missing_tiles,
            "target_srs": f"EPSG:{meta['epsg']}",
        },
        status_code=202,
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
