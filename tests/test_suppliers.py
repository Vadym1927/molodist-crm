"""Постачальники: кілька юр. осіб (ТОВ / ФОП), вікно-картка, упізнавання платежу за IBAN будь-якої юр. особи."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, BankTxn, Supplier, SupplierEntity
from app.services import suppliers as sp, payments as pay

IB1 = "UA" + "1" * 27
IB2 = "UA" + "2" * 27


def test_supplier_entities_and_card():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        s = Supplier(name="Мульти Постач", legal_name="ТОВ «Мульти»", edrpou="12345678", iban=IB1)
        db.add(s); db.commit()
        ents = sp.entities(db, s)                                   # старі реквізити → основна юр. особа
        assert len(ents) == 1 and ents[0].kind == "ТОВ" and ents[0].is_main
        sid = s.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post(f"/stock/suppliers/{sid}/entity", data={"kind": "ФОП", "legal_name": "ФОП Мультенко І. П.",
                   "edrpou": "1234567890", "iban": IB2.lower(), "back": "/stock?tab=suppliers"}, follow_redirects=False)
        assert r.status_code == 303 and f"card={sid}" in r.headers["location"]
        bad = c.post(f"/stock/suppliers/{sid}/entity", data={"iban": "UA123", "back": "/stock?tab=suppliers"}, follow_redirects=False)
        assert "msg=" in bad.headers["location"]
        html = c.get(f"/stock/suppliers/{sid}/card").text
        assert "ТОВ «Мульти»" in html and "ФОП Мультенко" in html and IB2 in html and "Скопіювати реквізити" in html
        assert c.get("/stock/suppliers/card?name=мульти постач").status_code == 200
        page = c.get("/stock?tab=suppliers").text
        assert f'data-sup="{sid}"' in page and 'id="spm-bg"' in page
        c.post("/stock/suppliers", data={"sid": str(sid), "name": "Мульти Постач", "phone": "+380501112233", "back": "/stock?tab=suppliers"})
    with SessionLocal() as db:
        s = db.get(Supplier, sid)
        assert s.phone == "+380501112233" and s.iban == IB1               # основна лишилась ТОВ
        t = BankTxn(id="sup-ent-1", fop="Бондар", account="a", at=__import__("datetime").datetime.now(), amount=-100,
                    counter_iban=IB2, counter_name="ФОП Мультенко")
        db.add(t); db.commit()
        assert pay.supplier_for(db, t).id == sid                        # платіж на ФОП постачальника
        fop = db.query(SupplierEntity).filter_by(supplier_id=sid, kind="ФОП").one()
        sp.save_entity(db, s, {"kind": "ФОП", "legal_name": fop.legal_name, "edrpou": fop.edrpou, "iban": fop.iban, "is_main": "1"}, fop.id)
        db.refresh(s)
        assert s.iban == IB2 and s.edrpou == "1234567890"
        tov = db.query(SupplierEntity).filter_by(supplier_id=sid, kind="ТОВ").one()
        assert not tov.is_main
        sp.delete_entity(db, s, fop.id)
        db.refresh(s); db.refresh(tov)
        assert tov.is_main and s.iban == IB1
