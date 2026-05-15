from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def build_pipeline(
    input_path: Path,
    output_path: Path,
    source_srs: str,
    target_srs: str,
    *,
    pixel_size: int = 5,
    min_z: int = -100,
    max_z: int = 3000,
    mode: str = "agressivo",
) -> dict:
    common_steps = [
        {
            "type": "readers.las",
            "filename": str(input_path),
        },
        {
            "type": "filters.reprojection",
            "in_srs": source_srs,
            "out_srs": source_srs,
        },
        {
            "type": "filters.range",
            "limits": f"Z[{min_z}:{max_z}]",
        },
        {
            "type": "filters.range",
            "limits": "ReturnNumber[1:15]",
        },
        {
            "type": "filters.range",
            "limits": "NumberOfReturns[1:15]",
        },
    ]

    if mode == "conservador":
        mode_steps = [
            {
                "type": "filters.smrf",
                "slope": 0.2,
                "window": 20.0,
                "scalar": 1.5,
            },
            {
                "type": "filters.range",
                "limits": "Classification[0:2]",
            },
            {
                "type": "filters.reprojection",
                "in_srs": source_srs,
                "out_srs": target_srs,
            },
            {
                "type": "writers.gdal",
                "filename": str(output_path),
                "output_type": "idw",
                "resolution": pixel_size,
            },
        ]
    else:
        mode_steps = [
            {
                "type": "filters.outlier",
                "method": "statistical",
                "mean_k": 16,
                "multiplier": 2.5,
            },
            {
                "type": "filters.smrf",
                "slope": 0.15,
                "window": 16.0,
                "scalar": 1.25,
            },
            {
                "type": "filters.range",
                "limits": "Classification[2:2]",
            },
            {
                "type": "filters.reprojection",
                "in_srs": source_srs,
                "out_srs": target_srs,
            },
            {
                "type": "writers.gdal",
                "filename": str(output_path),
                "output_type": "mean",
                "resolution": pixel_size,
            },
        ]

    return {"pipeline": [*common_steps, *mode_steps]}


def process_laz_to_dem(
    input_path: Path,
    output_path: Path,
    source_srs: str,
    target_srs: str,
    *,
    pixel_size: int = 5,
    mode: str = "agressivo",
) -> float:
    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo '{input_path}' nao encontrado.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline_dict = build_pipeline(
        input_path=input_path,
        output_path=output_path,
        source_srs=source_srs,
        target_srs=target_srs,
        pixel_size=pixel_size,
        mode=mode,
    )

    pdal_bin = shutil.which("pdal")
    if not pdal_bin:
        default_pdal_bin = Path("/opt/conda/envs/pdal/bin/pdal")
        if default_pdal_bin.exists():
            pdal_bin = str(default_pdal_bin)
        else:
            raise RuntimeError("Executavel 'pdal' nao encontrado no container.")

    started_at = time.time()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(pipeline_dict, tmp)
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

    elapsed = time.time() - started_at
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        log_tail = stderr[-1200:] if stderr else stdout[-1200:]
        raise RuntimeError(f"Falha ao executar PDAL CLI (code={result.returncode}): {log_tail}")

    if not output_path.exists():
        raise RuntimeError("Saida .tiff nao foi criada apos a execucao do pipeline.")

    return elapsed
