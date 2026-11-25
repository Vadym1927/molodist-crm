"""Склади: «Налаштування → Склади». Вбудовані склади (вітрина, ін'єкційна, класична, одноразка, обладнання, онлайн-магазин)
можна перейменувати; свої склади — додати, перейменувати, видалити (лише порожній: без товарів і рухів).
Позначка «Продає інтернет-магазин» — з яких складів molodist.store бере наявність.

Довідники в коді (catalog.WAREHOUSES, price_monitor.WAREHOUSES, shop_front.SHOP_WHS) змінюються НА МІСЦІ, тому
всі модулі, що імпортували їх раніше, одразу бачать нові склади. apply() — під час старту й після кожного збереження."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Product, StockMove, SyncState

KEY = "warehouses"
BUILTIN = {"vitrina": "Вітрина", "inject": "Ін'єкційна", "classic": "Класична", "single": "Одноразка", "equip": "Обладнання",
           "shop": "Онлайн-магазин"}
SHOP_DEFAULT = ["vitrina", "shop"]
NO_PRICE_MONITOR = ("equip",)


def _raw(db: Session) -> dict:
    r = db.get(SyncState, KEY)
    try:
        d = json.loads(r.value) if r and r.value else {}
    except ValueError:
        d = {}
    d.setdefault("names", {})
    d.setdefault("custom", [])
    d.setdefault("shop", list(SHOP_DEFAULT))
    return d


def _store(db: Session, d: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def items(db: Session) -> list[dict]:
    d = _raw(db)
    out = [{"key": k, "name": (d["names"].get(k) or v), "builtin": True, "shop": k in d["shop"]} for k, v in BUILTIN.items()]
    out += [{"key": c["key"], "name": c["name"], "builtin": False, "shop": c["key"] in d["shop"]} for c in d["custom"]]
    return out


def usage(db: Session) -> dict[str, dict]:
    prods = dict(db.execute(select(Product.warehouse, func.count()).group_by(Product.warehouse)).all())
    moves = dict(db.execute(select(StockMove.warehouse, func.count()).group_by(StockMove.warehouse)).all())
    return {k: {"products": int(prods.get(k) or 0), "moves": int(moves.get(k) or 0)} for k in set(prods) | set(moves) if k}


def apply(db: Session) -> None:
    from . import catalog, price_monitor, shop_front
    its = items(db)
    catalog.WAREHOUSES.clear()
    catalog.WAREHOUSES.update({x["key"]: x["name"] for x in its})
    price_monitor.WAREHOUSES.clear()
    price_monitor.WAREHOUSES.update({x["key"]: x["name"] for x in its if x["key"] not in NO_PRICE_MONITOR})
    shop_front.SHOP_WHS[:] = [x["key"] for x in its if x["shop"]] or ["vitrina"]


def save(db: Session, key: str | None, name: str, shop: bool) -> str | None:
    """Новий склад (key порожній) або перейменування / позначка магазину. Повертає текст помилки або None."""
    name = " ".join((name or "").split())[:60]
    if not name:
        return "Вкажіть назву складу"
    d = _raw(db)
    names_now = {x["name"].lower(): x["key"] for x in items(db)}
    if names_now.get(name.lower()) not in (None, key):
        return f"Склад «{name}» уже є"
    if not key:
        n = 1
        used = {c["key"] for c in d["custom"]} | set(BUILTIN)
        while f"c{n}" in used:
            n += 1
        key = f"c{n}"
        d["custom"].append({"key": key, "name": name})
    elif key in BUILTIN:
        if name == BUILTIN[key]:
            d["names"].pop(key, None)
        else:
            d["names"][key] = name
    else:
        c = next((c for c in d["custom"] if c["key"] == key), None)
        if not c:
            return "Склад не знайдено"
        c["name"] = name
    d["shop"] = [k for k in d["shop"] if k != key] + ([key] if shop else [])
    if not d["shop"]:
        return "Хоча б один склад має продавати в інтернет-магазині"
    _store(db, d)
    apply(db)
    return None


def delete(db: Session, key: str) -> str | None:
    if key in BUILTIN:
        return "Вбудований склад не видаляється — його можна перейменувати"
    d = _raw(db)
    if not any(c["key"] == key for c in d["custom"]):
        return "Склад не знайдено"
    u = usage(db).get(key) or {}
    if u.get("products") or u.get("moves"):
        return "На складі є товари або рухи — спершу перенесіть товари на інший склад"
    d["custom"] = [c for c in d["custom"] if c["key"] != key]
    d["shop"] = [k for k in d["shop"] if k != key] or list(SHOP_DEFAULT)
    _store(db, d)
    apply(db)
    return None
