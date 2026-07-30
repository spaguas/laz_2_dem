from __future__ import annotations

import queue
import shutil
import threading
import traceback
from pathlib import Path
from typing import Optional

from . import mdt_bacia
from .laz2dem import process_laz_to_dem
from .job_store import JobRecord, JobStore


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

    def _process_mdt_bacia(self, job: JobRecord) -> None:
        job_id = job.id
        output_path = Path(job.output_path)
        extra = job.extra or {}
        tile_files = [Path(p) for p in extra.get("tile_files", [])]
        missing_tiles = extra.get("missing_tiles", [])
        target_srs = extra.get("target_srs") or job.target_srs
        resolution = extra.get("resolution", 1)
        save_to_server = extra.get("save_to_server", False)
        merge_mode = extra.get("merge_mode", "fast")

        self.store.update_job(
            job_id,
            status="processing",
            progress=10,
            message=(
                f"Resolvendo tiles da bacia ({len(tile_files)} encontrado(s))."
                + (f" Ausentes no servidor: {', '.join(missing_tiles)}." if missing_tiles else "")
            ),
        )

        def on_blend_progress(done: int, total: int) -> None:
            pct = 15 + int((done / total) * 70) if total else 15
            self.store.update_job(
                job_id,
                status="processing",
                progress=pct,
                message=f"Mesclando tiles com transição suave ({done}/{total})",
            )

        try:
            self.store.update_job(
                job_id,
                status="processing",
                progress=40,
                message=f"Mesclando {len(tile_files)} tile(s) com gdalwarp (CRS destino: {target_srs})",
            )
            mdt_bacia.merge_tiles(
                tile_paths=tile_files,
                output_path=output_path,
                target_srs=target_srs,
                resolution=resolution,
                mode=merge_mode,
                on_progress=on_blend_progress if merge_mode == "blend" else None,
            )

            if save_to_server and self.mdt_bacia_dir is not None:
                self.store.update_job(
                    job_id,
                    status="processing",
                    progress=90,
                    message="Copiando resultado para a pasta compartilhada do servidor",
                )
                self.mdt_bacia_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output_path, self.mdt_bacia_dir / output_path.name)

            message = "Merge concluído com sucesso."
            if missing_tiles:
                message += f" Tiles ausentes no servidor (ignorados): {', '.join(missing_tiles)}."

            self.store.update_job(
                job_id,
                status="completed",
                progress=100,
                message=message,
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message=f"Falha no merge: {exc}",
                error_log=traceback.format_exc(),
            )
