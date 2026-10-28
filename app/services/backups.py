"""Автоматичні резервні копії.

Щодня о BACKUP_HOUR:
  1) повна копія (база + фото/PDF) → BACKUP_DIR, зберігаються BACKUP_KEEP останніх;
  2) якщо задано BACKUP_COPY_DIR (другий диск / змонтована хмара) — копія і туди, з такою ж ротацією;
  3) якщо задано BACKUP_PASSWORD — зашифрована копія надсилається власнику в Telegram
     (ліміт Telegram 50 МБ: якщо повна копія більша — надсилається база без файлів, а файли лишаються на дисках).
Без пароля копія в Telegram не надсилається: там медичні дані.
Будь-який збій → сповіщення власнику (services.alerts).
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..models import SessionLocal
from . import alerts, exporter

log = logging.getLogger("backups")
MAGIC = b"MCRMENC1"
TG_LIMIT = 49 * 1024 * 1024
PREFIX = "MOLODIST_CRM_backup_"


# ---------------- шифрування (Fernet = AES-128-CBC + HMAC-SHA256; ключ з пароля через PBKDF2) ----------------

def _fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode())))


def encrypt_file(src: str, dst: str, password: str):
    salt = os.urandom(16)
    data = Path(src).read_bytes()
    Path(dst).write_bytes(MAGIC + salt + _fernet(password, salt).encrypt(data))


def is_encrypted(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


def decrypt_file(src: str, dst: str, password: str):
    from cryptography.fernet import InvalidToken
    raw = Path(src).read_bytes()
    salt, token = raw[len(MAGIC):len(MAGIC) + 16], raw[len(MAGIC) + 16:]
    try:
        Path(dst).write_bytes(_fernet(password, salt).decrypt(token))
    except InvalidToken:
        raise ValueError("Невірний пароль до зашифрованої копії")


# ---------------- копії ----------------

def _rotate(folder: Path, keep: int):
    files = sorted(folder.glob(PREFIX + "*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def list_backups() -> list[dict]:
    folder = Path(settings.backup_dir)
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob(PREFIX + "*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size_mb": round(st.st_size / 1024 / 1024, 1),
                    "created": datetime.fromtimestamp(st.st_mtime)})
    return out


def backup_path(name: str) -> Path:
    p = (Path(settings.backup_dir) / name).resolve()
    if p.parent != Path(settings.backup_dir).resolve() or not p.name.startswith(PREFIX) or not p.exists():
        raise FileNotFoundError(name)
    return p


def run_backup(trigger: str = "auto") -> dict:
    """Створити копію; повертає звіт. Помилки — у сповіщення, і виняток наверх."""
    rep: dict = {"trigger": trigger}
    try:
        folder = Path(settings.backup_dir)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{PREFIX}{datetime.now():%Y-%m-%d_%H%M%S}.zip"
        with SessionLocal() as db:
            tmp = exporter.backup_zip(db, with_files=settings.backup_with_files)
        dst = folder / name
        shutil.move(tmp, dst)
        rep.update(file=name, size_mb=round(dst.stat().st_size / 1024 / 1024, 1))
        _rotate(folder, settings.backup_keep)

        if settings.backup_copy_dir:
            copy_dir = Path(settings.backup_copy_dir)
            if not copy_dir.exists():
                raise RuntimeError(f"Папка другої копії недоступна: {copy_dir} (диск/хмара не змонтовані?)")
            shutil.copy2(dst, copy_dir / name)
            _rotate(copy_dir, settings.backup_keep)
            rep["copy"] = str(copy_dir)

        # у Telegram — лише нічна копія (не перед кожним оновленням CRM і не ручні)
        rep["telegram"] = _offsite_telegram(dst) if trigger == "auto" else "лише для нічної копії"
        alerts.success("backup", f"Остання копія: {name}, {rep['size_mb']} МБ")
        log.info("backup done: %s", rep)
        return rep
    except Exception as e:      # noqa: BLE001
        log.exception("backup failed")
        alerts.failure("backup", str(e), "Перевірте місце на диску і папки BACKUP_DIR / BACKUP_COPY_DIR.")
        raise


def _offsite_telegram(src: Path) -> str:
    if not settings.backup_telegram or not settings.telegram_bot_token:
        return "вимкнено"
    if not settings.backup_password:
        alerts.failure("backup_tg", "Копія не надіслана в Telegram: не задано BACKUP_PASSWORD",
                       "Задайте BACKUP_PASSWORD у .env — копія шифрується перед відправкою (там медичні дані).")
        return "немає пароля"
    import tempfile
    enc = Path(tempfile.gettempdir()) / (src.stem + ".zip.enc")
    try:
        encrypt_file(str(src), str(enc), settings.backup_password)
        note = ""
        if enc.stat().st_size > TG_LIMIT:            # завелика для Telegram — шлемо базу без файлів
            with SessionLocal() as db:
                small = exporter.backup_zip(db, with_files=False)
            encrypt_file(small, str(enc), settings.backup_password)
            os.unlink(small)
            note = "\n⚠️ Повна копія > 50 МБ — у Telegram лише база (без фото/PDF); повна — на диску сервера."
            if enc.stat().st_size > TG_LIMIT:
                return "завелика навіть без файлів"
        ok = alerts.send_document(enc, f"🗄 <b>Резервна копія MOLODIST CRM</b> {datetime.now():%d.%m.%Y %H:%M}\n"
                                       f"Зашифрована паролем BACKUP_PASSWORD. Відновлення: «Дані → Резервна копія».{note}")
        if ok:
            alerts.success("backup_tg")
        else:
            alerts.failure("backup_tg", "Не вдалося надіслати копію в Telegram",
                           "Перевірте, що власник написав боту /start і вказав chat ID у «Мій кабінет».")
        return "надіслано" if ok else "не вдалося надіслати"
    finally:
        enc.unlink(missing_ok=True)
