"""Доступ до розділів, вкладок і кнопок — галочками для кожної ролі (поверх дозволів ролі).

Кожен елемент інтерфейсу прив'язаний до адрес, на які він веде (метод + шлях + параметр вкладки).
Заборонене ролі: сервер відповідає «Немає доступу» (не обійти прямим посиланням), а в інтерфейсі такі посилання,
вкладки й кнопки ховаються автоматично (base.html → window.UI_DENY).
За замовчуванням усе дозволено (діють лише дозволи ролі). Власник обмежень не має.
Зберігається в SyncState ui_access: {"deny": {role: [key, …]}}; у пам'яті процесу — кеш."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "ui_access"
N = r"\d+"

# (key, назва, [(метод, regex шляху, (параметр, значення, за_замовчуванням) | None)])
def _g(path, q=None):
    return ("GET", path, q)


def _p(path, q=None):
    return ("POST", path, q)


_STOCK = [("balances", "Залишки"), ("critical", "Критичні залишки"), ("supply", "Поставки"), ("order", "Замовлення"),
          ("transfer", "Переміщення"), ("writeoff", "Списання"), ("return", "Повернення"), ("inventory", "Інвентаризація"),
          ("suppliers", "Постачальники"), ("norms", "Норми списання"), ("moves", "Рух товарів")]
_FIN = [("/finance$", "fin_overview", "Огляд фінансів"), ("/finance/ops", "fin_ops", "Фіноперації"),
        ("/finance/accruals", "fin_accruals", "Нарахування зарплати"), ("/finance/payroll", "fin_payroll", "ЗП за місяць"),
        ("/finance/settle", "fin_settle", "Взаєморозрахунки по з/п"), ("/finance/day", "fin_day", "Продажі за день"),
        ("/finance/sales", "fin_sales", "Продажі"), ("/finance/balances", "fin_balances", "Баланс пацієнтів"),
        ("/finance/bonuses", "fin_bonuses", "Рух бонусів"), ("/finance/pricing", "fin_pricing", "Планування ціни реалізації"),
        ("/payreq", "fin_payreq", "Рахунки на оплату")]
_REP = [("cash", "Стан рахунків"), ("cashflow", "Cashflow"), ("fop", "Ліміти ФОП"), ("expiry", "Терміни придатності"), ("revenue", "Виручка"),
        ("doctors", "Лікарі"), ("services", "Звіт з продажу послуг"), ("products", "Товари"), ("patients", "Пацієнти"),
        ("plans", "Бюті-плани"), ("certificates", "Сертифікати й абонементи"), ("supply", "Поставки"), ("cosmetics", "% по косметиці"), ("sources", "Джерела запису")]

GROUPS: list[tuple[str, list]] = [
    ("Розділи меню", [
        ("sec_dashboard", "Дашборд", [_g(r"/$")]),
        ("sec_calendar", "Записи (календар)", [_g(r"/calendar$")]),
        ("sec_patients", "Пацієнти (список)", [_g(r"/clients"), _g(r"/patients$")]),
        ("sec_appeals", "Звернення", [_g(r"/appeals")]),
        ("sec_plans", "Бюті-плани", [_g(r"/plans")]),
        ("sec_tasks", "Задачі", [_g(r"/tasks")]),
        ("sec_products", "Продукти", [_g(r"/products")]),
        ("sec_stock", "Склад", [_g(r"/stock$")]),
        ("sec_services", "Послуги (прайс)", [_g(r"/services")]),
        ("sec_techcards", "Техкарти", [_g(r"/techcards")]),
        ("sec_prices", "Моніторинг цін", [_g(r"/prices")]),
        ("sec_finance", "Фінанси", [_g(r"/finance")]),
        ("sec_sales", "Каса CRM (список чеків)", [_g(r"/sales$"), _g(r"/sales/archive")]),
        ("sec_reports", "Звіти", [_g(r"/reports")]),
        ("sec_team", "Команда (спеціалісти)", [_g(r"/specialists")]),
        ("sec_gifts", "Сертифікати й абонементи", [_g(r"/gifts")]),
        ("sec_campaigns", "Розсилки", [_g(r"/campaigns")]),
        ("sec_scripts", "Скрипти", [_g(r"/scripts")]),
        ("sec_data", "Дані й синхронізація", [_g(r"/data")]),
        ("sec_settings", "Налаштування", [_g(r"/settings$")]),
    ]),
    ("Записи й візити", [
        ("cal_create", "Створювати й змінювати записи", [_g(r"/appointments/new"), _p(r"/appointments$")]),
        ("cal_status", "Змінювати статус візиту (прийшов, не прийшов, скасовано)", [_p(rf"/appointments/{N}/status")]),
        ("cal_materials", "Змінювати витратні матеріали візиту", [_p(rf"/appointments/{N}/materials")]),
        ("cal_blocks", "Блокувати час у календарі", [_p(r"/calendar/blocks")]),
        ("cal_note", "Нотатки до візиту", [_p(r"/calendar/visit/note")]),
    ]),
    ("Пацієнти", [
        ("pt_card", "Відкривати картку пацієнта", [_g(r"/patients/\d+$")]),
        ("pt_create", "Створювати пацієнта", [_p(r"/patients$")]),
        ("pt_edit", "Редагувати пацієнта", [_p(rf"/patients/{N}$")]),
        ("pt_merge", "Об'єднувати дублікати", [_p(rf"/patients/{N}/merge")]),
        ("pt_medcard", "Медична картка", [_g(rf"/patients/{N}/medcard"), _p(rf"/patients/{N}/medcard")]),
        ("pt_photos", "Фото пацієнта", [_g(rf"/patients/{N}/photos"), _p(rf"/patients/{N}/photos")]),
        ("pt_photos_lib", "Налаштування → Фото пацієнтів", [_g(r"/settings/patient-photos")]),
        ("pt_photos_cat", "Переносити фото між розділами", [_p(r"/files/category")]),
        ("pt_photos_del", "Видаляти фото пацієнтів", [_p(r"/files/delete")]),
        ("pt_files_zip", "Завантажувати файли пацієнта архівом", [_g(rf"/patients/{N}/files\.zip")]),
        ("pt_print", "Друк картки", [_g(rf"/patients/{N}/print")]),
        ("pt_bonus", "Нараховувати бонуси, персональна знижка", [_p(rf"/patients/{N}/bonus"), _p(rf"/patients/{N}/discount")]),
        ("pt_pdisc", "Налаштування → Персональні знижки", [_g(r"/settings/personal-discounts"), _p(r"/settings/personal-discounts")]),
        ("pt_message", "Писати пацієнту", [_p(rf"/patients/{N}/message")]),
        ("pt_upload", "Завантажувати бюті-план (PDF)", [_p(r"/upload$")]),
        ("plan_edit", "Редагувати бюті-план", [_p(rf"/plans/{N}/items")]),
        ("plan_delete", "Видаляти бюті-план", [_p(rf"/plans/{N}/delete")]),
    ]),
    ("Каса", [
        ("sale_new", "Новий продаж (каса)", [_g(r"/sales/new"), _p(r"/sales$")]),
        ("sale_view", "Відкривати чек", [_g(rf"/sales/{N}$")]),
        ("sale_refund", "Повернення чека", [_p(rf"/sales/{N}/refund")]),
        ("sale_cancel", "Скасування чека", [_p(rf"/sales/{N}/cancel")]),
        ("sale_archive", "Архівувати чек", [_p(rf"/sales/{N}/archive")]),
        ("sale_fiscal", "Фіскалізувати / надіслати чек", [_p(rf"/sales/{N}/fiscalize"), _p(rf"/sales/{N}/send-receipt")]),
        ("sale_paylink", "Посилання на оплату", [_p(rf"/sales/{N}/paylink"), _p(r"/paylinks")]),
        ("sale_shift", "Відкривати й закривати касову зміну", [_p(r"/shift/")]),
        ("sale_rro", "Каса Checkbox: зміна, внесення / винесення, X- і Z-звіт", [_p(r"/rro/")]),
    ]),
    ("Фінанси — вкладки", [(k, l, [_g(p)]) for p, k, l in _FIN]),
    ("Фінанси — дії", [
        ("fin_op_edit", "Додавати й змінювати фіноперації", [_p(r"/finance/ops$")]),
        ("fin_op_delete", "Видаляти / сторнувати фіноперації", [_p(rf"/finance/ops/{N}/delete"), _p(r"/finance/ops/storno")]),
        ("fin_op_import", "Імпорт фінооперацій", [_p(r"/finance/ops/import")]),
        ("fin_op_export", "Експорт фінооперацій у Excel", [_g(r"/finance/ops/export\.xlsx")]),
        ("fin_accrual_make", "Створювати нарахування зарплати", [_g(r"/finance/accruals/new"), _p(r"/finance/accruals$")]),
        ("fin_accrual_post", "Проводити зарплату на баланс", [_p(rf"/finance/accruals/{N}/post")]),
        ("fin_accrual_delete", "Видаляти нарахування", [_p(rf"/finance/accruals/{N}/delete")]),
        ("fin_payroll_adj", "Премії й утримання", [_p(r"/finance/payroll/adj"), _p(r"/finance/payroll/rule")]),
    ]),
    ("Склад — вкладки", [(f"stock_{k}", l, [_g(r"/stock$", ("tab", k, "balances"))]) for k, l in _STOCK]),
    ("Склад — дії", [
        ("stock_doc_create", "Створювати документи (поставка, списання, переміщення…)", [_p(r"/stock/docs$"), _p(r"/stock/docs/quick")]),
        ("stock_doc_edit", "Змінювати й проводити документи", [_p(rf"/stock/docs/{N}$")]),
        ("stock_doc_pay", "Оплачувати поставки", [_p(rf"/stock/docs/{N}/pay"), _p(rf"/stock/invoices/{N}/paid")]),
        ("stock_doc_archive", "Архівувати / відновлювати документи", [_p(r"/stock/docs/\d+/archive"), _p(r"/stock/docs/\d+/restore")]),
        ("stock_import", "Імпорт залишків і звірка", [_p(r"/stock/balances/"), _p(r"/stock/start")]),
        ("stock_suppliers_edit", "Змінювати постачальників", [_p(r"/stock/suppliers")]),
        ("stock_norms_edit", "Змінювати норми списання", [_p(r"/stock/norms")]),
        ("stock_mono", "monobank: підключення й синхронізація", [_p(r"/stock/mono/")]),
        ("product_edit", "Змінювати товари й ціни", [_p(rf"/products/{N}$"), _p(r"/products/bulk"), _p(rf"/products/{N}/purchases")]),
        ("product_import", "Імпорт товарів", [_p(r"/products/import")]),
        ("service_edit", "Змінювати послуги й прайс", [_p(r"/services$")]),
        ("techcard_edit", "Змінювати техкарти", [_p(r"/techcards$"), _p(r"/techcards/import-cb")]),
        ("techcard_delete", "Видаляти техкарти", [_p(rf"/techcards/{N}/delete")]),
        ("prices_settings", "Налаштування моніторингу цін", [_p(r"/prices/settings"), _p(r"/prices/accept")]),
    ]),
    ("Звіти", [("rep_hub", "Сторінка всіх звітів", [_g(r"/reports$", ("r", "", ""))])]
     + [(f"rep_{k}", l, [_g(r"/reports$", ("r", k, ""))]) for k, l in _REP]
     + [("rep_export", "Вивантажувати звіти (Excel / CSV)", [_g(r"/export/report\.")])]),
    ("Команда", [
        ("team_card", "Картка спеціаліста", [_g(rf"/specialists/{N}$")]),
        ("team_edit", "Змінювати спеціалістів", [_p(r"/specialists$"), _p(rf"/specialists/{N}")]),
        ("team_schedule", "Графік роботи (перегляд)", [_g(r"/specialists/schedule")]),
        ("team_schedule_edit", "Змінювати графік", [_p(r"/specialists/schedule/")]),
    ]),
    ("Задачі й звернення", [
        ("task_edit", "Створювати й змінювати задачі", [_p(r"/tasks")]),
        ("appeal_edit", "Створювати й обробляти звернення", [_p(r"/appeals")]),
        ("campaign_launch", "Запускати розсилки", [_p(r"/campaigns/[^/]+/launch")]),
    ]),
    ("Налаштування", [
        ("set_salary", "Оклади й схеми зарплати", [_g(r"/settings/salary"), _p(r"/settings/salary")]),
        ("set_cashboxes", "Каси й рахунки", [_g(r"/settings/cashboxes"), _p(r"/settings/cashboxes")]),
        ("set_checkbox", "Checkbox (РРО, термінали)", [_g(r"/settings/checkbox"), _p(r"/settings/checkbox")]),
        ("set_payments", "Платіжні системи", [_g(r"/settings/payments"), _p(r"/settings/payments")]),
        ("set_loyalty", "Бонуси й знижки", [_g(r"/settings/loyalty"), _p(r"/settings/loyalty")]),
        ("set_articles", "Статті фінансів", [_g(r"/settings/(articles|accrual-articles)"), _p(r"/settings/(articles|accrual-articles)")]),
        ("set_positions", "Посади й поля співробітників", [_g(r"/settings/(positions|staff-fields)"), _p(r"/settings/(positions|staff-fields)")]),
        ("set_clinic", "Клініка (локації, години, звіти)", [_g(r"/settings/clinic"), _p(r"/settings/clinic")]),
        ("set_channels", "Канали й телефонія", [_g(r"/settings/(channels|telephony|appeals)"), _p(r"/settings/(channels|telephony|appeals)")]),
        ("set_keys", "Ключі інтеграцій (monobank, AI, сайт)", [_g(r"/settings/keys"), _p(r"/settings/keys")]),
        ("set_price_cats", "Категорії цін", [_g(r"/settings/price-cats"), _p(r"/settings/price-cats")]),
        ("data_backup", "Резервні копії", [_g(r"/backup/"), _p(r"/backup/")]),
        ("data_import", "Імпорт даних і перенос", [_p(r"/import/"), _p(r"/migrate/")]),
        ("data_export_files", "Експорт файлів", [_g(r"/export/files")]),
    ]),
]
ITEMS = {k: (lbl, pats) for _g_, items in GROUPS for k, lbl, pats in items}
_COMPILED = {k: [(m, re.compile("^" + p), q) for m, p, q in pats] for k, (_l, pats) in ITEMS.items()}
_CACHE: dict = {"deny": None}


def _load(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {r: set(v) & set(ITEMS) for r, v in (d.get("deny") or {}).items()}


def deny_map(db: Session | None = None) -> dict[str, set]:
    if db is not None:
        _CACHE["deny"] = _load(db)
    if _CACHE["deny"] is None:
        from ..models import SessionLocal
        with SessionLocal() as s:
            _CACHE["deny"] = _load(s)
    return _CACHE["deny"]


def denied_keys(role: str | None) -> set:
    if not role or role == "owner":
        return set()
    return set(deny_map().get(role, set()))


def save(db: Session, role: str, allowed: list[str]) -> set:
    """allowed — відмічені галочки; решта елементів — заборонені для ролі."""
    if role == "owner":
        return set()
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    try:
        d = json.loads(st.value) if st.value else {}
    except ValueError:
        d = {}
    deny = set(ITEMS) - set(allowed)
    d.setdefault("deny", {})[role] = sorted(deny)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()
    deny_map(db)
    return deny


def blocked(role: str | None, method: str, path: str, query: dict) -> str | None:
    """Назва забороненого елемента, якщо запит веде на нього; інакше None."""
    for k in denied_keys(role):
        for m, rx, q in _COMPILED[k]:
            if m != method.upper() and not (m == "GET" and method.upper() == "HEAD"):
                continue
            if not rx.match(path):
                continue
            if q and str(query.get(q[0], q[2]) or q[2]) != q[1]:
                continue
            return ITEMS[k][0]
    return None


def client_rules(role: str | None) -> list:
    """Для браузера: [метод, regex, [параметр, значення, за замовчуванням] | null] — ховати посилання й кнопки."""
    out = []
    for k in sorted(denied_keys(role)):
        out += [[m, "^" + p, list(q) if q else None] for m, p, q in ITEMS[k][1]]
    return out
