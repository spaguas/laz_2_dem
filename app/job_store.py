from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class JobRecord:
    id: str
    original_filename: str
    input_path: str
    output_path: str
    status: str
    progress: int
    message: str
    created_at: str
    updated_at: str
    source_srs: str = "EPSG:4674"
    target_srs: str = "EPSG:4674"
    batch_id: Optional[str] = None
    error_log: Optional[str] = None


class JobStore:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobRecord] = {}
        self._load()

    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _load(self) -> None:
        if not self.storage_path.exists():
            return

        with self.storage_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        for item in raw:
            normalized = dict(item)
            normalized.setdefault("source_srs", "EPSG:4674")
            normalized.setdefault("target_srs", "EPSG:4674")
            normalized.setdefault("batch_id", None)
            normalized.setdefault("error_log", None)
            job = JobRecord(**normalized)
            self._jobs[job.id] = job

    def _save(self) -> None:
        with self.storage_path.open("w", encoding="utf-8") as f:
            json.dump([asdict(j) for j in self._jobs.values()], f, indent=2)

    def create_job(self, job: JobRecord) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._save()

    def update_job(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        error_log: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = max(0, min(100, progress))
            if message is not None:
                job.message = message
            if error_log is not None:
                job.error_log = error_log
            job.updated_at = self._now()
            self._save()

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return JobRecord(**asdict(job))

    def list_jobs(self) -> List[JobRecord]:
        with self._lock:
            jobs = [JobRecord(**asdict(j)) for j in self._jobs.values()]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def get_batch_summary(self, batch_id: str) -> dict:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.batch_id == batch_id]
        counts: dict = {"total": len(jobs), "queued": 0, "processing": 0, "completed": 0, "failed": 0}
        for job in jobs:
            if job.status in counts:
                counts[job.status] += 1
        counts["remaining"] = counts["queued"] + counts["processing"]
        return counts

    def get_overall_summary(self) -> dict:
        with self._lock:
            jobs = list(self._jobs.values())
        counts: dict = {"total": len(jobs), "queued": 0, "processing": 0, "completed": 0, "failed": 0}
        for job in jobs:
            if job.status in counts:
                counts[job.status] += 1
        counts["remaining"] = counts["queued"] + counts["processing"]
        return counts
