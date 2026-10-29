"""Checkbox (ПРРО) — оборот ФОП за Z-звітами: готівка / безготівка по місяцях.

API: POST /cashier/signin {login, password} → access_token (Bearer); GET /reports/search?is_z_report=true
&from_date&to_date&limit&offset → Z-звіти змін з payments[{type: CASH|CASHLESS|…, sell_sum, return_sum}]
(суми в копійках). Обов'язкові заголовки X-Client-Name / X-Client-Version. CRM нічого не фіскалізує — лише читає."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import FopMonth

log = logging.getLogger("checkbox")
HEADERS = {"X-Client-Name": "MOLODIST CRM", "X-Client-Version": "1.0"}
KYIV = ZoneInfo("Europe/Kyiv")


class CheckboxError(RuntimeError):
    pass


def _saved(db: Session) -> list[dict]:
    """ФОП, підключені з інтерфейсу: зашифровано ключем із SECRET_KEY (у базі лише шифротекст)."""
    import json
    from ..models import SyncState
    from .cleverbox import _token_cipher
    st = db.get(SyncState, "checkbox_accounts_enc")
    if not st or not st.value:
        return []
    try:
        return json.loads(_token_cipher().decrypt(st.value.encode()).decode())
    except Exception:  # noqa: BLE001
        log.warning("Saved Checkbox accounts cannot be decrypted")
        return []


def save_accounts(db: Session, items: list[dict]) -> None:
    import json
    from ..models import SyncState
    from .cleverbox import _token_cipher
    st = db.get(SyncState, "checkbox_accounts_enc") or SyncState(key="checkbox_accounts_enc")
    st.value = _token_cipher().encrypt(json.dumps(items, ensure_ascii=False).encode()).decode()
    st.updated_at = datetime.utcnow()
    db.merge(st)
    db.commit()


def accounts(db: Session | None = None) -> list[dict]:
    """ФОП з .env (CHECKBOX_N_*) + підключені в «Звіти → Ліміти ФОП». Однакова назва — перемагає інтерфейс."""
    if db is None:
        from ..models import SessionLocal
        with SessionLocal() as s:
            return accounts(s)
    out: dict[str, dict] = {}
    for i in (1, 2):
        name = getattr(settings, f"checkbox_{i}_name", "") or f"ФОП {i}"
        login = getattr(settings, f"checkbox_{i}_login", "")
        pwd = getattr(settings, f"checkbox_{i}_password", "")
        out[name] = {"name": name, "configured": bool(login and pwd), "login": login, "password": pwd, "source": "env"}
    for a in _saved(db):
        if a.get("name"):
            out[a["name"]] = {"name": a["name"], "configured": bool(a.get("login") and a.get("password")),
                              "login": a.get("login", ""), "password": a.get("password", ""), "source": "ui"}
    return list(out.values())


def connect(db: Session, name: str, login: str, password: str, client_factory=None) -> str | None:
    """Зберегти ФОП з інтерфейсу; порожній пароль — лишити попередній. Повертає помилку входу або None."""
    name, login = name.strip()[:100], login.strip()[:200]
    items = [a for a in _saved(db) if a.get("name") != name]
    prev = next((a for a in _saved(db) if a.get("name") == name), None) or \
        next((a for a in accounts(db) if a["name"] == name), {})
    password = password or prev.get("password", "")
    err = None
    try:
        (client_factory or Checkbox)(login, password)
    except Exception as e:  # noqa: BLE001
        err = str(e)[:200]
    items.append({"name": name, "login": login, "password": password})
    save_accounts(db, items)
    _status(db, name, err)
    return err


def disconnect(db: Session, name: str) -> None:
    save_accounts(db, [a for a in _saved(db) if a.get("name") != name])


def _status(db: Session, name: str, err: str | None) -> None:
    import json
    from ..models import SyncState
    st = db.get(SyncState, "checkbox_status") or SyncState(key="checkbox_status")
    cur = json.loads(st.value) if st.value else {}
    cur[name] = {"ok": err is None, "error": err, "at": datetime.now().isoformat(timespec="minutes")}
    st.value, st.updated_at = json.dumps(cur, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def statuses(db: Session) -> dict:
    import json
    from ..models import SyncState
    st = db.get(SyncState, "checkbox_status")
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


class Checkbox:
    def __init__(self, login: str, password: str, base: str | None = None, http: httpx.Client | None = None):
        self.base = (base or settings.checkbox_base_url).rstrip("/")
        self.http = http or httpx.Client(timeout=30, headers=HEADERS)
        r = self.http.post(f"{self.base}/cashier/signin", json={"login": login, "password": password})
        if r.status_code >= 400:
            raise CheckboxError(f"вхід касира: {r.status_code} {_msg(r)}")
        self.token = r.json().get("access_token")
        if not self.token:
            raise CheckboxError("вхід касира: немає access_token")

    def get(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{self.base}/{path}", params=params, headers={"Authorization": f"Bearer {self.token}", **HEADERS})
        if r.status_code >= 400:
            raise CheckboxError(f"{path}: {r.status_code} {_msg(r)}")
        return r.json()

    def z_reports(self, start: date, end: date):
        """Checkbox шукає не більше ніж за 3 місяці — запитуємо помісячно."""
        cur = start
        while cur <= end:
            nxt = min(end, (cur.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
            yield from self._z_page(cur, nxt)
            cur = nxt + timedelta(days=1)

    def _z_page(self, start: date, end: date):
        offset = 0
        while True:
            res = self.get("reports/search", {"is_z_report": "true", "limit": 100, "offset": offset,
                                              "from_date": f"{start.isoformat()}T00:00:00+03:00",
                                              "to_date": f"{end.isoformat()}T23:59:59+03:00"})
            rows = res.get("results") or []
            yield from rows
            offset += len(rows)
            total = (res.get("meta") or {}).get("total") or res.get("total")
            if not rows or len(rows) < 100 or (total and offset >= int(total)):
                break


def _msg(r) -> str:
    try:
        j = r.json()
        return str(j.get("message") or j.get("detail") or "")[:150]
    except ValueError:
        return r.text[:150]


def _when(rep: dict) -> datetime | None:
    v = rep.get("created_at") or rep.get("date") or (rep.get("shift") or {}).get("closed_at")
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(KYIV)


def split(rep: dict) -> tuple[float, float]:
    """(готівка, безготівка) у гривнях: продажі − повернення."""
    cash = cashless = 0.0
    for p in rep.get("payments") or []:
        amt = ((p.get("sell_sum") or 0) - (p.get("return_sum") or 0)) / 100
        if str(p.get("type") or "").upper() == "CASH":
            cash += amt
        else:
            cashless += amt
    return round(cash, 2), round(cashless, 2)


def sync(db: Session, start: date | None = None, end: date | None = None, client_factory=None) -> dict:
    """Перераховує місяці [start..end] для кожного налаштованого ФОП (місяці з checkbox перезаписуються)."""
    end = end or date.today()
    start = start or (end.replace(day=1) - timedelta(days=1)).replace(day=1)      # поточний + попередній місяць
    start = start.replace(day=1)
    rep = {}
    for acc in accounts(db):
        if not acc["configured"]:
            rep[acc["name"]] = "не налаштовано"
            continue
        try:
            cb = (client_factory or Checkbox)(acc["login"], acc["password"])
            agg = defaultdict(lambda: [0.0, 0.0])
            n = 0
            for z in cb.z_reports(start, end):
                dt = _when(z)
                if not dt:
                    continue
                c, nc = split(z)
                agg[(dt.year, dt.month)][0] += c
                agg[(dt.year, dt.month)][1] += nc
                n += 1
            y, m = start.year, start.month
            while (y, m) <= (end.year, end.month):
                fm = db.scalar(select(FopMonth).where(FopMonth.fop == acc["name"], FopMonth.year == y, FopMonth.month == m))
                cash, cashless = agg.get((y, m), (0.0, 0.0))
                if fm is None:
                    if not (cash or cashless):
                        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
                        continue
                    fm = FopMonth(fop=acc["name"], year=y, month=m)
                    db.add(fm)
                fm.cash, fm.cashless, fm.source, fm.updated_at = round(cash, 2), round(cashless, 2), "checkbox", datetime.utcnow()
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            db.commit()
            rep[acc["name"]] = f"{n} Z-звітів"
            _status(db, acc["name"], None)
        except Exception as e:  # noqa: BLE001
            log.warning("checkbox %s: %s", acc["name"], e)
            rep[acc["name"]] = f"помилка: {e}"
            _status(db, acc["name"], str(e)[:200])
    from .cleverbox import _state
    _state(db, "checkbox_last", datetime.now().isoformat(timespec="minutes") + " · " +
           "; ".join(f"{k}: {v}" for k, v in rep.items()))
    db.commit()
    return rep
