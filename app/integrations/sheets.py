"""Вивантаження в Google Sheets (service account).

1) Google Cloud → створити service account → ключ JSON → шлях у GOOGLE_SERVICE_ACCOUNT_FILE.
2) Розшарити таблицю на email сервісного акаунта (Редактор), ID таблиці — у GOOGLE_SHEET_ID.
Аркуші «Плани», «Позиції», «KPI лікарів», «Прогноз» перезаписуються при кожному експорті.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..services.kpi import all_statuses, kpi_summary, forecast

FLAG = {"green": "записана", "yellow": "без запису", "red": "прострочено", "done": "завершено"}


def _ws(sh, title, rows, cols):
    import gspread
    try:
        ws = sh.worksheet(title)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(rows, 10), cols=max(cols, 5))
    return ws


def build_tables(db: Session, statuses=None, finance: bool = True) -> dict[str, list[list]]:
    sts = statuses if statuses is not None else all_statuses(db)
    plans = [["ID плану", "Пацієнт", "Телефон", "Лікар", "Дата плану", "Статус", "Сума плану", "Реалізовано",
              "% реалізації", "Процедур виконано", "Процедур у плані", "Засобів куплено", "Засобів у плані",
              "Наступний запис", "Наступна процедура"]]
    items = [["ID плану", "Пацієнт", "Тип", "Позиція", "К-сть", "Ціна", "Сума", "Виконано/куплено",
              "Записано", "Залишок", "Остання дата"]]
    for ps in sorted(sts, key=lambda s: s.plan.session_at or datetime.min, reverse=True):
        p, pat = ps.plan, ps.plan.patient
        nxt = ps.next_procedure
        plans.append([p.id, pat.full_name, pat.phone or "", p.doctor.name if p.doctor else "",
                      p.session_at.strftime("%d.%m.%Y") if p.session_at else "", FLAG.get(ps.flag, ps.flag),
                      ps.planned_sum, ps.realized_sum, ps.realization_pct / 100, ps.proc_done, ps.proc_planned,
                      ps.prod_bought, ps.prod_planned,
                      ps.next_booking.strftime("%d.%m.%Y %H:%M") if ps.next_booking else "",
                      nxt.item.name if nxt else ""])
        for st in ps.items:
            items.append([p.id, pat.full_name, "процедура" if st.item.kind == "procedure" else "догляд",
                          st.item.name, st.item.qty, st.item.unit_price, st.item.total, st.done, st.booked,
                          st.remaining, st.last_done.strftime("%d.%m.%Y") if st.last_done else ""])
    total, by_doc = kpi_summary(sts)
    kpi = [["Лікар", "Планів", "Активних", "Завершених", "Сума планів", "Реалізовано", "% реалізації",
            "Записано (сума)", "% активних із записом", "Конверсія в старт", "Середній план", "Прострочених"]]
    for r in by_doc + [total]:
        kpi.append([r.label, r.plans, r.active, r.completed, r.planned_sum, r.realized_sum, r.realization_pct / 100,
                    r.booked_sum, r.booked_pct / 100, r.conversion_pct / 100, r.avg_plan, r.red])
    out = {"Плани": plans, "Позиції": items, "KPI лікарів": kpi}
    if finance:
        fc = [["Місяць", "Підтверджено (записи)", "Очікується за планами", "Повторний догляд", "Разом"]]
        for r in forecast(sts):
            fc.append([r["month"], r["confirmed"], r["expected"], r["care"], r["total"]])
        out["Прогноз"] = fc
    return out


def export(db: Session) -> str:
    import gspread
    if not (settings.google_service_account_file and settings.google_sheet_id):
        raise RuntimeError("Не задано GOOGLE_SERVICE_ACCOUNT_FILE / GOOGLE_SHEET_ID")
    gc = gspread.service_account(filename=settings.google_service_account_file)
    sh = gc.open_by_key(settings.google_sheet_id)
    for title, rows in build_tables(db).items():
        ws = _ws(sh, title, len(rows) + 5, len(rows[0]) + 1)
        ws.update(rows, "A1", value_input_option="USER_ENTERED")
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    return sh.url
