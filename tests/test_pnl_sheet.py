"""Імпорт P&L з таблиці власника: парсер, збереження, сторінка, порівняння в CRM-звіті."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal
from app.services import pnl_sheet as ps

CSV = """## Sheet name: PnL 2024 Гібрид
,"1,5",2
,Січень,Лютий ,Березень,Квітень,Травень,Червень,Липень,Серпень,Вересень,Жовтень,Листопад,Грудень
Дохід (REVENUE),100000,120000,0,0,0,0,0,0,0,0,0,0,220000
Послуги загалом ,80000,100000
Лазерна епіляція,30000,40000
Косметика,20000,20000
СОБІВАРТІСТЬ (Cost of Services),40000,50000
Собівартість послуг ,30000,40000
1. Витратні матеріали,10000,12000
ВАЛОВИЙ ПРИБУТОК (GROSS PROFIT),60000,70000

ОПЕРАЦІЙНІ ВИТРАТИ (OPEX),"30000,5",31000
Оренда,10000,10000
Зарплата,20000,21000
ОПЕРАЦІЙНИЙ ПРИБУТОК (Operating Profit),"29999,5",39000
РАЗОВІ ВИТРАТИ ,0,1000
ЧИСТИЙ ПРИБУТОК (Net Profit),"29999,5",38000
Нові Пацієнти ,10,12
Відцоток по послугам ,"80,00","83,33"
Лазерна епіляція,"30,00","33,33"
## Sheet name: PnL 2024 Вересень
,Січень,Лютий ,Березень,Квітень,Травень,Червень,Липень,Серпень,Вересень,Жовтень,Листопад,Грудень
Дохід (REVENUE),1,2
"""


def test_parse_and_page():
    settings.admin_password = "owner-pass-1"
    d = ps.parse(CSV)
    assert list(d) == [2024] and d[2024]["sheet"] == "PnL 2024 Гібрид"
    rows = {r["name"].strip(): r for r in d[2024]["rows"]}
    assert rows["Дохід (REVENUE)"]["kind"] == "head" and rows["Дохід (REVENUE)"]["vals"][:2] == [100000, 120000]
    assert rows["Послуги загалом"]["kind"] == "group" and rows["Лазерна епіляція"]["kind"] == "line"
    assert rows["ОПЕРАЦІЙНІ ВИТРАТИ (OPEX)"]["vals"][0] == 30000.5 and rows["Нові Пацієнти"]["vals"][1] == 12
    assert "Відцоток по послугам" not in rows                               # відсотки нижче — не імпортуються
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            res = ps.import_text(db, CSV, "test.csv")
            assert res == {2024: len(d[2024]["rows"])}
            v = ps.year_view(db, 2024)
            assert v["key"]["net"][1] == 38000 and v["months"] == [0, 1]
        t = c.get("/reports?r=pnlm&year=2024&src=sheet").text
        assert "таблиця власника" in t and "ЧИСТИЙ ПРИБУТОК" in t and "Лазерна епіляція" in t
        with SessionLocal() as db:
            ps.import_text(db, CSV.replace("2024", "2022"), "y2022.csv")           # 2022 має дані CRM (test_pnl_monthly) → рядки порівняння
        t2 = c.get("/reports?r=pnlm&year=2022&src=crm").text
        assert "за таблицею" in t2 or "даних немає" in t2
        r = c.post("/reports/pnlm/import", files={"file": ("x.csv", CSV.encode(), "text/csv")}, follow_redirects=False)
        assert r.status_code == 303 and "src=sheet" in r.headers["location"]


def test_hybrid(monkeypatch):
    from app.services import pnl_monthly as pm, autonomy
    from datetime import date
    with SessionLocal() as db:
        ps.import_text(db, CSV.replace("2024", "2022"), "y2022.csv")
        monkeypatch.setattr(autonomy, "since", lambda db_: date(2022, 7, 1))
        crm = pm.build(db, 2022)
        hy = ps.hybrid(db, 2022, crm)
        assert hy["crm_from"] == 7 and hy["months_src"][0] == "sheet" and hy["months_src"][6] == "crm"
        rev = next(r for r in hy["rows"] if r["field"] == "rev")
        assert rev["vals"][0] == 100000 and rev["srcs"][6] == "crm" and rev["vals"][6] == crm["rows"][6]["rev"]
        rent = next(r for r in hy["rows"] if r["field"] == "opex_g" and r["name"].strip() == "Оренда")
        assert rent["vals"][6] == crm["rows"][6]["opex_g"]["rent"]
        laser = next(r for r in hy["rows"] if r["name"].strip() == "Лазерна епіляція")
        assert laser["srcs"][6] == "crm"
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/reports?r=pnlm&year=2022").text          # таблиця є → за замовчуванням гібрид
        assert "гібрид" in t and "Таблиця власника" in t


def test_assign_many_to_one():
    a = ps.assign(["Ботулінотерапія", "Лазерна епіляція", "IPL", "Софія", "Колган"],
                  ["Ботулінотерапія (Xeomin, Botox, Dysport, Neuronox)", "Ботокс/Диспорт/Нейронокс (ботулінотерапія)",
                   "Лазерна епіляція ( -50% на перший візит)", "IPL (видалення судин)", "Сирова Софія", "KPI Лікар Колган", "Щось нове"])
    assert len(a["Ботулінотерапія"]) == 2 and a["Лазерна епіляція"] and a["IPL"] and a["Софія"] == ["Сирова Софія"] and a["Колган"] == ["KPI Лікар Колган"]


def test_directions():
    cats = [{"Лазер": (1000, 100), "Ботокс": (3000, 1500)}] + [{}] * 11
    d = ps.directions(cats, [400] + [0] * 11, [(1000, 600, 50)] + [(0, 0, 0)] * 11)
    by = {r["name"]: r for r in d["rows"]}
    assert by["Лазер"]["lab"][0] == 100 and by["Лазер"]["profit"][0] == 800 and by["Ботокс"]["profit"][0] == 1200
    assert by["Косметика"]["profit"][0] == 350 and by["Лазер"]["share"][0] == 20.0 and d["T_prof"] == 2350
    assert abs(by["Лазер"]["contrib"][0] - 100 * 800 / 2350) < 0.01 and by["Лазер"]["margin"][0] == 80.0
    with SessionLocal() as db:
        ps.import_text(db, CSV.replace("2024", "2022"), "y2022.csv")
        hy = ps.hybrid(db, 2022, None)
        dd = ps.directions_from_hybrid(hy)
        assert {r["name"] for r in dd["rows"]} == {"Лазерна епіляція", "Косметика"} and dd["rows"][0]["rev"][0] == 30000
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for src in ("hybrid", "sheet"):
            t = c.get(f"/reports?r=pnlm&year=2022&src={src}").text
            assert "Валовий внесок напрямків" in t, src


def test_only_crm_after_2026():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            ps.import_text(db, CSV.replace("2024", "2027"), "y2027.csv")        # навіть якщо таблицю за 2027 завантажать — не показуємо
        t = c.get("/reports?r=pnlm&year=2027&src=hybrid").text
        assert "Таблиця власника" not in t and "гібрид" not in t.lower().split("<body")[-1][:3000]
