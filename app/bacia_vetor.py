"""Bacia fornecida pelo usuário como arquivo vetorial (shapefile, GeoPackage, KML/KMZ).

- Lê o arquivo, junta todas as feições poligonais numa única geometria.
- Se o CRS não for EPSG:31982/31983 (SIRGAS 2000 / UTM 22S/23S), escolhe a zona
  pela longitude (22S a oeste de -48°, 23S a leste) e reprojeta. Bacia entre as
  duas zonas vai para a zona que contém a maior parte da área (área geodésica).
- Seleciona as folhas de MDT que intersectam a bacia pelo índice de articulação.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pyogrio
import shapely
from pyproj import Geod
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.geometry.polygon import orient
from shapely.geometry.base import BaseGeometry

from .mdt_pipeline import VALID_EPSGS

GPKG_DIR = Path(__file__).resolve().parent / "gpkg"
ARTICULACAO_PATH = GPKG_DIR / "articulacao_lidar_correcao.gpkg"

WGS84 = "EPSG:4326"
LON_LIMITE_ZONAS = -48.0
EPSG_22S = 31982
EPSG_23S = 31983
GEOD = Geod(ellps="GRS80")

SHAPEFILE_PARTES = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx"}
EXTENSOES_ACEITAS = SHAPEFILE_PARTES | {".zip", ".gpkg", ".kml", ".kmz"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_ZIP_DESCOMPACTADO = 500 * 1024 * 1024


class BaciaVetorError(ValueError):
    """Erro de entrada do usuário (mensagem pronta para exibir)."""


@dataclass
class BaciaVetor:
    nome: str
    geometry_utm: BaseGeometry
    geometry_wgs84: BaseGeometry
    epsg: int
    crs_original: str
    reprojetada: bool
    n_feicoes: int
    area_km2: float
    area_zona_km2: dict


def nome_ascii(texto: str, max_len: int = 60) -> str:
    """Só ASCII, sem acentos e sem espaços."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    limpo = re.sub(r"[^A-Za-z0-9._-]+", "_", sem_acento).strip("._-")
    return limpo[:max_len] or "bacia"


