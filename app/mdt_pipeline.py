"""Pipeline padrão de geração de MDT (modelo TerraHidro).

Ordem validada no guia: mosaico -> recorte -> limpeza -> validação. As causas dos
problemas vistos nos lotes ("poços" de -9999 misturado com cota real, deslocamento
entre MDTs vizinhos, nodata vazando para dentro da bacia) são corrigidas já na
geração:

0. Saída padrão: GeoTIFF Float32, nodata -32768, CRS gravado no arquivo, DEFLATE +
   TILED. Nomes só ASCII, uma pasta por processamento, log por processamento.
1. Bacia validada antes de tocar no MDT: CRS precisa ser EPSG:31982 ou 31983.
2. Extensão de trabalho = extensão da bacia ajustada para múltiplos do pixel.
3. Mosaico folha a folha, com inventário de cada folha e normalização de TODOS os
   valores de nodata conhecidos antes da reamostragem (nearest quando a folha já
   está no CRS/grade certos; bilinear quando há reprojeção/reamostragem).
4. Recorte pela bacia (cópia CLIPPED_DEM_ORIGINAL.tif antes da limpeza).
5. Limpeza de valores espúrios (negativos, anel ao redor deles e pixels abaixo de
   um LIMIAR automático que não sejam vales reais) + preenchimento de buracos.
6. Gravação do arquivo final.
7. Validação automática — o processamento falha se não passar.

O mosaico é montado em memória (numpy) e não via gdalwarp: o `-srcnodata` do
gdalwarp aceita só um valor por banda, e as folhas fonte misturam 0 (borda),
-9999 e -32768. Abrir uma folha por vez também elimina de vez o problema de
"Too many open files" do merge antigo.
"""
from __future__ import annotations

import json
import math
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.fill import fillnodata
from rasterio.transform import Affine, from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from scipy import ndimage
from shapely.geometry.base import BaseGeometry

from .grid_srs import infer_srs_from_name

# --- 0. Convenções de saída -------------------------------------------------
NODATA = -32768.0
VALID_EPSGS = (31982, 31983)
GTIFF_PROFILE = {
    "driver": "GTiff",
    "dtype": "float32",
    "count": 1,
    "nodata": NODATA,
    "compress": "DEFLATE",
    "tiled": True,
    "blockxsize": 256,
    "blockysize": 256,
    "BIGTIFF": "IF_SAFER",
}

# Valores tratados como nodata nas folhas fonte, além do nodata declarado no
# próprio arquivo: 0 (borda das folhas), -9999 (nodata mal declarado, origem dos
# "poços") e -32768. Qualquer valor <= NODATA_FLOOR (ex.: -3.4e38) também.
KNOWN_NODATA_VALUES = (0.0, -9999.0, -32768.0)
NODATA_FLOOR = -9999.0

# --- 5. Limpeza ---------------------------------------------------------------
ANEL_ITERACOES = 2
LIMIAR_PERCENTIL = 0.01  # percentil 0,01%
LIMIAR_MARGEM_M = 20.0
VALE_JANELA = 7
VALE_DESVIO_MAX_M = 15.0
FILL_ESPURIOS_MAX_DIST = 200
FILL_BURACOS_MAX_DIST = 50
MEDIANA_CHUNK = 200_000

# --- 7. Validação -------------------------------------------------------------
ELEV_MIN_PLAUSIVEL = 0.0
ELEV_MAX_PLAUSIVEL = 3000.0  # ponto mais alto de SP ~2.800 m (Serra da Mantiqueira)
FRACAO_CORRIGIDOS_ALERTA = 0.01
PERCENTIS_LOG = (0.01, 0.1, 1.0, 5.0, 50.0)

# --- Mosaico ------------------------------------------------------------------
CBLEND_PIXELS = 20
# Teto de pixels do raster de trabalho: o mosaico e a limpeza rodam em memória
# (~18 bytes/pixel no pico). Acima disso o job falha antes de derrubar o
# container por falta de memória.
MAX_PIXELS = int(os.environ.get("MDT_MAX_PIXELS", "800000000"))

ProgressCallback = Callable[[int, str], None]


class MdtPipelineError(RuntimeError):
    """Erro de processamento com mensagem já pronta para o usuário."""


class MdtValidationError(MdtPipelineError):
    def __init__(self, falhas: list[str]) -> None:
        self.falhas = falhas
        super().__init__("Validação automática reprovada: " + "; ".join(falhas) + ".")


