"""«Фото в стилі MOLODIST»: сервер прибирає фон із фото товару й кладе товар на кремовий фон бренду.

• Видалення фону — модель ISNet (isnet-general-use.onnx, ~170 МБ, та сама, що в rembg) через onnxruntime на CPU.
  Модель завантажується один раз при першій обробці в data/models (поза папкою files — у бекапи не потрапляє).
• Кадр: квадрат 1200×1200, фон #F7F4F0, товар по центру (до 76% кадру), м'яка тінь під товаром, JPEG.
• Нове фото додається в картку, стає головним і показується на сайті; старі фото лишаються в CRM, але
  ховаються із сайту (їх можна повернути галочкою «сайт»). Обробка — фоновою задачею, по черзі."""
from __future__ import annotations

import io
import logging
from pathlib import Path

from ..config import settings

log = logging.getLogger("photo_style")

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
MODEL_NAME = "isnet-general-use.onnx"
SIZE = 1200
BG = (247, 244, 240)          # #F7F4F0 — крем брендбуку
FILL = 0.76                   # частка кадру, яку займає товар (більша сторона)
_session = None


def model_path() -> Path:
    return Path(settings.files_dir).resolve().parent / "models" / MODEL_NAME


def ensure_model(job=None) -> Path:
    p = model_path()
    if p.exists() and p.stat().st_size > 50_000_000:
        return p
    import httpx
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    if job:
        job.log("Завантажую модель видалення фону (~170 МБ, лише перший раз)…")
    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    tmp.replace(p)
    return p


def _sess(job=None):
    global _session
    if _session is None:
        import onnxruntime as ort
        o = ort.SessionOptions()
        o.intra_op_num_threads = 2                  # VPS спільний з іншими сервісами
        _session = ort.InferenceSession(str(ensure_model(job)), o, providers=["CPUExecutionProvider"])
    return _session


def release() -> None:
    global _session
    _session = None


