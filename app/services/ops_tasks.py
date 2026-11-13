"""Канал завдань для агента OpenClaw на сервері: власник (або Claude від його імені в CRM) ставить завдання,
агент забирає через API v1 /ops/pending, виконує на сервері й повертає вивід у /ops/{id}/result.

• check — лише перегляд (df, du, ls, logs, git status…): одразу в черзі.
• change — будь-яка зміна (видалення, перезапуск, налаштування): спершу погодження власника кнопкою в Telegram
  (у CRM кнопки погодження немає — погоджує лише людина в Telegram).
Секрети у виводі (токени, ключі, паролі) CRM маскує перед збереженням."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import OpsTask

KINDS = {"check": "Перевірка (лише перегляд)", "change": "Зміна на сервері (з погодженням)"}
STATUS = {"approval": "чекає погодження в Telegram", "pending": "у черзі", "taken": "виконується", "done": "виконано",
          "error": "помилка", "rejected": "відхилено"}
MAX_OUT = 60_000

RULES = """Ти виконуєш завдання власника MOLODIST CRM на цьому сервері.
• kind=check — ЛИШЕ команди, що нічого не змінюють (df, du, ls, find без -delete, cat логів, git status/diff/log, docker ps/df/logs, systemctl status, journalctl).
  Якщо для виконання потрібно щось змінити — не роби, поверни error з поясненням, що потрібна задача kind=change.
• kind=change — виконуй рівно те, що написано в завданні (власник уже погодив його в Telegram). Нічого понад це.
• Ніколи: не виводь вміст .env, токени, ключі, паролі; не чіпай базу даних (DELETE/DROP/restore), Nginx, бот пацієнтів; не правь код у /opt/molodist-crm (зміни коду — лише через GitHub).
• Відповідь — у POST /api/v1/ops/{id}/result: {"output": "вивід команд як є (команда і її результат)", "exit_code": 0} або {"error": "чому не виконано"}.
• Відповідай українською. Вивід не скорочуй довільно (до 60 000 символів), не переказуй своїми словами."""

_SECRET = [
    (re.compile(r"(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|authorization)[\w.-]*\s*[=:]\s*)(\S+)"), r"\1***"),
    (re.compile(r"\bbot\d{6,}:[\w-]{30,}"), "bot***"),
    (re.compile(r"\b(?:sk|pk|mcrm|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{16,}"), "***"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "*** private key ***"),
]


def redact(text: str) -> str:
    for rx, rep in _SECRET:
        text = rx.sub(rep, text)
    return text


def create(db: Session, title: str, text: str, kind: str, user_id: int | None) -> OpsTask:
    kind = kind if kind in KINDS else "check"
    t = OpsTask(title=(title or "").strip()[:200] or "Завдання", text=(text or "").strip()[:8000], kind=kind,
                status="approval" if kind == "change" else "pending", user_id=user_id)
    db.add(t)
    db.commit()
    if kind == "change":
        ask_approval(db, t)
    return t


def recent(db: Session, limit: int = 50) -> list[OpsTask]:
    return list(db.scalars(select(OpsTask).order_by(OpsTask.id.desc()).limit(limit)))


def pending(db: Session, limit: int = 5) -> list[OpsTask]:
    stale = datetime.now() - timedelta(minutes=15)                 # «взяв і зник» — повертаємо в чергу
    rows = list(db.scalars(select(OpsTask).where((OpsTask.status == "pending") | ((OpsTask.status == "taken") & (OpsTask.taken_at < stale)))
                           .order_by(OpsTask.id).limit(max(1, min(limit, 20)))))
    now = datetime.now()
    for t in rows:
        t.status, t.taken_at = "taken", now
    db.commit()
    return rows


def result(db: Session, t: OpsTask, output: str | None, error: str | None, exit_code=None) -> OpsTask:
    if t.status not in ("taken", "pending"):
        raise ValueError(f"Завдання в статусі «{STATUS.get(t.status, t.status)}» — результат не приймається")
    out = redact(str(output or ""))[:MAX_OUT]
    if exit_code not in (None, "", 0, "0"):
        out = (out + f"\n[exit code: {exit_code}]").strip()
    t.output = out or None
    t.error = redact(str(error))[:1000] if error else None
    t.status, t.done_at = ("error" if error and not output else "done"), datetime.now()
    db.commit()
    if t.kind == "change":
        from . import alerts
        alerts.send(f"🛠 OpenClaw виконав: «{t.title}» — {STATUS[t.status]}. Вивід — у CRM: «Завдання для OpenClaw».")
    return t


def decide(db: Session, t: OpsTask, ok: bool, user_id: int | None) -> bool:
    if t.status != "approval":
        return False
    t.status = "pending" if ok else "rejected"
    t.approved_by, t.approved_at = user_id, datetime.now()
    db.commit()
    return True


def ask_approval(db: Session, t: OpsTask) -> int:
    """Картка в Telegram власнику з кнопками «Погодити» / «Відхилити»."""
    from .pay_requests import owner_tg_ids
    if not settings.telegram_bot_token:
        return 0
    body = t.text if len(t.text) < 3000 else t.text[:3000] + "…"
    text = f"🛠 <b>Зміна на сервері — потрібне ваше погодження</b>\n<b>{_h(t.title)}</b>\n\n<pre>{_h(body)}</pre>\n\nВиконає агент OpenClaw після погодження."
    kb = {"inline_keyboard": [[{"text": "✅ Погодити", "callback_data": f"ops:ok:{t.id}"}, {"text": "✖️ Відхилити", "callback_data": f"ops:no:{t.id}"}]]}
    n = 0
    for chat in owner_tg_ids(db):
        try:
            r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", timeout=30,
                           json={"chat_id": chat, "text": text, "parse_mode": "HTML", "reply_markup": kb, "disable_web_page_preview": True})
            n += r.status_code == 200
        except Exception:  # noqa: BLE001
            pass
    return n


def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def as_json(t: OpsTask) -> dict:
    return {"id": t.id, "kind": t.kind, "title": t.title, "text": t.text, "created_at": t.created_at.isoformat(timespec="seconds"),
            "approved": t.kind == "check" or t.approved_at is not None}
