"""Оклади співробітників (як у CleverBox «Налаштування окладів»): шаблони нарахування зарплати.
SyncState salary_plans: [plan]. Співробітнику оклад призначається в картці (поле «Зарплата» = назва окладу).

plan = {id, name,
  fixed: {on, amount, period day|month|hour},                       — фіксована ставка
  comm:  {on, min_day, sub_cost, cost_src techcard|fixed, no_disc,   — комісійні за послуги й продажі
          services:[%, mode], products:[%, mode], certs:[%, mode], subs:[%, mode],
          items:[{kind service|service_cat|product|product_cat|brand, name, val, mode}]},
  turn:  {on, workdays_only, services:[%, mode], products, certs, subs},   — % від загального обороту
  bonus: {on, mode fixed|pct, visit, checkout}}                     — бонуси за запис / розрахунок (налаштування)
mode: cost — % від вартості, paid — % від оплати, fixed — фіксована сума за одиницю."""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "salary_plans"
MODES = {"cost": "% від вартості", "paid": "% від оплати", "fixed": "фіксована сума"}
PERIODS = {"day": "За день", "month": "За місяць", "hour": "За годину"}
ITEM_KINDS = {"service": "Послуга", "service_cat": "Категорія послуг", "product": "Товар", "product_cat": "Категорія товарів", "brand": "Бренд"}
TYPES = ("services", "products", "certs", "subs")
LINE_TYPE = {"service": "services", "product": "products", "certificate": "certs", "subscription": "subs"}


def blank(name: str = "") -> dict:
    z = lambda m="cost": [0.0, m]  # noqa: E731
    return {"name": name,
            "fixed": {"on": False, "amount": 0.0, "period": "day"},
            "comm": {"on": False, "min_day": 0.0, "sub_cost": False, "cost_src": "techcard", "no_disc": False,
                     "services": z(), "products": z("paid"), "certs": z(), "subs": z(), "items": []},
            "turn": {"on": False, "workdays_only": False, "services": z(), "products": z(), "certs": z(), "subs": z()},
            "bonus": {"on": False, "mode": "fixed", "visit": 0.0, "checkout": 0.0}}


SEED = ["Дарина 2026 NEW", "Марина 2026 NEW", "Софія 2026 NEW", "Анна 2026 NEW", "CSD", "Адмін Sales 2026 NEW",
        "Адмін Sales 2026 NEW Стажор", "Адмін Sales 2026 NEW Стажування 2 рівень", "Колган 2026 NEW"]


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return [_norm(x) for x in json.loads(st.value)]
        except ValueError:
            pass
    xs = []
    for i, n in enumerate(SEED, 1):
        p = {"id": i, **blank(n)}
        if n.endswith("Стажування 2 рівень"):                 # з налаштувань CleverBox: 1200 ₴ за день
            p["fixed"] = {"on": True, "amount": 1200.0, "period": "day"}
        xs.append(p)
    _save(db, xs)
    return xs


def _norm(x: dict) -> dict:
    b = blank(x.get("name", ""))
    for k in ("fixed", "comm", "turn", "bonus"):
        b[k].update(x.get(k) or {})
    return {**x, **b}


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def names(db: Session) -> list[str]:
    return [x["name"] for x in items(db)]


def by_name(db: Session, name: str | None) -> dict | None:
    return next((x for x in items(db) if name and x["name"] == name), None)


def _f(v) -> float:
    try:
        return round(float(str(v).replace(",", ".").replace(" ", "")), 4)
    except (TypeError, ValueError):
        return 0.0


def from_form(f) -> dict:
    """Форма редагування → план."""
    p = blank(str(f.get("name") or "").strip()[:120])
    on = lambda k: f.get(k) == "1"  # noqa: E731
    mode = lambda k: f.get(k) if f.get(k) in MODES else "cost"  # noqa: E731
    p["fixed"] = {"on": on("fixed_on"), "amount": _f(f.get("fixed_amount")), "period": f.get("fixed_period") if f.get("fixed_period") in PERIODS else "day"}
    c = p["comm"]
    c.update(on=on("comm_on"), min_day=_f(f.get("comm_min_day")), sub_cost=on("comm_sub_cost"),
             cost_src="fixed" if f.get("comm_cost_src") == "fixed" else "techcard", no_disc=on("comm_no_disc"))
    for t in TYPES:
        c[t] = [_f(f.get(f"comm_{t}")), mode(f"comm_{t}_mode")]
        p["turn"][t] = [_f(f.get(f"turn_{t}")), mode(f"turn_{t}_mode")]
    c["items"] = [{"kind": k, "name": str(n).strip()[:200], "val": _f(v), "mode": m if m in MODES else "cost"}
                  for k, n, v, m in zip(f.getlist("it_kind"), f.getlist("it_name"), f.getlist("it_val"), f.getlist("it_mode"))
                  if k in ITEM_KINDS and str(n).strip()][:300]
    p["turn"].update(on=on("turn_on"), workdays_only=on("turn_workdays"))
    p["bonus"] = {"on": on("bonus_on"), "mode": "pct" if f.get("bonus_mode") == "pct" else "fixed",
                  "visit": _f(f.get("bonus_visit")), "checkout": _f(f.get("bonus_checkout"))}
    return p


