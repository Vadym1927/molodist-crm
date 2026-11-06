import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import pytest
from app.models import Base, engine


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app.models import SessionLocal, SyncState      # режими роботи в тестах — вимкнені (на сервері вмикаються міграцією)
    with SessionLocal() as db:
        db.add(SyncState(key="work_mode_init_20261101", value="1"))
        db.add(SyncState(key="shift_required", value="0"))      # касова зміна в тестах не обовʼязкова (окремий тест)
        db.add(SyncState(key="patient_msgs", value="on"))       # у тестах надсилання пацієнтам увімкнене (на сервері — вимкнене)
        db.commit()
    from app.integrations import messaging as _msg
    _msg._PCACHE.clear()
    from app.services import autonomy
    autonomy._CACHE["cfg"] = dict(autonomy.DEFAULT)
    from app.services import ui_access
    ui_access._CACHE["deny"] = {}
    yield
