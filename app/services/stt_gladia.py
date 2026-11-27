"""Розпізнавання записів дзвінків через Gladia (Франція, api.gladia.io v2) для режиму AI-оцінки «CRM».

Конвеєр: запис із телефонії (Ringostat/Binotel) → POST /v2/upload → POST /v2/pre-recorded (solaria-1, uk+ru з
перемиканням мов, діаризація на 2 спікери, словник MOLODIST) → опитування до status=done → репліки з ролями
«Адміністратор: / Пацієнт:» → Call.transcript → оцінка Claude (telephony.evaluate).

Ключ Gladia — у налаштуваннях телефонії (шифрується, як секрети провайдерів). Планувальник: job_stt кожні 10 хв,
до 6 дзвінків за раз; дзвінок пробуємо не більше 3 разів (ai_meta.stt_tries). Коротші за 20 с — без розпізнавання."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Call

API = "https://api.gladia.io/v2"
MIN_SEC = 20
MAX_TRIES = 3
VOCAB = ["MOLODIST", "медичний простір", "бюті-план", "консультація", "ботулінотерапія", "біоревіталізація", "Rejuran",
         "Juvederm", "Restylane", "Belotero", "Radiesse", "Plenhyage", "Jalupro", "мезотерапія", "IPL", "лазерна епіляція",
         "STRATOSPHERE", "Endymed", "OBSERV", "Medik8", "AG SKIN", "Image Skincare", "Genosys", "передоплата",
         "Лівобережна", "Дарниця", "Колган", "Сирова", "Чугунова"]
ADMIN_HINTS = ("molodist", "молодіст", "молодист", "медичний простір", "адміністратор", "слухаю", "клініка")


def key(db: Session) -> str:
    from . import telephony as tel
    enc = (tel._raw(db).get("stt") or {}).get("gladia_key")
    if not enc:
        return ""
    try:
        return tel._cipher().decrypt(enc.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def set_key(db: Session, value: str | None) -> None:
    from . import telephony as tel
    d = tel._raw(db)
    stt = d.get("stt") or {}
    if value:
        stt["gladia_key"] = tel._cipher().encrypt(value.strip().encode()).decode()
    else:
        stt.pop("gladia_key", None)
    d["stt"] = stt
    tel._save(db, d)


def _roles(utts: list[dict], direction: str) -> str:
    """Репліки Gladia → «Адміністратор: … / Пацієнт: …». Адміністратор — хто представився клінікою;
    інакше на вхідному — хто заговорив першим, на вихідному — другий спікер (першим зазвичай відповідає пацієнт)."""
    if not utts:
        return ""
    key_ = "speaker" if any(u.get("speaker") is not None for u in utts) else "channel"
    spk = [u.get(key_) for u in utts]
    order = list(dict.fromkeys(spk))
    adm = next((u.get(key_) for u in utts if any(h in (u.get("text") or "").lower() for h in ADMIN_HINTS)), None)
    if adm is None:
        adm = order[0] if direction != "out" or len(order) < 2 else order[1]
    lines: list[list] = []
    for u in sorted(utts, key=lambda x: x.get("start") or 0):
        who = "Адміністратор" if u.get(key_) == adm else "Пацієнт"
        t = (u.get("text") or "").strip()
        if not t:
            continue
        if lines and lines[-1][0] == who:
            lines[-1][1] += " " + t
        else:
            lines.append([who, t])
    return "\n".join(f"{w}: {t}" for w, t in lines)


def _audio(c: Call, http: httpx.Client) -> bytes:
    from ..api import REC_HOSTS
    from urllib.parse import urlparse
    u = urlparse(c.record_url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in REC_HOSTS):
        raise ValueError("запис недоступний")
    r = http.get(c.record_url)
    if r.status_code >= 400 or not r.content:
        raise RuntimeError(f"телефонія відповіла HTTP {r.status_code}")
    return r.content


def transcribe(db: Session, c: Call, http: httpx.Client | None = None, wait_s: int = 240) -> str:
    k = key(db)
    if not k:
        raise ValueError("Не задано ключ Gladia (Налаштування → IP-телефонія → AI-оцінка)")
    own = http is None
    http = http or httpx.Client(timeout=90, follow_redirects=True)
    try:
        audio = _audio(c, http)
        h = {"x-gladia-key": k}
        up = http.post(f"{API}/upload", headers=h, files={"audio": (f"call-{c.id}.mp3", audio, "audio/mpeg")})
        if up.status_code >= 400:
            raise RuntimeError(f"Gladia upload {up.status_code}: {up.text[:200]}")
        job = http.post(f"{API}/pre-recorded", headers={**h, "Content-Type": "application/json"}, json={
            "audio_url": up.json()["audio_url"], "model": "solaria-1",
            "language_config": {"languages": ["uk", "ru"], "code_switching": True},
            "diarization": True, "diarization_config": {"number_of_speakers": 2},
            "custom_vocabulary": True, "custom_vocabulary_config": {"vocabulary": VOCAB, "default_intensity": 0.5},
        })
        if job.status_code >= 400:
            raise RuntimeError(f"Gladia {job.status_code}: {job.text[:200]}")
        url = job.json().get("result_url") or f"{API}/pre-recorded/{job.json()['id']}"
        t0 = time.time()
        while True:
            r = http.get(url, headers=h).json()
            st = r.get("status")
            if st == "done":
                tr = ((r.get("result") or {}).get("transcription") or {})
                return _roles(tr.get("utterances") or [], c.direction or "in") or (tr.get("full_transcript") or "")
            if st == "error":
                raise RuntimeError(f"Gladia: помилка {r.get('error_code')}")
            if time.time() - t0 > wait_s:
                raise TimeoutError("Gladia ще не завершила розпізнавання")
            time.sleep(3)
    finally:
        if own:
            http.close()


def pending(db: Session, limit: int = 6, days: int = 62) -> list[Call]:
    since = datetime.now() - timedelta(days=days)
    rows = db.scalars(select(Call).where(Call.transcript.is_(None), Call.record_url.is_not(None), Call.status == "answered",
                                         Call.at >= since).order_by(Call.at.desc()).limit(200)).all()
    out = []
    for c in rows:
        if (c.duration or 0) < MIN_SEC or int((c.ai_meta or {}).get("stt_tries") or 0) >= MAX_TRIES:
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def stats(db: Session, days: int = 62) -> dict:
    """Стан черги для сторінки налаштувань: у черзі / розпізнано / помилки (остання помилка)."""
    since = datetime.now() - timedelta(days=days)
    q = n = err = 0
    last = ""
    for c in db.scalars(select(Call).where(Call.status == "answered", Call.record_url.is_not(None), Call.at >= since)):
        m = c.ai_meta or {}
        if m.get("stt") == "gladia":
            n += 1
        elif c.transcript is None and (c.duration or 0) >= MIN_SEC:
            if m.get("stt_error"):
                err += 1
                last = last or m["stt_error"]
            if int(m.get("stt_tries") or 0) < MAX_TRIES:
                q += 1
    return {"queue": q, "done": n, "errors": err, "last_error": last}


def run(db: Session, limit: int = 6) -> int:
    """Розпізнати й оцінити нові дзвінки (режим «CRM» + ключ Gladia). Повертає кількість розпізнаних."""
    from . import telephony as tel
    d = tel._raw(db)
    if d.get("ai_engine", "crm") != "crm" or not key(db):
        return 0
    n = 0
    for c in pending(db, limit):
        meta = dict(c.ai_meta or {})
        meta["stt_tries"] = int(meta.get("stt_tries") or 0) + 1
        try:
            txt = transcribe(db, c)
        except Exception as e:  # noqa: BLE001
            meta["stt_error"] = str(e)[:300]
            c.ai_meta = meta
            db.commit()
            continue
        meta.pop("stt_error", None)
        meta["stt"] = "gladia"
        c.ai_meta, c.transcript = meta, (txt or "")[:60000] or None
        db.commit()
        n += 1
        if d.get("ai_on") and c.transcript and c.ai_score is None:
            try:
                tel.evaluate(db, c)
            except Exception:  # noqa: BLE001
                db.rollback()
    return n