def mask_of(img, job=None):
    """Маска товару 0..255 (PIL «L») того ж розміру, що й фото."""
    import numpy as np
    from PIL import Image
    s = _sess(job)
    im = img.convert("RGB").resize((1024, 1024), Image.LANCZOS)
    a = np.asarray(im).astype(np.float32)
    a = a / max(float(a.max()), 1e-6)
    a = (a - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([1.0, 1.0, 1.0], dtype=np.float32)
    x = a.transpose(2, 0, 1)[None].astype(np.float32)
    out = s.run(None, {s.get_inputs()[0].name: x})[0][:, 0, :, :]
    mn, mx = float(out.min()), float(out.max())
    pred = (out - mn) / max(mx - mn, 1e-6)
    m = Image.fromarray((np.squeeze(pred) * 255).astype("uint8"), mode="L").resize(img.size, Image.LANCZOS)
    return m


def fill_holes(m):
    """Білі ділянки товару на світлому фоні модель часто бачить «дірками» (етикетка, кришка, біла коробка):
    усе, до чого не дійти від краю кадру поза силуетом, — це товар."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    solid = m.point(lambda v: 255 if v > 90 else 0).filter(ImageFilter.MaxFilter(5))
    bb = solid.getbbox()
    if not bb or ((bb[2] - bb[0]) > 0.9 * m.width and (bb[3] - bb[1]) > 0.9 * m.height):
        return m                                                # контур — рамка всього кадру (сцена з фоном): не заливаємо
    out = Image.new("L", (solid.width + 2, solid.height + 2), 0)
    out.paste(solid, (1, 1))
    ImageDraw.floodfill(out, (0, 0), 128)                       # фон, досяжний від краю
    inside = out.crop((1, 1, solid.width + 1, solid.height + 1)).point(lambda v: 0 if v == 128 else 255)
    inside = inside.filter(ImageFilter.MinFilter(5))            # повернути розмір після розширення
    return Image.fromarray(np.maximum(np.asarray(m), np.asarray(inside)))


def compose(img, mask):
    """Товар (за маскою) → квадрат SIZE на кремовому фоні з м'якою тінню. Повертає JPEG-байти."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    rgba = img.convert("RGBA")
    m = fill_holes(mask.point(lambda v: 0 if v < 20 else (255 if v > 160 else round((v - 20) * 255 / 140))))
    rgba.putalpha(m)
    hist = m.histogram()
    if sum(hist[128:]) < 0.04 * m.width * m.height:
        raise ValueError("не вдалося відділити товар від фону — краще інше фото (товар на контрастному фоні)")
    box = m.point(lambda v: 255 if v > 40 else 0).getbbox() or (0, 0, *img.size)
    obj = rgba.crop(box)
    w, h = obj.size
    k = (SIZE * FILL) / max(w, h)
    obj = obj.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    w, h = obj.size
    x, y = (SIZE - w) // 2, (SIZE - h) // 2 + round(SIZE * 0.02)
    # тінь: розмита еліпсна пляма під основою + слабкий розмитий силует, зміщений вниз-праворуч
    base = Image.new("L", (SIZE, SIZE), 0)
    ew, eh = round(w * 0.9), max(12, round(h * 0.06))
    ImageDraw.Draw(base).ellipse((SIZE // 2 - ew // 2, y + h - eh // 2, SIZE // 2 + ew // 2, y + h + eh // 2), fill=90)
    base = base.filter(ImageFilter.GaussianBlur(SIZE * 0.02))
    sil = Image.new("L", (SIZE, SIZE), 0)
    sil.paste(obj.getchannel("A").point(lambda v: round(v * 0.22)), (x + 6, y + 10))
    sil = sil.filter(ImageFilter.GaussianBlur(SIZE * 0.012))
    shadow = Image.fromarray(np.maximum(np.asarray(base), np.asarray(sil)))
    canvas = Image.new("RGB", (SIZE, SIZE), BG)
    canvas = Image.composite(Image.new("RGB", (SIZE, SIZE), (120, 108, 96)), canvas, shadow.point(lambda v: round(v * 0.55)))
    canvas.paste(obj, (x, y), obj)
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=90, optimize=True, progressive=True)
    return buf.getvalue()


def styled_bytes(data: bytes, job=None) -> bytes:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = rgba.getchannel("A")
        w, h = alpha.size
        edge = [alpha.getpixel((x, y)) for x in range(0, w, max(1, w // 40)) for y in (0, h - 1)] + \
               [alpha.getpixel((x, y)) for y in range(0, h, max(1, h // 40)) for x in (0, w - 1)]
        if sum(1 for v in edge if v < 30) > 0.6 * len(edge):  # справді вирізаний товар (прозорі краї) — модель не потрібна
            return compose(rgba.convert("RGB"), alpha)
        bg = Image.new("RGB", rgba.size, (255, 255, 255))      # прозорі куточки webp — на білий і далі як звичайне фото
        bg.paste(rgba, (0, 0), alpha)
        img = bg
    img = trim_frame(img.convert("RGB"))
    return compose(img, mask_of(img, job))


def trim_frame(img):
    """Фото постачальника часто — сцена в однотонній рамці (кремовий/білий кант довкола): модель тоді бачить усю сцену
    як «товар». Обрізаємо рамку кольору кута, якщо вона є з усіх боків."""
    from PIL import Image, ImageChops
    w, h = img.size
    corner = img.getpixel((2, 2))
    diff = ImageChops.difference(img, Image.new("RGB", img.size, corner)).convert("L").point(lambda v: 255 if v > 28 else 0)
    bb = diff.getbbox()
    if not bb:
        return img
    l, t, r, b = bb
    if min(l, t, w - r, h - b) >= 0.02 * min(w, h) and (r - l) * (b - t) > 0.35 * w * h:
        return img.crop(bb)                                   # рамка з усіх боків, а всередині — велика сцена
    return img


def source_file(db, p):
    """Фото для обробки: головне фото товару, інакше перше фото для сайту."""
    from sqlalchemy import select
    from ..models import ProductFile
    from .product_files import photo_link
    fs = db.scalars(select(ProductFile).where(ProductFile.product_id == p.id, ProductFile.kind == "photo").order_by(ProductFile.id)).all()
    fs = [f for f in fs if not (f.name or "").startswith("molodist-style")]
    main = next((f for f in fs if photo_link(f) == p.photo_url), None)
    return main or next((f for f in fs if f.site), None) or (fs[0] if fs else None)


def run(job, pids: list[int], user_id: int | None = None) -> dict:
    from ..models import Product, ProductFile, SessionLocal
    from . import ingest
    from .product_files import photo_link
    rep = {"done": 0, "no_photo": 0, "failed": 0, "ids": []}
    try:
        for pid in pids:
            with SessionLocal() as db:
                p = db.get(Product, pid)
                src = source_file(db, p) if p else None
                if not src or not Path(src.path).exists():
                    rep["no_photo"] += 1
                    job.log(f"#{pid}: немає фото")
                    continue
                try:
                    out = styled_bytes(Path(src.path).read_bytes(), job)
                except Exception as e:  # noqa: BLE001
                    rep["failed"] += 1
                    job.log(f"#{pid}: не вдалося — {e}")
                    log.exception("photo_style %s", pid)
                    continue
                nm = f"molodist-style-{pid}.jpg"
                f = ProductFile(product_id=pid, path=ingest.store(out, nm), name=nm, kind="photo", mime="image/jpeg",
                                uploaded_by=user_id, site=True)
                db.add(f)
                db.flush()
                for o in db.query(ProductFile).filter(ProductFile.product_id == pid, ProductFile.kind == "photo", ProductFile.id != f.id):
                    o.site = False                                  # старі фото — лише в CRM
                p.photo_url = photo_link(f)
                db.commit()
                rep["done"] += 1
                rep["ids"].append(f.id)
                job.log(f"#{pid}: готово ({p.name[:60]})")
    finally:
        release()                                                   # не тримаємо модель у памʼяті між задачами
    try:
        from . import shop_front
        shop_front._cache["cat_at"] = 0.0
    except Exception:  # noqa: BLE001
        pass
    return rep
