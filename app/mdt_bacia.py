from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from pyproj import Transformer

from .grid_srs import infer_srs_from_name

DEFAULT_TARGET_SRS = "EPSG:31982"
TILE_EXTENSIONS = (".tif", ".tiff")
MERGE_NODATA = -32768
CBLEND_PIXELS = 20

MergeMode = Literal["fast", "blend"]
ProgressCallback = Callable[[int, int], None]


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


def _find_bin(name: str) -> str:
    bin_path = shutil.which(name)
    if bin_path:
        return bin_path
    for candidate in (Path(f"/opt/conda/envs/pdal/bin/{name}"), Path(f"/opt/conda/bin/{name}")):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"Executavel '{name}' nao encontrado no container.")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        log_tail = stderr[-1200:] if stderr else stdout[-1200:]
        raise RuntimeError(f"Falha ao executar {cmd[0]} (code={result.returncode}): {log_tail}")
    return result


def _read_extent(path: Path, gdalinfo_bin: str) -> tuple[float, float, float, float]:
    result = _run([gdalinfo_bin, "-json", str(path)])
    info = json.loads(result.stdout)
    corners = info["cornerCoordinates"]
    xmin, ymax = corners["upperLeft"]
    xmax, ymin = corners["lowerRight"]
    return xmin, ymin, xmax, ymax


def _reproject_bbox(
    xmin: float, ymin: float, xmax: float, ymax: float, src_srs: str, dst_srs: str
) -> tuple[float, float, float, float]:
    if src_srs == dst_srs:
        return xmin, ymin, xmax, ymax
    transformer = Transformer.from_crs(src_srs, dst_srs, always_xy=True)
    corners = [(xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)]
    projected = [transformer.transform(x, y) for x, y in corners]
    xs = [p[0] for p in projected]
    ys = [p[1] for p in projected]
    return min(xs), min(ys), max(xs), max(ys)


def _write_cutline_geojson(tmp_dir: Path, index: int, xmin: float, ymin: float, xmax: float, ymax: float) -> Path:
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin],
                    ]],
                },
            }
        ],
    }
    path = tmp_dir / f"cutline_{index}.geojson"
    path.write_text(json.dumps(geojson), encoding="utf-8")
    return path


def _merge_fast(
    tile_paths: list[Path],
    output_path: Path,
    target_srs: str,
    resolution: float,
) -> None:
    gdalwarp_bin = _find_bin("gdalwarp")
    cmd = [
        gdalwarp_bin,
        "-t_srs", target_srs,
        "-tr", str(resolution), str(resolution),
        "-r", "bilinear",
        "-dstnodata", str(MERGE_NODATA),
        "-co", "COMPRESS=LZW",
        *[str(path) for path in tile_paths],
        str(output_path),
    ]
    _run(cmd)


def _merge_blend(
    tile_paths: list[Path],
    output_path: Path,
    target_srs: str,
    resolution: float,
    on_progress: Optional[ProgressCallback] = None,
) -> None:
    gdalwarp_bin = _find_bin("gdalwarp")
    gdalinfo_bin = _find_bin("gdalinfo")

    tiles_info = []
    for tile in tile_paths:
        native_srs = infer_srs_from_name(tile.stem) or target_srs
        extent = _read_extent(tile, gdalinfo_bin)
        tiles_info.append((tile, native_srs, extent))

    union_xmin = union_ymin = union_xmax = union_ymax = None
    for tile, native_srs, (xmin, ymin, xmax, ymax) in tiles_info:
        pxmin, pymin, pxmax, pymax = _reproject_bbox(xmin, ymin, xmax, ymax, native_srs, target_srs)
        union_xmin = pxmin if union_xmin is None else min(union_xmin, pxmin)
        union_ymin = pymin if union_ymin is None else min(union_ymin, pymin)
        union_xmax = pxmax if union_xmax is None else max(union_xmax, pxmax)
        union_ymax = pymax if union_ymax is None else max(union_ymax, pymax)

    total = len(tiles_info)
    with tempfile.TemporaryDirectory(prefix="mdt_bacia_cutlines_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for i, (tile, native_srs, (xmin, ymin, xmax, ymax)) in enumerate(tiles_info):
            cutline_path = _write_cutline_geojson(tmp_dir, i, xmin, ymin, xmax, ymax)
            cmd = [gdalwarp_bin]
            if i == 0:
                # Only the first call may pass dataset-creation options (gdalwarp
                # refuses them once the destination file already exists).
                cmd += [
                    "-t_srs", target_srs,
                    "-tr", str(resolution), str(resolution),
                    "-dstnodata", str(MERGE_NODATA),
                    "-co", "COMPRESS=LZW",
                    "-te", str(union_xmin), str(union_ymin), str(union_xmax), str(union_ymax),
                ]
            cmd += [
                "-r", "bilinear",
                "-cutline", str(cutline_path),
                "-cutline_srs", native_srs,
                "-cblend", str(CBLEND_PIXELS),
            ]
            cmd += [str(tile), str(output_path)]
            _run(cmd)
            if on_progress:
                on_progress(i + 1, total)


def merge_tiles(
    tile_paths: list[Path],
    output_path: Path,
    target_srs: str,
    resolution: float,
    mode: MergeMode = "fast",
    on_progress: Optional[ProgressCallback] = None,
) -> float:
    if not tile_paths:
        raise ValueError("Nenhum tile MDT informado para o merge.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    started_at = time.time()
    if mode == "blend":
        _merge_blend(tile_paths, output_path, target_srs, resolution, on_progress=on_progress)
    else:
        _merge_fast(tile_paths, output_path, target_srs, resolution)
    elapsed = time.time() - started_at

    if not output_path.exists():
        raise RuntimeError("Saida .tif nao foi criada apos a execucao do gdalwarp.")

    return elapsed
