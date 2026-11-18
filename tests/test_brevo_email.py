"""Email через Brevo: ключ зберігається зашифрованим, тестовий лист, кошик іде на email, якщо він є."""
from datetime import datetime, timedelta

from app.integrations import brevo
from app.models import SessionLocal, Patient, ShopAccount, ShopCart, SyncState
from app.services import shop_carts as scr


class R:
    def __init__(self, code=201, js=None):
        self.status_code, self._js, self.text = code, js or {"messageId": "m1"}, ""

    def json(self):
        return self._js


def test_brevo_key_encrypted_and_cart_email(monkeypatch):
    sent = []
    monkeypatch.setattr(brevo.httpx, "post", lambda url, json, headers, timeout: sent.append((url, json, headers)) or R())
    with SessionLocal() as db:
        brevo.set_key(db, "xkeysib-secret")
        raw = db.get(SyncState, brevo.KEY).value
        assert "xkeysib-secret" not in raw and brevo.has_key(brevo.cfg(db))
        try:
            brevo.send(db, "a@b.ua", "s", "t")
            assert False
        except brevo.EmailError:
            pass                                            # вимкнено — не надсилає
        assert brevo.send(db, "a@b.ua", "s", "t", force=True) == "m1" and sent[-1][2]["api-key"] == "xkeysib-secret"
        c = brevo.cfg(db); c.update(on=True, cart_on=True); brevo.save(db, c)
        p = Patient(full_name="Олійник Юлія", phone="+380671112233"); db.add(p); db.commit()
        db.add(ShopAccount(patient_id=p.id, first_name="Юлія", email="yulia@test.ua")); db.commit()
        cart = ShopCart(patient_id=p.id, items=[{"id": 1, "qty": 1}], total=1575, status="open", updated_at=datetime.utcnow() - timedelta(hours=3))
        db.add(cart); db.commit()
        data = {"items": [{"variants": [{"id": 1, "name": "Пілінг Good Hair"}]}]}
        assert scr.remind(db, {}, cart.id, data, "https://molodist.store") == "email"
        body = sent[-1][1]
        assert body["to"][0]["email"] == "yulia@test.ua" and "Пілінг Good Hair" in body["textContent"] and "Юлія" in body["textContent"]


def test_email_settings_page():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/settings/email")
        assert h.status_code == 200 and "Brevo" in h.text
        r = c.post("/settings/email", data={"api_key": "xkeysib-zz", "sender": "info@molodist.com.ua", "test_to": "v@x.ua"}, follow_redirects=False)
        assert r.status_code == 303
        assert "xkeysib-zz" not in c.get("/settings/email").text
