from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def build_pipeline(input_path: Path, output_path: Path, source_srs: str, target_srs: str) -> str:
    pipeline = [
        {
            "type": "readers.las",
            "filename": str(input_path),
            "override_srs": source_srs,
        },
        {
            "type": "filters.csf",
            "resolution": 1.0,
            "threshold": 0.5,
            "hdiff": 0.3,
            "rigidness": 3,
            "iterations": 500,
            "smooth": True,
        },
        {
            "type": "filters.expression",
            "expression": "Classification == 2",
        },
        {
            "type": "filters.reprojection",
            "in_srs": source_srs,
            "out_srs": target_srs,
        },
        {
            "type": "writers.gdal",
            "filename": str(output_path),
            "gdaldriver": "GTiff",
            "output_type": "idw",
            "resolution": 1.0,
            "radius": 1.5,
            "nodata": -9999,
            "override_srs": target_srs,
        },
    ]
    return json.dumps(pipeline)


def process_laz_to_tif(input_path: Path, output_path: Path, source_srs: str, target_srs: str) -> None:
    pdal_bin = shutil.which("pdal")
    if not pdal_bin:
        raise RuntimeError("Executável 'pdal' não encontrado no container.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline_json = build_pipeline(input_path, output_path, source_srs, target_srs)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        tmp.write(pipeline_json)
        pipeline_path = Path(tmp.name)

    try:
        result = subprocess.run(  # noqa: S603
            [pdal_bin, "pipeline", str(pipeline_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        pipeline_path.unlink(missing_ok=True)

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        log_tail = stderr[-1200:] if stderr else stdout[-1200:]
        raise RuntimeError(f"Falha ao executar PDAL CLI (code={result.returncode}): {log_tail}")

    if not output_path.exists():
        raise RuntimeError("Saída .tif não foi criada após a execução do pipeline.")
