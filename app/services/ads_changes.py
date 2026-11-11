"""Керування рекламою з погодженням: CRM знаходить сміттєві пошукові запити (і приймає інші пропозиції — паузи, ставки,
ключі, оголошення), власник погоджує в CRM або кнопками в Telegram, скрипт Google Ads раз на годину застосовує погоджене
й повертає результат. Без погодження нічого в кабінеті не змінюється; бюджети автоматично не підвищуються."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AdsChange, AdsStat, SyncState, User

KINDS = {"negative": "Мінус-слово", "pause_keyword": "Пауза ключа", "enable_keyword": "Увімкнути ключ", "pause_campaign": "Пауза кампанії",
         "enable_campaign": "Увімкнути кампанію", "budget": "Денний бюджет", "add_keyword": "Новий ключ", "rsa": "Нове оголошення",
         "broad_to_phrase": "Широкі ключі → фразові", "remove_negative": "Прибрати мінус-слово",
         "new_campaign": "Нова кампанія (на паузі)"}
STATUS = {"proposed": "чекає погодження", "approved": "погоджено, застосується протягом години", "rejected": "відхилено",
          "applied": "застосовано", "failed": "помилка"}
CFG_KEY = "ads_changes_cfg"
DEFAULT = {"min_cost_waste": 400.0, "min_clicks_waste": 5, "days": 30, "last_analyze": None, "tg": True}

# сміття для клініки естетичної медицини (Київ)
JUNK = [
    (r"ваканс|робот[аиу]\b|работ[аеу]\b|зарплат|резюме|підробіт|подработ", "пошук роботи"),
    (r"навчан|обучен|курс[иаів]?\b|школ[аи]\b|академі|академи|сертифікац|тренінг|тренинг|майстер-клас|мастер-класс|як стати|как стать", "навчання, курси"),
    (r"своїми руками|своими руками|в домашн|вдома\b|дома\b|самостійно|самостоятельно|рецепт", "зробити самому вдома"),
    (r"безкоштовн|бесплатн|даром|задарма", "безкоштовно"),
    (r"купити|купить|оптом|опт\b|інтернет.магазин|интернет.магазин|розетка|rozetka|prom\.ua|olx|аптек", "купити препарат / товар"),
    (r"що таке|что такое|вікіпед|википед|wiki|реферат|форум|наслідки|последствия|побічн|побочн|протипоказ|противопоказ|шкода|вред", "інформаційні запити"),
    (r"харків|харьков|львів|львов|одес|дніпр(?!овськ|овск)|днепр(?!овск)|запоріж|запорож|вінниц|винниц|полтав|черкас|житомир|\bсуми\b|\bсумы\b|рівн|ровн|луцьк|луцк|"
     r"тернопіл|тернопол|ужгород|франківськ|франковск|кропивн|миколаїв|николаев|херсон|чернівц|черновц|чернігів|чернигов|біла церква|белая церковь|бровар|ірпін|ирпен|буча", "інше місто / передмістя"),
    (r"ветеринар|для собак|для кішок|для кошек|тварин|животн", "тварини"),
]
STOP = {"в", "у", "на", "і", "й", "та", "з", "із", "зі", "до", "для", "по", "від", "и", "с", "в", "к", "от", "у", "ціна", "цена", "київ", "киев", "kyiv"}


def cfg(db: Session) -> dict:
    st = db.get(SyncState, CFG_KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {**DEFAULT, **d}


def save_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=CFG_KEY, value=json.dumps(d, ensure_ascii=False, default=str), updated_at=datetime.utcnow()))
    db.commit()


def _known(db: Session) -> set[str]:
    return {k for (k,) in db.execute(select(AdsChange.key))}


BANNED = re.compile(r"кл[іи]н[іи](к|ц)", re.I)      # у текстах реклами не «клініка», а «медичний простір» (ключі — можна)


def ad_texts(payload: dict | None) -> list[str]:
    p = payload or {}
    out = list(p.get("headlines") or []) + list(p.get("descriptions") or [])
    for g in p.get("groups") or []:
        out += list(g.get("headlines") or []) + list(g.get("descriptions") or [])
    return [str(x) for x in out]


DRUGS = re.compile(r"botox|ботокс|xeomin|ксеомін|ксеомин|dysport|диспорт|neuronox|нейронокс", re.I)   # брендбук: назва процедури, не препарату


def check_texts(payload: dict | None) -> None:
    bad = [t for t in ad_texts(payload) if BANNED.search(t)]
    if bad:
        raise ValueError("У заголовках і описах реклами не вживаємо «клініка» — пишемо «медичний простір»: " + "; ".join(bad[:3]))
    bad = [t for t in ad_texts(payload) if DRUGS.search(t)]
    if bad:
        raise ValueError("У текстах реклами — «ботулінотерапія», без назв препаратів ботулотоксину (Botox, Xeomin, Dysport…): " + "; ".join(bad[:3]))


def propose(db: Session, kind: str, campaign: str | None, target: str | None, reason: str, payload: dict | None = None,
            stats: dict | None = None, author: str = "auto", campaign_id: str | None = None, force: bool = False) -> AdsChange | None:
    if kind not in KINDS:
        raise ValueError(f"Невідомий тип зміни: {kind}")
    check_texts(payload)
    key = f"{kind}|{(campaign or '').strip().lower()}|{(target or '').strip().lower()}"
    if key in _known(db):
        if not force:
            return None
        key = f"{key}|{datetime.now():%Y%m%d%H%M%S}"
    ch = AdsChange(kind=kind, key=key[:400], campaign=campaign, campaign_id=campaign_id, target=(target or "")[:400] or None,
                   payload=payload, reason=reason, stats=stats, author=author)
    db.add(ch)
    db.flush()
    return ch


def analyze(db: Session, today: date | None = None) -> list[AdsChange]:
    """Пошукові запити за N днів → пропозиції мінус-слів: сміттєві за змістом (фразова відповідність по слову-маркеру) і
    дорогі без жодної конверсії (точна відповідність)."""
    c = cfg(db)
    today = today or date.today()
    d0 = today - timedelta(days=int(c["days"]))
    agg: dict = defaultdict(lambda: {"cost": 0.0, "clicks": 0.0, "conv": 0.0, "impr": 0.0})
    camp_ids = {}
    for s_ in db.scalars(select(AdsStat).where(AdsStat.source == "google", AdsStat.level == "campaign", AdsStat.day >= d0)):
        camp_ids[(s_.name or "").strip()] = s_.key
    for s_ in db.scalars(select(AdsStat).where(AdsStat.source == "google", AdsStat.level == "term", AdsStat.day >= d0)):
        a = agg[((s_.parent or "").strip(), (s_.name or s_.key).strip().lower())]
        a["cost"] += s_.cost
        a["clicks"] += s_.clicks
        a["conv"] += s_.conversions
        a["impr"] += s_.impressions
    own: dict = defaultdict(set)                                  # власні ключі кампанії — їх не мінусуємо
    for s_ in db.scalars(select(AdsStat).where(AdsStat.source == "google", AdsStat.level == "keyword", AdsStat.day >= d0 - timedelta(days=60))):
        own[(s_.parent or "").strip()].add(re.sub(r'[\[\]"+]', "", (s_.name or "")).strip().lower())
    new = []
    marker_hits: dict = defaultdict(lambda: {"terms": [], "cost": 0.0, "clicks": 0.0, "why": ""})
    for (camp, term), a in agg.items():
        hit = None
        for rx, why in JUNK:
            m = re.search(rx, term)
            if m:
                word = next((w for w in re.findall(r"[\w'ʼ\-]+", term) if re.search(rx, w)), m.group(0)).strip()
                hit = (word, why)
                break
        if term in own.get(camp, set()):
            continue
        if hit:
            k = marker_hits[(camp, hit[0])]
            k["terms"].append(term)
            k["cost"] += a["cost"]
            k["clicks"] += a["clicks"]
            k["why"] = hit[1]
        elif a["conv"] == 0 and a["cost"] >= float(c["min_cost_waste"]) and a["clicks"] >= float(c["min_clicks_waste"]):
            ch = propose(db, "negative", camp, term, f"Дорогий запит без жодної конверсії за {c['days']} дн.",
                         {"match": "EXACT"}, {"cost": round(a["cost"], 2), "clicks": a["clicks"], "terms": [term]}, campaign_id=camp_ids.get(camp))
            if ch:
                new.append(ch)
    for (camp, word), k in marker_hits.items():
        if len(word) < 3 or word in STOP or any(word in kw.split() for kw in own.get(camp, set())):
            continue
        ch = propose(db, "negative", camp, word, f"Сміття: {k['why']}", {"match": "PHRASE"},
                     {"cost": round(k["cost"], 2), "clicks": k["clicks"], "terms": sorted(k["terms"])[:8]}, campaign_id=camp_ids.get(camp))
        if ch:
            new.append(ch)
    c = cfg(db)
    c["last_analyze"] = datetime.now().isoformat(timespec="minutes")
    save_cfg(db, c)
    db.commit()
    return new


def describe(ch: AdsChange) -> str:
    p = ch.payload or {}
    if ch.kind == "negative":
        t = f"[{ch.target}]" if p.get("match") == "EXACT" else f"\"{ch.target}\""
        return f"мінус {t} у «{ch.campaign}»"
    if ch.kind == "budget":
        return f"бюджет «{ch.campaign}»: {p.get('from', '?')} → {p.get('amount')} /день"
    if ch.kind in ("pause_keyword", "enable_keyword"):
        return f"{KINDS[ch.kind].lower()} «{ch.target}» у «{ch.campaign}»"
    if ch.kind == "add_keyword":
        return f"новий ключ «{ch.target}» ({p.get('match', 'PHRASE')}) у «{ch.campaign} / {p.get('adgroup', '')}»"
    if ch.kind == "new_campaign":
        gs = p.get("groups") or []
        return (f"нова кампанія «{ch.campaign}» на паузі: бюджет {p.get('budget')} /день, груп {len(gs)}, "
                f"ключів {sum(len(g.get('keywords') or []) for g in gs)}, мінус-слів {len(p.get('negatives') or [])}; "
                f"налаштування гео й розкладу — як у «{p.get('copy_from') or 'основній'}»; сторінка {gs[0].get('final_url') if gs else '—'}")
    if ch.kind == "remove_negative":
        return f"прибрати мінус «{ch.target}» у «{ch.campaign}»"
    if ch.kind == "broad_to_phrase":
        return f"усі ввімкнені ключі з широкою відповідністю в «{ch.campaign}» → фразова (широкі на паузу)"
    if ch.kind == "rsa":
        return f"нове оголошення в «{ch.campaign} / {p.get('adgroup', '')}»: {' | '.join((p.get('headlines') or [])[:3])}"
    return f"{KINDS.get(ch.kind, ch.kind)} «{ch.campaign or ch.target}»"


def edit(db: Session, ch: AdsChange, payload: dict) -> bool:
    """Виправити пропозицію, поки скрипт її не застосував (тексти оголошень, ключі, бюджет). Погоджена лишається погодженою."""
    if ch.status not in ("proposed", "approved"):
        return False
    check_texts(payload)
    ch.payload = payload
    db.flush()
    return True


def decide(db: Session, ch: AdsChange, ok: bool, user_id: int | None) -> bool:
    if ch.status != "proposed":
        return False
    ch.status, ch.decided_by, ch.decided_at = ("approved" if ok else "rejected"), user_id, datetime.now()
    db.commit()
    return True


def queue(db: Session) -> list[dict]:
    """Для скрипта Google Ads: погоджені зміни, які ще не застосовано."""
    out = []
    for ch in db.scalars(select(AdsChange).where(AdsChange.status == "approved").order_by(AdsChange.id).limit(200)):
        out.append({"id": ch.id, "kind": ch.kind, "campaign": ch.campaign, "campaign_id": ch.campaign_id, "target": ch.target, **(ch.payload or {})})
    return out


def applied(db: Session, results: list) -> int:
    n = 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        ch = db.get(AdsChange, int(r.get("id") or 0))
        if not ch or ch.status != "approved":
            continue
        ok = bool(r.get("ok"))
        ch.status, ch.applied_at, ch.result = ("applied" if ok else "failed"), datetime.now(), str(r.get("note") or "")[:500] or None
        n += 1
    db.commit()
    return n


# ---------------- Telegram ----------------
def _owners(db: Session) -> list[str]:
    return [str(u.telegram_chat_id) for u in db.scalars(select(User).where(User.role == "owner", User.active.is_(True), User.telegram_chat_id.is_not(None)))]


def tg_text(chs: list[AdsChange]) -> str:
    cost = sum((c.stats or {}).get("cost", 0) for c in chs if c.status == "proposed")
    lines = [f"📣 <b>Google Ads: {len(chs)} пропозицій</b>" + (f" · злили на сміття {cost:,.0f} ₴ без ПДВ".replace(",", " ") if cost else "")]
    icon = {"proposed": "▫️", "approved": "✅", "rejected": "❌", "applied": "🟢", "failed": "⚠️"}
    for i, c in enumerate(chs, 1):
        st = c.stats or {}
        ex = ", ".join((st.get("terms") or [])[:3])
        lines.append(f"{icon.get(c.status, '▫️')} <b>{i}.</b> {escape(describe(c))}"
                     + (f" — {st['cost']:,.0f} ₴, {st.get('clicks', 0):.0f} кл.".replace(",", " ") if st.get("cost") else "")
                     + f"\n   <i>{escape(c.reason or '')}</i>" + (f"\n   запити: {escape(ex)}" if ex and ex != c.target else ""))
    lines.append("\nНатисніть номер, щоб погодити; «Відхилити решту» — закрити інші. Застосує скрипт у кабінеті протягом години.")
    return "\n".join(lines)[:4000]


def tg_kb(chs: list[AdsChange]) -> dict:
    from ..config import settings
    pend = [(i, c) for i, c in enumerate(chs, 1) if c.status == "proposed"]
    rows, row = [], []
    for i, c in pend:
        row.append({"text": f"✅ {i}", "callback_data": f"ads:ok:{c.id}"})
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if pend:
        ids = ".".join(str(c.id) for _, c in pend)
        if len(ids) <= 50:
            rows.append([{"text": "✅ Погодити всі", "callback_data": f"ads:all:{ids}"}, {"text": "❌ Відхилити решту", "callback_data": f"ads:none:{ids}"}])
    rows.append([{"text": "Відкрити в CRM", "url": f"{settings.public_url.rstrip('/')}/marketing/google#changes"}])
    return {"inline_keyboard": rows}


def notify(db: Session, chs: list[AdsChange]) -> int:
    """Нові пропозиції → повідомлення власнику з кнопками (по 10 у повідомленні)."""
    from . import alerts
    if not chs or not cfg(db).get("tg", True):
        return 0
    sent = 0
    for i in range(0, len(chs), 10):
        part = chs[i:i + 10]
        for chat in _owners(db):
            import httpx
            from ..config import settings
            if not settings.telegram_bot_token:
                continue
            try:
                r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", timeout=30,
                               data={"chat_id": chat, "text": tg_text(part), "parse_mode": "HTML", "disable_web_page_preview": "true",
                                     "reply_markup": json.dumps(tg_kb(part), ensure_ascii=False)})
                mid = (r.json().get("result") or {}).get("message_id") if r.status_code == 200 else None
            except Exception:      # noqa: BLE001
                mid = None
            if mid:
                for c in part:
                    c.tg = (c.tg or []) + [[chat, mid]]
                sent += 1
    db.commit()
    return sent


def tg_refresh(db: Session, chat: str, mid: int) -> list[AdsChange]:
    """Зміни, показані в цьому повідомленні Telegram (щоб перемалювати після натискання)."""
    out = []
    for c in db.scalars(select(AdsChange).where(AdsChange.created_at >= datetime.now() - timedelta(days=60)).order_by(AdsChange.id)):
        if any(str(x[0]) == str(chat) and int(x[1]) == int(mid) for x in (c.tg or [])):
            out.append(c)
    return out


def run_daily(db: Session) -> dict:
    new = analyze(db)
    n = notify(db, new)
    return {"new": len(new), "tg": n}
