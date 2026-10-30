"""Поля співробітників (як у CleverBox «Поля співробітників»): свої поля для картки спеціаліста.
SyncState staff_fields: [{id, key, name, type, required, on, sys, options}]. sys — вбудоване поле картки (не видаляється);
власні поля зберігаються в Doctor.extra[key]."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "staff_fields"
TYPES = {"text": "Текст", "phones": "Кілька значень (телефони)", "textarea": "Довгий текст", "number": "Число", "date": "Дата",
         "select": "Список (одне значення)", "multiselect": "Список (кілька значень)", "checkbox": "Так / ні"}
SYS = [("name", "Імʼя та прізвище", "text", True), ("position", "Посада", "select", False), ("phone", "Телефон", "text", False),
       ("email", "Email", "text", False), ("in_calendar", "Показувати в календарі", "checkbox", False), ("bio", "Короткий опис", "textarea", False)]
SEED = [("patronymic", "По-батькові", "text", []), ("phones", "Додаткові телефони", "phones", []), ("start_work", "Дата початку роботи", "date", []),
        ("passport", "Паспорт", "text", []), ("tax_code", "Код (ІПН)", "text", []), ("birthday", "Дата народження", "date", []),
        ("medbook", "Дата закінчення терміну медкнижки", "date", []), ("salary", "Зарплата", "select", ["Оклад", "Відсоток", "Оклад + відсоток"]),
        ("clinic", "Клініка", "multiselect", []), ("gender", "Стать", "select", ["Жіноча", "Чоловіча"]), ("directions", "Направлення", "multiselect", [])]


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    xs, n = [], 0
    order = ["name", "patronymic", "phone", "phones", "start_work", "passport", "tax_code", "email", "birthday", "medbook", "salary",
             "position", "in_calendar", "clinic", "gender", "bio", "directions"]
    allf = {k: {"key": k, "name": nm, "type": t, "required": r, "on": True, "sys": True, "options": []} for k, nm, t, r in SYS}
    allf.update({k: {"key": k, "name": nm, "type": t, "required": False, "on": True, "sys": False, "options": o} for k, nm, t, o in SEED})
    for k in order:
        n += 1
        xs.append({"id": n, **allf[k]})
    _save(db, xs)
    return xs


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def custom(db: Session) -> list[dict]:
    """Власні увімкнені поля — для картки спеціаліста."""
    return [x for x in items(db) if not x.get("sys") and x.get("on", True)]


def _slug(name: str, taken: set) -> str:
    base = "f_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "field")
    k, i = base, 1
    while k in taken:
        i += 1
        k = f"{base}_{i}"
    return k


def save(db: Session, fid: int | None, name: str, typ: str, required: bool, options: str) -> str | None:
    name = " ".join((name or "").split())[:80]
    if not name:
        return "Вкажіть назву поля"
    xs = items(db)
    opts = [o.strip()[:80] for o in (options or "").splitlines() if o.strip()][:60]
    x = next((x for x in xs if fid and x["id"] == fid), None)
    if x:
        x["name"] = name
        if not x.get("sys"):
            x["type"] = typ if typ in TYPES else x["type"]
            x["options"] = opts
        if x["key"] != "name":
            x["required"] = required
    else:
        if typ not in TYPES:
            return "Оберіть тип поля"
        xs.append({"id": max([y["id"] for y in xs] + [0]) + 1, "key": _slug(name, {y["key"] for y in xs}), "name": name, "type": typ,
                   "required": required, "on": True, "sys": False, "options": opts})
    _save(db, xs)
    return None


def toggle(db: Session, fid: int) -> None:
    xs = items(db)
    for x in xs:
        if x["id"] == fid and x["key"] != "name":
            x["on"] = not x.get("on", True)
    _save(db, xs)


def delete(db: Session, fid: int) -> None:
    _save(db, [x for x in items(db) if x["id"] != fid or x.get("sys")])


def reorder(db: Session, ids: list[int]) -> None:
    xs = items(db)
    pos = {i: n for n, i in enumerate(ids)}
    xs.sort(key=lambda x: pos.get(x["id"], 10_000))
    _save(db, xs)


def read_form(fields: list[dict], form) -> dict:
    out = {}
    for f in fields:
        k = f"x_{f['key']}"
        if f["type"] == "multiselect":
            out[f["key"]] = [v for v in form.getlist(k) if v][:60]
        elif f["type"] == "phones":
            out[f["key"]] = [v.strip()[:40] for v in form.getlist(k) if v.strip()][:10]
        elif f["type"] == "checkbox":
            out[f["key"]] = form.get(k) == "1"
        else:
            out[f["key"]] = str(form.get(k) or "").strip()[:2000]
    return out
