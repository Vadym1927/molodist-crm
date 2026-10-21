"""Службові команди CRM (запуск на сервері):

  docker compose exec app python -m app.cli reset-password          # новий тимчасовий пароль власника
  docker compose exec app python -m app.cli reset-password anna     # тимчасовий пароль користувача anna
  docker compose exec app python -m app.cli users                   # список користувачів
"""
import secrets
import sys

from .models import init_db, SessionLocal, User
from .security import hash_password, ensure_owner


def main(argv: list[str]) -> int:
    init_db()
    cmd = argv[0] if argv else "help"
    with SessionLocal() as db:
        ensure_owner(db)
        if cmd == "users":
            for u in db.query(User).order_by(User.id):
                print(f"{u.login:20} {u.role:8} {'активний' if u.active else 'вимкнений'}")
            return 0
        if cmd == "reset-password":
            q = db.query(User)
            u = q.filter(User.login == argv[1]).first() if len(argv) > 1 else q.filter(User.role == "owner").first()
            if not u:
                print("Користувача не знайдено")
                return 1
            p = "Tmp-" + secrets.token_urlsafe(8)
            u.password_hash, u.active = hash_password(p), True
            db.commit()
            print(f"ЛОГІН: {u.login}   ТИМЧАСОВИЙ ПАРОЛЬ: {p}")
            print("Увійдіть і одразу змініть пароль у «Мій кабінет». Якщо вхід заблоковано — docker compose restart app")
            return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
