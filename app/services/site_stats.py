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
SECTIONS = {"lhero": "Перший екран", "hero": "Перший екран", "facts": "Факти", "pain": "Знайомо?", "reel": "Рілс", "rezultaty": "Результати",
            "yak-likuiemo": "Як лікуємо", "konsultatsiia": "Консультація", "tsiny": "Ціни", "likar": "Лікар", "likari": "Лікарі",
            "vidhuky": "Відгуки", "zapys": "Форма заявки", "shop": "Магазин", "insta": "Instagram", "zapytannia": "Запитання",
            "kontakty": "Контакти", "poslugy": "Послуги", "beauty-plan": "BEAUTY PLAN", "prostir": "Простір"}
PAGES = {"/": "Головна", "/likuvannya-akne": "Акне"}


def _clean(s, n=120) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def ingest(db: Session, d: dict, ua: str = "") -> int:
    from . import site_leads as sl
    sid = re.sub(r"[^a-zA-Z0-9]", "", str(d.get("sid") or ""))[:32]
    page = _clean(d.get("page"), 120).split("?")[0].split("#")[0] or "/"
    page = re.sub(r"^/_site", "", page) or "/"
    evs = d.get("ev") if isinstance(d.get("ev"), list) else []
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


def report(db: Session, day: date) -> dict:
    """Звіт за день: сесії (усього / з реклами), по сторінках — блоки, кліки, форми, заявки, де закривали."""
    a, b = datetime.combine(day, datetime.min.time()), datetime.combine(day + timedelta(days=1), datetime.min.time())
    rows = db.scalars(select(SiteEvent).where(SiteEvent.at >= a, SiteEvent.at < b)).all()
    ses = defaultdict(list)
    for r in rows:
        ses[(r.page, r.sid)].append(r)
    out = {"day": day.isoformat(), "sessions": len({s for _, s in ses}), "pages": []}
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
        out["pages"].append({"page": p, "name": PAGES.get(p, p), "sessions": n, "paid": sum(1 for evs in sl if any(e.paid for e in evs)),
                             "median_sec": med, "bounce_10s": sum(1 for t in times if t < 10), "scroll": round(sum(scrolls) / len(scrolls)) if scrolls else None,
                             "forms": forms, "pops": pops, "leads": leads, "conv": round(100 * leads / n, 1) if n else 0,
                             "sections": [(SECTIONS.get(k, k), v, round(100 * v / n)) for k, v in secs.most_common()],
                             "clicks": clicks.most_common(8), "exits": [(SECTIONS.get(k, k), v) for k, v in exits.most_common(5)]})
    return out


def text(r: dict) -> str:
    """Короткий звіт для Telegram."""
    if not r["sessions"]:
        return f"📊 Сайт за {r['day']}: відвідувань не було."
    L = [f"📊 <b>Сайт за {r['day']}</b>", f"Відвідувань: <b>{r['sessions']}</b> · з реклами: <b>{r['paid']}</b>",
         "Джерела: " + ", ".join(f"{k} {v}" for k, v in r["sources"]),
         "Пристрої: " + ", ".join(f"{ {'phone': 'телефон', 'pc': 'ПК', 'tablet': 'планшет'}.get(k, k)} {v}" for k, v in r["devices"])]
    for p in r["pages"][:4]:
        L += ["", f"<b>{p['name']}</b> ({p['page']}) — {p['sessions']} відв., реклама {p['paid']}",
              f"Час (медіана): {p['median_sec'] or 0} с · пішли до 10 с: {p['bounce_10s']} · прокрутка: {p['scroll'] or 0}%",
              f"Відкрили форму: {p['forms']} · банер: {p['pops']} · заявок: <b>{p['leads']}</b> ({p['conv']}%)"]
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
