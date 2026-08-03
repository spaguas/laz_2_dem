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

Aba "MDT por bacia": o usuário seleciona uma bacia por coordenadas ou código UC; o app resolve os
tiles MDT (`.tif`) da bacia a partir das listas em `app/folhas_por_bacia/folhas_{uc}.csv` e agenda
um merge assíncrono com a resolução espacial escolhida, gerando `MDT_BACIA_{UC}_{resolução}m.tif`.
A bacia em si (geometria/área) vem de `app/gpkg/Subbacias_Q5_SP.gpkg`.

Modos de mesclagem (`gdalwarp`):
- **Rápido**: uma chamada só combinando todos os tiles. Pode deixar bordas visíveis entre tiles.
- **Suave**: um `gdalwarp` por tile, com `-cutline`/`-cblend`, pra suavizar a transição entre
  tiles vizinhos. Bem mais lento em bacias com muitos tiles.

Depois do merge, o app preenche por interpolação (`rasterio.fill.fillnodata`) os buracos de
NoData internos à bacia (falhas no meio do terreno, não a borda) e recorta o resultado final pelo
contorno exato da bacia — fora da bacia permanece NoData.

Variáveis de ambiente novas (mesmo padrão de `NUVENS_HOST_PATH`):
- `MDT_HOST_PATH`: pasta com os tiles MDT de origem (produção: `/home/sf-maps/MDT`).
- `MDT_BACIA_HOST_PATH`: pasta acessível fora do container para quem marcar "salvar cópia no
  servidor" (produção: `/home/sf-maps/mdt_bacia`).