# --- Upload --------------------------------------------------------------------
def _extrair_zip(zip_path: Path, destino: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            membros = [m for m in zf.infolist() if not m.is_dir()]
            if sum(m.file_size for m in membros) > MAX_ZIP_DESCOMPACTADO:
                raise BaciaVetorError("Arquivo .zip grande demais depois de descompactado.")
            for m in membros:
                nome = nome_ascii(Path(m.filename).name, max_len=120)
                if Path(nome).suffix.lower() not in EXTENSOES_ACEITAS - {".zip"}:
                    continue
                with zf.open(m) as origem, (destino / nome).open("wb") as alvo:
                    shutil.copyfileobj(origem, alvo)
    except zipfile.BadZipFile as exc:
        raise BaciaVetorError(f"Arquivo compactado inválido: {zip_path.name}.") from exc


def salvar_upload(arquivos: list[tuple[str, bytes]], destino: Path) -> Path:
    """Grava os arquivos enviados em `destino` e devolve o caminho do vetor
    principal (.shp, .gpkg ou .kml)."""
    if not arquivos:
        raise BaciaVetorError("Nenhum arquivo enviado.")
    if sum(len(c) for _, c in arquivos) > MAX_UPLOAD_BYTES:
        raise BaciaVetorError("Arquivos grandes demais (limite de 100 MB).")

    destino.mkdir(parents=True, exist_ok=True)
    for nome_original, conteudo in arquivos:
        nome = nome_ascii(Path(nome_original or "").name, max_len=120)
        ext = Path(nome).suffix.lower()
        if ext not in EXTENSOES_ACEITAS:
            raise BaciaVetorError(
                f"Formato não suportado: {nome_original}. Envie shapefile (.shp + .shx + .dbf + .prj "
                "ou .zip), GeoPackage (.gpkg) ou KML/KMZ."
            )
        caminho = destino / nome
        caminho.write_bytes(conteudo)
        if ext in (".zip", ".kmz"):
            _extrair_zip(caminho, destino)
            caminho.unlink()

    def achar(ext: str) -> list[Path]:
        return sorted(p for p in destino.iterdir() if p.is_file() and p.suffix.lower() == ext)

    for ext in (".shp", ".gpkg", ".kml"):
        encontrados = achar(ext)
        if len(encontrados) > 1:
            raise BaciaVetorError(
                f"Mais de um arquivo {ext} enviado ({', '.join(p.name for p in encontrados)}). "
                "Envie uma única bacia."
            )
        if encontrados:
            principal = encontrados[0]
            if ext == ".shp":
                faltando = [
                    e for e in (".shx", ".dbf", ".prj")
                    if not (principal.with_suffix(e).is_file() or principal.with_suffix(e.upper()).is_file())
                ]
                if faltando:
                    raise BaciaVetorError(
                        f"Shapefile incompleto: faltam {', '.join(faltando)} com o mesmo nome de "
                        f"{principal.name}. O .prj é obrigatório para identificar o CRS."
                    )
            return principal
    raise BaciaVetorError("Nenhum .shp, .gpkg ou .kml encontrado nos arquivos enviados.")


# --- Leitura e geometria --------------------------------------------------------
def _somente_poligonos(geom: BaseGeometry) -> BaseGeometry:
    if geom is None or geom.is_empty:
        return Polygon()
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    partes = [g for g in getattr(geom, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
    return shapely.union_all(partes) if partes else Polygon()


def _ler_camadas_poligonais(path: Path) -> list[tuple[str, gpd.GeoDataFrame]]:
    try:
        camadas = pyogrio.list_layers(path)
    except Exception as exc:  # noqa: BLE001
        raise BaciaVetorError(f"Não foi possível abrir o arquivo vetorial {path.name}: {exc}") from exc

    resultado = []
    for nome, _tipo in camadas:
        try:
            gdf = gpd.read_file(path, layer=nome)
        except Exception as exc:  # noqa: BLE001
            raise BaciaVetorError(f"Falha ao ler a camada '{nome}' de {path.name}: {exc}") from exc
        if gdf.empty:
            continue
        poligonal = gdf.geometry.notna() & gdf.geometry.geom_type.isin(
            ["Polygon", "MultiPolygon", "GeometryCollection"]
        )
        gdf = gdf[poligonal]
        if not gdf.empty:
            resultado.append((nome, gdf))
    return resultado


def ler_bacia(path: Path) -> tuple[BaseGeometry, BaseGeometry, gpd.GeoDataFrame]:
    """Lê o vetor e devolve (geometria única no CRS original, a mesma em WGS84,
    GeoDataFrame com as feições poligonais)."""
    camadas = _ler_camadas_poligonais(path)
    if not camadas:
        raise BaciaVetorError("O arquivo não contém nenhum polígono.")
    if len(camadas) > 1 and path.suffix.lower() != ".kml":
        nomes = ", ".join(n for n, _ in camadas)
        raise BaciaVetorError(f"O arquivo tem mais de uma camada de polígonos ({nomes}). Envie só a da bacia.")

    crs = camadas[0][1].crs
    if crs is None:
        raise BaciaVetorError("O arquivo não tem sistema de coordenadas (CRS) definido.")
    gdf = gpd.GeoDataFrame(
        geometry=[g for _, c in camadas for g in c.to_crs(crs).geometry], crs=crs
    )

    geoms = shapely.make_valid(shapely.force_2d(gdf.geometry.values))
    geom = _somente_poligonos(shapely.union_all(geoms))
    if geom.is_empty or geom.area <= 0:
        raise BaciaVetorError("A geometria da bacia está vazia ou não tem área.")

    geom_wgs84 = gpd.GeoSeries([geom], crs=crs).to_crs(WGS84).iloc[0]
    minx, miny, maxx, maxy = geom_wgs84.bounds
    if not all(map(lambda v: abs(v) < 1e9, (minx, miny, maxx, maxy))) or not (
        -180 <= minx <= 180 and -180 <= maxx <= 180 and -90 <= miny <= 90 and -90 <= maxy <= 90
    ):
        raise BaciaVetorError("Coordenadas da bacia inválidas para o CRS declarado no arquivo.")
    return geom, geom_wgs84, gdf


def _area_geodesica_km2(geom: BaseGeometry) -> float:
    geom = _somente_poligonos(geom)
    if geom.is_empty:
        return 0.0
    partes = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    # orient(): anel externo anti-horário e furos horários -> área positiva e
    # furos descontados corretamente pelo pyproj.
    total = sum(GEOD.geometry_area_perimeter(orient(p, 1.0))[0] for p in partes)
    return abs(total) / 1e6


def escolher_zona_utm(geom_wgs84: BaseGeometry) -> tuple[int, dict]:
    """22S para longitude < -48°, 23S para longitude > -48°; bacia entre as duas
    zonas vai para a que contém a maior parte da área."""
    oeste = geom_wgs84.intersection(box(-180, -90, LON_LIMITE_ZONAS, 90))
    leste = geom_wgs84.intersection(box(LON_LIMITE_ZONAS, -90, 180, 90))
    areas = {"22S": _area_geodesica_km2(oeste), "23S": _area_geodesica_km2(leste)}
    epsg = EPSG_22S if areas["22S"] >= areas["23S"] else EPSG_23S
    return epsg, areas


def preparar_bacia(path: Path, nome: str) -> BaciaVetor:
    geom_original, geom_wgs84, gdf = ler_bacia(path)
    epsg_original = gdf.crs.to_epsg()
    crs_original = f"EPSG:{epsg_original}" if epsg_original else gdf.crs.to_string()

    epsg_zona, areas = escolher_zona_utm(geom_wgs84)
    if epsg_original in VALID_EPSGS:
        # Já está em SIRGAS 2000 / UTM 22S ou 23S: mantém o CRS do arquivo.
        epsg = epsg_original
        reprojetada = False
        geom_utm = geom_original
    else:
        epsg = epsg_zona
        reprojetada = True
        geom_utm = _somente_poligonos(
            shapely.make_valid(gpd.GeoSeries([geom_original], crs=gdf.crs).to_crs(epsg).iloc[0])
        )

    return BaciaVetor(
        nome=nome,
        geometry_utm=geom_utm,
        geometry_wgs84=geom_wgs84,
        epsg=epsg,
        crs_original=crs_original,
        reprojetada=reprojetada,
        n_feicoes=len(gdf),
        area_km2=areas["22S"] + areas["23S"],
        area_zona_km2=areas,
    )


# --- Folhas pelo índice de articulação --------------------------------------------
@lru_cache(maxsize=1)
def load_articulacao() -> gpd.GeoDataFrame:
    gdf = gpd.read_file(ARTICULACAO_PATH)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(WGS84)
    return gdf


def folhas_por_geometria(geom_wgs84: BaseGeometry) -> list[str]:
    gdf = load_articulacao()
    idx = gdf.sindex.query(geom_wgs84, predicate="intersects")
    return sorted({str(n).strip() for n in gdf.iloc[idx]["folha"] if str(n).strip()})


# --- Persistência da bacia preparada -----------------------------------------------
def salvar_geometria(geom: BaseGeometry, epsg: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame(geometry=[geom], crs=f"EPSG:{epsg}").to_file(path, driver="GPKG")


def carregar_geometria(path: Path, epsg: Optional[int] = None) -> BaseGeometry:
    gdf = gpd.read_file(path)
    if epsg is not None and gdf.crs is not None and gdf.crs.to_epsg() != epsg:
        gdf = gdf.to_crs(epsg)
    return shapely.union_all(gdf.geometry.values)
