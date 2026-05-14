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
