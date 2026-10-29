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


def accounts() -> list[dict]:
    out = []
    for i in (1, 2):
        name = getattr(settings, f"checkbox_{i}_name", "") or f"ФОП {i}"
        login = getattr(settings, f"checkbox_{i}_login", "")
        pwd = getattr(settings, f"checkbox_{i}_password", "")
        out.append({"name": name, "configured": bool(login and pwd), "login": login, "password": pwd})
    return out


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
    for acc in accounts():
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
        except Exception as e:  # noqa: BLE001
            log.warning("checkbox %s: %s", acc["name"], e)
            rep[acc["name"]] = f"помилка: {e}"
    from .cleverbox import _state
    _state(db, "checkbox_last", datetime.now().isoformat(timespec="minutes") + " · " +
           "; ".join(f"{k}: {v}" for k, v in rep.items()))
    db.commit()
    return rep
