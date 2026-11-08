"""Звіт «Комісії банків і еквайрингу»: комісії по рахунках проти обороту, ставка, статті, Excel, сторінка."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, FinOp
from app.services import bank_fees as bf


def test_bank_fees():
    settings.admin_password = "owner-pass-1"
    assert bf.is_fee("Комісія банку Еквайринг MonoBank") and bf.is_fee("РКО") and not bf.is_fee("Зобовʼязання Розтермінування MONOBANK")
    assert not bf.is_fee("Відсотки за кредитом банку") and not bf.is_fee("Закупка товара")
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            rows = [(1, "income", "Оплата послуг, товарів", 10000, "Термінал Т", 3), (2, "expense", "Комісія банку Еквайринг MonoBank", 130, "Термінал Т", 3),
                    (3, "income", "Оплата послуг, товарів", 5000, "Готівка Г", 3), (4, "expense", "Комісія банку MonoBank", 50, "Операційний Ф", 4),
                    (5, "expense", "Зобовʼязання Розтермінування MONOBANK", 7904, "Операційний Ф", 4), (6, "transfer", "Переміщення", 999, "Термінал Т", 4),
                    (7, "income", "Оплата послуг, товарів", 20000, "Термінал Т", 4), (8, "expense", "Комісія банку Еквайринг MonoBank", 260, "Термінал Т", 4)]
            for n, k, art, amt, acc, m in rows:
                db.add(CbFinOp(id=890000 + n, at=datetime(2023, m, 10, 12), kind=k, article=art, amount=amt, account=acc, source="import"))
            db.commit()
            r = bf.build(db, 2023)
            by = {b["name"]: b for b in r["rows"]}
            t = by["Термінал Т"]
            assert t["turn"] == 30000 and t["fees"] == 390 and t["pct"] == 1.3 and t["n"] == 2 and t["m_fee"]["2023-03"] == 130
            assert by["Операційний Ф"]["fees"] == 50 and by["Готівка Г"]["fees"] == 0 and by["Готівка Г"]["turn"] == 5000
            assert r["fees"] == 440 and r["turn"] == 35000 and r["rows"][0]["name"] == "Термінал Т"
            assert dict(r["arts"])["Комісія банку Еквайринг MonoBank"] == 390
            rm = bf.build(db, 2023, 4)
            assert rm["fees"] == 310 and rm["label"] == "квітень 2023"
            assert bf.build(db, None)["fees"] >= 440
            assert len(bf.to_xlsx(r)) > 1000
        t = c.get("/reports?r=fees&year=2023").text
        assert "Комісії банків і еквайрингу" in t and "Термінал Т" in t and "Ефективна ставка" in t
        assert c.get("/reports?r=fees&year=2023&month=4&fmt=xlsx").status_code == 200
        assert 'href="/reports?r=fees"' in c.get("/reports").text
