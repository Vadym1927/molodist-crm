from app.models import SessionLocal
from app.services import cosmetics_pct as cp


def test_pack_fix_roundtrip():
    with SessionLocal() as db:
        cp.set_pack_fix(db, 9001, 0)
        cp.set_pack_fix(db, 9002, 120.5)
        assert cp.pack_fixes(db)[9001] == 0 and cp.pack_fixes(db)[9002] == 120.5
        cp.set_pack_fix(db, 9001, None)
        assert 9001 not in cp.pack_fixes(db)
