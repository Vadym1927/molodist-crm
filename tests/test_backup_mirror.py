from pathlib import Path

from app.services import backups


def test_mirror_and_prune(tmp_path):
    src, dst = tmp_path / "files", tmp_path / "bk" / "files"
    (src / "2026" / "11").mkdir(parents=True)
    (src / "2026" / "11" / "a.jpg").write_bytes(b"x" * 10)
    r = backups.mirror_files(src, dst)
    assert r["ok"] and r["copied"] == 1 and (dst / "2026" / "11" / "a.jpg").exists()
    assert backups.mirror_files(src, dst)["copied"] == 0
    (src / "b.pdf").write_bytes(b"y")
    assert backups.mirror_files(src, dst)["copied"] == 1
    bk = tmp_path / "bk"
    import os, time
    for i, size in enumerate((10, 400, 400, 400)):
        p = bk / f"{backups.PREFIX}{i}.zip"
        with open(p, "wb") as fh:
            fh.truncate(size * 1024 * 1024 if size > 10 else 100)
        os.utime(p, (time.time() + i, time.time() + i))
    assert backups.prune_legacy(bk) == 2
    left = sorted(x.name for x in bk.glob(backups.PREFIX + "*.zip"))
    assert left == [f"{backups.PREFIX}0.zip", f"{backups.PREFIX}3.zip"]
