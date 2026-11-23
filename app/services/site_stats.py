"""Поведінка відвідувачів на сайті (головна й сторінки болей): що дивились, що клікали, де закривали вкладку.

Скрипт сторінки шле події пачками (navigator.sendBeacon) на /api/ev — без імен, телефонів та IP.
Щоденний звіт за вчора — у Telegram власнику й на сторінці «Звіти → Поведінка на сайті»."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from ..models import SiteEvent

KINDS = {"view", "sec", "click", "form", "pop", "lead", "exit"}
SECTIONS = {"osvita": "Освіта", "pro-likaria": "Про лікаря", "lhero": "Перший екран", "hero": "Перший екран", "facts": "Факти", "pain": "Знайомо?", "reel": "Рілс", "video-rezultat": "Відео: результат", "rezultaty": "Результати",
            "yak-likuiemo": "Як лікуємо", "konsultatsiia": "Консультація", "tsiny": "Ціни", "likar": "Лікар", "likari": "Лікарі",
            "vidhuky": "Відгуки", "zapys": "Форма заявки", "shop": "Магазин", "insta": "Instagram", "zapytannia": "Запитання",
            "kontakty": "Контакти", "shcho-dopomahaie": "Що допомагає", "rezultat": "Результат", "ipl": "IPL-терапія", "shop2": "Магазин", "poslugy": "Послуги", "beauty-plan": "BEAUTY PLAN", "prostir": "Простір"}
PAGES = {"/sofiasirova": "Софія Сирова", "/": "Головна", "/likuvannya-akne": "Акне", "/likuvannya-pigmentatsii": "Пігментація й судини", "/zapys": "Онлайн-запис", "/onlain-zapys": "Онлайн-запис", "/tmyana-znevodnena-shkira": "Тьмяна шкіра", "/zmorshky-ta-vikovi-zminy": "Зморшки", "/dermatology": "Дерматологія",
         "/beautyplan": "BEAUTY PLAN", "/laserepilation": "Лазерна епіляція", "/price": "Прайс"}


def _clean(s, n=120) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


STORE = "store:"                                   # префікс сторінок магазину molodist.store у журналі (сайт клініки — без префікса)
STAFF_HOSTS = ("crm.molodist.com.ua", "cbox.mobi", "shop-express")   # переходи співробітників (з CRM / CleverBox) — не покупці


def is_staff(d: dict) -> bool:
    ref = str(d.get("ref") or "").lower()
    return any(h in ref for h in STAFF_HOSTS)


def ingest(db: Session, d: dict, ua: str = "", site: str = "") -> int:
    from . import site_leads as sl
    sid = re.sub(r"[^a-zA-Z0-9]", "", str(d.get("sid") or ""))[:32]
    page = _clean(d.get("page"), 120).split("?")[0].split("#")[0] or "/"
    page = re.sub(r"^/_(site|shop)", "", page) or "/"
    if site == "store":
        if is_staff(d) and not d.get("utm_source") and not d.get("gclid"):
            return 0                                   # співробітник перейшов з CRM — у звіт не пишемо
        page = (STORE + page)[:120]
    evs = d.get("ev") if isinstance(d.get("ev"), list) else []
    if site == "store":
        evs = [e for e in evs if not (isinstance(e, dict) and e.get("k") == "lead")]   # замовлення пише сервер (order_lead), без дублів
    if not sid or not evs:
        return 0
    paid, src = sl.traffic({k: d.get(k) for k in ("utm_source", "utm_medium", "utm_campaign", "gclid", "ref")})
    dev = sl.device(ua).split(" ")[0]
    dev = {"телефон": "phone", "компʼютер": "pc", "планшет": "tablet"}.get(dev, dev[:12] or None)
    now = datetime.now()
    n = 0
    for e in evs[:60]:
        if not isinstance(e, dict) or e.get("k") not in KINDS:
            continue
        v = e.get("v")
        db.add(SiteEvent(at=now, sid=sid, page=page, kind=e["k"], target=_clean(e.get("t"), 120) or None,
                         value=int(v) if isinstance(v, (int, float)) and 0 <= v < 100000 else None, device=dev, src=src[:80],
                         paid=paid, campaign=_clean(d.get("utm_campaign"), 160) or None))
        n += 1
    db.commit()
    return n


def land(db: Session, page: str, campaign: str = "", ua: str = "") -> None:
    """Сервер побачив перехід з реклами Google (є gclid / gbraid / wbraid) — рахуємо без JavaScript, щоб порівняти з кліками в кабінеті."""
    from . import site_leads as sl
    if re.search(r"bot|crawl|spider|preview|adsbot|mediapartners", ua or "", re.I):
        return
    dev = sl.device(ua).split(" ")[0]
    dev = {"телефон": "phone", "компʼютер": "pc", "планшет": "tablet"}.get(dev, dev[:12] or None)
    db.add(SiteEvent(at=datetime.now(), sid="", page=(STORE + (page or "/"))[:120], kind="land", device=dev, src="Google Ads", paid=True,
                     campaign=_clean(campaign, 160) or None))
    db.commit()


STORE_SECTIONS = {"gal": "Фото", "info": "Ціна й кнопка «В кошик»", "tip": "Порада лікаря", "desc": "Опис", "reviews": "Відгуки", "rel": "З цим купують",
                  "lst": "Список товарів", "flt": "Підкатегорії", "hero": "Перший екран", "cart": "Кошик", "co": "Оформлення", "cab": "Кабінет / вхід"}


def store_page_name(p: str, names: dict | None = None) -> str:
    q = p[len(STORE):] if p.startswith(STORE) else p
    if q == "/":
        return "Головна"
    if q.startswith("/checkout/done"):
        return "Дякуємо (замовлення)"
    if q.startswith("/checkout"):
        return "Оформлення"
    if q.startswith("/cabinet") or q.startswith("/login"):
        return "Кабінет / вхід"
    if names and q in names:
        return names[q]
    return q


def report(db: Session, day: date, site: str = "clinic", names: dict | None = None) -> dict:
    """Звіт за день: сесії (усього / з реклами), по сторінках — блоки, кліки, форми, заявки, де закривали.
    site="store" — магазин molodist.store (форма = почали оформлення, заявка = замовлення, land = перехід з реклами за даними сервера)."""
    a, b = datetime.combine(day, datetime.min.time()), datetime.combine(day + timedelta(days=1), datetime.min.time())
    q = select(SiteEvent).where(SiteEvent.at >= a, SiteEvent.at < b)
    q = q.where(SiteEvent.page.like(STORE + "%")) if site == "store" else q.where(~SiteEvent.page.like(STORE + "%"))
    allrows = db.scalars(q).all()
    lands = [r for r in allrows if r.kind == "land"]
    rows = [r for r in allrows if r.kind != "land"]
    ses = defaultdict(list)
    for r in rows:
        ses[(r.page, r.sid)].append(r)
    out = {"day": day.isoformat(), "site": site, "sessions": len({s for _, s in ses}), "pages": [], "ad_lands": len(lands)}
    by_page = defaultdict(list)
    for (p, s), evs in ses.items():
        by_page[p].append(evs)
    out["paid"] = len({s for (p, s), evs in ses.items() if any(e.paid for e in evs)})
    srcs = Counter(next((e.src for e in evs if e.src), "—") for evs in ses.values())
    out["sources"] = srcs.most_common(6)
    out["devices"] = Counter(next((e.device for e in evs if e.device), "—") for evs in ses.values()).most_common()
    for p, sl in sorted(by_page.items(), key=lambda x: -len(x[1])):
        n = len(sl)
        secs = Counter()
        clicks = Counter()
        exits = Counter()
        times, scrolls, forms, leads, pops = [], [], 0, 0, 0
        for evs in sl:
            seen = {e.target for e in evs if e.kind == "sec" and e.target}
            secs.update(seen)
            clicks.update(e.target for e in evs if e.kind == "click" and e.target)
            forms += any(e.kind == "form" for e in evs)
            pops += any(e.kind == "pop" for e in evs)
            leads += any(e.kind == "lead" for e in evs)
            ex = [e for e in evs if e.kind == "exit"]
            if ex:
                last = ex[-1]
                exits[last.target or "—"] += 1
                if last.value is not None:
                    times.append(last.value)
            sc = [e.value for e in evs if e.kind == "sec" and e.value is not None]
            if sc:
                scrolls.append(max(sc))
        med = sorted(times)[len(times) // 2] if times else None
        sec_names = STORE_SECTIONS if site == "store" else SECTIONS
        out["pages"].append({"page": p[len(STORE):] if p.startswith(STORE) else p, "name": store_page_name(p, names) if site == "store" else PAGES.get(p, p), "sessions": n, "paid": sum(1 for evs in sl if any(e.paid for e in evs)),
                             "median_sec": med, "bounce_10s": sum(1 for t in times if t < 10), "scroll": round(sum(scrolls) / len(scrolls)) if scrolls else None,
                             "forms": forms, "pops": pops, "leads": leads, "conv": round(100 * leads / n, 1) if n else 0,
                             "sections": [(sec_names.get(k, k), v, round(100 * v / n)) for k, v in secs.most_common()],
                             "clicks": clicks.most_common(8), "exits": [(sec_names.get(k, k), v) for k, v in exits.most_common(5)]})
    if site == "store":                                # воронка магазину по сесіях вкладки
        sids = defaultdict(list)
        for r in rows:
            sids[r.sid].append(r)
        def has(evs, pred):
            return any(pred(e) for e in evs)
        out["funnel"] = {
            "sessions": len(sids),
            "product": sum(1 for evs in sids.values() if has(evs, lambda e: e.kind == "view" and e.target == "product")),
            "cart": sum(1 for evs in sids.values() if has(evs, lambda e: e.kind == "click" and (e.target or "").startswith("🛒"))),
            "checkout": sum(1 for evs in sids.values() if has(evs, lambda e: e.kind == "form")),
            "orders": sum(1 for evs in sids.values() if has(evs, lambda e: e.kind == "lead")),
            "paid_sessions": sum(1 for evs in sids.values() if has(evs, lambda e: e.paid)),
            "quick": sum(1 for evs in sids.values() if has(evs, lambda e: e.kind == "exit" and e.value is not None and e.value < 10)
                         and not has(evs, lambda e: e.kind in ("click", "form", "lead"))),
        }
    return out


def text(r: dict) -> str:
    """Короткий звіт для Telegram."""
    store = r.get("site") == "store"
    title = "Магазин molodist.store" if store else "Сайт"
    if not r["sessions"] and not r.get("ad_lands"):
        return f"📊 {title} за {r['day']}: відвідувань не було."
    L = [f"📊 <b>{title} за {r['day']}</b>", f"Відвідувань: <b>{r['sessions']}</b> · з реклами: <b>{r['paid']}</b>",
         "Джерела: " + ", ".join(f"{k} {v}" for k, v in r["sources"]),
         "Пристрої: " + ", ".join(f"{ {'phone': 'телефон', 'pc': 'ПК', 'tablet': 'планшет'}.get(k, k)} {v}" for k, v in r["devices"])]
    if store:
        f = r.get("funnel") or {}
        L += [f"Переходів з реклами Google (сервер): <b>{r.get('ad_lands', 0)}</b> · з них відкрили сторінку повністю: <b>{f.get('paid_sessions', 0)}</b>",
              f"Воронка: відвідали {f.get('sessions', 0)} → товар {f.get('product', 0)} → у кошик {f.get('cart', 0)} → оформлення {f.get('checkout', 0)} → замовлення <b>{f.get('orders', 0)}</b>",
              f"Пішли за 10 с нічого не зробивши: {f.get('quick', 0)}"]
    for p in r["pages"][:8]:
        L += ["", f"<b>{p['name']}</b> ({p['page']}) — {p['sessions']} відв., реклама {p['paid']}",
              f"Час (медіана): {p['median_sec'] or 0} с · пішли до 10 с: {p['bounce_10s']} · прокрутка: {p['scroll'] or 0}%",
              (f"Почали оформлення: {p['forms']} · замовлень: <b>{p['leads']}</b>" if store else f"Відкрили форму: {p['forms']} · банер: {p['pops']} · заявок: <b>{p['leads']}</b> ({p['conv']}%)")]
        if p["sections"]:
            L.append("Дійшли до: " + ", ".join(f"{k} {pc}%" for k, _, pc in p["sections"][:7]))
        if p["clicks"]:
            L.append("Клікали: " + ", ".join(f"{k} ({v})" for k, v in p["clicks"][:5]))
        if p["exits"]:
            L.append("Закривали на: " + ", ".join(f"{k} ({v})" for k, v in p["exits"]))
    return "\n".join(L)[:3900]


def cleanup(db: Session, days: int = 120) -> None:
    db.execute(delete(SiteEvent).where(SiteEvent.at < datetime.now() - timedelta(days=days)))
    db.commit()


def lead_sessions(db: Session, day: date) -> list[dict]:
    """Шлях кожного відвідувача, який залишив заявку за день: усі події його сесії по порядку (без персональних даних)."""
    a = datetime.combine(day, datetime.min.time())
    b = a + timedelta(days=1)
    sids = [r for r in db.scalars(select(SiteEvent.sid).where(SiteEvent.at >= a, SiteEvent.at < b, SiteEvent.kind == "lead").distinct())]
    out = []
    kinds = {"view": "Відкрив сторінку", "sec": "Дійшов до блоку", "click": "Клік", "form": "Відкрив форму", "pop": "Побачив банер",
             "lead": "ЗАЯВКА", "exit": "Закрив / згорнув вкладку"}
    for sid in sids:
        evs = list(db.scalars(select(SiteEvent).where(SiteEvent.sid == sid, SiteEvent.at >= a - timedelta(hours=6), SiteEvent.at < b)
                              .order_by(SiteEvent.at, SiteEvent.id)))
        if not evs:
            continue
        lead = next(e for e in evs if e.kind == "lead")
        rows = []
        for e in evs:
            t = e.target or ""
            if e.kind in ("sec", "exit"):
                t = SECTIONS.get(t, t)
            extra = f" · прокрутка {e.value}%" if e.kind == "sec" and e.value else (f" · на сторінці {e.value} с" if e.kind == "exit" and e.value else "")
            rows.append({"at": e.at.strftime("%H:%M:%S"), "page": e.page, "what": kinds.get(e.kind, e.kind), "t": t + extra, "kind": e.kind})
        out.append({"sid": sid[:8], "lead_at": lead.at.strftime("%H:%M"), "first": evs[0].at.strftime("%H:%M:%S"),
                    "sec_to_lead": int((lead.at - evs[0].at).total_seconds()), "device": evs[0].device, "src": evs[0].src, "paid": bool(evs[0].paid),
                    "pages": list(dict.fromkeys(e.page for e in evs)), "rows": rows})
    return sorted(out, key=lambda x: x["lead_at"])


KINDS_UA = {"view": "Відкрив сторінку", "sec": "Дійшов до блоку", "click": "Клік", "form": "Почав оформлення", "pop": "Побачив банер",
            "lead": "ЗАМОВЛЕННЯ", "exit": "Закрив / згорнув вкладку"}
STORE_SEC = {"gal": "Фото", "info": "Ціна й кнопка «В кошик»", "tip": "Порада лікаря", "desc": "Опис", "reviews": "Відгуки", "rel": "З цим купують",
             "flt": "Підкатегорії", "lst": "Список товарів", "hero": "Перший екран", "co": "Оформлення", "cab": "Кабінет / вхід", "top": "верх сторінки"}


def order_lead(db: Session, sid: str, number: str, total: float, utm: dict | None = None, ua: str = "") -> None:
    """Замовлення в магазині → подія «замовлення» в журналі поведінки одразу на сервері.
    Не залежить від того, чи покупець повернувся з monobank на сторінку «Дякуємо»."""
    from . import site_leads as sl
    sid = re.sub(r"[^a-zA-Z0-9]", "", str(sid or ""))[:32]
    if not sid:
        return
    paid, src = sl.traffic({k: (utm or {}).get(k) for k in ("utm_source", "utm_medium", "utm_campaign", "gclid", "ref")})
    dev = sl.device(ua).split(" ")[0]
    dev = {"телефон": "phone", "компʼютер": "pc", "планшет": "tablet"}.get(dev, dev[:12] or None)
    db.add(SiteEvent(at=datetime.now(), sid=sid, page=STORE + "/checkout/", kind="lead", target=_clean(number, 120), value=int(total or 0) if 0 <= (total or 0) < 100000 else None,
                     device=dev, src=src[:80], paid=paid, campaign=_clean((utm or {}).get("utm_campaign"), 160) or None))


def order_path(db: Session, o, names: dict | None = None) -> dict | None:
    """Шлях покупця на сайті для картки замовлення: сесія з id вкладки, збереженим у замовленні.
    Для старих замовлень (без id) — сесія того дня, де на оформленні клікали відділення / місто цього замовлення."""
    raw = o.raw or {}
    sid = str(raw.get("sid") or "")
    at = o.created_at or datetime.now()
    guess = False
    if not sid:
        a = datetime.combine(at.date(), datetime.min.time())
        keys = [k for k in (str(o.address or "")[:24], str(o.city or "")[:20]) if len(k) >= 4]
        for k in keys:
            row = db.execute(select(SiteEvent.sid).where(SiteEvent.at >= a, SiteEvent.at <= at + timedelta(minutes=10), SiteEvent.page.like(STORE + "/checkout%"),
                                                         SiteEvent.kind == "click", SiteEvent.target.like(f"%{k}%")).order_by(SiteEvent.at.desc())).first()
            if row:
                sid, guess = row[0], True
                break
    if not sid:
        return None
    evs = list(db.scalars(select(SiteEvent).where(SiteEvent.sid == sid, SiteEvent.at >= at - timedelta(days=2), SiteEvent.at <= at + timedelta(hours=2))
                          .order_by(SiteEvent.at, SiteEvent.id)))
    if not evs:
        return None
    rows = []
    for e in evs:
        t = e.target or ""
        if e.kind in ("sec", "exit"):
            t = STORE_SEC.get(t, t)
        elif e.kind == "lead":
            t = f"№{t}"
        extra = f" · прокрутка {e.value}%" if e.kind == "sec" and e.value else (f" · на сторінці {e.value} с" if e.kind == "exit" and e.value else "")
        rows.append({"at": e.at.strftime("%d.%m %H:%M:%S"), "page": store_page_name(e.page, names), "what": KINDS_UA.get(e.kind, e.kind), "t": t + extra, "kind": e.kind})
    pages = list(dict.fromkeys(r["page"] for r in rows))
    return {"guess": guess, "first": evs[0].at.strftime("%d.%m %H:%M"), "mins": int((at - evs[0].at).total_seconds() // 60) if at >= evs[0].at else 0,
            "device": {"phone": "телефон", "pc": "компʼютер", "tablet": "планшет"}.get(evs[0].device or "", evs[0].device or "—"), "src": evs[0].src or "—",
            "paid": bool(evs[0].paid), "pages": pages, "rows": rows}
