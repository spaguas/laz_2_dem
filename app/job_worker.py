from __future__ import annotations

import queue
import shutil
import threading
import traceback
from pathlib import Path
from typing import Optional

import geopandas as gpd
from pyproj import Transformer

from . import bacia_vetor, bacias, mdt_pipeline
from .laz2dem import process_laz_to_dem
from .job_store import JobRecord, JobStore
from .mdt_pipeline import MdtReport, MdtValidationError, log_path_for


class JobWorker:
    def __init__(self, store: JobStore, mdt_bacia_dir: Optional[Path] = None) -> None:
        self.store = store
        self.mdt_bacia_dir = mdt_bacia_dir
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread.start()

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return

        if job.kind == "mdt_bacia":
            self._process_mdt_bacia(job)
        else:
            self._process_laz2dem(job)

    def _process_laz2dem(self, job: JobRecord) -> None:
        job_id = job.id
        input_path = Path(job.input_path)
        output_path = Path(job.output_path)

        self.store.update_job(job_id, status="processing", progress=10, message="Lendo arquivo .laz")
        if not input_path.exists():
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message="Arquivo de entrada não encontrado.",
            )
            return

        report = MdtReport(titulo=f"MDT da folha {input_path.name}")
        try:
            self.store.update_job(
                job_id,
                status="processing",
                progress=40,
                message="Executando pipeline PDAL do laz2dem",
            )
            self.store.update_job(
                job_id,
                status="processing",
                progress=45,
                message="Filtrando pontos e classificando terreno",
            )
            self.store.update_job(
                job_id,
                status="processing",
                progress=60,
                message=f"Gerando GeoTIFF no CRS inferido: {job.target_srs} (com limpeza e validação padrão)",
            )
            process_laz_to_dem(
                input_path=input_path,
                output_path=output_path,
                source_srs=job.source_srs,
                target_srs=job.target_srs,
                report=report,
            )

            message = "Processamento concluído e validado com sucesso."
            if report.avisos:
                message += " ATENÇÃO: " + " | ".join(report.avisos)
            self.store.update_job(
                job_id,
                status="completed",
                progress=100,
                message=message,
            )
        except MdtValidationError as exc:
            # MDT reprovado não fica disponível para download; o log explica o motivo.
            output_path.unlink(missing_ok=True)
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=str(exc),
                error_log=report.as_text(),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=f"Falha no processamento: {exc}",
                error_log=traceback.format_exc() + "\n" + report.as_text(),
            )
        finally:
            report.write(log_path_for(output_path))

    def _load_bacia_geometry(self, extra: dict, epsg: int):
        bacia_path = extra.get("bacia_path")
        if bacia_path:
            return bacia_vetor.carregar_geometria(Path(bacia_path), epsg)
        info = bacias.get_bacia_by_uc(extra.get("uc"))
        if info is None:
            raise RuntimeError("Geometria da bacia não encontrada.")
        return gpd.GeoSeries([info.geometry], crs="EPSG:4326").to_crs(epsg).iloc[0]

    def _process_mdt_bacia(self, job: JobRecord) -> None:
        job_id = job.id
        output_path = Path(job.output_path)
        extra = job.extra or {}
        tile_files = [Path(p) for p in extra.get("tile_files", [])]
        missing_tiles = extra.get("missing_tiles", [])
        resolution = float(extra.get("resolution", 1))
        save_to_server = extra.get("save_to_server", False)
        merge_mode = extra.get("merge_mode", "fast")
        epsg = extra.get("epsg") or mdt_pipeline.epsg_from_srs(extra.get("target_srs") or job.target_srs)

        report = MdtReport(titulo=f"MDT por bacia - {job.original_filename}")
        report.set("job_id", job_id)
        report.set("origem_bacia", extra.get("origem", "uc"))
        for chave in ("uc", "nome_bacia", "arquivo_enviado", "crs_original", "reprojetada", "area_zona_km2"):
            if chave in extra:
                report.set(chave, extra[chave])
        report.set("folhas_ausentes_no_servidor", missing_tiles)
        report.set("modo_mosaico", merge_mode)

        def on_progress(pct: int, msg: str) -> None:
            self.store.update_job(job_id, status="processing", progress=pct, message=msg)

        self.store.update_job(
            job_id,
            status="processing",
            progress=5,
            message=(
                f"Preparando bacia ({len(tile_files)} folha(s) encontrada(s))."
                + (f" Ausentes no servidor: {', '.join(missing_tiles)}." if missing_tiles else "")
            ),
        )

        try:
            bacia_geom = self._load_bacia_geometry(extra, epsg)

            barragem_xy = None
            barragem = extra.get("barragem")  # [lon, lat] em WGS84
            if barragem:
                transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
                barragem_xy = transformer.transform(barragem[0], barragem[1])
                report.set("barragem_wgs84_lon_lat", barragem)
            else:
                report.log("Coordenada da barragem não informada; checagem do ponto não será feita.")

            mdt_pipeline.gerar_mdt_bacia(
                bacia_geom=bacia_geom,
                epsg=epsg,
                resolution=resolution,
                tile_paths=tile_files,
                output_path=output_path,
                report=report,
                modo=merge_mode,
                barragem_xy=barragem_xy,
                on_progress=on_progress,
            )

            if save_to_server and self.mdt_bacia_dir is not None:
                self.store.update_job(
                    job_id,
                    status="processing",
                    progress=98,
                    message="Copiando resultado para a pasta compartilhada do servidor",
                )
                self.mdt_bacia_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output_path, self.mdt_bacia_dir / output_path.name)
                report.log(f"Cópia gravada em {self.mdt_bacia_dir / output_path.name}.")
                report.write(log_path_for(self.mdt_bacia_dir / output_path.name))

            message = "MDT gerado e validado com sucesso."
            if missing_tiles:
                message += f" Tiles ausentes no servidor (ignorados): {', '.join(missing_tiles)}."
            if report.avisos:
                message += " ATENÇÃO: " + " | ".join(report.avisos)

            self.store.update_job(
                job_id,
                status="completed",
                progress=100,
                message=message,
            )
        except MdtValidationError as exc:
            # MDT reprovado não fica disponível para download; a cópia
            # CLIPPED_DEM_ORIGINAL.tif e o log ficam na pasta do processamento.
            output_path.unlink(missing_ok=True)
            message = str(exc)
            if missing_tiles:
                message += f" Tiles ausentes no servidor: {', '.join(missing_tiles)}."
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=message,
                error_log=report.as_text(),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=f"Falha na geração do MDT: {exc}",
                error_log=traceback.format_exc() + "\n" + report.as_text(),
            )
        finally:
            report.write(log_path_for(output_path))
