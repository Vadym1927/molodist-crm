"""Робочі Telegram-чати, з яких бот CRM забирає файли в картки пацієнтів (бюті-плани, аналізи OBSERV,
консультативні висновки, інші документи й фото).

Чати з .env (TELEGRAM_ALLOWED_CHAT_IDS) працюють як і раніше; нові чати власник вмикає в
«Налаштування → Telegram: файли з чатів» (SyncState tg_ingest). Бот запам'ятовує кожен груповий чат,
куди його додали або де він бачив файл, — щоб чат можна було просто увімкнути галочкою."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "tg_ingest"


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        c = json.loads(st.value) if st and st.value else {}
    except ValueError:
        c = {}
    c.setdefault("chats", {})
    c.setdefault("reply", True)
    return c


def _save(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def enabled(db: Session) -> set[str]:
    return {cid for cid, x in cfg(db)["chats"].items() if x.get("on")}


def seen(db: Session, chat_id, title: str | None) -> bool:
    """Запам'ятати чат. True — якщо чат новий."""
    c = cfg(db)
    cid = str(chat_id)
    new = cid not in c["chats"]
    x = c["chats"].setdefault(cid, {"on": False})
    x["title"] = (title or x.get("title") or cid)[:120]
    x["seen"] = datetime.utcnow().isoformat(timespec="minutes")
    _save(db, c)
    return new


def set_chats(db: Session, on_ids: list[str], reply: bool, add_id: str = "", add_title: str = "") -> None:
    c = cfg(db)
    for cid, x in c["chats"].items():
        x["on"] = cid in on_ids
    add_id = add_id.strip()
    if add_id.lstrip("-").isdigit():
        c["chats"][add_id] = {**c["chats"].get(add_id, {}), "on": True, "title": (add_title.strip() or c["chats"].get(add_id, {}).get("title") or add_id)[:120]}
    c["reply"] = reply
    _save(db, c)


def remove(db: Session, cid: str) -> None:
    c = cfg(db)
    c["chats"].pop(str(cid), None)
    _save(db, c)
