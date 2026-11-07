"""Звіт «Deye» (лише власник): окупність станції Deye порівняно з генератором.

Логіка (як у CEO Dashboard · Енергія):
  • Вартість зарядки = кВт·год на заряд × тариф відповідного місяця;
  • Еквівалент генератора = години на інверторі в робочий час (09:00–21:00) × л/год (1 л) × ціна WOG А-95 цього дня;
  • Економія = вартість генератора − вартість зарядки;
  • Окупність = накопичена економія / вартість станції.
Ціна WOG А-95 — щодня з публічного API WOG (api.wog.ua/fuel_stations → fuel_filters «95» Євро5-Е10, копійки),
фіксується на день; для минулих дат — вручну. Дані станції — з Deye Cloud OpenAPI або вручну.
Налаштування / тарифи — SyncState «energy»; ключі Deye — зашифровано, у відповідях не показуються.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EnergyDay, FuelPrice, SyncState

log = logging.getLogger("energy")
KEY = "energy"
WOG_URL = "https://api.wog.ua/fuel_stations"
DEYE_BASE = "https://eu1-developer.deyecloud.com/v1.0"
SECRETS = ("app_secret", "email", "password")


# ---------------- налаштування ----------------

def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("station_cost", 241541.98)
    d.setdefault("litres_per_hour", 1.0)
    d.setdefault("work_from", 9)
    d.setdefault("work_to", 21)
    d.setdefault("tariffs", {})                 # "2026-11": 13.16
    d.setdefault("deye", {"base": DEYE_BASE, "app_id": "", "station_id": "", "secrets": {}})
    d["deye"].setdefault("secrets", {})
    return d


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False, default=str), datetime.utcnow()
    db.merge(st)
    db.commit()


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def set_secret(d: dict, name: str, value: str) -> None:
    if name == "password":                         # Deye приймає SHA-256 пароля — сам пароль не зберігаємо
        value = hashlib.sha256(value.encode()).hexdigest()
    d["deye"]["secrets"][name] = _cipher().encrypt(value.encode()).decode()


def secret(d: dict, name: str) -> str:
    v = d["deye"]["secrets"].get(name)
    if not v:
        return ""
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def deye_ready(d: dict) -> bool:
    return bool(d["deye"].get("app_id") and all(d["deye"]["secrets"].get(k) for k in SECRETS))


def tariff(d: dict, day: date) -> float:
    t = d["tariffs"]
    k = f"{day:%Y-%m}"
    if k in t:
        return float(t[k])
    prev = sorted(x for x in t if x <= k)            # тариф ще не внесли — беремо останній відомий
    return float(t[prev[-1]]) if prev else 0.0


# ---------------- WOG ----------------

def parse_wog(js: dict) -> float | None:
    """api.wog.ua/fuel_stations → ціна А-95 (звичайного, не Mustang) ₴/л; ціни там у копійках."""
    ff = ((js or {}).get("data") or {}).get("fuel_filters") or []
    c = [f for f in ff if str(f.get("name")) == "95" and "mustang" not in str(f.get("brand") or "").lower()]
    if not c:
        return None
    p = float(c[0].get("price") or 0)
    return round(p / 100, 2) if p > 1000 else (round(p, 2) if p else None)


def capture_fuel(db: Session, day: date | None = None, http: httpx.Client | None = None) -> FuelPrice | None:
    """Зафіксувати ціну WOG А-95 на день (сьогодні). Уже є — не перезаписуємо (ціна дня фіксується раз)."""
    day = day or date.today()
    fp = db.get(FuelPrice, day)
    if fp:
        return fp
    try:
        js = (http or httpx.Client(timeout=20)).get(WOG_URL).json()
    except Exception as e:  # noqa: BLE001
        log.warning("WOG: %s", e)
        return None
    p = parse_wog(js)
    if not p:
        return None
    fp = FuelPrice(day=day, price=p, captured_at=datetime.utcnow(), source="WOG А-95 · api.wog.ua")
    db.add(fp)
    db.commit()
    return fp


def fuel_for(db: Session, day: date, prices: dict | None = None) -> float | None:
    """Ціна на день: ціна, внесена в день вручну → зафіксована WOG → остання відома до цього дня."""
    prices = prices if prices is not None else {f.day: f.price for f in db.scalars(select(FuelPrice))}
    if day in prices:
        return prices[day]
    prev = [k for k in prices if k <= day]
    if prev:
        return prices[max(prev)]
    nxt = [k for k in prices if k > day]
    return prices[min(nxt)] if nxt else None


# ---------------- розрахунок ----------------

def calc_day(e: EnergyDay, d: dict, fuel: float | None) -> dict:
    t = tariff(d, e.day)
    f = float(e.fuel_price or fuel or 0)
    lit = round(float(e.business_hours or 0) * float(d["litres_per_hour"] or 1), 2)
    charge = round(float(e.charge_kwh or 0) * t, 2)
    gen = round(lit * f, 2)
    return {"day": e.day, "month": f"{e.day:%Y-%m}", "discharge": float(e.discharge_kwh or 0), "charge_kwh": float(e.charge_kwh or 0),
            "battery_hours": float(e.battery_hours or 0), "business_hours": float(e.business_hours or 0), "tariff": t, "fuel": f,
            "litres": lit, "charge_cost": charge, "gen_cost": gen, "savings": round(gen - charge, 2), "source": e.source, "e": e}


def rows(db: Session, d: dict | None = None) -> list[dict]:
    d = d or cfg(db)
    prices = {f.day: f.price for f in db.scalars(select(FuelPrice))}
    return [calc_day(e, d, fuel_for(db, e.day, prices)) for e in db.scalars(select(EnergyDay).order_by(EnergyDay.day.desc()))]


def summary(rs: list[dict]) -> dict:
    k = ("discharge", "charge_kwh", "battery_hours", "business_hours", "charge_cost", "gen_cost", "savings", "litres")
    return {x: round(sum(r[x] for r in rs), 2) for x in k} | {"days": len(rs)}


def by_month(rs: list[dict]) -> list[dict]:
    g = defaultdict(list)
    for r in rs:
        g[r["month"]].append(r)
    return [{"month": m, "rows": v, **summary(v)} for m, v in sorted(g.items(), reverse=True)]


def payback(db: Session, d: dict, rs: list[dict]) -> dict:
    tot = round(sum(r["savings"] for r in rs), 2)
    cost = float(d["station_cost"] or 0)
    pct = round(100 * tot / cost, 2) if cost else None
    left = round(max(cost - tot, 0), 2)
    # прогноз: середня економія за 30 останніх днів з даними
    last = sorted(rs, key=lambda r: r["day"])[-30:]
    per_day = (sum(r["savings"] for r in last) / max(1, (last[-1]["day"] - last[0]["day"]).days + 1)) if last else 0
    eta = (date.today() + timedelta(days=int(left / per_day))) if per_day > 0 and left else None
    return {"total": tot, "cost": cost, "pct": pct, "left": left, "per_day": round(per_day, 2), "eta": eta}


def save_day(db: Session, day: date, vals: dict, source: str = "manual") -> EnergyDay:
    e = db.get(EnergyDay, day) or EnergyDay(day=day)
    for k in ("discharge_kwh", "charge_kwh", "battery_hours", "business_hours", "fuel_price"):
        if k in vals and vals[k] is not None:
            setattr(e, k, float(vals[k]))
    e.source, e.updated_at = source, datetime.utcnow()
    db.merge(e)
    db.commit()
    return e


def import_json(db: Session, js: dict) -> int:
    """Перенести дані з CEO Dashboard · Енергія (/api/data): дні, тарифи, вартість станції, ціни пального."""
    d = cfg(db)
    a = js.get("assumptions") or {}
    if a.get("station_cost_uah"):
        d["station_cost"] = float(a["station_cost_uah"])
    if a.get("generator_litres_per_battery_hour"):
        d["litres_per_hour"] = float(a["generator_litres_per_battery_hour"])
    d["tariffs"].update({k: float(v) for k, v in (js.get("tariffs") or {}).items()})
    save_cfg(db, d)
    n = 0
    for x in js.get("days") or []:
        try:
            day = date.fromisoformat(x["day"])
        except (KeyError, ValueError):
            continue
        save_day(db, day, {"discharge_kwh": x.get("discharge_kwh"), "charge_kwh": x.get("charge_kwh"),
                           "battery_hours": x.get("battery_hours"), "business_hours": x.get("business_hours")}, "import")
        if x.get("fuel_uah_per_litre") and not db.get(FuelPrice, day):
            db.add(FuelPrice(day=day, price=float(x["fuel_uah_per_litre"]), source="імпорт CEO Dashboard"))
            db.commit()
        n += 1
    f = (js.get("fuel") or {}).get("today") or {}
    if f.get("day") and f.get("uah_per_litre"):
        try:
            fd = date.fromisoformat(f["day"])
            if not db.get(FuelPrice, fd):
                db.add(FuelPrice(day=fd, price=float(f["uah_per_litre"]), source="WOG А-95 (імпорт)"))
                db.commit()
        except ValueError:
            pass
    return n


# ---------------- Deye Cloud ----------------

class Deye:
    def __init__(self, d: dict, http: httpx.Client | None = None):
        self.d, self.base = d, (d["deye"].get("base") or DEYE_BASE).rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.tok = None

    def _post(self, path: str, body: dict, params: dict | None = None, auth: bool = True) -> dict:
        h = {"Content-Type": "application/json"}
        if auth:
            h["Authorization"] = f"bearer {self.token()}"
        r = self.http.post(f"{self.base}/{path.lstrip('/')}", json=body, params=params, headers=h)
        try:
            js = r.json()
        except ValueError:
            raise RuntimeError(f"Deye {path}: HTTP {r.status_code}")
        if r.status_code >= 400 or js.get("success") is False:
            raise RuntimeError(f"Deye {path}: {js.get('msg') or js.get('message') or r.status_code}")
        return js

    def token(self) -> str:
        if not self.tok:
            js = self._post("account/token", {"appSecret": secret(self.d, "app_secret"), "email": secret(self.d, "email"),
                                              "password": secret(self.d, "password")}, {"appId": self.d["deye"]["app_id"]}, auth=False)
            self.tok = js.get("accessToken") or (js.get("data") or {}).get("accessToken")
            if not self.tok:
                raise RuntimeError("Deye: немає accessToken у відповіді")
        return self.tok

    def stations(self) -> list[dict]:
        js = self._post("station/list", {"page": 1, "size": 20})
        return js.get("stationList") or (js.get("data") or {}).get("stationList") or []

    def daily(self, sid, d1: date, d2: date) -> list[dict]:
        js = self._post("station/history", {"stationId": int(sid), "granularity": 2, "startAt": d1.isoformat(), "endAt": d2.isoformat()})
        return js.get("stationDataItems") or []

    def frames(self, sid, day: date) -> list[dict]:
        js = self._post("station/history", {"stationId": int(sid), "granularity": 1, "startAt": day.isoformat(), "endAt": day.isoformat()})
        return js.get("stationDataItems") or []


def _num(x: dict, *keys) -> float | None:
    for k in keys:
        if x.get(k) not in (None, ""):
            try:
                return float(x[k])
            except (TypeError, ValueError):
                pass
    return None


def hours_from_frames(fr: list[dict], w_from: int, w_to: int) -> tuple[float, float]:
    """Години без мережі (імпорт з мережі ≈ 0, станція живить навантаження) — усього й у робочий час."""
    pts = []
    for x in fr:
        ts = _num(x, "timeStamp", "timestamp")
        if ts is None:
            continue
        dt = datetime.fromtimestamp(ts if ts < 1e12 else ts / 1000)
        grid = _num(x, "purchasePower", "gridPower", "wirePower", "buyPower")
        load = _num(x, "consumptionPower", "loadPower")
        pts.append((dt, grid, load))
    pts.sort()
    tot = work = 0.0
    for (a, g, ld), (b, _, _) in zip(pts, pts[1:]):
        step = min((b - a).total_seconds(), 15 * 60) / 3600          # розрив у даних — не більше 15 хв на точку
        if g is not None and abs(g) < 20 and (ld or 0) > 20:
            tot += step
            if w_from <= a.hour < w_to:
                work += step
    return round(tot, 2), round(work, 2)


def sync_deye(db: Session, days: int = 3, http: httpx.Client | None = None, since: date | None = None, overwrite: bool = False) -> dict:
    """Підтягнути з Deye дні (останні N або з дати since): кВт·год заряду / розряду і години без світла (5-хв дані).
    overwrite — перезаписати й дні, внесені вручну / перенесені (для звірки старих ручних даних)."""
    d = cfg(db)
    if not deye_ready(d):
        return {"ok": False, "msg": "Deye Cloud не підключено"}
    cl = Deye(d, http)
    sid = d["deye"].get("station_id")
    if not sid:
        st = cl.stations()
        if not st:
            return {"ok": False, "msg": "У Deye немає станцій"}
        sid = str(st[0].get("id"))
        d["deye"]["station_id"] = sid
        d["deye"]["station_name"] = st[0].get("name")
        save_cfg(db, d)
    end = date.today()
    start = since or (end - timedelta(days=days - 1))
    items = []
    a = start
    while a <= end:                                     # Deye віддає історію шматками — по 30 днів
        b = min(a + timedelta(days=29), end)
        items += cl.daily(sid, a, b)
        a = b + timedelta(days=1)
    n = 0
    for it in items:
        ts = _num(it, "timeStamp")
        if it.get("year") and it.get("month") and it.get("day"):
            day = date(int(it["year"]), int(it["month"]), int(it["day"]))
        elif ts:
            day = datetime.fromtimestamp(ts if ts < 1e12 else ts / 1000).date()
        else:
            continue
        if not (start <= day <= end):
            continue
        e = db.get(EnergyDay, day)
        if e and e.source == "manual" and not overwrite:   # внесене вручну Deye не перезаписує (хіба що просили)
            continue
        try:
            bh, wh = hours_from_frames(cl.frames(sid, day), int(d["work_from"]), int(d["work_to"]))
        except Exception as ex:  # noqa: BLE001
            log.warning("deye frames %s: %s", day, ex)
            bh = wh = None
        e = save_day(db, day, {"discharge_kwh": _num(it, "dischargeValue"), "charge_kwh": _num(it, "chargeValue"),
                               "battery_hours": bh, "business_hours": wh}, "deye")
        e.raw = {k: it.get(k) for k in list(it)[:30]}
        db.commit()
        n += 1
    d["deye"]["last_sync"] = datetime.now().isoformat(timespec="minutes")
    save_cfg(db, d)
    return {"ok": True, "msg": f"Оновлено днів: {n}", "n": n}
