"""Експлуатація: захист входу, автобекапи з шифруванням, сповіщення про збої, швидкодія (без N+1)."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, AuditLog
from app.services import alerts, backups

PDF = Path(__file__).parent / "sample_plan.pdf"


@pytest.fixture
def sent(monkeypatch):
    """Перехоплює всі виклики Telegram Bot API."""
    calls = []

    def fake_api(method, **kw):
        calls.append((method, kw))
        return True
    monkeypatch.setattr(alerts, "_api", fake_api)
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    monkeypatch.setattr(settings, "telegram_alert_chat_id", "111")
    return calls


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import security
    security.login_limiter.fails.clear(); security.ip_limiter.fails.clear(); security.api_limiter.fails.clear()
    monkeypatch.setattr(settings, "admin_password", "owner-pass-1")
    monkeypatch.setattr(settings, "files_dir", str(tmp_path / "files"))
    with TestClient(app) as c:
        yield c


def test_login_bruteforce(client, sent):
    c = client
    for _ in range(4):
        assert "Невірний логін або пароль" in c.post("/login", data={"login": "vadim", "password": "bad"}).text
    assert "заблоковано на 15 хв" in c.post("/login", data={"login": "vadim", "password": "bad"}).text
    # навіть правильний пароль не пускає, поки діє блок
    r = c.post("/login", data={"login": "vadim", "password": "owner-pass-1"}, follow_redirects=False)
    assert r.status_code == 200 and "Забагато невдалих спроб" in r.text
    time.sleep(0.2)
    assert any("блокування входу" in kw["data"]["text"] for m, kw in sent if m == "sendMessage")
    with SessionLocal() as db:
        acts = [a.action for a in db.query(AuditLog).all()]
        assert acts.count("login.failed") == 5 and "login.blocked" in acts
    # API: перебір ключів з однієї адреси → 429
    for _ in range(20):
        c.get("/api/v1/patients", headers={"X-API-Key": "mcrm_wrongwrongwrong"})
    assert c.get("/api/v1/patients", headers={"X-API-Key": "mcrm_wrongwrongwrong"}).status_code == 429


def test_login_block_is_per_login_not_whole_clinic(client):
    c = client
    for _ in range(5):
        c.post("/login", data={"login": "anna", "password": "bad"})
    # інший користувач з тієї ж адреси (клініка за одним IP) входить нормально
    r = c.post("/login", data={"login": "vadim", "password": "owner-pass-1"}, follow_redirects=False)
    assert r.status_code == 303


def test_alerts_dedup_and_recovery(sent):
    alerts.success("sync")                                   # скидаємо стан
    sent.clear()
    assert alerts.failure("sync", "403 акаунт вимкнено", alerts.hint_for("403")) is True
    assert alerts.failure("sync", "403 акаунт вимкнено") is False       # повтор не спамить
    assert alerts.failure("sync", "інша помилка") is True               # нова помилка — нове повідомлення
    alerts.success("sync")
    alerts.success("sync")                                   # друге «ок» нічого не шле
    texts = [kw["data"]["text"] for m, kw in sent if m == "sendMessage"]
    assert len(texts) == 3 and "CLEVERBOX_TOKEN" in texts[0] and "відновлено" in texts[2]


@pytest.mark.skipif(not PDF.exists(), reason="потрібен tests/sample_plan.pdf")
def test_auto_backup_rotation_offsite_and_encrypted_restore(client, sent, tmp_path, monkeypatch):
    c = client
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "bk"))
    monkeypatch.setattr(settings, "backup_copy_dir", str(tmp_path / "bk2"))
    monkeypatch.setattr(settings, "backup_keep", 2)
    monkeypatch.setattr(settings, "backup_password", "s3cret-pass")
    (tmp_path / "bk2").mkdir()
    c.post("/login", data={"login": "vadim", "password": "owner-pass-1"})
    c.post("/upload", files={"files": ("plan.pdf", PDF.read_bytes(), "application/pdf")})

    docs = []
    monkeypatch.setattr(alerts, "send_document", lambda path, caption, chat=None: docs.append(Path(path).read_bytes()) or True)
    names = []
    for i in range(3):
        rep = backups.run_backup("test")
        names.append(rep["file"])
        assert rep["telegram"] == "надіслано"
        time.sleep(1.1)                                       # різні мітки часу в назві
    assert len(set(names)) == 3
    assert len(list((tmp_path / "bk").glob("*.zip"))) == 2           # зберігаються BACKUP_KEEP останніх
    assert len(list((tmp_path / "bk2").glob("*.zip"))) == 2          # ротація другої копії
    assert docs and docs[-1].startswith(backups.MAGIC)               # у Telegram — лише зашифрована
    assert b"backup.json" not in docs[-1]
    assert "Зробити копію зараз" in c.get("/data").text

    # відновлення з зашифрованої копії: без пароля / з невірним — помилка, з правильним — ок
    enc = docs[-1]
    r = c.post("/backup/restore", files={"file": ("b.zip.enc", enc)}, data={"confirm": "ВІДНОВИТИ"})
    assert r.status_code == 400 and "зашифрована" in r.text
    r = c.post("/backup/restore", files={"file": ("b.zip.enc", enc)}, data={"confirm": "ВІДНОВИТИ", "password": "no"})
    assert r.status_code == 400 and "Невірний пароль" in r.text
    r = c.post("/backup/restore", files={"file": ("b.zip.enc", enc)},
               data={"confirm": "ВІДНОВИТИ", "password": "s3cret-pass"}, follow_redirects=False)
    assert r.status_code == 303


def test_backup_failure_alert(sent, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "bk"))
    monkeypatch.setattr(settings, "backup_copy_dir", str(tmp_path / "not-mounted"))
    alerts.success("backup")
    sent.clear()
    with pytest.raises(RuntimeError):
        backups.run_backup("test")
    texts = [kw["data"]["text"] for m, kw in sent if m == "sendMessage"]
    assert texts and "Резервна копія: помилка" in texts[0] and "не змонтовані" in texts[0]


def test_no_n_plus_one(monkeypatch):
    """Статуси планів рахуються фіксованою кількістю запитів, незалежно від кількості планів."""
    from datetime import datetime
    from sqlalchemy import event
    from app.models import engine, Patient
    from app.parsers.observ import ParsedPlan, PlanItem
    from app.services.plans import import_plan
    from app.services.kpi import all_statuses
    with SessionLocal() as db:
        for i in range(60):
            p = Patient(full_name=f"Тест{i} Пацієнт{i}")
            db.add(p); db.flush()
            import_plan(db, ParsedPlan(p.full_name, "Лікар А", datetime(2026, 1, 1 + i % 28), None, None, None,
                                       [PlanItem("procedure", "IPL", 3, 3900, 11700)], 11700), patient=p)
        db.commit()
    n = {"q": 0}
    listener = lambda *a: n.__setitem__("q", n["q"] + 1)       # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with SessionLocal() as db:
            sts = all_statuses(db)
            all_statuses(db, doctor_id=1)                          # повторний виклик — з памʼяті
        assert len(sts) == 60 and n["q"] <= 10, n["q"]
    finally:
        event.remove(engine, "before_cursor_execute", listener)
