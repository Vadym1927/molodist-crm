"""Фото «до / після» з карток у архіві Tilda: вирізаємо окремо «до» й «після» (без фіолетової рамки картки) і ставимо
легкий водяний знак MOLODIST. Оригінали лежать у архіві сайту на сервері; готові файли кешуються в files/site_crops."""
from __future__ import annotations

import re
from pathlib import Path

from ..config import settings

# рамка фото в картці 1024×1280 (частки ширини / висоти): ліва половина — «до», права — «після»; верх обрізано нижче кола з апаратом
B1, B2 = (0.145, 0.45, 0.495, 0.84), (0.505, 0.45, 0.855, 0.84)
_T = "images/tild{}__photo_2025-04-04_150.jpeg"
CARDS = {"pg1": _T.format("3738-3038-4733-a339-653439343832"),     # пігментація
         "pg2": _T.format("3133-3063-4637-b039-633235373963"),     # пігментація
         "pg3": _T.format("3231-3538-4561-a661-383731373739"),     # пігментація
         "sd1": _T.format("6333-3264-4339-b537-363539633161"),     # судини
         "ak8": _T.format("3839-3763-4135-a437-666532316461"),     # акне, IPL
         "ak9": _T.format("3238-3432-4361-b932-363061613261")}     # акне, IPL
_H = "images/tild{}__photo_2025-04-03_213.jpeg"      # картки «гігієна обличчя»
CARDS.update({"sk1": _H.format("3235-3230-4962-a431-653865333633"), "sk2": _H.format("6161-3638-4563-b437-623566376261"),
              "sk3": _H.format("3563-6133-4539-a163-663935613436"),
              "sk4": _H.format("3238-3063-4464-b664-623564613737"), "sk5": _H.format("3366-6162-4530-b138-663462326131")})
CROPS = {f"{k}-{n}.webp": (f, b) for k, f in CARDS.items() for n, b in ((1, B1), (2, B2))}
_S = "images/tild{}.png"                          # сторіз «IPL-терапія · одна процедура»: «до» вгорі, «після» внизу
for _k, _f in (("rz1", "3463-6364-4235-a631-306462386366__img_0546"), ("rz2", "6138-3765-4530-b036-383037356636__img_0549"),
               ("dk1", "3034-3731-4534-a332-393637663635__img_0547")):
    CROPS[f"{_k}-1.webp"] = (_S.format(_f), (0.0, 0.162, 1.0, 0.438))
    CROPS[f"{_k}-2.webp"] = (_S.format(_f), (0.0, 0.446, 1.0, 0.722))
NO_WM = {"ipl-hero.webp", "ipl-hero2.webp", "rz1-1.webp", "rz1-2.webp", "rz2-1.webp", "rz2-2.webp", "dk1-1.webp", "dk1-2.webp", "laser-cab.webp"}   # на цих уже є знак або він не потрібен
CROPS["ipl-hero2.webp"] = ("images/tild6439-6134-4335-b739-626562646263__photo_2024-02-15_110.jpeg", (0.0, 0.0, 1.0, 1.0))
CROPS["laser-cab.webp"] = ("media:laser-2.webp", (0.0, 0.47, 0.355, 0.87))      # кабінет: апарат і столик, без людей
for _k, _f, _b in (("sk-gig", "3136-6333-4937-a139-393630666666____2023-09-08__141149.png", (0.0, 0.30, 1.0, 0.86)),      # методи на сторінці «якість шкіри»
                   ("sk-pil", "6535-3730-4461-b535-646337663265__photo_2024-01-05_180.jpeg", (0.27, 0.0, 0.73, 1.0)),
                   ("sk-care", "3433-6430-4633-a265-616430343330__photo_2024-03-25_134.jpeg", (0.0, 0.12, 1.0, 0.87))):
    CROPS[f"{_k}.webp"] = ("images/tild" + _f, _b)
    NO_WM.add(f"{_k}.webp")
CROPS["ipl-hero.webp"] = ("images/tild6161-3430-4038-a233-396134326666__photo_2024-02-15_110.jpeg", (0.0, 0.02, 1.0, 0.98))
NAME_RE = re.compile(r"^[a-z0-9-]{2,40}\.webp$")
VER = "2"


def folder() -> Path:
    d = Path(settings.files_dir) / "site_crops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(name: str) -> Path | None:
    """Готовий файл (створюється при першому запиті). None — якщо немає оригіналу в архіві."""
    if not NAME_RE.match(name or "") or name not in CROPS:
        return None
    out = folder() / f"v{VER}-{name}"
    if out.is_file():
        return out
    from . import tilda_site as ts
    rel, box = CROPS[name]
    if rel.startswith("media:"):                     # джерело — медіатека сайту
        from . import site_pages as _sp
        src = _sp.media_dir() / rel[6:]
    else:
        src = ts.root() / rel
    if not src.is_file():
        return None
    from PIL import Image
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))
    if name not in NO_WM:
        wm_p = Path(__file__).resolve().parent.parent / "static" / "site" / "brand" / "wordmark-white-600.png"
        if wm_p.is_file():                                   # легкий водяний знак унизу праворуч
            wm = Image.open(wm_p).convert("RGBA")
            ww = int(im.width * 0.34)
            wm = wm.resize((ww, max(1, int(wm.height * ww / wm.width))))
            wm.putalpha(wm.getchannel("A").point(lambda a: int(a * 0.45)))
            im = im.convert("RGBA")
            im.alpha_composite(wm, (im.width - ww - int(im.width * 0.04), im.height - wm.height - int(im.height * 0.03)))
            im = im.convert("RGB")
    im.thumbnail((900, 1200))
    im.save(out, "WEBP", quality=82)
    return out


def url(name: str, base: str) -> str:
    return f"{base}/crop/{name}" if path(name) else ""
