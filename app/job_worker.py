from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from .laz2dem import process_laz_to_dem
from .job_store import JobStore


class JobWorker:
    def __init__(self, store: JobStore) -> None:
        self.store = store
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
                message=f"Gerando GeoTIFF no CRS inferido: {job.target_srs}",
            )
            process_laz_to_dem(
                input_path=input_path,
                output_path=output_path,
                source_srs=job.source_srs,
                target_srs=job.target_srs,
            )
            self.store.update_job(
                job_id,
                status="processing",
                progress=90,
                message="Finalizando geração do GeoTIFF",
            )

            self.store.update_job(
                job_id,
                status="completed",
                progress=100,
                message="Processamento concluído com sucesso.",
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=f"Falha no processamento: {exc}",
                error_log=traceback.format_exc(),
            )
