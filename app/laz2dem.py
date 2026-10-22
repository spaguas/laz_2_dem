from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import mdt_pipeline
from .mdt_pipeline import NODATA, MdtReport


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
    bounds: Optional[tuple[float, float, float, float]] = None,
) -> dict:
    # Saída bruta do PDAL já no padrão (Float32, nodata -32768) e, quando a
    # extensão é conhecida, numa grade com origem múltipla do pixel -- evita
    # deslocamento entre MDTs de folhas vizinhas.
    writer_opts: dict = {
        "nodata": NODATA,
        "data_type": "float32",
        "gdalopts": "COMPRESS=DEFLATE,TILED=YES",
    }
    if bounds is not None:
        xmin, ymin, xmax, ymax = bounds
        writer_opts["bounds"] = f"([{xmin}, {xmax}], [{ymin}, {ymax}])"

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
        {
            "type": "filters.sample",
            "radius": pixel_size / 3,
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
                **writer_opts,
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
                **writer_opts,
            },
        ]

    return {"pipeline": [*common_steps, *mode_steps]}


def _find_pdal() -> str:
    pdal_bin = shutil.which("pdal")
    if pdal_bin:
        return pdal_bin
    default_pdal_bin = Path("/opt/conda/envs/pdal/bin/pdal")
    if default_pdal_bin.exists():
        return str(default_pdal_bin)
    raise RuntimeError("Executavel 'pdal' nao encontrado no container.")


def _read_las_bounds(pdal_bin: str, input_path: Path) -> Optional[tuple[float, float, float, float]]:
    """Extensão do cabeçalho LAS (só metadados, não lê os pontos)."""
    try:
        result = subprocess.run(  # noqa: S603
            [pdal_bin, "info", "--metadata", str(input_path)],
            check=False, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return None
        meta = json.loads(result.stdout).get("metadata", {})
        if isinstance(meta, list):
            meta = meta[0] if meta else {}
        return float(meta["minx"]), float(meta["miny"]), float(meta["maxx"]), float(meta["maxy"])
    except (subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def process_laz_to_dem(
    input_path: Path,
    output_path: Path,
    source_srs: str,
    target_srs: str,
    *,
    pixel_size: int = 5,
    mode: str = "agressivo",
    report: Optional[MdtReport] = None,
) -> float:
    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo '{input_path}' nao encontrado.")

    report = report or MdtReport(titulo=f"MDT da folha {input_path.name}")
    epsg = mdt_pipeline.validar_crs_bacia(mdt_pipeline.epsg_from_srs(target_srs))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pdal_bin = _find_pdal()

    bounds = None
    if source_srs == target_srs:
        header_bounds = _read_las_bounds(pdal_bin, input_path)
        if header_bounds is not None:
            bounds = mdt_pipeline.extensao_alinhada(header_bounds, pixel_size)
            report.log(f"Extensão do cabeçalho LAS {header_bounds} alinhada para {bounds}.")
    if bounds is None:
        report.log("Extensão do cabeçalho LAS indisponível; grade será alinhada depois do PDAL.")

    raw_path = output_path.with_name(f"{output_path.stem}_RAW_PDAL.tif")
    pipeline_dict = build_pipeline(
        input_path=input_path,
        output_path=raw_path,
        source_srs=source_srs,
        target_srs=target_srs,
        pixel_size=pixel_size,
        mode=mode,
        bounds=bounds,
    )
    report.set("crs", f"EPSG:{epsg}")
    report.set("resolucao_m", pixel_size)
    report.set("modo_pdal", mode)

    started_at = time.time()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        json.dump(pipeline_dict, tmp)
        pipeline_path = Path(tmp.name)

    try:
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
            raise RuntimeError(
                f"Falha ao executar PDAL CLI (code={result.returncode}): {log_tail}")

        if not raw_path.exists():
            raise RuntimeError(
                "Saida .tiff nao foi criada apos a execucao do pipeline.")
        report.log("Pipeline PDAL concluído; aplicando limpeza e validação padrão.")

        # Passos 5–7 da pipeline padrão (limpeza, gravação e validação).
        mdt_pipeline.padronizar_mdt_folha(raw_path, output_path, epsg, float(pixel_size), report)
    finally:
        raw_path.unlink(missing_ok=True)

    return time.time() - started_at