def save(db: Session, pid: int | None, plan: dict) -> str | None:
    if not plan["name"]:
        return "Вкажіть назву окладу"
    xs = items(db)
    if any(x["name"].lower() == plan["name"].lower() and x["id"] != pid for x in xs):
        return "Оклад з такою назвою вже є"
    x = next((x for x in xs if pid and x["id"] == pid), None)
    if x:
        old = x["name"]
        x.update(plan)
        if old != plan["name"]:
            _rename_in_cards(db, old, plan["name"])
    else:
        xs.append({"id": max([y["id"] for y in xs] + [0]) + 1, **plan})
    _save(db, xs)
    return None


def _rename_in_cards(db: Session, old: str, new: str) -> None:
    from sqlalchemy import select
    from ..models import Doctor
    for d in db.scalars(select(Doctor).where(Doctor.extra.is_not(None))):
        if (d.extra or {}).get("salary") == old:
            d.extra = {**d.extra, "salary": new}


def copy_plan(db: Session, pid: int) -> int | None:
    xs = items(db)
    x = next((x for x in xs if x["id"] == pid), None)
    if not x:
        return None
    n, base = 2, x["name"] + " (копія)"
    name = base
    while any(y["name"] == name for y in xs):
        name, n = f"{base} {n}", n + 1
    nid = max(y["id"] for y in xs) + 1
    xs.append({**copy.deepcopy(x), "id": nid, "name": name})
    _save(db, xs)
    return nid


def delete(db: Session, pid: int) -> None:
    _save(db, [x for x in items(db) if x["id"] != pid])


def reorder(db: Session, ids: list[int]) -> None:
    xs = items(db)
    pos = {i: n for n, i in enumerate(ids)}
    xs.sort(key=lambda x: pos.get(x["id"], 10_000))
    _save(db, xs)


# ---------------- розрахунок ----------------

def _rate(p: dict, ln: dict) -> tuple[float, str]:
    c = p["comm"]
    for it in c["items"]:                               # окремі ставки: послуга / категорія / товар / бренд
        k, nm = it["kind"], it["name"].lower()
        if ((k == "service" and ln["type"] == "service" and ln["name"].lower() == nm)
                or (k == "service_cat" and ln["type"] == "service" and (ln.get("category") or "").lower() == nm)
                or (k == "product" and ln["type"] == "product" and ln["name"].lower() == nm)
                or (k == "product_cat" and ln["type"] == "product" and (ln.get("category") or "").lower() == nm)
                or (k == "brand" and ln["type"] == "product" and (ln.get("brand") or "").lower() == nm)):
            return it["val"], it["mode"]
    v, m = c.get(LINE_TYPE.get(ln["type"], "services"), [0, "cost"])
    return float(v or 0), m


def _amt(base_cost: float, base_paid: float, qty: float, v: float, mode: str) -> float:
    if mode == "fixed":
        return v * (qty or 1)
    return (base_paid if mode == "paid" else base_cost) * v / 100


def accrue(p: dict, lines: list[dict], days: set, hours: float, clinic: list[dict]) -> dict:
    """lines — позиції співробітника за місяць: {day, type service|product|certificate|subscription, name, category, brand,
    cost (вартість), paid (оплачено), qty, disc}; days — робочі дні (графік або дні з продажами); clinic — усі позиції клініки."""
    out = {"fixed": 0.0, "comm": 0.0, "comm_services": 0.0, "comm_products": 0.0, "turn": 0.0}
    fx = p["fixed"]
    if fx["on"]:
        out["fixed"] = fx["amount"] if fx["period"] == "month" else (fx["amount"] * hours if fx["period"] == "hour" else fx["amount"] * len(days))
    c = p["comm"]
    if c["on"]:
        per_day: dict = defaultdict(float)
        for ln in lines:
            if c["no_disc"] and ln["type"] == "product" and ln.get("disc"):
                continue
            v, m = _rate(p, ln)
            a = _amt(ln["cost"], ln["paid"], ln.get("qty") or 1, v, m)
            per_day[ln["day"]] += a
            out["comm_services" if ln["type"] == "service" else "comm_products"] += a
        tot = sum(per_day.values())
        if c["min_day"] > 0:                            # мінімум за день: більше з мінімуму чи відсотка за день
            tot = sum(max(c["min_day"], per_day.get(d, 0.0)) for d in (set(per_day) | set(days)))
        out["comm"] = tot
    t = p["turn"]
    if t["on"]:
        for ln in clinic:
            if t["workdays_only"] and ln["day"] not in days:
                continue
            v, m = t.get(LINE_TYPE.get(ln["type"], "services"), [0, "cost"])
            out["turn"] += _amt(ln["cost"], ln["paid"], ln.get("qty") or 1, float(v or 0), m)
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(out["fixed"] + out["comm"] + out["turn"], 2)
    return out
