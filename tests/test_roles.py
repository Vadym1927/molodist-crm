from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, SyncState, User, ROLES
from app.security import hash_password, PERMS, BASE_PERMS


def test_roles():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/access/roles").text
        assert "Адміністратор" in t and "Права доступу" in t
        assert "REST API" in c.get("/access/api").text and "Ролі" not in c.get("/access/api").text.split("<main>")[1][:600]
        c.post("/access/roles", data={"name": "Маркетолог", "perms": ["appeals", "campaigns"]})
        assert "Маркетолог" in ROLES.values()
        key = next(k for k, v in ROLES.items() if v == "Маркетолог")
        assert key in PERMS["appeals"] and key not in PERMS["sales"]
        with SessionLocal() as db:
            db.add(User(login="mk-user", name="Маркетолог Тест", role=key, password_hash=hash_password("mk-pass-12"), active=True)); db.commit()
        c.post("/access/roles", data={"key": "admin", "name": "Адміністратор", "perms": ["patients_all", "appeals"]})
        assert "admin" not in PERMS["sales"]
        c.post("/access/roles/admin/reset")
        assert "admin" in PERMS["sales"] and PERMS["stock"] >= BASE_PERMS["stock"]
        c.get("/logout")
        c.post("/login", data={"login": "mk-user", "password": "mk-pass-12"})
        assert c.get("/appeals").status_code == 200 and c.get("/sales").status_code == 403
    with SessionLocal() as db:
        db.query(User).filter_by(login="mk-user").delete()
        db.query(SyncState).filter_by(key="roles_cfg").delete(); db.commit()
        from app.services import roles as rl
        rl.apply(db)
