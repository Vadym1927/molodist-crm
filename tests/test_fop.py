"""Ліміти ФОП: імпорт Google-таблиці, ліміт, запас, Checkbox (Z-звіти) — без реальних запитів."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, FopMonth
from app.services import fop as fs
from app.integrations import checkbox as chb

K26 = [["7 211 598", "", "3761191"], ["", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень"],
       ["Готівка ", 72800, 123405, 70322, 129040, 153085, 108440, 89090, 49004, 84600],
       ["Безготівка ", 207377, 342709, 432775, 391367, 534322, 224698, 232489, 269992, 245676]]
B25 = [["6 672 000"], ["", "Січень"], ["Готівка ", 75441, 67970, "91214"], ["Безготівка ", 267890, 260108, "292701,3"]]


def test_fop_import_and_limits():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        sheets = [("Колган 2026", K26), ("Бондар 2025", B25)]
        assert fs.is_fop_workbook(sheets)
        rep = fs.import_workbook(db, sheets)
        assert rep["rows"] == 12
        b = fs.build(db, 2026)
        k = next(x for x in b["fops"] if x["name"] == "Колган")
        assert k["total"] == 3761191 and k["pct"] == 52.2 and k["left"] == 7211598 - 3761191
        assert fs.build(db, 2025)["limit"] == 6672000
        b25 = next(x for x in fs.build(db, 2025)["fops"] if x["name"] == "Бондар")
        assert b25["months"][3].cashless == 292701.3
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/reports?r=fop&year=2026").text
        assert "Статус за кожним ФОП" in page and "3 761 191" in page and "52.2% ліміту" in page


def test_checkbox_sync_fake():
    class FakeCB:
        def __init__(self, login, password):
            assert login == "k-login"

        def z_reports(self, start, end):
            yield {"created_at": "2039-03-31T21:30:00+00:00",          # 01.04 за Києвом → квітень
                   "payments": [{"type": "CASH", "sell_sum": 100000, "return_sum": 0},
                                {"type": "CASHLESS", "sell_sum": 250000, "return_sum": 50000}]}
            yield {"created_at": "2039-03-15T18:00:00+00:00", "payments": [{"type": "CARD", "sell_sum": 70000}]}

    old = (settings.checkbox_1_login, settings.checkbox_1_password)
    settings.checkbox_1_login, settings.checkbox_1_password = "k-login", "secret"
    try:
        with SessionLocal() as db:
            rep = chb.sync(db, date(2039, 3, 1), date(2039, 4, 30), client_factory=FakeCB)
            assert rep["Колган"] == "2 Z-звітів" and rep["Бондар"] == "не налаштовано"
            apr = db.query(FopMonth).filter_by(fop="Колган", year=2039, month=4).one()
            mar = db.query(FopMonth).filter_by(fop="Колган", year=2039, month=3).one()
            assert (apr.cash, apr.cashless, apr.source) == (1000, 2000, "checkbox") and mar.cashless == 700
    finally:
        settings.checkbox_1_login, settings.checkbox_1_password = old


def test_connect_fop_from_ui_encrypted():
    from app.models import SyncState
    settings.admin_password = "owner-pass-1"

    class OkCB:
        def __init__(self, login, password):
            if password != "pw-ok":
                raise chb.CheckboxError("вхід касира: 401 bad")

    with SessionLocal() as db:
        assert chb.connect(db, "Тестовий", "cashier1", "wrong", client_factory=OkCB)
        assert chb.connect(db, "Тестовий", "cashier1", "pw-ok", client_factory=OkCB) is None
        assert chb.connect(db, "Тестовий", "cashier1", "", client_factory=OkCB) is None      # порожній — лишити старий
        raw = db.get(SyncState, "checkbox_accounts_enc").value
        assert "pw-ok" not in raw and "cashier1" not in raw                                 # у базі лише шифротекст
        a = next(x for x in chb.accounts(db) if x["name"] == "Тестовий")
        assert a["configured"] and a["password"] == "pw-ok" and a["source"] == "ui"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/reports?r=fop").text
        assert "Підключення ФОП" in page and "ФОП Тестовий" in page and "pw-ok" not in page
        c.post("/reports/fop/disconnect", data={"name": "Тестовий"})
    with SessionLocal() as db:
        assert not any(x["name"] == "Тестовий" for x in chb.accounts(db))
