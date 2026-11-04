"""KPI лікарів: синтетичні дані за вересень–жовтень 2026 → метрики, порівняння, зони, премія, нарахування."""
import random
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, Patient, Check, Visit, Appointment, StaffShift, VisitNote, PatientFile, SalaryAdj, CbService
from app.services import doctor_kpi as dk

Y, M = 2026, 10
D_FROM, D_TO = date(Y, M, 1), date(Y, M, 31)


def seed(db) -> dict:
    """3 лікарі, 40 пацієнтів, чеки/візити за вересень і жовтень. Повторний виклик нічого не дублює."""
    if db.scalar(select(Doctor.id).where(Doctor.name == "KPI Лікар Сирова")):
        return {d.name: d for d in db.scalars(select(Doctor).where(Doctor.name.like("KPI Лікар%")))}
    rnd = random.Random(7)
    docs = {}
    for nm, pos in [("KPI Лікар Сирова", "лікар-косметолог"), ("KPI Лікар Колган", "головний лікар"), ("KPI Лікар Ганна", "косметолог")]:
        d = Doctor(name=nm, position=pos, active=True, source="manual")
        db.add(d)
        docs[nm] = d
    db.flush()
    for i, (nm, cat, price) in enumerate([("Біоревіталізація KPI", "Біоревіталізація/мезотерапія", 5500), ("Ботулінотерапія KPI", "Ботулінотерапія", 7000),
                                          ("Чистка KPI", "Очищення шкіри", 3200), ("Лазерна епіляція KPI", "Лазерна епіляція", 1100)]):
        db.add(CbService(id=990001 + i, name=nm, category=cat, price=price))
    pats = []
    for i in range(40):
        p = Patient(full_name=f"KPI Пацієнт {i:02d}", phone=f"+38050100{i:04d}", source="Рекомендація" if i % 7 == 0 else "Instagram")
        db.add(p)
        pats.append(p)
    db.flush()
    cid = max(int(db.scalar(select(Check.id).order_by(Check.id.desc()).limit(1)) or 0), 990000)
    vid = 0
    svc = [("Біоревіталізація KPI", 5500), ("Ботулінотерапія KPI", 7000), ("Чистка KPI", 3200), ("Лазерна епіляція KPI", 1100)]
    for mon, n_days in [(9, 30), (10, 31)]:
        for dname, d in docs.items():
            share = {"KPI Лікар Сирова": 1.0, "KPI Лікар Колган": 0.5, "KPI Лікар Ганна": 0.8}[dname]
            for day in range(1, n_days + 1):
                dd = date(Y, mon, day)
                if dd.weekday() >= 5 and dname != "KPI Лікар Ганна":
                    continue
                db.add(StaffShift(doctor_id=d.id, day=dd, start="10:00", end="18:00"))
                n_vis = int(4 * share) if dd.weekday() < 5 else 2
                for k in range(n_vis):
                    # Сирова: одні й ті ж пацієнти (повторні), Ганна — нові щоразу; у жовтні частина — ті, що були у вересні
                    if dname == "KPI Лікар Ганна":
                        p = pats[rnd.randrange(20, 40)] if mon == 10 and rnd.random() < 0.5 else pats[rnd.randrange(0, 40)]
                    else:
                        p = pats[rnd.randrange(0, 12)] if dname == "KPI Лікар Сирова" else pats[rnd.randrange(10, 25)]
                    st = datetime(Y, mon, day, 10 + k * 2, 0)
                    vid += 1
                    status = "cancelled" if rnd.random() < (0.05 if dname == "KPI Лікар Сирова" else 0.18) else "done"
                    db.add(Visit(source="cb_visit", external_id=f"kpi-{vid}", patient_id=p.id, service_name=svc[k % 4][0], master_name=dname,
                                 starts_at=st, ends_at=st + timedelta(minutes=90 if dname != "KPI Лікар Ганна" else 45), status=status))
                    if status != "done":
                        continue
                    if dname == "KPI Лікар Сирова" or rnd.random() < 0.5:
                        db.add(VisitNote(key=f"cb:{vid}", patient_id=p.id, text="нотатка"))
                    if rnd.random() < (0.6 if dname == "KPI Лікар Сирова" else 0.2):
                        db.add(PatientFile(patient_id=p.id, kind="photo", path="x.jpg", stage="before", created_at=st))
                    name, price = svc[k % 4] if dname != "KPI Лікар Ганна" else svc[3]
                    items = [{"item_type": "service", "item_name": name, "quantity": 1, "payd_amount": price, "price": price, "initiator_name": dname,
                              "category_name": None}]
                    amt = price
                    if dname != "KPI Лікар Ганна" and rnd.random() < (0.45 if dname == "KPI Лікар Сирова" else 0.2):
                        items.append({"item_type": "product", "item_name": "Крем KPI", "quantity": 1, "payd_amount": 1800, "price": 1800, "initiator_name": dname})
                        amt += 1800
                    cid += 1
                    db.add(Check(id=cid, date=st, amount=amt, discount=0, patient_id=p.id, author=dname, items=items, payments=[]))
    # майбутні записи (перезапис) для частини пацієнтів Сирової
    for p in pats[:8]:
        db.add(Appointment(doctor_id=docs["KPI Лікар Сирова"].id, patient_id=p.id, start=datetime(2026, 11, 20, 12), end=datetime(2026, 11, 20, 13), status="booked"))
    db.commit()
    return docs


