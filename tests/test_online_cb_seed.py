from app.models import SessionLocal, CbService, Doctor, SyncState
from app.services import online_booking as ob


def test_apply_cb_seed():
    with SessionLocal() as db:
        st = db.get(SyncState, "online_cb_seed_20261116")
        if st:
            db.delete(st)
        s1 = db.get(CbService, 387) or CbService(id=387, name="Консультація Beauty plan Anti Age", price=2500)
        s2 = db.get(CbService, 504) or CbService(id=504, name="Масаж Stratosphere 50", price=1800)
        s3 = db.get(CbService, 126) or CbService(id=126, name="Масаж обличчя", price=1000)
        s3.online = True
        db.add_all([s1, s2, s3])
        names = {d.name for d in db.query(Doctor)}
        for n in ("Софія Сирова", "Ганна Чугунова", "Інша Лікарка"):
            if n not in names:
                db.add(Doctor(name=n, active=True))
        db.commit()
        r = ob.apply_cb_seed(db, force=True)
        assert r["online"] >= 2 and ob.apply_cb_seed(db).get("skipped")
        sof = db.query(Doctor).filter_by(name="Софія Сирова").first()
        han = db.query(Doctor).filter_by(name="Ганна Чугунова").first()
        assert db.get(CbService, 387).online and db.get(CbService, 387).doctor_ids == [sof.id]
        assert db.get(CbService, 504).doctor_ids == [han.id] and db.get(CbService, 126).online is False
        off = set(ob.cfg(db)["doctors_off"])
        assert sof.id not in off and han.id not in off and db.query(Doctor).filter_by(name="Інша Лікарка").first().id in off
