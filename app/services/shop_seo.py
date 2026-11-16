"""SEO категорій і брендів магазину: осмислені title / description замість автозаглушок Shop-Express («Бренд › Molodist.shop»).

Застосовується один раз і лише там, де title ще автозаглушка або порожній, — правки, зроблені вручну в «Категорії магазину», не чіпає.
Адреси сторінок не змінюються."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

TAIL = " Оригінальна продукція, підбір догляду лікарем MOLODIST, доставка по Україні."
BRANDS = {
    "ag-skin": ("AG SKIN", "професійна косметика для домашнього догляду: очищення, сироватки, креми з ретинолом і кислотами, сонцезахист SPF."),
    "demax": ("Demax", "професійна космецевтика: креми для проблемної та чутливої шкіри, ретинол, сонцезахист SPF, догляд після процедур."),
    "genosys": ("Genosys", "дерматологічна косметика для відновлення шкіри: сироватки, креми й догляд після косметологічних процедур."),
    "image": ("Image Skincare", "професійний догляд за шкірою: сироватки з вітаміном C, анти-ейдж креми, засоби для шкіри навколо очей, маски."),
    "innoaesthetics": ("INNOAESTHETICS", "космецевтика для домашнього догляду: тоніки, сонцезахист, засоби проти пігментації й недосконалостей шкіри."),
    "instytutum": ("Instytutum", "преміальний догляд за шкірою обличчя: сироватки, креми й засоби з активними інгредієнтами."),
    "jalupro": ("Jalupro", "догляд для підтримки пружності й відновлення шкіри після косметологічних процедур."),
    "medicare": ("Medicare Professional", "ліпосомальні сироватки й креми: барʼєрне зволоження, освітлення, ліфтинг і догляд за проблемною шкірою."),
    "mediceuticals": ("Mediceuticals", "професійний догляд за волоссям і шкірою голови: сироватки й набори для росту волосся та проти випадіння."),
    "medik8": ("Medik8", "професійна косметика з вітаміном A, вітаміном C і SPF: сироватки, ретиноли, креми та очищення."),
    "meline-2103213618": ("Meline", "дерматологічний догляд проти гіперпігментації: денні й нічні креми та сироватки для рівного тону шкіри."),
    "regenera": ("Regenera (Good Hair)", "лосьйони проти випадіння волосся й пілінг для шкіри голови — догляд для росту та густоти волосся."),
    "rejuran": ("Rejuran", "відновлювальний догляд для шкіри з полінуклеотидами: креми, сироватки й засоби після процедур."),
    "slearshoice": ("ClearChoice", "професійний догляд для обличчя, шиї й декольте: анти-ейдж креми, ліфтинг-сироватки, відновлення після процедур."),
    "tebiskin": ("Tebiskin", "дерматокосметика для жирної, проблемної та чутливої шкіри: очищення, заспокійливі креми, догляд при акне."),
    "zo-skin-health": ("ZO Skin Health", "космецевтика доктора Зейна Обаджі: система догляду для здорової шкіри — очищення, активні сироватки, захист."),
}
CATS = {
    "/shop/brendi/": ("Бренди професійної косметики: ZO Skin Health, Medik8, Medicare | MOLODIST",
                      "Бренди професійної та лікувальної косметики в магазині MOLODIST: ZO Skin Health, Medik8, Medicare, Demax, AG SKIN, Tebiskin, Image Skincare, Rejuran." + TAIL),
    "/shop/oblichchya/": ("Догляд за обличчям — купити професійну косметику для обличчя | MOLODIST",
                          "Професійна косметика для обличчя: очищення, тонізація, активні сироватки, зволоження й анти-ейдж, SPF, маски, догляд за шкірою навколо очей." + TAIL),
    "/shop/oblichchya/kosmetika-dlya-oblichchya/": ("Очищення шкіри обличчя — гелі, пінки, емульсії для вмивання | MOLODIST",
                                                    "Засоби для очищення шкіри обличчя: гелі, пінки й емульсії для жирної, сухої, чутливої та проблемної шкіри." + TAIL),
    "/shop/oblichchya/tonizatsiya/": ("Тоніки для обличчя — купити професійні тоніки й лосьйони | MOLODIST",
                                      "Тоніки й лосьйони для обличчя: зволоження, відновлення pH, кислотні тоніки для проблемної шкіри та пігментації." + TAIL),
    "/shop/oblichchya/aktivni-sirovatki/": ("Сироватки для обличчя — з ретинолом, вітаміном C, кислотами | MOLODIST",
                                            "Активні сироватки для обличчя: ретинол, вітамін C, ніацинамід, пептиди, гіалуронова кислота — проти зморшок, пігментації й акне." + TAIL),
    "/shop/oblichchya/zvolozhennyazhivlennyaanti-eydzh/": ("Креми для обличчя — зволоження, живлення, анти-ейдж | MOLODIST",
                                                           "Креми для обличчя: зволожувальні, живильні й анти-ейдж, денні та нічні, для сухої, чутливої, жирної та зрілої шкіри." + TAIL),
    "/shop/oblichchya/spf/": ("Сонцезахисні креми SPF 30, SPF 50 для обличчя | MOLODIST",
                              "Сонцезахист для обличчя й тіла: креми та флюїди SPF 30 і SPF 50+, з тоном і без, для чутливої та проблемної шкіри." + TAIL),
    "/shop/oblichchya/ochi-ta-gubi/": ("Догляд за шкірою навколо очей і губами — креми, сироватки | MOLODIST",
                                       "Креми й сироватки для шкіри навколо очей і догляд за губами: проти набряків, темних кіл і зморшок." + TAIL),
    "/shop/oblichchya/maski-dlya-oblichchya-136216624/": ("Маски для обличчя — зволожувальні, очищувальні, анти-ейдж | MOLODIST",
                                                          "Професійні маски для обличчя: зволоження, очищення пор, заспокоєння та відновлення шкіри після процедур." + TAIL),
    "/shop/oblichchya/skrabi/": ("Скраби й пілінги для обличчя — домашній догляд | MOLODIST",
                                 "Скраби й ензимні пілінги для делікатного відлущення та рівного тону шкіри обличчя." + TAIL),
    "/shop/oblichchya/sos-zasobi/": ("SOS-засоби для шкіри — проти висипань і подразнень | MOLODIST",
                                     "SOS-засоби локальної дії: проти висипань, почервонінь і подразнень, для швидкого відновлення шкіри." + TAIL),
    "/shop/oblichchya/dekorativna-kosmetika/": ("Декоративна косметика з доглядом — тональні засоби з SPF | MOLODIST",
                                                "Декоративна косметика з доглядовими властивостями: тональні засоби й коректори із SPF для чутливої та проблемної шкіри." + TAIL),
    "/shop/sale/": ("SALE — професійна косметика зі знижками | MOLODIST",
                    "Акційні пропозиції магазину MOLODIST: професійна косметика для обличчя, тіла й волосся зі знижками." + TAIL),
}
AUTO = re.compile(r"(›|—|-)\s*Molodist\.shop\s*$", re.I)


def brand_seo(seg: str, name: str) -> tuple[str, str]:
    nm, what = BRANDS.get(seg, (name, "професійна косметика для домашнього догляду за шкірою."))
    t = f"{nm} — купити косметику {nm.split(' (')[0]} в Україні, ціни | MOLODIST"
    if len(t) > 68:
        t = f"{nm} — купити косметику в Україні, ціни | MOLODIST"
    return t, f"{nm} у магазині MOLODIST: {what}{TAIL}"


def generic(name: str) -> tuple[str, str]:
    return f"{name} — купити професійну косметику | MOLODIST", f"{name}: професійна косметика в магазині MOLODIST.{TAIL}"


def is_auto(title: str, desc: str = "") -> bool:
    return not (title or "").strip() or bool(AUTO.search(title or "")) or "Жмаченка" in (desc or "")


def apply(db: Session, force: bool = False) -> int:
    from . import shop_front as sf
    st = sf.site(db)
    n = 0
    for c in st.get("cats") or []:
        if not force and not is_auto(c.get("title") or "", c.get("desc") or ""):
            continue
        path = c["path"]
        if path in CATS:
            t, d = CATS[path]
        elif path.startswith("/shop/brendi/") and path != "/shop/brendi/":
            t, d = brand_seo(path.strip("/").split("/")[-1], c.get("name") or "")
            if path.strip("/").split("/")[-1] in BRANDS:
                c["name"] = BRANDS[path.strip("/").split("/")[-1]][0]
        elif "MOLODIST" in (c.get("title") or "") and not AUTO.search(c.get("title") or ""):
            continue
        else:
            t, d = generic(c.get("name") or "Каталог")
        c["title"], c["desc"] = t[:300], d[:1000]
        n += 1
    if n:
        sf.save_site(db, st)
    return n


def clean_title(t: str) -> str:
    """Заголовок сторінки без хвоста конструктора: «… › Molodist.shop» → «… | MOLODIST»."""
    t = re.sub(r"\s*(›|\||—|-)\s*Molodist\.shop\s*$", "", t or "", flags=re.I).strip()
    return t if not t or "MOLODIST" in t else f"{t} | MOLODIST"


def clean_desc(d: str, title: str) -> str:
    d = (d or "").strip()
    if not d or re.search(r"—\s*Molodist\.shop\s*$", d, re.I) or "Жмаченка" in d:
        base = re.sub(r"\s*\|\s*MOLODIST\s*$", "", clean_title(title))
        return f"{base} — купити в магазині MOLODIST.{TAIL}"
    return d
