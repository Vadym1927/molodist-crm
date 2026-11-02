"""Фото й документи товарів. Перенос з файл-менеджера CleverBox (папка «Товари»): рядки
[cb_product_id, cb_file_id, url, назва, дата "дд.мм.рррр гг:хх", тип img|file]. Сервер сам завантажує файли зі сховища CleverBox.
Файл прив'язується до всіх товарів CRM з цим cb_product_id (той самий товар на різних складах). Повторний запуск не дублює.
Перше фото стає головним фото товару, якщо в товару його ще немає."""
from __future__ import annotations

import mimetypes
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from ..config import settings
from ..models import Product, ProductFile, SessionLocal
from .cb_photos import ALLOWED_HOST, MIN_FREE, _taken

IMG = re.compile(r"\.(jpe?g|png|webp|gif|bmp)$", re.I)


def photo_link(f: ProductFile) -> str:
    return f"/products/files/{f.id}"


def run(job, rows: list) -> dict:
    from . import ingest
    rep = {"files": len(rows), "saved": 0, "exists": 0, "no_product": 0, "failed": 0, "bytes": 0, "main_photo": 0}
    with SessionLocal() as db:
        by_cb: dict[int, list[int]] = {}
        for pid, cid in db.execute(select(Product.id, Product.cb_product_id).where(Product.cb_product_id.is_not(None))):
            by_cb.setdefault(int(cid), []).append(pid)
        have = {x for (x,) in db.execute(select(ProductFile.cb_file_id).where(ProductFile.cb_file_id.is_not(None)))}
    todo = []
    for r in rows:
        try:
            cid, fid, url, name, dt, typ = int(r[0]), int(r[1]), str(r[2] or ""), str(r[3] or ""), str(r[4] or ""), str(r[5] or "")
        except (TypeError, ValueError, IndexError):
            rep["failed"] += 1
            continue
        if fid in have:
            rep["exists"] += 1
            continue
        if cid not in by_cb:
            rep["no_product"] += 1
            continue
        if not url.startswith("https://") or ALLOWED_HOST not in url.split("/")[2]:
            rep["failed"] += 1
            continue
        todo.append((by_cb[cid], fid, url, name or url.rsplit("/", 1)[-1], dt, "photo" if typ == "img" or IMG.search(name) else "doc"))
    job.log(f"Файлів: {len(rows)}, до завантаження: {len(todo)}, уже є: {rep['exists']}, без товару в CRM: {rep['no_product']}")
    http = httpx.Client(timeout=60, follow_redirects=True)

    def fetch(t):
        try:
            r = http.get(t[2])
            if r.status_code == 200 and r.content and len(r.content) < 40 * 1024 * 1024:
                return t, r.content
        except httpx.HTTPError:
            pass
        return t, None
    root = Path(settings.files_dir)
    root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i in range(0, len(todo), 20):
            if shutil.disk_usage(root).free < MIN_FREE:
                rep["stopped_low_disk"] = True
                job.log("Зупинено: на сервері мало вільного місця")
                break
            batch = list(ex.map(fetch, todo[i:i + 20]))
            with SessionLocal() as db:
                for (pids, fid, url, name, dt, kind), data in batch:
                    if data is None:
                        rep["failed"] += 1
                        continue
                    path = ingest.store(data, f"product_{fid}_{name}")
                    tk = _taken(dt) or datetime.utcnow()
                    for pid in pids:
                        pf = ProductFile(product_id=pid, path=path, name=name[:200], kind=kind, cb_file_id=fid, uploaded_at=tk,
                                         mime=(mimetypes.guess_type(name)[0] or "")[:100] or None)
                        db.add(pf)
                        db.flush()
                        p = db.get(Product, pid)
                        if kind == "photo" and p and not p.photo_url:
                            p.photo_url = photo_link(pf)
                            rep["main_photo"] += 1
                    rep["saved"] += 1
                    rep["bytes"] += len(data)
                db.commit()
            job.log(f"Завантажено {rep['saved']} з {len(todo)} ({rep['bytes'] // (1024 * 1024)} МБ), помилок {rep['failed']}")
    http.close()
    return rep
