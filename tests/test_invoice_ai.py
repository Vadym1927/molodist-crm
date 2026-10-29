"""Розпізнавання накладної: Excel локально, фото — через Claude API (тут — імітація), зіставлення з каталогом."""
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, Supplier
from app.services import invoice_ai


def _xlsx() -> bytes:
    wb = Workbook(); ws = wb.active
    ws.append(["Видаткова накладна № РН-991 від 28.10.2026"])
    ws.append(["Постачальник: ТОВ «Розпізнавач»  ЄДРПОУ 44556677"])
    ws.append([])
    ws.append(["№", "Артикул", "Найменування", "Од.", "Кількість", "Ціна", "Сума"])
    ws.append([1, "RZ-1", "Крем розпізнавальний 50 мл", "шт", 3, "1 200,00", "3 600,00"])
    ws.append([2, "", "Невідомий засіб Х", "шт", 2, 500, 1000])
    ws.append(["", "", "Разом", "", "", "", 4600])
    buf = BytesIO(); wb.save(buf); return buf.getvalue()


def test_excel_invoice_recognized_and_matched():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Product(name="Крем розпізнавальний 50 мл", name_norm="крем розпізнавальний 50 мл", sku="RZ-1", warehouse="inject"))
        db.add(Supplier(name="Розпізнавач", edrpou="44556677"))
        db.commit()
        parsed, src = invoice_ai.extract(db, _xlsx(), "rn.xlsx")
        assert src == "excel" and parsed["supplier"]["edrpou"] == "44556677" and parsed["number"] == "РН-991"
        assert [l["qty"] for l in parsed["lines"]] == [3, 2] and parsed["lines"][0]["price"] == 1200
        m = invoice_ai.match(db, parsed)
        assert m["supplier"] == "Розпізнавач" and m["supplier_known"] and m["matched"] == 1 and m["unmatched"] == 1
        assert m["lines"][0]["product"]["wh_key"] == "inject" and m["lines"][1]["product"] is None
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post("/stock/recognize", files={"file": ("rn.xlsx", _xlsx(), "application/vnd.ms-excel")}).json()
        assert j["source"] == "excel" and j["matched"] == 1
        bad = c.post("/stock/recognize", files={"file": ("photo.jpg", b"\xff\xd8x", "image/jpeg")})
        assert bad.status_code == 400 and "ключ Claude API" in bad.json()["error"]


def test_photo_invoice_via_claude_fake():
    def fake(key, data, mime, text=None, http=None):
        assert key == "sk-test" and mime == "image/jpeg"
        return {"supplier": {"name": "ТОВ Фото Дистриб", "edrpou": "", "iban": ""}, "number": "77", "date": "2026-10-27",
                "total": 900, "lines": [{"name": "Крем розпізнавальний 50 мл", "sku": "RZ-1", "qty": 1, "price": 900, "sum": 900, "expiry": "2027-05-01"}]}
    with SessionLocal() as db:
        if not db.query(Product).filter_by(sku="RZ-1").first():
            db.add(Product(name="Крем розпізнавальний 50 мл", name_norm="крем розпізнавальний 50 мл", sku="RZ-1")); db.commit()
        invoice_ai.save_key(db, "sk-test")
        parsed, src = invoice_ai.extract(db, b"\xff\xd8jpeg", "nakl.jpg", "image/jpeg", claude=fake)
        m = invoice_ai.match(db, parsed)
        assert src == "ai" and m["lines"][0]["product"]["sku"] == "RZ-1" and m["lines"][0]["expiry"] == "2027-05-01"
        assert not m["supplier_known"] and m["supplier"] == "ТОВ Фото Дистриб"
        invoice_ai.save_key(db, "")
        assert invoice_ai.api_key(db) == ""
