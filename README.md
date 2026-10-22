# LAZ -> GeoTIFF (SIRGAS 2000)

Aplicação web em Python para:
- Upload de arquivos `.laz`
- Seleção de projeção de origem e destino (EPSG) na interface
- Agendamento/execução de processamento em background
- Acompanhamento de status com barra de progresso
- Geração de `.tif` com reprojeção via PDAL para o SRS de destino selecionado

## Executar com Docker

```bash
docker compose up --build
```

Acesse: `http://localhost:9090`

Nota para Portainer: não use bind mount `.:/app` em produção, pois isso pode sobrescrever o código da imagem e gerar `ModuleNotFoundError: No module named 'app'`.

## Executar localmente

Pré-requisitos:
- Python 3.11+
- Bibliotecas de sistema: PDAL e GDAL instaladas

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 9090
```

## Fluxo da aplicação

1. Usuário envia `.laz` pela interface.
2. API cria um job com status `queued`.
3. Worker em thread de background executa pipeline PDAL.
4. Frontend consulta `/api/jobs/{job_id}` a cada 2 segundos.
5. Ao concluir, o usuário baixa o `.tif` gerado.

## MDT por bacia hidrográfica

Aba "MDT por bacia". A bacia pode ser informada de três formas:

- **Coordenadas**: a bacia de `app/gpkg/Subbacias_Q5_SP.gpkg` que contém o ponto. O ponto também é
  usado como coordenada da barragem na validação.
- **Código UC**: a bacia com esse código no mesmo GeoPackage.
- **Arquivo vetorial**: shapefile (`.shp` + `.shx` + `.dbf` + `.prj`, soltos ou num `.zip`),
  GeoPackage (`.gpkg`) ou KML/KMZ enviado pelo usuário. Todas as feições poligonais viram uma única
  bacia. Se o CRS não for EPSG:31982 (SIRGAS 2000 / UTM 22S) ou EPSG:31983 (UTM 23S), a bacia é
  reprojetada para a zona correta: 22S a oeste de -48°, 23S a leste; bacia entre as duas zonas vai
  para a zona que contém a maior parte da área (área geodésica). As folhas são selecionadas pelo índice
  de articulação `app/gpkg/articulacao_lidar_correcao.gpkg`. Só o usuário escolhe a resolução (padrão
  5 m) e, opcionalmente, a coordenada da barragem; o resultado fica só para download (nunca é
  copiado para a pasta do servidor).

Nos fluxos por coordenadas/UC as folhas vêm de `app/folhas_por_bacia/folhas_{uc}.csv` e o CRS do MDT
segue a mesma regra da zona com maior área.

### Pipeline padrão (`app/mdt_pipeline.py`)

Ordem validada no guia do modelo TerraHidro: mosaico → recorte → limpeza → validação.

1. **Validar bacia**: CRS precisa ser EPSG:31982 ou 31983.
2. **Extensão de trabalho**: extensão da bacia ajustada para múltiplos do pixel (evita deslocamento
   entre MDTs vizinhos).
3. **Mosaico**: uma folha por vez, com inventário (CRS, resolução, origem, nodata declarado, valores
   de nodata encontrados, negativos). Todos os nodata conhecidos (declarado, `0` da borda, `-9999`,
   `-32768`, valores ≤ -9999) viram nodata antes da reamostragem — o `-9999` mal declarado era a
   origem dos "poços". Reamostragem `nearest` quando a folha já está no CRS/grade certos e `bilinear`
   quando há reprojeção (ex.: folha 22S numa bacia 23S). Modo **Suave** (blend) pondera as
   sobreposições pela distância à borda de cada folha.
4. **Recorte** pela bacia, com cópia `CLIPPED_DEM_ORIGINAL.tif` antes da limpeza.
5. **Limpeza** (só pixels com dado): negativos + anel de 2 pixels ao redor deles + pixels abaixo do
   LIMIAR (`percentil 0,01% dos válidos − 20 m`) que não sejam vale real (desvio < 15 m da mediana 7×7
   dos vizinhos válidos) são preenchidos com FillNodata (`maxSearchDist=200`); depois o que está fora
   volta a nodata. Buracos de nodata dentro da bacia são preenchidos com `maxSearchDist=50`.
6. **Arquivo final**: GeoTIFF Float32, nodata -32768, DEFLATE, TILED, CRS gravado no arquivo.
7. **Validação automática** (o job falha se não passar): CRS 31982/31983 igual ao da bacia, grade
   alinhada, 0 negativos e 0 nodata dentro da bacia, min/max entre 0 e 3000 m, ponto da barragem
   sobre pixel válido (quando informado). Mais de 1% de pixels corrigidos gera aviso para revisão
   manual. Folhas ausentes no servidor deixam buracos grandes demais para o preenchimento, então o job
   reprova na checagem de nodata.

Cada processamento grava numa pasta própria em `app/outputs/mdt_bacia/<bacia>_<data>_<job>/`
(nomes só ASCII, nunca sobrescreve saídas antigas): `bacia.gpkg`, `CLIPPED_DEM_ORIGINAL.tif`, o MDT
final `MDT_BACIA_<bacia>_<resolução>m.tif` e o log `MDT_BACIA_..._LOG.txt` (folhas usadas, inventário,
CRS, LIMIAR, percentis, pixels corrigidos e resultado de cada checagem). O log pode ser baixado pela
interface, inclusive quando a validação reprova. O mosaico e a limpeza rodam em memória; o teto de
pixels do raster de trabalho é configurável por `MDT_MAX_PIXELS` (padrão 800 milhões).

O fluxo `.laz` → GeoTIFF também usa o padrão: o PDAL grava Float32/nodata -32768 numa grade com origem
múltipla do pixel e o raster passa pelos passos 5–7 (buraco interno que sobra vira aviso, não falha,
porque folhas isoladas podem ter lagos sem retorno de solo). O log fica ao lado do `.tiff`.

Variáveis de ambiente (mesmo padrão de `NUVENS_HOST_PATH`):
- `MDT_HOST_PATH`: pasta com os tiles MDT de origem (produção: `/home/sf-maps/MDT`).
- `MDT_BACIA_HOST_PATH`: pasta acessível fora do container para quem marcar "salvar cópia no
  servidor" (produção: `/home/sf-maps/mdt_bacia`).
- `MDT_MAX_PIXELS`: teto de pixels do raster de trabalho do MDT por bacia.
