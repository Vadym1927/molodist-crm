"""monobank (особистий API): виписка рахунків ФОП → вихідні платежі постачальникам → рознесення на поставки.

Токен: https://api.monobank.ua/ (вхід через застосунок monobank) — окремо для кожного власника ФОП.
GET /personal/client-info (рахунки) · GET /personal/statement/{account}/{from}/{to} — до 31 доби + 1 год,
не частіше 1 запиту на 60 с на токен. Суми в копійках. Токени зберігаються зашифрованими (SECRET_KEY)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..models import BankTxn, SyncState

log = logging.getLogger("monobank")
BASE = "https://api.monobank.ua"
STATEMENT_GAP = 61          # с між запитами виписки одного токена


class MonoError(RuntimeError):
    pass


def _cipher():
    from .cleverbox import _token_cipher
    return _token_cipher()


def saved(db: Session) -> list[dict]:
    st = db.get(SyncState, "mono_tokens_enc")
    if not st or not st.value:
        return []
    try:
        return json.loads(_cipher().decrypt(st.value.encode()).decode())
    except Exception:  # noqa: BLE001
        log.warning("Saved monobank tokens cannot be decrypted")
        return []


def _save(db: Session, items: list[dict]) -> None:
    st = db.get(SyncState, "mono_tokens_enc") or SyncState(key="mono_tokens_enc")
    st.value = _cipher().encrypt(json.dumps(items, ensure_ascii=False).encode()).decode()
    st.updated_at = datetime.utcnow()
    db.merge(st)
    db.commit()


def public(db: Session) -> list[dict]:
    """Без токенів — для сторінки."""
    return [{"fop": x["fop"], "accounts": x.get("accounts", []), "watch": x.get("watch", []),
             "error": x.get("error"), "checked": x.get("checked"), "last": x.get("last")} for x in saved(db)]


def _get(path: str, token: str, http: httpx.Client | None = None) -> object:
    c = http or httpx.Client(timeout=30)
    r = c.get(f"{BASE}{path}", headers={"X-Token": token})
    if r.status_code == 429:
        raise MonoError("monobank: забагато запитів, спробуйте за хвилину")
    if r.status_code >= 400:
        try:
            msg = r.json().get("errorDescription") or r.text[:120]
        except ValueError:
            msg = r.text[:120]
        raise MonoError(f"monobank {r.status_code}: {msg}")
    return r.json()


def connect(db: Session, fop: str, token: str, getter=None) -> str | None:
    """Перевіряє токен (client-info), зберігає рахунки. Порожній токен — лишити попередній."""
    getter = getter or _get
    items = saved(db)
    prev = next((x for x in items if x["fop"] == fop), {})
    token = token.strip() or prev.get("token", "")
    rec = {"fop": fop, "token": token, "watch": prev.get("watch", []), "checked": datetime.now().isoformat(timespec="minutes")}
    try:
        info = getter("/personal/client-info", token)
        accs = []
        for a in info.get("accounts", []):
            accs.append({"id": a.get("id"), "iban": a.get("iban"), "type": a.get("type"),
                         "currency": a.get("currencyCode"), "masked": (a.get("maskedPan") or [""])[0]})
        rec["accounts"] = accs
        rec["owner"] = info.get("name")
        if not rec["watch"]:                    # за замовчуванням — гривневі рахунки ФОП
            rec["watch"] = [a["id"] for a in accs if a["type"] == "fop" and a["currency"] == 980] or \
                           [a["id"] for a in accs if a["currency"] == 980][:1]
        rec["error"] = None
    except Exception as e:  # noqa: BLE001
        rec["accounts"], rec["error"] = prev.get("accounts", []), str(e)[:200]
    _save(db, [x for x in items if x["fop"] != fop] + [rec])
    return rec["error"]


def set_watch(db: Session, fop: str, account_ids: list[str]) -> None:
    items = saved(db)
    for x in items:
        if x["fop"] == fop:
            x["watch"] = account_ids
    _save(db, items)


def disconnect(db: Session, fop: str) -> None:
    _save(db, [x for x in saved(db) if x["fop"] != fop])


def sync(db: Session, days: int = 2, getter=None, sleep=time.sleep) -> dict:
    """Виписка за останні `days` діб (≤31) по кожному рахунку, що відстежується; потім — рознесення."""
    from ..services import payments
    getter = getter or _get
    days = max(1, min(days, 31))
    rep, items = {}, saved(db)
    for x in items:
        n, first = 0, True
        try:
            for acc in x.get("watch", []):
                if not first:
                    sleep(STATEMENT_GAP)
                first = False
                to = int(time.time())
                frm = to - days * 86400
                for t in getter(f"/personal/statement/{acc}/{frm}/{to}", x["token"]) or []:
                    tid = str(t.get("id"))
                    if not tid or (t.get("amount") or 0) >= 0:
                        continue                 # цікавлять лише списання
                    bt = db.get(BankTxn, tid)
                    if bt:
                        continue
                    db.add(BankTxn(id=tid, fop=x["fop"], account=acc, at=datetime.fromtimestamp(int(t.get("time") or 0)),
                                   amount=round((t.get("amount") or 0) / 100, 2), counter_name=(t.get("counterName") or "")[:300] or None,
                                   counter_iban=(t.get("counterIban") or "")[:40] or None,
                                   counter_edrpou=(t.get("counterEdrpou") or "")[:20] or None,
                                   description=((t.get("description") or "") + (" · " + t["comment"] if t.get("comment") else ""))[:500] or None))
                    n += 1
            db.commit()
            x["last"], x["error"] = datetime.now().isoformat(timespec="minutes"), None
            rep[x["fop"]] = f"{n} нових списань"
        except Exception as e:  # noqa: BLE001
            x["error"] = str(e)[:200]
            rep[x["fop"]] = f"помилка: {e}"
    if items:
        _save(db, items)
    rep["рознесено"] = payments.rematch_all(db)
    return rep
