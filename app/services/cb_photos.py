"""Фото пацієнтів з CleverBox (файли картки клієнта) → файли пацієнтів CRM.
Список файлів збирається у веб-кабінеті CleverBox (лише читання): [cb_client_id, cb_file_id, url_зображення, назва, дата, тип, етап(before|after)?].
Фото до/після (файл-менеджер CleverBox, папки before/after) приходять з етапом — у CRM стають фото «До» / «Після».
Якщо на диску лишається менше MIN_FREE — перенос зупиняється, щоб не заповнити сервер.
Сервер CRM сам завантажує зображення за прямими посиланнями сховища CleverBox. Повторний запуск не дублює (cb_file_id)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from sqlalchemy import select

from ..models import Patient, PatientFile, SessionLocal

ALLOWED_HOST = "upcloudobjects.com"          # сховище файлів CleverBox
MIN_FREE = 3 * 1024 ** 3                      # 3 ГБ вільного місця — нижче не завантажуємо


def _taken(s: str):
    try:
        return datetime.strptime(str(s)[:16], "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def _by_record(db) -> dict[int, int]:
    """№ запису (візиту) CleverBox → пацієнт CRM: з візитів CleverBox і перенесених у CRM записів."""
    from ..models import Appointment, Visit
    out = {}
    for ext, pid in db.execute(select(Visit.external_id, Visit.patient_id).where(Visit.source == "cb_visit")):
        try:
            out.setdefault(int(str(ext).split("-")[0]), pid)
        except ValueError:
            pass
    for rid, pid in db.execute(select(Appointment.cb_record_id, Appointment.patient_id).where(Appointment.cb_record_id.is_not(None))):
        try:
            out.setdefault(int(rid), pid)
        except (TypeError, ValueError):
            pass
    return out


def run(job, files: list, by: str = "client") -> dict:
    """by="client": перше поле — id клієнта CleverBox; by="record": № запису (візиту) CleverBox (фото до/після)."""
    from . import ingest
    rep = {"files": len(files), "saved": 0, "exists": 0, "no_patient": 0, "not_image": 0, "failed": 0, "bytes": 0}   # not_image — без посилання
    with SessionLocal() as db:
        by_cb = _by_record(db) if by == "record" else \
            {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
        have = {x: pid for x, pid in db.execute(select(PatientFile.cb_file_id, PatientFile.patient_id).where(PatientFile.cb_file_id.is_not(None)))}
    relink: dict[int, int] = {}
    todo = []
    for f in files:
        try:
            cid, fid, url, name, dt, typ = int(f[0]), int(f[1]), str(f[2] or ""), str(f[3] or ""), str(f[4] or ""), str(f[5] or "")
            stage = str(f[6]) if len(f) > 6 and str(f[6]) in ("before", "after") else None
        except (TypeError, ValueError, IndexError):
            rep["failed"] += 1
            continue
        if fid in have:
            rep["exists"] += 1
            if cid in by_cb and have[fid] != by_cb[cid]:
                relink[fid] = by_cb[cid]              # файл уже є, але в іншого пацієнта — переносимо до правильного
            continue
        if cid not in by_cb:
            rep["no_patient"] += 1
            continue
        if not url.startswith("https://") or ALLOWED_HOST not in url.split("/")[2]:
            rep["not_image"] += 1                        # немає прямого посилання на сховище CleverBox
            continue
        todo.append((by_cb[cid], fid, url, name or url.rsplit("/", 1)[-1], dt, "photo" if typ == "img" else "doc", stage))
    if relink:
        with SessionLocal() as db:
            for f in db.scalars(select(PatientFile).where(PatientFile.cb_file_id.in_(list(relink)))):
                f.patient_id = relink[f.cb_file_id]
            db.commit()
        rep["relinked"] = len(relink)
        job.log(f"Перенесено до правильних пацієнтів: {len(relink)}")
    job.log(f"Файлів: {len(files)}, до завантаження: {len(todo)}, уже є: {rep['exists']}, без пацієнта: {rep['no_patient']}, не фото: {rep['not_image']}")
    http = httpx.Client(timeout=40, follow_redirects=True)

    def fetch(t):
        try:
            r = http.get(t[2])
            if r.status_code == 200 and r.content and len(r.content) < 25 * 1024 * 1024:
                return t, r.content
        except httpx.HTTPError:
            pass
        return t, None
    done = 0
    from pathlib import Path
    import shutil
    from ..config import settings
    root = Path(settings.files_dir)
    root.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i in range(0, len(todo), 40):
            if shutil.disk_usage(root).free < MIN_FREE:
                rep["stopped_low_disk"] = True
                job.log(f"Зупинено: на сервері менше {MIN_FREE // 1024 ** 3} ГБ вільного місця")
                break
            batch = list(ex.map(fetch, todo[i:i + 40]))
            with SessionLocal() as db:
                for (pid, fid, url, name, dt, kind, stage), data in batch:
                    if data is None:
                        rep["failed"] += 1
                        continue
                    path = ingest.store(data, f"cb_{fid}_{name}")
                    tk = _taken(dt)
                    db.add(PatientFile(patient_id=pid, kind=kind, path=path, caption=(f"З CleverBox ({'до' if stage == 'before' else 'після'}): {name}" if stage else f"З CleverBox: {name}")[:500],
                                       taken_at=tk.date() if tk else None, cb_file_id=fid, stage=stage,
                                       created_at=tk or datetime.utcnow()))
                    rep["saved"] += 1
                    rep["bytes"] += len(data)
                db.commit()
            done += len(batch)
            job.log(f"Завантажено {rep['saved']} з {len(todo)} ({rep['bytes'] // (1024 * 1024)} МБ), помилок {rep['failed']}")
    http.close()
    return rep


def audit(files: list, by: str = "client") -> dict:
    """Звірка без змін: у кого в CRM файл CleverBox лежить не в того пацієнта.
    files — свіжий список з карток CleverBox [cb_client_id|record_id, cb_file_id, ...]. Повертає пацієнтів із чужими файлами,
    файли CleverBox, яких у CRM немає, і файли CRM з cb_file_id, яких у списку немає (невідомо чиї)."""
    with SessionLocal() as db:
        by_cb = _by_record(db) if by == "record" else \
            {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
        names = {p.id: (p.full_name or f"#{p.id}") for p in db.scalars(select(Patient))}
        cbid = {p.id: p.cleverbox_id for p in db.scalars(select(Patient))}
        have = {x: pid for x, pid in db.execute(select(PatientFile.cb_file_id, PatientFile.patient_id).where(PatientFile.cb_file_id.is_not(None)))}
    wrong: dict[int, list] = {}          # пацієнт CRM, у якого лежить чужий файл → [(fid, правильний пацієнт)]
    missing, no_patient, seen = 0, 0, set()
    for f in files:
        try:
            cid, fid = int(f[0]), int(f[1])
        except (TypeError, ValueError, IndexError):
            continue
        seen.add(fid)
        owner = by_cb.get(cid)
        if owner is None:
            no_patient += 1
            continue
        cur = have.get(fid)
        if cur is None:
            missing += 1
        elif cur != owner:
            wrong.setdefault(cur, []).append((fid, owner))
    unknown = [fid for fid in have if fid not in seen]
    rows = sorted(({"patient_id": pid, "name": names.get(pid), "cb_id": cbid.get(pid), "files": len(v),
                    "owners": sorted({names.get(o) or f"#{o}" for _, o in v})} for pid, v in wrong.items()),
                  key=lambda r: -r["files"])
    return {"files": len(files), "wrong_patients": len(rows), "wrong_files": sum(r["files"] for r in rows), "rows": rows,
            "missing_in_crm": missing, "no_patient": no_patient, "unknown_in_crm": len(unknown)}

