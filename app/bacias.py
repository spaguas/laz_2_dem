from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
from typing import Optional

import geopandas as gpd
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

GPKG_DIR = Path(__file__).resolve().parent / "gpkg"
BACIAS_PATH = GPKG_DIR / "Subbacias_Q5_SP.gpkg"
FOLHAS_DIR = Path(__file__).resolve().parent / "folhas_por_bacia"

WGS84 = "EPSG:4326"


@dataclass
class BaciaInfo:
    uc: int
    upst_area: Optional[float]
    geometry: BaseGeometry


def _ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None or gdf.crs.to_string() != WGS84:
        gdf = gdf.to_crs(WGS84)
    return gdf


@lru_cache(maxsize=1)
def load_bacias() -> gpd.GeoDataFrame:
    return _ensure_wgs84(gpd.read_file(BACIAS_PATH))


def _row_to_bacia_info(row) -> BaciaInfo:
    upst_area = row.get("Upst_Area_")
    return BaciaInfo(
        uc=int(row["UC"]),
        upst_area=float(upst_area) if upst_area is not None else None,
        geometry=row.geometry,
    )


def get_bacia_by_uc(uc: int) -> Optional[BaciaInfo]:
    gdf = load_bacias()
    matches = gdf[gdf["UC"] == uc]
    if matches.empty:
        return None
    return _row_to_bacia_info(matches.iloc[0])


def get_bacia_by_point(lat: float, lon: float) -> Optional[BaciaInfo]:
    gdf = load_bacias()
    point = Point(lon, lat)
    candidate_idx = list(gdf.sindex.query(point, predicate="intersects"))
    if not candidate_idx:
        return None
    candidates = gdf.iloc[candidate_idx]
    matches = candidates[candidates.contains(point)]
    if matches.empty:
        matches = candidates[candidates.intersects(point)]
    if matches.empty:
        return None
    return _row_to_bacia_info(matches.iloc[0])


def resolve_tile_names_for_uc(uc: int) -> list[str]:
    csv_path = FOLHAS_DIR / f"folhas_{uc}.csv"
    if not csv_path.is_file():
        return []

    names: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if not row:
                continue
            value = row[0].strip()
            if not value or value.lower() == "folha":
                continue
            names.append(value)
    return sorted(set(names))
