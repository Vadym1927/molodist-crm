import json

from app.models import SessionLocal, SyncState, init_db
from app.services import appeals
from app.services.appointments import add_barb_source, sources, SRC_KEY


def test_barb_source_added_once():
    init_db()
    with SessionLocal() as db:
        for k in ("barb_source_v1", SRC_KEY, appeals.KEY):
            st = db.get(SyncState, k)
            if st:
                db.delete(st)
        db.merge(SyncState(key=SRC_KEY, value=json.dumps([{"name": "Дзвінок", "on": True}, {"name": "Інше", "on": True}])))
        db.merge(SyncState(key=appeals.KEY, value=json.dumps({"sources": ["Instagram", "Інше"]})))
        db.commit()
        assert add_barb_source(db) is True
        assert sources(db) == ["Дзвінок", "barb.ua", "Інше"]
        assert appeals.dirs(db)["sources"] == ["Instagram", "barb.ua", "Інше"]
        # власник видалив — повторно не додаємо
        db.merge(SyncState(key=SRC_KEY, value=json.dumps([{"name": "Дзвінок", "on": True}])))
        db.commit()
        assert add_barb_source(db) is False
        assert "barb.ua" not in sources(db)
