"""Терміни придатності: звіт по кожному складу (партії) + сповіщення в CRM і Telegram.

Звіт: партії з залишком > 0 (облік у CRM) або, якщо облік не розпочато, — найближчий термін закупівель товару.
Групи: прострочено · ≤30 днів · 31–90 · 91–180 · >180 · без терміну. Вартість — залишок × середня ціна закупівлі.
Сповіщення (SyncState expiry_notify): кому — чат адмінів і вибрані користувачі з Telegram (головний лікар, власник…);
щотижневий підсумок у вибраний день + миттєві попередження, коли партія доходить до 30 / 7 / 0 днів (один раз)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Product, ReminderLog, SyncState, User

BUCKETS = [("expired", "Прострочено", None, -1), ("d30", "До 30 днів", 0, 30), ("d90", "31–90 днів", 31, 90),
           ("d180", "91–180 днів", 91, 180), ("later", "Понад 180 днів", 181, 10 ** 6), ("none", "Без терміну", None, None)]
LABELS = {k: v for k, v, *_ in BUCKETS}
THRESHOLDS = (30, 7, 0)
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
DEFAULT_CFG = {"users": [], "admin_chat": True, "weekday": 0, "days": 90, "alerts": True}


def bucket(days: int | None) -> str:
    if days is None:
        return "none"
    if days < 0:
        return "expired"
    for k, _l, lo, hi in BUCKETS[1:5]:
        if lo <= days <= hi:
            return k
    return "later"


def rows(db: Session) -> list[dict]:
    from .catalog import WAREHOUSES, product_rows
    from .stock import ledger_active, batch_balances
    today = date.today()
    prs = {r.product.id: r for r in product_rows(db, [])}
    out = []
    if ledger_active(db):
        for (pid, wh, e), q in batch_balances(db).items():
            if q <= 1e-6 or pid not in prs:
                continue
            r = prs[pid]
            p = r.product
            k = p.coef if p.coef and p.coef > 0 else 1
            unit_cost = ((r.cost_avg or r.cost_last or 0) / k)
            days = (e - today).days if e else None
            out.append({"product": p, "wh": wh, "wh_name": WAREHOUSES.get(wh, wh), "expiry": e, "days": days,
                        "bucket": bucket(days), "qty": round(q, 3), "unit": p.unit or "шт", "value": round(q * unit_cost, 2)})
    else:                                          # облік не розпочато — найближчий термін закупівель
        for pid, r in prs.items():
            if not r.stock or r.stock <= 0:
                continue
            p = r.product
            days = (r.expiry - today).days if r.expiry else None
            k = p.coef if p.coef and p.coef > 0 else 1
            out.append({"product": p, "wh": p.warehouse or "vitrina", "wh_name": WAREHOUSES.get(p.warehouse or "vitrina"),
                        "expiry": r.expiry, "days": days, "bucket": bucket(days), "qty": r.stock, "unit": p.unit or "шт",
                        "value": round(r.stock * (r.cost_avg or r.cost_last or 0) / k, 2)})
    out.sort(key=lambda x: (x["days"] is None, x["days"] if x["days"] is not None else 0, x["product"].name))
    return out


def report(db: Session) -> dict:
    from .catalog import WAREHOUSES
    data = rows(db)
    by_wh = {k: {"key": k, "name": v, "rows": [], "b": defaultdict(lambda: {"n": 0, "value": 0.0})} for k, v in WAREHOUSES.items()}
    tot = defaultdict(lambda: {"n": 0, "value": 0.0})
    for x in data:
        w = by_wh.setdefault(x["wh"], {"key": x["wh"], "name": x["wh_name"], "rows": [], "b": defaultdict(lambda: {"n": 0, "value": 0.0})})
        w["rows"].append(x)
        for t in (w["b"][x["bucket"]], tot[x["bucket"]]):
            t["n"] += 1
            t["value"] += x["value"]
    return {"warehouses": [w for w in by_wh.values() if w["rows"]], "total": tot, "rows": data, "BUCKETS": BUCKETS}


# ---------------- налаштування й розсилка ----------------

def config(db: Session) -> dict:
    st = db.get(SyncState, "expiry_notify")
    try:
        return {**DEFAULT_CFG, **(json.loads(st.value) if st and st.value else {})}
    except ValueError:
        return dict(DEFAULT_CFG)


def save_config(db: Session, cfg: dict) -> None:
    st = db.get(SyncState, "expiry_notify") or SyncState(key="expiry_notify")
    st.value, st.updated_at = json.dumps({**DEFAULT_CFG, **cfg}), datetime.utcnow()
    db.merge(st)
    db.commit()


def recipients(db: Session, cfg: dict | None = None) -> list[tuple[str, str]]:
    """[(chat_id, підпис)] — чат адмінів + вибрані користувачі з привʼязаним Telegram."""
    cfg = cfg or config(db)
    out = []
    if cfg.get("admin_chat") and settings.telegram_admin_chat_id:
        out.append((settings.telegram_admin_chat_id, "чат адмінів"))
    ids = [int(i) for i in cfg.get("users") or []]
    for u in db.scalars(select(User).where(User.id.in_(ids or [0]), User.active.is_(True))):
        if u.telegram_chat_id and all(c != u.telegram_chat_id for c, _ in out):
            out.append((u.telegram_chat_id, u.name))
    return out


def _line(x: dict) -> str:
    d = x["days"]
    when = "без терміну" if d is None else (f"прострочено {-d} дн" if d < 0 else ("сьогодні" if d == 0 else f"{d} дн"))
    exp = f" до {x['expiry']:%d.%m.%Y}" if x["expiry"] else ""
    return f"• {escape(x['product'].name[:60])} — {x['qty']:g} {escape(x['unit'])}{exp} ({when})"


def summary_text(db: Session, days: int = 90) -> str | None:
    rep = report(db)
    parts = []
    for w in rep["warehouses"]:
        hot = [x for x in w["rows"] if x["days"] is not None and x["days"] <= days]
        if not hot:
            continue
        val = sum(x["value"] for x in hot)
        parts.append(f"\n<b>{escape(w['name'])}</b> — {len(hot)} парт. на {val:,.0f} ₴".replace(",", " ") + "\n" +
                     "\n".join(_line(x) for x in hot[:15]) + (f"\n… ще {len(hot) - 15}" if len(hot) > 15 else ""))
    if not parts:
        return None
    return f"🗓 <b>Терміни придатності</b> (≤ {days} дн і прострочене)" + "".join(parts) + \
        "\n\nЗвіт: Звіти → Терміни придатності"


def new_alerts(db: Session) -> list[dict]:
    """Партії, що щойно дійшли до 30 / 7 / 0 днів — кожна межа один раз (ReminderLog)."""
    out = []
    for x in rows(db):
        d = x["days"]
        if d is None:
            continue
        th = next((t for t in sorted(THRESHOLDS) if d <= t), None)
        if th is None:
            continue
        key = f"{x['product'].id}:{x['wh']}:{x['expiry']}"
        if db.scalar(select(ReminderLog).where(ReminderLog.kind == "expiry", ReminderLog.entity_key == key,
                                               ReminderLog.period_key == f"t{th}")):
            continue
        db.add(ReminderLog(kind="expiry", entity_key=key, period_key=f"t{th}"))
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            continue
        out.append({**x, "threshold": th})
    db.commit()
    return out


def send(db: Session, text: str, cfg: dict | None = None) -> int:
    from .alerts import _api
    n = 0
    for chat, _who in recipients(db, cfg):
        n += _api("sendMessage", data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"})
    return n


def daily(db: Session, today: date | None = None, force_summary: bool = False) -> dict:
    """Щодня: попередження про нові межі; у вибраний день тижня — підсумок по складах."""
    today = today or date.today()
    cfg = config(db)
    rep = {"alerts": 0, "summary": 0}
    if not recipients(db, cfg):                    # нікому слати — межі не «витрачаємо»
        return rep
    if cfg.get("alerts"):
        fresh = new_alerts(db)
        if fresh:
            head = {0: "сьогодні або прострочено", 7: "≤ 7 днів", 30: "≤ 30 днів"}
            by = defaultdict(list)
            for x in fresh:
                by[(x["wh_name"], x["threshold"])].append(x)
            text = "⚠️ <b>Терміни придатності</b>" + "".join(
                f"\n\n<b>{escape(w)}</b> · {head[t]}\n" + "\n".join(_line(x) for x in xs[:20])
                for (w, t), xs in sorted(by.items(), key=lambda kv: (kv[0][1], kv[0][0])))
            rep["alerts"] = send(db, text, cfg)
    if force_summary or today.weekday() == int(cfg.get("weekday", 0)):
        key = f"{today.isocalendar().year}W{today.isocalendar().week:02d}"
        done = db.scalar(select(ReminderLog).where(ReminderLog.kind == "expiry_week", ReminderLog.period_key == key))
        if force_summary or not done:
            text = summary_text(db, int(cfg.get("days", 90)))
            if text:
                rep["summary"] = send(db, text, cfg)
            if not done and not force_summary:
                db.add(ReminderLog(kind="expiry_week", entity_key="all", period_key=key))
                db.commit()
    return rep
