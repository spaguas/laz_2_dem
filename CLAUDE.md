# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python/FastAPI web app that converts LIDAR point clouds (`.laz`) into GeoTIFF DEMs via a PDAL
pipeline, and separately merges pre-existing MDT raster tiles across a hydrographic basin
(bacia) into a single GeoTIFF via GDAL. Single-page frontend (vanilla JS, no build step) polls a
JSON job API for progress. No test suite exists in this repo.

## Running

Docker (primary way to run/test changes end-to-end — needs PDAL/GDAL system libs that aren't
trivial to install locally):

```bash
docker compose up --build
```

Serves on `http://localhost:9091` (host) → container port 9090.

Local (requires PDAL + GDAL system libraries already installed, e.g. via conda):

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 9090
# or: python script.py
```

There is no lint/test/build command configured in this repo (no pytest, no linter config).

## Architecture

**Two independent processing pipelines share one job queue/store:**

1. **`.laz` → GeoTIFF** (`app/laz2dem.py`): builds a PDAL pipeline JSON (reprojection, Z/return
   filters, outlier removal, SMRF ground classification, then `writers.gdal`) and shells out to
   the `pdal` CLI (`pdal pipeline <json>`). Two quality modes: `agressivo` (default, tighter
   ground filtering) vs `conservador`. Source/target SRS is inferred from the grid filename, not
   chosen freely by the user — see `app/grid_srs.py`: filenames matching `^S[A-Z]-(22|23)-` map
   to EPSG:31982 (zone 22) or EPSG:31983 (zone 23).

2. **MDT por bacia** (`app/mdt_pipeline.py`, driven by `app/job_worker.py`): the basin comes from
   a UC code or lat/lon point (resolved against `app/gpkg/Subbacias_Q5_SP.gpkg`, tiles listed in
   `app/folhas_por_bacia/folhas_{uc}.csv`) or from a user-uploaded shapefile/GPKG/KML/KMZ
   (`app/bacia_vetor.py`, tiles selected by intersecting `app/gpkg/articulacao_lidar_correcao.gpkg`).
   The basin is always worked in EPSG:31982 or 31983 — anything else is reprojected to the UTM zone
   holding most of the basin's geodesic area (22S west of -48°, 23S east). `gerar_mdt_bacia` runs the
   standard TerraHidro pipeline: grid-aligned extent → in-memory mosaic (one tile open at a time; all
   known nodata values — declared, 0, -9999, -32768 — normalized *before* resampling; nearest when the
   tile is already on the target grid, bilinear otherwise; `blend` mode feathers overlaps) → clip →
   spurious-value cleaning (negatives + 2-px ring + below-LIMIAR pixels that aren't real valleys,
   FillNodata 200 px) → interior-hole fill (50 px) → Float32/-32768/DEFLATE/TILED write → automatic
   validation that raises `MdtValidationError` (job fails, output deleted, log kept). Each job writes
   into its own folder under `app/outputs/mdt_bacia/` with `CLIPPED_DEM_ORIGINAL.tif` and a
   `*_LOG.txt`. The mosaic is **not** a gdalwarp call on purpose: `-srcnodata` takes one value per
   band and the source tiles mix several nodata values; don't switch it back to a single unbounded
   gdalwarp either ("Too many open files" on large basins). Memory is bounded by `MDT_MAX_PIXELS`.

   The `.laz` flow reuses the same standard (`padronizar_mdt_folha`): PDAL writes Float32/-32768 on a
   pixel-aligned grid, then cleaning + validation run, with leftover interior holes as a warning only.

**Job execution model** (`app/job_store.py`, `app/job_worker.py`): a single background thread
(`JobWorker`) consumes a `queue.Queue` of job IDs sequentially — jobs are not parallelized.
`JobStore` persists all job state to a flat JSON file (`app/data/jobs.json`) protected by a
`threading.Lock`; there's no database. `JobRecord.kind` (`"laz2dem"` vs `"mdt_bacia"`) determines
which branch `JobWorker._process` dispatches to, and `JobRecord.extra` carries pipeline-specific
params (tile list, resolution, merge mode, etc.) as a loosely-typed dict. `app/main.py` is the
FastAPI app: routes create jobs, enqueue them, and the frontend (`app/static/app.js`) polls
`GET /api/jobs/{id}` every 2s.

**Directories are runtime data, not static config** — `app/nuvens` (source `.laz` files),
`app/mdt_tiles` (source MDT tiles), `app/outputs` (generated files), `app/data` (jobs.json),
`app/mdt_bacia` (optional server-side copy of basin merges). In Docker these are bind-mounted via
env vars following the `NUVENS_HOST_PATH` pattern — see `docker-compose.yml` and `.env.example`
for the full list (`MDT_HOST_PATH`, `MDT_BACIA_HOST_PATH`). **Do not bind-mount the whole repo
(`.:/app`) in production** — it overwrites the image's code and breaks the module import.

## Conventions specific to this repo

- Subprocess-based point-cloud tooling (PDAL CLI via `subprocess.run`, not `pdal.Pipeline`/`osgeo` bindings);
  raster work for MDTs is rasterio + numpy/scipy (no `osgeo` import). PDAL errors are surfaced by
  tailing stderr/stdout, not parsed structurally.
- User-facing strings (job messages, HTTP error details, comments explaining non-obvious
  decisions) are in Portuguese; keep new ones consistent with that.
- Grid/tile filenames double as the only source of SRS info (via `grid_srs.infer_srs_from_name`)
  — there's no per-request SRS override in the `.laz` upload flow.
