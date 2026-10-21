"""Фонові задачі з прогресом (перенос даних, великі імпорти) — без зовнішніх черг."""
from __future__ import annotations

import logging
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("jobs")


@dataclass
class Job:
    id: str
    title: str
    status: str = "running"            # running | done | error
    progress: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    started: datetime = field(default_factory=datetime.now)
    finished: datetime | None = None

    def log(self, msg: str):
        self.progress.append(f"{datetime.now():%H:%M:%S} {msg}")
        log.info("[%s] %s", self.title, msg)


JOBS: dict[str, Job] = {}


def start(title: str, fn, *args, **kwargs) -> Job:
    """fn(job, *args, **kwargs) -> dict. Не дозволяє дві однакові задачі паралельно."""
    for j in JOBS.values():
        if j.title == title and j.status == "running":
            return j
    job = Job(id=uuid.uuid4().hex[:10], title=title)
    JOBS[job.id] = job

    def run():
        try:
            job.result = fn(job, *args, **kwargs)
            job.status = "done"
            job.log("Готово")
        except Exception as e:      # noqa: BLE001
            job.status, job.error = "error", str(e)
            job.log(f"Помилка: {e}")
            log.error(traceback.format_exc())
            try:
                from . import alerts
                alerts.send(f"🔴 <b>{job.title}: помилка</b>\n{str(e)[:500]}")
            except Exception:   # noqa: BLE001
                pass
        finally:
            job.finished = datetime.now()

    threading.Thread(target=run, daemon=True).start()
    return job


def recent(n: int = 10) -> list[Job]:
    return sorted(JOBS.values(), key=lambda j: j.started, reverse=True)[:n]
