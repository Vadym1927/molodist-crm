"""Ротація копій: копії перед оновленнями не витісняють щоденні."""
import os
import time
from datetime import datetime, timedelta

from app.services import backups


def test_rotate_keeps_one_per_day(tmp_path):
    now = datetime.now()
    files = []
    for d in range(5):                                   # по одній копії за 5 днів
        files.append((f"{d}-day", now - timedelta(days=d, hours=1)))
    for i in range(20):                                   # 20 копій сьогодні (оновлення CRM)
        files.append((f"today-{i}", now - timedelta(minutes=i)))
    for name, t in files:
        p = tmp_path / f"{backups.PREFIX}{name}.zip"
        p.write_bytes(b"x")
        ts = time.mktime(t.timetuple())
        os.utime(p, (ts, ts))
    backups._rotate(tmp_path, 3)
    left = sorted(p.name for p in tmp_path.glob(backups.PREFIX + "*.zip"))
    assert len(left) == 3 + 2                              # 3 найновіші (сьогодні) + вчора і позавчора
    assert any("1-day" in n for n in left) and any("2-day" in n for n in left) and not any("3-day" in n for n in left)
