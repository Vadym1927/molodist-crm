"""Сайт з архіву Tilda на нашому сервері (перехід з Tilda без втрати адрес).

Власник завантажує ZIP-експорт Tilda (файл або посилання export.tildacdn.com) у «Налаштування → Сайт».
CRM розпаковує його у files_dir/tilda_site, читає htaccess (адреса → pageNNN.html) і віддає сторінки за тими
самими адресами на домені сайту. Сторінки, переписані в CRM (site_pages.PAGES), мають пріоритет.
У кожну сторінку додається tilda-bridge.js: форми Tilda → /api/lead (Звернення в CRM з мітками реклами).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

KEY = "tilda_site"
ASSET_DIRS = ("css", "js", "images", "files")
MAX_BYTES = 400 * 1024 * 1024
_cache: dict = {"at": "", "cfg": None}


class TildaError(RuntimeError):
    pass


def root() -> Path:
    return Path(settings.files_dir) / "tilda_site"


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {"map": {}, "index": "", "e404": "", "at": "", "pages": 0, "home": "tilda", "on": True, **d}


def save(db: Session, c: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(c, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def parse_htaccess(text: str) -> tuple[dict, str, str]:
    m: dict[str, str] = {}
    for slug, page in re.findall(r"RewriteRule\s+\^([^\s$]+?)/?\$\s+(page\d+\.html)", text):
        m.setdefault(slug.strip("/").lower(), page)
    idx = re.search(r"DirectoryIndex\s+(\S+)", text)
    e404 = re.search(r"ErrorDocument\s+404\s+/?(\S+)", text)
    return m, (idx.group(1) if idx else ""), (e404.group(1) if e404 else "")


def import_zip(db: Session, data: bytes) -> dict:
    """Розпакувати архів Tilda (з захистом від шляхів ../), замінити попередню версію атомарно."""
    if len(data) > MAX_BYTES:
        raise TildaError("Архів завеликий.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise TildaError("Це не ZIP-архів Tilda.") from e
    names = [n for n in z.namelist() if not n.endswith("/")]
    hta = next((n for n in names if n.rsplit("/", 1)[-1] == "htaccess"), None)
    if not hta:
        raise TildaError("В архіві немає файлу htaccess — це не експорт Tilda.")
    prefix = hta[: -len("htaccess")]
    tmp = root().with_name("tilda_site.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    base = tmp.resolve()
    for n in names:
        if not n.startswith(prefix):
            continue
        rel = n[len(prefix):]
        dst = (tmp / rel).resolve()
        if base not in dst.parents:
            continue                                         # zip-slip
        dst.parent.mkdir(parents=True, exist_ok=True)
        with z.open(n) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out)
    m, idx, e404 = parse_htaccess((tmp / "htaccess").read_text("utf-8", "replace"))
    m = {k: v for k, v in m.items() if (tmp / v).is_file()}
    old = root().with_name("tilda_site.old")
    shutil.rmtree(old, ignore_errors=True)
    if root().exists():
        root().rename(old)
    tmp.rename(root())
    shutil.rmtree(old, ignore_errors=True)
    c = cfg(db)
    c.update(map=m, index=idx if (root() / idx).is_file() else "", e404=e404 if (root() / e404).is_file() else "",
             at=datetime.now().strftime("%d.%m.%Y %H:%M"), pages=len(m))
    save(db, c)
    _cache["at"] = ""
    return c


def fetch(url: str) -> bytes:
    import httpx
    if not re.match(r"^https://export\.tildacdn\.com/export/zips/[\w.-]+\.zip$", url.strip()):
        raise TildaError("Потрібне посилання на архів з export.tildacdn.com.")
    try:
        with httpx.stream("GET", url.strip(), timeout=120, follow_redirects=True) as r:
            if r.status_code != 200:
                raise TildaError(f"Tilda відповіла {r.status_code} — експортуйте архів ще раз.")
            buf = io.BytesIO()
            for ch in r.iter_bytes():
                buf.write(ch)
                if buf.tell() > MAX_BYTES:
                    raise TildaError("Архів завеликий.")
            return buf.getvalue()
    except httpx.HTTPError as e:
        raise TildaError(f"Не вдалося завантажити архів: {e.__class__.__name__}") from e


def _c(db: Session) -> dict:
    c = cfg(db)
    return c if c.get("on") and c.get("map") is not None else {}


def page_file(db: Session, slug: str) -> Path | None:
    c = _c(db)
    if not c:
        return None
    s = slug.strip("/").lower()
    name = c["index"] if s == "" else (c["map"].get(s) or (s if re.fullmatch(r"page\d+\.html", s) else ""))
    p = root() / name if name else None
    return p if p and p.is_file() else None


def not_found(db: Session) -> Path | None:
    c = _c(db)
    p = root() / c["e404"] if c and c.get("e404") else None
    return p if p and p.is_file() else None


def asset(folder: str, path: str) -> Path | None:
    if folder not in ASSET_DIRS:
        return None
    base = (root() / folder).resolve()
    p = (base / path).resolve()
    return p if base in p.parents and p.is_file() else None


def render(db: Session, p: Path, base: str = "") -> str:
    """HTML сторінки Tilda: стара пошта → нова, міст форм у CRM, базова адреса для перегляду в CRM."""
    from . import site_pages
    html = p.read_text("utf-8", "replace")
    email = site_pages.cfg(db).get("email") or "info@molodist.com.ua"
    html = html.replace("info.molodostua@gmail.com", email)
    inj = (f'<script>window.MLDS_TILDA={{"base":{json.dumps(base)}}};</script>'
           f'<script src="/static/site/tilda-bridge.js?v=3" defer></script>')
    return html.replace("</body>", inj + "</body>", 1) if "</body>" in html else html + inj
