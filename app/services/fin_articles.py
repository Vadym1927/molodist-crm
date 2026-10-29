"""Статті фінансових операцій — довідник для класифікації доходів і витрат (як у CleverBox «Статті фінансових операцій»).
SyncState fin_articles: [{id, name, kind income|expense, active, salary}]. Стартовий набір — статті CleverBox з тими ж ID.
«salary» — стаття зарплати (нарахування з/п і взаєморозрахунки рахують виплати за нею)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinOp, SyncState

KEY = "fin_articles"
KINDS = {"income": "Прихід", "expense": "Витрата"}
SALARY_DEFAULT = "Персонал Зарплата"
SEED = [
    (104, "Оплата послуг, товарів", "income"),
    (105, "Внесення коштів в касу", "income"),
    (314, "Пополнение баланса", "income"),
    (190, "Інші оплати", "income"),
    (337, "Девіденди", "expense"),
    (355, "Повернення пацієнтам", "expense"),
    (153, "Оренда прим.Клініки", "expense"),
    (930, "Комунальні прим.клініки", "expense"),
    (993, "Інша оренда (приміщення зберігання )", "expense"),
    (924, "Реклама Google Ads", "expense"),
    (159, "Реклама Таргет Instagram", "expense"),
    (161, "Реклама Зовнішня", "expense"),
    (925, "Реклама СМС Розсилка", "expense"),
    (166, "Реклама Інша", "expense"),
    (927, "Реклама Barb", "expense"),
    (158, "Маркетинг Фото/відео контент", "expense"),
    (181, "Маркетинг Таргетолог", "expense"),
    (182, "Маркетинг СММ", "expense"),
    (315, "Маркетинг Маркетолог", "expense"),
    (1232, "Маркетинг Брендовані матерали", "expense"),
    (1233, "Маркетинг Друковані матеріали", "expense"),
    (859, "Програми Clever Box", "expense"),
    (162, "Програми Checkbox", "expense"),
    (172, "Програми Телефонія Ringostat", "expense"),
    (1128, "Програми Вчасно", "expense"),
    (160, "Сервіси та підписки Google", "expense"),
    (1065, "Сервіси та підписки Helsi", "expense"),
    (926, "Сервіси та підписки Shop-Express", "expense"),
    (1219, "Сервіси та підписки Tilda", "expense"),
    (1218, "Сервіси та підписки ukraine.com.ua", "expense"),
    (1191, "Сервіси та підписки OpenAI", "expense"),
    (1006, "Cервіси та підписки АІ агент", "expense"),
    (187, "Комісія банку MonoBank", "expense"),
    (188, "Комісія банку Еквайринг MonoBank", "expense"),
    (1126, "Комісія Банку Інша", "expense"),
    (906, "Податки ЄСВ ФОП Колган", "expense"),
    (1220, "Податки ЄСВ ФОП Бондар", "expense"),
    (931, "Податки ЄСВ на ЗП ФОП Колган", "expense"),
    (1221, "Податки ЄСВ на ЗП ФОП Бондар", "expense"),
    (907, "Податки ЄП ФОП Колган", "expense"),
    (1222, "Податки ЄП ФОП Бондар", "expense"),
    (909, "Податки ВЗ ФОП Колган", "expense"),
    (908, "Податки ВЗ ФОП Бондар", "expense"),
    (932, "Податки ПДФО на ЗП ФОП Колган", "expense"),
    (1223, "Податки ПДФО на ЗП ФОП Бондар", "expense"),
    (1057, "Податки ВЗ на ЗП ФОП Колган", "expense"),
    (1224, "Податки ВЗ на ЗП ФОП Бондар", "expense"),
    (1133, "Капітальні витрати Обладнення в кабінети", "expense"),
    (168, "Капітальні витрати Меблі в кабінети", "expense"),
    (170, "Капітальні витрати Ремонтні роботи прим. клініки", "expense"),
    (1231, "Капітальні витрати Адміністративні", "expense"),
    (171, "Капітальні витрати Адміністративне обладнення", "expense"),
    (1227, "Капітальні витрати Інше", "expense"),
    (1144, "Амортизація обладнання ТО Апаратів", "expense"),
    (1122, "Амортизація обладнання Ремонт Апаратів", "expense"),
    (1225, "Амортизація Ремонт техніки адміністративної", "expense"),
    (1226, "Амортизація обладнення Інша", "expense"),
    (1138, "Разові витрати Адміністративні послуги", "expense"),
    (1010, "Разові витрати Навчання Персоналу", "expense"),
    (923, "Разові витрати Допомога ЗСУ", "expense"),
    (928, "Разові витрати Навчання Колган Д.О.", "expense"),
    (156, "Персонал Зарплата", "expense"),
    (950, "Персонал", "expense"),
    (1228, "Персонал Корпоратив", "expense"),
    (1229, "Персонал ДР", "expense"),
    (1230, "Персонал Інше", "expense"),
    (1234, "Господарські Адміністративні", "expense"),
    (1239, "Господарські Кабінет", "expense"),
    (1132, "Господарські матеріал для відправок", "expense"),
    (1127, "Господарські Канцелярія", "expense"),
    (1109, "Господарські Кава ,Чай", "expense"),
    (922, "Господарські Молоко , Рослинне Молоко", "expense"),
    (1190, "Госоподарські Raffaello", "expense"),
    (1235, "Господарські Вода", "expense"),
    (1236, "Господарські Одноразовий посуд", "expense"),
    (1237, "Господарські одн. мат. адмістративні", "expense"),
    (1238, "Господарські одн.мат. кабінети", "expense"),
    (978, "Господарські Хімчистка", "expense"),
    (933, "Господарські Утилізація (по Договору )", "expense"),
    (1240, "Господарські НП Відправки", "expense"),
    (911, "Господарські Доставка НП", "expense"),
    (184, "Господарські Доставки", "expense"),
    (1241, "Господарські Інше", "expense"),
    (738, "Підрядники Юрист", "expense"),
    (1125, "Підрядники Інші", "expense"),
    (1242, "Господарські Інтернет", "expense"),
    (981, "Господарські Бензин Генератор", "expense"),
    (304, "Фінансові Фін.корегування", "expense"),
    (255, "Закупка товара", "expense"),
    (1243, "Закупка розхідні матеріали ін'єкційна косметологія", "expense"),
    (1244, "Закупка розхідні матеріали класична косметологія", "expense"),
    (1245, "Закупка одноразові матеріали списання", "expense"),
    (1246, "Закупка косметика вітрина", "expense"),
    (1247, "Закупка розхідники апарати", "expense"),
    (1248, "Аптечки невідкладної допомоги", "expense"),
    (1249, "Аптечки Staff", "expense"),
    (1252, "Зобовʼязання Розтермінування MONOBANK", "expense"),
    (1253, "Зобовʼязання Розтермінування PRIVATBANK", "expense"),
    (1254, "Зобовʼязання Інвестиційний Фонд", "expense"),
    (1105, "РУСОРІЗ", "expense"),
    (185, "Хоз расходы", "expense"),
    (155, "Податки", "expense"),
    (154, "Бухгалтерія", "expense"),
    (169, "Техника", "expense"),
    (189, "Оплата обучения", "income"),
    (935, "Консультації", "expense"),
    (1108, "Виплата прибутку через ЗП", "expense"),
    (1112, "\"МЛ \"ДІЛА\"", "income"),
    (1121, "Девіденди", "expense"),
    (1135, "Ringostat", "expense"),
    (1278, "Адміністративні Мобільний Звʼязок", "expense"),
    (1279, "Податки інші", "expense")
]
# статті, що вже використовувались у CRM до довідника
LEGACY = [("Зарплата", "expense"), ("Оренда", "expense"), ("Комунальні послуги", "expense"), ("Закупівля товару", "expense"),
          ("Витратні матеріали", "expense"), ("Маркетинг і реклама", "expense"), ("Внесення в касу", "income"),
          ("Повернення від постачальника", "income"), ("Інше надходження", "income"), ("Онлайн-оплата", "income")]


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    out = [{"id": i, "name": n, "kind": k, "active": True, "salary": n == SALARY_DEFAULT} for i, n, k in SEED]
    have = {x["name"] for x in out}
    used = {c for (c,) in db.execute(select(FinOp.category).distinct()) if c}
    nid = 100000
    for n, k in LEGACY:
        if n not in have and (n in used or n in ("Онлайн-оплата",)):
            nid += 1
            out.append({"id": nid, "name": n, "kind": k, "active": True, "salary": n == "Зарплата"})
    return out


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def names(db: Session, kind: str, active_only: bool = True) -> list[str]:
    return [x["name"] for x in items(db) if x["kind"] == kind and (x.get("active") or not active_only)]


def salary_names(db: Session) -> list[str]:
    s = [x["name"] for x in items(db) if x.get("salary")]
    return s or ["Зарплата"]


def salary_name(db: Session) -> str:
    act = [x["name"] for x in items(db) if x.get("salary") and x.get("active")]
    return (act or salary_names(db))[0]


def save(db: Session, aid: int | None, name: str, kind: str, active: bool, salary: bool) -> dict:
    xs = items(db)
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Вкажіть назву статті")
    if kind not in KINDS:
        raise ValueError("Оберіть тип: прихід або витрата")
    if any(x["name"].lower() == name.lower() and x["kind"] == kind and int(x["id"]) != int(aid or 0) for x in xs):
        raise ValueError("Така стаття вже є")
    x = next((x for x in xs if aid and int(x["id"]) == int(aid)), None)
    if not x:
        x = {"id": max([int(y["id"]) for y in xs] + [0]) + 1}
        xs.append(x)
    old = x.get("name")
    x.update(name=name, kind=kind, active=active, salary=salary and kind == "expense")
    _save(db, xs)
    if old and old != name:                                    # перейменування — переносимо операції CRM
        for op in db.scalars(select(FinOp).where(FinOp.category == old)):
            op.category = name
        db.commit()
    return x


def usage(db: Session) -> dict[str, int]:
    from sqlalchemy import func
    from ..models import CbFinOp
    out: dict[str, int] = {}
    for c, n in db.execute(select(FinOp.category, func.count()).where(FinOp.deleted_at.is_(None)).group_by(FinOp.category)):
        out[c] = out.get(c, 0) + n
    for c, n in db.execute(select(CbFinOp.article, func.count()).group_by(CbFinOp.article)):
        if c:
            out[c] = out.get(c, 0) + n
    return out


def delete(db: Session, aid: int) -> bool:
    """Без операцій — видаляємо; з операціями — лише вимикаємо (історія лишається). True — видалено."""
    xs = items(db)
    x = next((x for x in xs if int(x["id"]) == int(aid)), None)
    if not x:
        return True
    if usage(db).get(x["name"]):
        x["active"] = False
        _save(db, xs)
        return False
    _save(db, [y for y in xs if y is not x])
    return True


def group(name: str) -> str:
    w = name.split(" ")[0]
    return w if w and w[0].isupper() else "Інше"