# --- Log por processamento ----------------------------------------------------
@dataclass
class MdtReport:
    titulo: str
    dados: dict = field(default_factory=dict)
    linhas: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    def log(self, mensagem: str) -> None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.linhas.append(f"[{ts} UTC] {mensagem}")

    def aviso(self, mensagem: str) -> None:
        self.avisos.append(mensagem)
        self.log(f"AVISO: {mensagem}")

    def set(self, chave: str, valor) -> None:
        self.dados[chave] = valor

    def as_text(self) -> str:
        partes = [f"# {self.titulo}", "", "## Eventos", *self.linhas]
        if self.avisos:
            partes += ["", "## Avisos", *(f"- {a}" for a in self.avisos)]
        partes += ["", "## Dados", json.dumps(self.dados, indent=2, ensure_ascii=False, default=_json_default)]
        return "\n".join(partes) + "\n"

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.as_text(), encoding="utf-8")


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def log_path_for(output_path: Path) -> Path:
    return output_path.parent / f"{output_path.stem}_LOG.txt"


# --- Utilitários de grade ----------------------------------------------------
def epsg_from_srs(srs: str | int | CRS | None) -> Optional[int]:
    if srs is None:
        return None
    if isinstance(srs, int):
        return srs
    crs = srs if isinstance(srs, CRS) else CRS.from_user_input(srs)
    return crs.to_epsg()


def validar_crs_bacia(epsg: Optional[int]) -> int:
    if epsg not in VALID_EPSGS:
        raise MdtPipelineError(
            f"CRS da bacia inválido (EPSG:{epsg}). Só são aceitos EPSG:31982 (SIRGAS 2000 / UTM 22S) "
            "e EPSG:31983 (SIRGAS 2000 / UTM 23S)."
        )
    return epsg  # type: ignore[return-value]


def _multiplo(valor: float, res: float, tol: float = 1e-6) -> bool:
    q = valor / res
    return abs(q - round(q)) < tol


def extensao_alinhada(bounds: Iterable[float], res: float) -> tuple[float, float, float, float]:
    """Extensão ajustada para múltiplos do pixel (floor no mínimo, ceil no máximo).
    É o que evita deslocamento entre MDTs vizinhos."""
    xmin, ymin, xmax, ymax = bounds
    return (
        round(math.floor(xmin / res) * res, 6),
        round(math.floor(ymin / res) * res, 6),
        round(math.ceil(xmax / res) * res, 6),
        round(math.ceil(ymax / res) * res, 6),
    )


def _normalizar_nodata(arr: np.ndarray, declarado: Optional[float]) -> dict:
    """Converte em NODATA todo valor de nodata conhecido (in place) e devolve o
    inventário de valores da folha."""
    contagem: dict[str, int] = {}
    invalido = ~np.isfinite(arr)
    if invalido.any():
        contagem["nao_finitos"] = int(invalido.sum())
    if declarado is not None and np.isfinite(declarado):
        m = arr == np.float32(declarado)
        if m.any():
            contagem[f"declarado({declarado:g})"] = int(m.sum())
        invalido |= m
    for valor in KNOWN_NODATA_VALUES:
        if declarado is not None and float(declarado) == valor:
            continue
        m = arr == np.float32(valor)
        if m.any():
            contagem[f"{valor:g}"] = int(m.sum())
        invalido |= m
    abaixo = (arr <= NODATA_FLOOR) & ~invalido
    if abaixo.any():
        contagem[f"<={NODATA_FLOOR:g}"] = int(abaixo.sum())
        invalido |= abaixo

    arr[invalido] = NODATA
    validos = ~invalido
    n_validos = int(validos.sum())
    inventario = {
        "pixels": int(arr.size),
        "validos": n_validos,
        "nodata_por_valor": contagem,
        "negativos": int(((arr < 0) & validos).sum()),
    }
    if n_validos:
        vals = arr[validos]
        inventario["min"] = float(vals.min())
        inventario["max"] = float(vals.max())
    return inventario


