"""Сегмент «Нові за 30 днів» і поля клієнта з CleverBox."""
from datetime import datetime, timedelta

from app.integrations.cleverbox import apply_client_fields
from app.models import Patient
from app.services.clients import ClientRow


def row(p, first=None, visits=0, nxt=None):
    r = ClientRow(p, None, first, nxt, visits, 0.0)
    r.first_visit = first
    return r


def test_new_segment():
    now = datetime.now()
    migrated = Patient(full_name="Стара", cleverbox_id=1, created_at=now)          # перенесена сьогодні
    assert "new" not in row(migrated, first=now - timedelta(days=400), visits=5).segment_flags
    assert "new" not in row(migrated).segment_flags                                 # без дати додавання
    assert "new" in row(migrated, first=now - timedelta(days=3), visits=1).segment_flags
    assert "new" in row(migrated, nxt=now + timedelta(days=2)).segment_flags        # записана на 1-й візит
    reg = Patient(full_name="Нова", cleverbox_id=2, created_at=now, cb_registered_at=now - timedelta(days=5))
    assert "new" in row(reg).segment_flags
    manual = Patient(full_name="Ручна", created_at=now - timedelta(days=2))
    assert "new" in row(manual).segment_flags


def test_apply_client_fields():
    p = Patient(full_name="Х")
    apply_client_fields(p, {"id": 5, "date_add": "01.10.2026 12:30", "birthday": "1990-05-07", "email": "a@b.ua"})
    assert p.cb_registered_at == datetime(2026, 10, 1, 12, 30)
    assert p.birthday.isoformat() == "1990-05-07" and p.email == "a@b.ua"
    apply_client_fields(p, {"id": 5, "birthday": "00.00.0000"})
    assert p.birthday.isoformat() == "1990-05-07"
