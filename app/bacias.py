from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import geopandas as gpd
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

GPKG_DIR = Path(__file__).resolve().parent / "data" / "gpkg"
BACIAS_PATH = GPKG_DIR / "Subbacias_Q5_SP.gpkg"
TILES_PATH = GPKG_DIR / "articulacao_lidar.gpkg"

WGS84 = "EPSG:4326"


@dataclass
class BaciaInfo:
    uc: int
    upst_area: Optional[float]
    geometry: BaseGeometry


def _resolve_tile_name(row) -> Optional[str]:
    nomenc_2k = row.get("Nomenc_2k")
    if isinstance(nomenc_2k, str) and nomenc_2k.strip():
        return nomenc_2k.strip()
    nomenc_5k = row.get("Nomenc_5k")
    if isinstance(nomenc_5k, str) and nomenc_5k.strip():
        return nomenc_5k.strip()
    return None


def _ensure_wgs84(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None or gdf.crs.to_string() != WGS84:
        gdf = gdf.to_crs(WGS84)
    return gdf


@lru_cache(maxsize=1)
def load_bacias() -> gpd.GeoDataFrame:
    return _ensure_wgs84(gpd.read_file(BACIAS_PATH))


@lru_cache(maxsize=1)
def load_tiles() -> gpd.GeoDataFrame:
    gdf = _ensure_wgs84(gpd.read_file(TILES_PATH))
    gdf["tile_name"] = gdf.apply(_resolve_tile_name, axis=1)
    return gdf


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


@lru_cache(maxsize=1)
def get_bacias_geojson() -> str:
    gdf = load_bacias()[["UC", "geometry"]].rename(columns={"UC": "uc"})
    return gdf.to_json()


def resolve_tile_names(geometry: BaseGeometry) -> list[str]:
    gdf = load_tiles()
    candidate_idx = list(gdf.sindex.query(geometry, predicate="intersects"))
    if not candidate_idx:
        return []
    candidates = gdf.iloc[candidate_idx]
    matches = candidates[candidates.geometry.intersects(geometry)]
    names = [name for name in matches["tile_name"].tolist() if name]
    return sorted(set(names))