# --- 3. Mosaico ---------------------------------------------------------------
def montar_mosaico(
    tile_paths: list[Path],
    epsg: int,
    bounds: tuple[float, float, float, float],
    res: float,
    report: MdtReport,
    *,
    modo: str = "fast",
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[np.ndarray, Affine]:
    xmin, ymin, xmax, ymax = bounds
    width = int(round((xmax - xmin) / res))
    height = int(round((ymax - ymin) / res))
    if width <= 0 or height <= 0:
        raise MdtPipelineError("Extensão de trabalho vazia.")
    if width * height > MAX_PIXELS:
        raise MdtPipelineError(
            f"Raster de trabalho grande demais ({width} x {height} = {width * height:,} pixels, "
            f"limite {MAX_PIXELS:,}). Use uma resolução espacial maior (em metros)."
        )

    transform = from_origin(xmin, ymax, res, res)
    dst_crs = CRS.from_epsg(epsg)
    report.log(f"Grade de trabalho: {width} x {height} px, origem ({xmin}, {ymax}), pixel {res:g} m")

    blend = modo == "blend"
    if blend:
        soma = np.zeros((height, width), dtype=np.float32)
        peso = np.zeros((height, width), dtype=np.float32)
    else:
        mosaico = np.full((height, width), NODATA, dtype=np.float32)

    inventario: list[dict] = []
    total = len(tile_paths)
    for i, path in enumerate(tile_paths):
        item: dict = {"folha": path.name}
        inventario.append(item)
        with rasterio.open(path) as src:
            src_crs = src.crs
            if src_crs is None:
                inferido = infer_srs_from_name(path.stem)
                if inferido is None:
                    item["status"] = "ignorada (sem CRS)"
                    report.aviso(f"Folha {path.name} sem CRS no arquivo nem no nome; ignorada.")
                    if on_progress:
                        on_progress(i + 1, total)
                    continue
                src_crs = CRS.from_user_input(inferido)
                item["crs_inferido_pelo_nome"] = True
            src_epsg = src_crs.to_epsg()
            nome_epsg = epsg_from_srs(infer_srs_from_name(path.stem))
            if nome_epsg and src_epsg and nome_epsg != src_epsg:
                report.aviso(f"Folha {path.name}: CRS do arquivo (EPSG:{src_epsg}) diverge do nome (EPSG:{nome_epsg}).")

            st = src.transform
            item.update({
                "crs": f"EPSG:{src_epsg}" if src_epsg else src_crs.to_string(),
                "resolucao": [abs(st.a), abs(st.e)],
                "origem": [st.c, st.f],
                "nodata_declarado": src.nodata,
            })

            arr = src.read(1, out_dtype="float32")
            item.update(_normalizar_nodata(arr, src.nodata))
            if item["validos"] == 0:
                item["status"] = "ignorada (sem pixels válidos)"
                if on_progress:
                    on_progress(i + 1, total)
                continue

            if src_crs == dst_crs:
                bx0, by0, bx1, by1 = src.bounds
            else:
                bx0, by0, bx1, by1 = transform_bounds(src_crs, dst_crs, *src.bounds, densify_pts=21)

            c0 = max(0, math.floor((bx0 - xmin) / res) - 1)
            c1 = min(width, math.ceil((bx1 - xmin) / res) + 1)
            r0 = max(0, math.floor((ymax - by1) / res) - 1)
            r1 = min(height, math.ceil((ymax - by0) / res) + 1)
            if c1 <= c0 or r1 <= r0:
                item["status"] = "ignorada (fora da extensão)"
                if on_progress:
                    on_progress(i + 1, total)
                continue

            mesma_grade = (
                src_crs == dst_crs
                and st.b == 0 and st.d == 0
                and abs(abs(st.a) - res) < 1e-6 and abs(abs(st.e) - res) < 1e-6
                and _multiplo(st.c, res) and _multiplo(st.f, res)
            )
            resampling = Resampling.nearest if mesma_grade else Resampling.bilinear
            item["reamostragem"] = "nearest" if mesma_grade else "bilinear"

            buf = np.full((r1 - r0, c1 - c0), NODATA, dtype=np.float32)
            reproject(
                source=arr,
                destination=buf,
                src_transform=st,
                src_crs=src_crs,
                src_nodata=NODATA,
                dst_transform=from_origin(xmin + c0 * res, ymax - r0 * res, res, res),
                dst_crs=dst_crs,
                dst_nodata=NODATA,
                resampling=resampling,
            )
            del arr

        validos = buf != NODATA
        if blend:
            # Peso cresce de 0 (borda dos dados da folha) até 1 a CBLEND_PIXELS
            # pixels para dentro: nas sobreposições a transição fica suave, fora
            # delas o valor original é preservado (soma/peso = valor).
            dist = ndimage.distance_transform_edt(np.pad(validos, 1, constant_values=False))[1:-1, 1:-1]
            w = (np.minimum(dist, CBLEND_PIXELS) / CBLEND_PIXELS).astype(np.float32)
            w[~validos] = 0
            soma[r0:r1, c0:c1] += np.where(validos, buf, 0) * w
            peso[r0:r1, c0:c1] += w
        else:
            view = mosaico[r0:r1, c0:c1]
            novos = validos & (view == NODATA)
            view[novos] = buf[novos]
        item["status"] = "usada"
        if on_progress:
            on_progress(i + 1, total)

    if blend:
        mosaico = np.full((height, width), NODATA, dtype=np.float32)
        tem = peso > 0
        mosaico[tem] = soma[tem] / peso[tem]
        del soma, peso, tem

    usadas = [it for it in inventario if it.get("status") == "usada"]
    report.set("inventario_folhas", inventario)
    report.log(f"Mosaico: {len(usadas)}/{total} folha(s) usadas (modo {modo}).")
    mal_declarado = [
        it["folha"] for it in inventario
        if any(not k.startswith("declarado") for k in it.get("nodata_por_valor", {}))
    ]
    if mal_declarado:
        report.log(
            f"{len(mal_declarado)} folha(s) com valores de nodata não declarados (0/-9999/-32768) "
            "tratados como nodata antes da reamostragem."
        )
    if not usadas:
        raise MdtPipelineError("Nenhuma folha MDT com dados válidos dentro da extensão da bacia.")
    return mosaico, transform


# --- 5. Limpeza ---------------------------------------------------------------
def _mediana_vizinhos(A: np.ndarray, valido: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Mediana dos vizinhos válidos numa janela VALE_JANELA x VALE_JANELA (sem o
    pixel central), só para os pixels pedidos."""
    r = VALE_JANELA // 2
    offs = [(dr, dc) for dr in range(-r, r + 1) for dc in range(-r, r + 1) if (dr, dc) != (0, 0)]
    dr = np.array([o[0] for o in offs])
    dc = np.array([o[1] for o in offs])
    h, w = A.shape
    out = np.empty(rows.size, dtype=np.float32)
    for s in range(0, rows.size, MEDIANA_CHUNK):
        e = s + MEDIANA_CHUNK
        rr = rows[s:e, None] + dr[None, :]
        cc = cols[s:e, None] + dc[None, :]
        dentro_img = (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
        rr = np.clip(rr, 0, h - 1)
        cc = np.clip(cc, 0, w - 1)
        vals = A[rr, cc].astype(np.float32)
        vals[~(dentro_img & valido[rr, cc])] = np.nan
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            out[s:e] = np.nanmedian(vals, axis=1)
    return out


def limpar_valores_espurios(A: np.ndarray, report: MdtReport) -> tuple[np.ndarray, int]:
    """Passo 5.1–5.6. Tudo calculado só com os pixels com dado (fora = nodata).
    Devolve o raster limpo e a quantidade de pixels marcados como inválidos."""
    fora = A == NODATA
    neg = (A < 0) & ~fora
    anel = ndimage.binary_dilation(neg, iterations=ANEL_ITERACOES) & ~fora & ~neg
    base = ~fora & ~neg & ~anel
    n_base = int(base.sum())
    if n_base == 0:
        raise MdtPipelineError("Nenhum pixel válido (não negativo) dentro da área de trabalho.")

    p = float(np.percentile(A[base], LIMIAR_PERCENTIL))
    limiar = p - LIMIAR_MARGEM_M
    rows, cols = np.nonzero(base & (A < limiar))
    n_cand = int(rows.size)
    n_protegidos = 0
    abaixo = np.zeros_like(base)
    if n_cand:
        med = _mediana_vizinhos(A, base, rows, cols)
        desvio = np.abs(A[rows, cols] - med)
        protegido = np.isfinite(med) & (desvio < VALE_DESVIO_MAX_M)
        n_protegidos = int(protegido.sum())
        abaixo[rows[~protegido], cols[~protegido]] = True

    invalidos = neg | anel | abaixo
    n_invalidos = int(invalidos.sum())
    stats = {
        "percentil_0_01_validos": p,
        "limiar": limiar,
        "negativos": int(neg.sum()),
        "anel": int(anel.sum()),
        "abaixo_limiar": n_cand,
        "abaixo_limiar_protegidos_vale_real": n_protegidos,
        "abaixo_limiar_marcados": n_cand - n_protegidos,
        "marcados_invalidos": n_invalidos,
    }

    if n_invalidos:
        work = A.copy()
        work[invalidos] = NODATA
        mask = (~fora & ~invalidos).astype(np.uint8)
        A = fillnodata(work, mask=mask, max_search_distance=FILL_ESPURIOS_MAX_DIST, smoothing_iterations=0)
        del work, mask
        A = A.astype(np.float32, copy=False)
        # restaura "fora" -- nunca substituir por 0
        A[fora] = NODATA
        stats["nao_alcancados_pelo_fill"] = int((invalidos & (A == NODATA)).sum())

    report.set("limpeza", stats)
    report.log(
        f"Limpeza: LIMIAR = {limiar:.2f} m (p0,01% = {p:.2f} m − {LIMIAR_MARGEM_M:g} m); "
        f"negativos {stats['negativos']}, anel {stats['anel']}, abaixo do LIMIAR {n_cand} "
        f"({n_protegidos} protegidos como vale real); {n_invalidos} pixel(s) preenchidos "
        f"(FillNodata maxSearchDist={FILL_ESPURIOS_MAX_DIST})."
    )
    return A, n_invalidos


def preencher_buracos(A: np.ndarray, dentro: np.ndarray, report: MdtReport) -> tuple[np.ndarray, int]:
    """Passo 5.7: preenche só os pixels sem dado dentro da área (maxSearchDist=50)."""
    validos = A != NODATA
    buracos = dentro & ~validos
    n_buracos = int(buracos.sum())
    if not n_buracos:
        report.set("buracos_internos", {"pixels": 0, "preenchidos": 0})
        return A, 0
    filled = fillnodata(A.copy(), mask=validos.astype(np.uint8),
                        max_search_distance=FILL_BURACOS_MAX_DIST, smoothing_iterations=0)
    out = np.where(buracos, filled, A).astype(np.float32, copy=False)
    out[~dentro] = NODATA
    n_preenchidos = int((buracos & (out != NODATA)).sum())
    report.set("buracos_internos", {"pixels": n_buracos, "preenchidos": n_preenchidos})
    report.log(
        f"Buracos internos de nodata: {n_buracos} px, {n_preenchidos} preenchidos "
        f"(maxSearchDist={FILL_BURACOS_MAX_DIST})."
    )
    return out, n_preenchidos


# --- 6. Gravação ----------------------------------------------------------------
def gravar_geotiff(path: Path, A: np.ndarray, transform: Affine, epsg: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.tmp{path.suffix}")
    profile = dict(GTIFF_PROFILE, width=A.shape[1], height=A.shape[0],
                   crs=CRS.from_epsg(epsg), transform=transform)
    try:
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(A.astype(np.float32, copy=False), 1)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- 7. Validação -----------------------------------------------------------------
def validar_mdt(
    path: Path,
    dentro: np.ndarray,
    epsg: int,
    res: float,
    report: MdtReport,
    *,
    barragem_xy: Optional[tuple[float, float]] = None,
    pixels_corrigidos: int = 0,
    nodata_interno_e_erro: bool = True,
) -> None:
    falhas: list[str] = []
    checagens: dict[str, dict] = {}

    def check(nome: str, ok: bool, detalhe: str, erro: bool = True) -> None:
        checagens[nome] = {"ok": bool(ok), "detalhe": detalhe}
        report.log(f"Checagem '{nome}': {'OK' if ok else ('FALHOU' if erro else 'ALERTA')} — {detalhe}")
        if not ok:
            (falhas.append(f"{nome}: {detalhe}") if erro else report.aviso(f"{nome}: {detalhe}"))

    with rasterio.open(path) as src:
        A = src.read(1)
        t = src.transform
        epsg_arq = src.crs.to_epsg() if src.crs else None
        dtype = src.dtypes[0]
        nodata = src.nodata

    if A.shape != dentro.shape:
        raise MdtPipelineError("Máscara da área não confere com a grade do arquivo final.")

    check("CRS", epsg_arq in VALID_EPSGS and epsg_arq == epsg,
          f"arquivo EPSG:{epsg_arq}, esperado EPSG:{epsg}")
    check("Formato", dtype == "float32" and nodata == NODATA, f"tipo {dtype}, nodata {nodata}")
    alinhada = (
        abs(t.a - res) < 1e-6 and abs(t.e + res) < 1e-6 and t.b == 0 and t.d == 0
        and _multiplo(t.c, res) and _multiplo(t.f, res)
    )
    check("Grade alinhada", alinhada, f"origem ({t.c}, {t.f}), pixel ({t.a}, {-t.e}), múltiplo de {res:g} m")

    validos = dentro & (A != NODATA)
    n_dentro = int(dentro.sum())
    n_nodata = int((dentro & (A == NODATA)).sum())
    n_neg = int((validos & (A < 0)).sum())
    check("Sem negativos", n_neg == 0, f"{n_neg} pixel(s) negativos dentro da área")
    check("Sem nodata", n_nodata == 0, f"{n_nodata} pixel(s) sem dado dentro da área",
          erro=nodata_interno_e_erro)

    vals = A[validos]
    if vals.size:
        pcts = {f"p{p:g}": float(v) for p, v in zip(PERCENTIS_LOG, np.percentile(vals, PERCENTIS_LOG))}
        vmin, vmax = float(vals.min()), float(vals.max())
        report.set("estatisticas_finais", {"min": vmin, "max": vmax, "percentis": pcts, "pixels_area": n_dentro})
        report.log("Percentis finais: " + ", ".join(f"{k} = {v:.2f} m" for k, v in pcts.items()))
        check("Faixa plausível", ELEV_MIN_PLAUSIVEL <= vmin and vmax <= ELEV_MAX_PLAUSIVEL,
              f"min {vmin:.2f} m, max {vmax:.2f} m (aceito {ELEV_MIN_PLAUSIVEL:g}–{ELEV_MAX_PLAUSIVEL:g} m)")
    else:
        check("Faixa plausível", False, "nenhum pixel válido dentro da área")

    if barragem_xy is not None:
        col, row = ~t * barragem_xy
        row, col = int(math.floor(row)), int(math.floor(col))
        dentro_img = 0 <= row < A.shape[0] and 0 <= col < A.shape[1]
        ok = dentro_img and A[row, col] != NODATA
        valor = f"{float(A[row, col]):.2f} m" if dentro_img and A[row, col] != NODATA else "nodata/fora do raster"
        check("Ponto da barragem", ok,
              f"({barragem_xy[0]:.2f}, {barragem_xy[1]:.2f}) -> pixel ({row}, {col}) = {valor}")

    fracao = pixels_corrigidos / n_dentro if n_dentro else 0.0
    report.set("pixels_corrigidos", {"total": pixels_corrigidos, "fracao": fracao})
    check("Fração corrigida", fracao <= FRACAO_CORRIGIDOS_ALERTA,
          f"{pixels_corrigidos} px ({fracao:.3%}) corrigidos; acima de {FRACAO_CORRIGIDOS_ALERTA:.0%} "
          "pede revisão manual" if fracao > FRACAO_CORRIGIDOS_ALERTA
          else f"{pixels_corrigidos} px ({fracao:.3%}) corrigidos",
          erro=False)

    report.set("checagens", checagens)
    if falhas:
        raise MdtValidationError(falhas)


# --- Orquestração: MDT por bacia ------------------------------------------------
def gerar_mdt_bacia(
    *,
    bacia_geom: BaseGeometry,
    epsg: int,
    resolution: float,
    tile_paths: list[Path],
    output_path: Path,
    report: MdtReport,
    modo: str = "fast",
    barragem_xy: Optional[tuple[float, float]] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> None:
    """Gera o MDT da bacia (geometria já no CRS `epsg`). Grava, na pasta de
    `output_path`, a cópia CLIPPED_DEM_ORIGINAL.tif e o MDT final. Levanta
    MdtValidationError se a validação automática não passar."""

    def progresso(pct: int, msg: str) -> None:
        report.log(msg)
        if on_progress:
            on_progress(pct, msg)

    # 1. Validar bacia
    validar_crs_bacia(epsg)
    report.set("crs", f"EPSG:{epsg}")
    report.set("resolucao_m", resolution)

    # 2. Extensão alinhada
    bounds = extensao_alinhada(bacia_geom.bounds, resolution)
    report.set("extensao_bacia", list(bacia_geom.bounds))
    report.set("extensao_alinhada", list(bounds))

    # 3. Mosaico
    report.set("folhas_usadas", [p.name for p in tile_paths])
    progresso(15, f"Montando mosaico de {len(tile_paths)} folha(s) (EPSG:{epsg}, {resolution:g} m)")

    def on_tile(done: int, total: int) -> None:
        if on_progress:
            on_progress(15 + int(done / total * 50), f"Montando mosaico: folha {done}/{total}")

    A, transform = montar_mosaico(tile_paths, epsg, bounds, resolution, report, modo=modo, on_progress=on_tile)

    # 4. Recorte pela bacia -- a extensão já é a da bacia alinhada à grade, então
    # o "crop" é a própria grade; aqui só zera (nodata) o que está fora do polígono.
    progresso(68, "Recortando pela bacia")
    dentro = geometry_mask([bacia_geom], out_shape=A.shape, transform=transform, invert=True)
    if not dentro.any():
        raise MdtPipelineError("A bacia não cobre nenhum pixel na resolução escolhida.")
    A[~dentro] = NODATA
    gravar_geotiff(output_path.parent / "CLIPPED_DEM_ORIGINAL.tif", A, transform, epsg)

    # 5. Limpeza
    progresso(75, "Limpando valores espúrios (negativos, anel e abaixo do LIMIAR)")
    A, n_espurios = limpar_valores_espurios(A, report)
    progresso(85, "Preenchendo buracos internos de nodata")
    A, n_buracos = preencher_buracos(A, dentro, report)

    # 6. Arquivo final
    progresso(90, "Gravando GeoTIFF final (Float32, nodata -32768, DEFLATE, TILED)")
    gravar_geotiff(output_path, A, transform, epsg)
    del A

    # 7. Validação
    progresso(95, "Validando MDT final")
    validar_mdt(output_path, dentro, epsg, resolution, report,
                barragem_xy=barragem_xy, pixels_corrigidos=n_espurios + n_buracos)
    report.log("MDT validado com sucesso.")


# --- Padronização de uma folha gerada pelo PDAL (laz2dem) -----------------------
def padronizar_mdt_folha(
    raw_path: Path,
    output_path: Path,
    epsg: int,
    res: float,
    report: MdtReport,
) -> None:
    """Aplica os passos 0, 5, 6 e 7 a um MDT de folha única (sem bacia). A "área"
    é a região com dado do raster bruto, com os buracos internos incluídos."""
    with rasterio.open(raw_path) as src:
        A = src.read(1, out_dtype="float32")
        t = src.transform
        inventario = _normalizar_nodata(A, src.nodata)
    report.set("inventario_raster_bruto", inventario)

    if not (_multiplo(t.c, res) and _multiplo(t.f, res) and abs(t.a - res) < 1e-6 and abs(t.e + res) < 1e-6):
        # Fallback: grade do PDAL não alinhada -- reamostra (nearest) para a grade
        # alinhada que contém o raster bruto.
        h, w = A.shape
        bounds = extensao_alinhada((t.c, t.f + h * t.e, t.c + w * t.a, t.f), res)
        nt = from_origin(bounds[0], bounds[3], res, res)
        nw = int(round((bounds[2] - bounds[0]) / res))
        nh = int(round((bounds[3] - bounds[1]) / res))
        alinhado = np.full((nh, nw), NODATA, dtype=np.float32)
        crs = CRS.from_epsg(epsg)
        reproject(source=A, destination=alinhado, src_transform=t, src_crs=crs, src_nodata=NODATA,
                  dst_transform=nt, dst_crs=crs, dst_nodata=NODATA, resampling=Resampling.nearest)
        report.aviso("Grade do PDAL não estava alinhada a múltiplos do pixel; reamostrada (nearest).")
        A, t = alinhado, nt

    dentro = ndimage.binary_fill_holes(A != NODATA)
    if not dentro.any():
        raise MdtPipelineError("O raster gerado pelo PDAL não tem nenhum pixel com dado.")

    A, n_espurios = limpar_valores_espurios(A, report)
    A, n_buracos = preencher_buracos(A, dentro, report)
    gravar_geotiff(output_path, A, t, epsg)
    del A
    # Folha isolada: lagos/áreas sem retorno de solo podem deixar buracos maiores
    # que o alcance do preenchimento -- isso vira alerta, não falha.
    validar_mdt(output_path, dentro, epsg, res, report,
                pixels_corrigidos=n_espurios + n_buracos, nodata_interno_e_erro=False)