def test_doctor_kpi_compute_and_premium():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        docs = seed(db)
        cfg = dk.settings(db)
        cfg["doctors"][str(docs["KPI Лікар Сирова"].id)] = {"taxes": 3585.34, "revenue_target": 300000}
        cfg["ladder"] = [{"metric": "card_pct", "min": 85, "amount": 1000}, {"metric": "card_pct", "min": 90, "amount": 2000}]
        dk.save_settings(db, cfg)
        r = dk.compute(db, D_FROM, D_TO)
        rows = {m["name"]: m for m in r["rows"]}
        s, g = rows["KPI Лікар Сирова"], rows["KPI Лікар Ганна"]
        assert s["total"] > 0 and s["products"] > 0 and s["products_share"] > 0 and s["services"] == sum(c["sum"] for c in s["cats"] if c["name"] != "Косметика")
        assert s["patients"] <= 12 and s["old"] >= 10 and s["retention"] >= 80          # повторні пацієнти
        assert g["new"] > 0 and g["cancel_pct"] > s["cancel_pct"]
        assert s["sched_h"] > 0 and s["worked_h"] > 0 and 0 < s["load"] <= 100 and s["idle_h"] >= 0
        assert s["card_pct"] == 100 and s["discipline"] is not None and s["premium"]["total"] == 2000
        assert g["card_pct"] < 85 and g["premium"]["total"] == 0
        assert s["taxes"] == 3585.34 and s["profit"] == round(s["total"] - s["cost"] - s["salary"] - s["taxes"] - s["amort"], 2)
        assert s["revenue_pct"] is not None and any(x["key"] == "revenue_pct" for x in s["goals"])
        assert s["rebooked"] >= 1 and s["not_rebooked"] == s["patients"] - s["rebooked"] and len(s["not_rebooked_list"]) == s["not_rebooked"]
        assert s["ltv12"] >= s["ltv6"] >= s["total"]
        assert s["recommended"] >= 1
        assert s["prev"] and s["prev"]["total"] > 0 and s["conversion_pct"] is not None
        assert any("Перезаписано" in z["text"] or "Завантаження" in z["text"] for z in g["zones"])
        assert r["total"]["total"] == round(sum(m["total"] for m in r["rows"]), 2) and r["total"]["premium"] == 2000
        n = dk.accrue_premium(db, "2026-10", r["rows"], None)
        assert n == 1 and dk.accrue_premium(db, "2026-10", r["rows"], None) == 0
        adj = db.scalar(select(SalaryAdj).where(SalaryAdj.article == dk.PREMIUM_ARTICLE, SalaryAdj.month == "2026-10"))
        assert adj and adj.amount == 2000 and adj.doctor_id == s["doctor_id"]
        ser = dk.series_all(db, D_TO, 3)[s["doctor_id"]]
        assert len(ser) == 3 and ser[-1]["total"] == s["total"] and ser[-2]["total"] == s["prev"]["total"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/reports/doctors-kpi?m=2026-10").text
        assert "KPI Лікар Сирова" in page and "Зони покращення" in page and "Не перезаписані пацієнти" in page and "Рейтинг ефективності" in page
        page1 = c.get(f"/reports/doctors-kpi?m=2026-10&doc={s['doctor_id']}").text
        assert "KPI Лікар Ганна" not in page1.split("<section")[1] if "<section" in page1 else True
        st = c.get("/settings/doctor-kpi").text
        assert "Шкала премії" in st and "3585.34" in st
        r2 = c.post("/settings/doctor-kpi", data={"t_load_pct": "75", "l_metric": ["card_pct", "load_pct"], "l_min": ["85", "70"], "l_amount": ["1000", "500"],
                                                 f"d_{s['doctor_id']}_taxes": "4000", f"d_{s['doctor_id']}_revenue": ""}, follow_redirects=False)
        assert r2.status_code == 303
        with SessionLocal() as db:
            cfg = dk.settings(db)
            assert cfg["targets"]["load_pct"] == 75 and len(cfg["ladder"]) == 2 and cfg["doctors"][str(s["doctor_id"])]["taxes"] == 4000
        assert "KPI лікарів" in c.get("/reports").text


def test_doctor_kpi_history_archive_cosmetics():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        docs = seed(db)
        sh = dk.sheet_history(db)
        names = {h["name"] for h in sh.values()}
        assert "Софія Сирова" in names or any("Сирова" in n for n in names)
        hs = dk.history(db, next(k for k, h in sh.items() if "Сирова" in h["name"]), date(2026, 11, 4))
        assert len(hs["rows"]) >= 13 and hs["rows"][0]["key"] == "2025-10" and hs["rows"][0]["source"] == "sheet"
        r = dk.compute(db, D_FROM, D_TO)
        arch = dk.archive(db, D_FROM, {m["doctor_id"] for m in r["rows"]})
        assert any("Ісакова" in a["name"] for a in arch)
        c = dk.cosmetics(r["rows"])
        assert c["sum"] > 0 and c["top"][0]["name"] == "Крем KPI" and c["by_doc"]
        lt = dk.clinic_ltv(db)
        assert lt["patients"] > 0 and lt["avg"] > 0
        s = next(m for m in r["rows"] if m["name"] == "KPI Лікар Сирова")
        assert s["contrib"] == round(s["services"] - s["cost_services"] - s["salary"], 2) and s["visits_per_patient"] > 1
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = cl.get("/reports/doctors-kpi?m=2026-10").text
        assert "Лідер по обороту" in page and "Косметика" in page and "Архів команди" in page and "Маржа внеску послуг" in page
        hp = cl.get(f"/reports/doctors-kpi/history?key={s['doctor_id']}")
        assert hp.status_code == 200
        hp2 = cl.get("/reports/doctors-kpi/history?key=" + __import__("urllib.parse").parse.quote(next(a["key"] for a in arch if isinstance(a["key"], str))))
        assert hp2.status_code == 200 and "Оборот по місяцях" in hp2.text


def test_overlay_from_sheet():
    m = {"taxes": 0.0, "total": 0.0, "services": 0.0, "products": 0.0, "patients": 5, "new": 0, "old": 5, "cats": [], "n_services": 0, "cost": 0, "salary": 0}
    sh = {"services": 100000.0, "products": 20000.0, "patients": 40.0, "new": 5.0, "old": 35.0, "n_services": 50.0, "salary": 30000.0,
          "cost": 25000.0, "load": 60.0, "profit": 50000.0, "cats": {"Ботулінотерапія": 60000.0, "Косметика": 20000.0}}
    r = dk._overlay(dict(m), sh)
    assert r["source"] == "sheet" and r["total"] == 120000 and r["patients"] == 40 and r["load"] == 60 and r["cats"][0]["name"] == "Ботулінотерапія"
    assert dk._overlay({**m, "total": 5.0}, sh)["source"] == "crm"


def test_compare_months_and_picker():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        seed(db)
        c = dk.compare(db, ["2026-09", "2026-10"])
        assert [x["key"] for x in c["cols"]] == ["2026-09", "2026-10"] and c["groups"][0]["name"] == "Усі лікарі разом"
        g = next(x for x in c["groups"] if x["name"] == "KPI Лікар Сирова")
        assert g["months"]["2026-09"]["total"] > 0 and g["months"]["2026-10"]["total"] > 0
        assert dk.avail_from(db) <= "2025-10"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = cl.get("/reports/doctors-kpi/compare?ms=2026-09,2026-10")
        assert r.status_code == 200 and "Порівняння місяців" in r.text and "KPI Лікар Сирова" in r.text and "Оборот лікарів по місяцях" in r.text
        assert cl.get("/reports/doctors-kpi/compare?ms=2026-10", follow_redirects=False).status_code == 303
        p = cl.get("/reports/doctors-kpi?m=2026-10").text
        assert 'id="kmp"' in p and "Порівняти місяці" in p
