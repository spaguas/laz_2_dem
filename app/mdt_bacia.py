from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable

from .grid_srs import infer_srs_from_name

DEFAULT_TARGET_SRS = "EPSG:31982"
TILE_EXTENSIONS = (".tif", ".tiff")


def mdt_bacia_filename(uc: int, resolution: float) -> str:
    return f"MDT_BACIA_{uc}_{resolution:g}m.tif"


def resolve_tile_files(names: Iterable[str], mdt_dir: Path) -> tuple[list[Path], list[str]]:
    existing_by_lower = (
        {p.name.lower(): p for p in mdt_dir.iterdir() if p.is_file()} if mdt_dir.is_dir() else {}
    )

    found: list[Path] = []
    missing: list[str] = []
    for name in names:
        match = None
        for ext in TILE_EXTENSIONS:
            candidate = mdt_dir / f"{name}{ext}"
            if candidate.is_file():
                match = candidate
                break
        if match is None:
            for ext in TILE_EXTENSIONS:
                match = existing_by_lower.get(f"{name}{ext}".lower())
                if match is not None:
                    break
        if match is not None:
            found.append(match)
        else:
            missing.append(name)
    return found, missing


def pick_target_srs(tile_paths: list[Path]) -> str:
    counts: dict[str, int] = {}
    for path in tile_paths:
        srs = infer_srs_from_name(path.stem)
        if srs:
            counts[srs] = counts.get(srs, 0) + 1
    if not counts:
        return DEFAULT_TARGET_SRS
    return max(counts.items(), key=lambda item: (item[1], item[0] == DEFAULT_TARGET_SRS))[0]


def _find_gdalwarp() -> str:
    gdalwarp_bin = shutil.which("gdalwarp")
    if gdalwarp_bin:
        return gdalwarp_bin
    for candidate in (Path("/opt/conda/envs/pdal/bin/gdalwarp"), Path("/opt/conda/bin/gdalwarp")):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("Executavel 'gdalwarp' nao encontrado no container.")


def merge_tiles(
    tile_paths: list[Path],
    output_path: Path,
    target_srs: str,
    resolution: float,
) -> float:
    if not tile_paths:
        raise ValueError("Nenhum tile MDT informado para o merge.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    gdalwarp_bin = _find_gdalwarp()

    started_at = time.time()
    cmd = [
        gdalwarp_bin,
        "-t_srs", target_srs,
        "-tr", str(resolution), str(resolution),
        "-r", "bilinear",
        "-co", "COMPRESS=LZW",
        *[str(path) for path in tile_paths],
        str(output_path),
    ]
    result = subprocess.run(  # noqa: S603
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    elapsed = time.time() - started_at

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        log_tail = stderr[-1200:] if stderr else stdout[-1200:]
        raise RuntimeError(f"Falha ao executar gdalwarp (code={result.returncode}): {log_tail}")

    if not output_path.exists():
        raise RuntimeError("Saida .tif nao foi criada apos a execucao do gdalwarp.")

    return elapsed
