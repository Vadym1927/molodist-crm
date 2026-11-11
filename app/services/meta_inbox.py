"""Messenger / Instagram Direct → CRM (вебхук Meta) і відповіді з центру спілкування.

• GET  /api/public/meta/webhook — перевірка Meta (hub.verify_token = verify_token каналу «Instagram та Facebook»).
• POST /api/public/meta/webhook — події: object=page (Messenger) або instagram; підпис X-Hub-Signature-256 = HMAC-SHA256(App Secret).
• Нова розмова → звернення (джерело Instagram / Facebook), якщо за 24 год з цим співрозмовником ще немає відкритого.
• Відповідь пацієнту — Graph API /me/messages токеном сторінки; діє загальний вимикач «повідомлення пацієнтам» (messaging).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _secrets
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Appeal, ChatMessage, Patient
from . import channels as ch

log = logging.getLogger("meta_inbox")
GRAPH = "https://graph.facebook.com/v19.0"
IGRAPH = "https://graph.instagram.com/v21.0"           # Instagram API з входом через Instagram (токен IG, не сторінки)
CH_LABEL = {"facebook": "Facebook", "instagram": "Instagram", "telegram": "Telegram"}


def meta_channel(db: Session) -> dict | None:
    return next((c for c in ch.channels(db) if c["type"] == "meta"), None)


def _raw_meta(db: Session) -> dict | None:
    return next((c for c in ch._raw(db) if c["type"] == "meta"), None)


def verify_token(db: Session) -> str:
    """Verify token для Meta — генерується один раз і зберігається в параметрах каналу."""
    c = meta_channel(db)
    if not c:
        return ""
    tok = (c.get("params") or {}).get("verify_token")
    if not tok:
        tok = "molodist-" + _secrets.token_urlsafe(18)
        ch.update(db, c["id"], None, bool(c.get("on")), {"verify_token": tok}, {})
    return tok


def _sig_keys(db: Session) -> list[str]:
    c = _raw_meta(db)
    if not c:
        return []
    return [v.strip() for v in (ch.secret(c, "app_secret"), ch.secret(c, "ig_app_secret")) if v and v.strip()]


def check_signature(db: Session, body: bytes, header: str | None) -> bool:
    """Підпис Meta: HMAC-SHA256 тіла ключем застосунку. Messenger підписує App Secret застосунку Meta,
    Instagram API з входом через Instagram — окремим Instagram App Secret; приймаємо будь-який із двох."""
    keys = _sig_keys(db)
    if not keys:
        log.warning("Meta webhook: App Secret не задано — підпис не перевіряється")
        return True
    if not header or not header.startswith("sha256="):
        return False
    return any(hmac.compare_digest(hmac.new(k.encode(), body, hashlib.sha256).hexdigest(), header[7:]) for k in keys)


def signature_hint(db: Session, body: bytes) -> str:
    """Підказка в журнал: звідки подія і скільки ключів перевірено (самі ключі не показуємо)."""
    import json as _j
    try:
        obj = (_j.loads(body or b"{}") or {}).get("object") or "?"
    except ValueError:
        obj = "?"
    n = len(_sig_keys(db))
    if obj == "instagram":
        return f"подія Instagram, перевірено ключів: {n}. Для Instagram потрібен Instagram App Secret (застосунок → Instagram → API setup with Instagram login)"
    return f"подія {obj}, перевірено ключів: {n}. Потрібен App Secret того застосунку, де налаштовано вебхук (Налаштування застосунку → Основні)"


def _page_token(db: Session) -> str:
    c = _raw_meta(db)
    return ch.secret(c, "page_token") if c else ""


def _ig_token(db: Session) -> str:
    c = _raw_meta(db)
    return (ch.secret(c, "ig_token") or "").strip() if c else ""


def _ig_get(db: Session, sid: str, fields: str, http: httpx.Client | None = None) -> dict:
    tok = _ig_token(db)
    if not tok:
        return {}
    try:
        js = (http or httpx.Client(timeout=10)).get(f"{IGRAPH}/{sid}", params={"access_token": tok, "fields": fields}).json()
    except Exception:  # noqa: BLE001
        return {}
    return {} if js.get("error") else js


def _profile(db: Session, channel: str, sid: str, http: httpx.Client | None = None) -> str | None:
    if channel == "instagram" and _ig_token(db):
        js = _ig_get(db, sid, "name,username", http)
        u = js.get("username")
        if u or js.get("name"):
            return (f"@{u}" + (f" · {js['name']}" if js.get("name") else "")) if u else js.get("name")
    tok = _page_token(db)
    if not tok:
        return None
    fields = "name,username" if channel == "instagram" else "first_name,last_name,name"
    try:
        js = (http or httpx.Client(timeout=10)).get(f"{GRAPH}/{sid}", params={"access_token": tok, "fields": fields}).json()
    except Exception:  # noqa: BLE001
        return None
    if js.get("error"):
        return None
    if channel == "instagram":
        u = js.get("username")
        return (f"@{u}" + (f" · {js['name']}" if js.get("name") else "")) if u else js.get("name")
    return js.get("name") or " ".join(filter(None, (js.get("first_name"), js.get("last_name")))) or None


def _patient_for(db: Session, channel: str, name: str | None) -> Patient | None:
    if channel != "instagram" or not name or not name.startswith("@"):
        return None
    u = name[1:].split(" ")[0].strip().lower()
    if not u:
        return None
    for p in db.scalars(select(Patient).where(Patient.instagram.is_not(None))):
        v = (p.instagram or "").strip().lower().lstrip("@")
        v = v.rstrip("/").split("/")[-1] if "instagram.com" in v else v
        if v == u:
            return p
    return None


IGNORE_ATT = {"story_mention", "like_heart", "reel", "ig_reel", "share", "fallback", "unsupported_type"}


def is_direct(ev: dict) -> bool:
    """Лише справжнє повідомлення в Direct / Messenger від пацієнта: текст або фото / відео / файл / голосове.
    Ігноруємо: реакції (reaction), лайки, відповіді-реакції на сторіз, згадки в сторіз, репости, «ехо» власних
    повідомлень адміністратора, видалені й непідтримувані, прочитання / доставку. Коментарі приходять як changes — не сюди."""
    msg = ev.get("message") or {}
    if not msg or ev.get("reaction") or msg.get("is_echo") or msg.get("is_deleted") or msg.get("is_unsupported"):
        return False
    rt = msg.get("reply_to") or {}
    if rt.get("story"):                                    # відповідь / реакція на сторіз
        return False
    atts = msg.get("attachments") or []
    if any((a.get("type") or "") in IGNORE_ATT for a in atts):
        return False
    if msg.get("sticker_id") and not msg.get("text"):       # лайк у Messenger = стікер «👍»
        return False
    return bool((msg.get("text") or "").strip() or atts)


def _appeal_for(db: Session, channel: str, sid: str, pat_id: int | None, name: str | None, text: str | None) -> int | None:
    """Звернення для вхідного: відкрите звернення цього діалогу чи пацієнта — продовжуємо його;
    є відкрита задача по пацієнту — нового звернення не створюємо; інакше — нове звернення по каналу.
    Адміністратор закриває звернення — наступне вхідне відкриє нове."""
    from ..models import Task
    from . import appeals as ap
    ids = {i for (i,) in db.execute(select(ChatMessage.appeal_id).where(ChatMessage.thread == sid, ChatMessage.appeal_id.is_not(None)))}
    q = select(Appeal).where(Appeal.closed_at.is_(None), Appeal.deleted_at.is_(None))
    open_ = db.scalars(q.where(Appeal.id.in_(ids)).order_by(Appeal.id.desc())).first() if ids else None
    if not open_ and pat_id:
        open_ = db.scalars(q.where(Appeal.patient_id == pat_id).order_by(Appeal.id.desc())).first()
    if open_:
        return open_.id
    if pat_id and db.scalar(select(Task.id).where(Task.patient_id == pat_id, Task.status == "open").limit(1)):
        return None
    a = ap.create(db, {"name": name or f"{CH_LABEL[channel]} …{sid[-4:]}", "source": CH_LABEL[channel], "patient_id": pat_id,
                       "comment": f"Вхідне повідомлення в {CH_LABEL[channel]}: {(text or '[вкладення]')[:300]}"})
    return a.id


def ingest(db: Session, payload: dict, http: httpx.Client | None = None) -> int:
    """Подія вебхука → нові ChatMessage: вхідні від пацієнта й «ехо» — відповіді адміністратора, написані в самому Instagram /
    Messenger (вихідні в тій самій переписці; власні відповіді з CRM не дублюються — той самий mid). Повертає кількість нових."""
    obj = payload.get("object")
    channel = "instagram" if obj == "instagram" else "facebook"
    n = 0
    for entry in payload.get("entry") or []:
        own = str(entry.get("id") or "")
        for ev in entry.get("messaging") or []:
            msg = ev.get("message") or {}
            if msg.get("is_echo") and not msg.get("is_deleted") and not msg.get("is_unsupported"):
                n += _echo(db, channel, ev, msg, http, own)   # відповідь, яку адміністратор написав у самому Instagram / Messenger
                continue
            if not msg or not is_direct(ev):          # реакції, лайки, сторіз, прочитання — не Direct
                continue
            sid = str((ev.get("sender") or {}).get("id") or "")
            if not sid or sid == own:
                continue
            mid = msg.get("mid")
            if mid and db.scalar(select(ChatMessage.id).where(ChatMessage.mid == mid)):
                continue
            prev = db.scalars(select(ChatMessage).where(ChatMessage.thread == sid).order_by(ChatMessage.id.desc())).first()
            name = prev.name if prev and prev.name else _profile(db, channel, sid, http)
            pat_id = prev.patient_id if prev and prev.patient_id else ((p.id) if (p := _patient_for(db, channel, name)) else None)
            atts = [{"type": a.get("type"), "url": (a.get("payload") or {}).get("url")} for a in msg.get("attachments") or []] or None
            ts = ev.get("timestamp")
            at = datetime.utcfromtimestamp(ts / 1000) if isinstance(ts, (int, float)) else datetime.utcnow()
            m = ChatMessage(at=at, channel=channel, thread=sid, direction="in", text=(msg.get("text") or "")[:4000] or None,
                            attachments=atts, mid=mid, name=name, patient_id=pat_id)
            m.appeal_id = _appeal_for(db, channel, sid, pat_id, name, m.text)
            db.add(m)
            db.commit()
            n += 1
    return n


def _echo(db: Session, channel: str, ev: dict, msg: dict, http: httpx.Client | None = None, own: str = "") -> int:
    rid = str((ev.get("recipient") or {}).get("id") or "")
    if rid == own:                                       # ехо приходить від акаунта клініки до співрозмовника
        return 0
    mid = msg.get("mid")
    text = (msg.get("text") or "").strip()
    atts = [{"type": a.get("type"), "url": (a.get("payload") or {}).get("url")} for a in msg.get("attachments") or []
            if (a.get("type") or "") not in IGNORE_ATT] or None
    if not rid or not (text or atts):
        return 0
    if mid and db.scalar(select(ChatMessage.id).where(ChatMessage.mid == mid)):
        return 0                                         # це наша відповідь з CRM — вже є
    prev = db.scalars(select(ChatMessage).where(ChatMessage.thread == rid).order_by(ChatMessage.id.desc())).first()
    if not prev:                                         # адміністратор першим написав людині — переписку все одно показуємо
        name = _profile(db, channel, rid, http)
    else:
        name = prev.name
    ts = ev.get("timestamp")
    at = datetime.utcfromtimestamp(ts / 1000) if isinstance(ts, (int, float)) else datetime.utcnow()
    db.add(ChatMessage(at=at, channel=channel, thread=rid, direction="out", text=text[:4000] or None, attachments=atts, mid=mid,
                       name=name, patient_id=prev.patient_id if prev else None, appeal_id=prev.appeal_id if prev else None,
                       status="sent", read_at=datetime.utcnow()))
    db.commit()
    return 1


def threads(db: Session, channel: str = "", q: str = "") -> list[dict]:
    """Діалоги: останнє повідомлення, непрочитані, імʼя."""
    rows = db.execute(select(ChatMessage.thread, func.max(ChatMessage.id)).group_by(ChatMessage.thread)).all()
    last = {m.id: m for m in db.scalars(select(ChatMessage).where(ChatMessage.id.in_([i for _, i in rows])))} if rows else {}
    unread = dict(db.execute(select(ChatMessage.thread, func.count()).where(ChatMessage.direction == "in", ChatMessage.read_at.is_(None))
                             .group_by(ChatMessage.thread)).all())
    out = []
    for t, i in rows:
        m = last.get(i)
        if not m or (channel and m.channel != channel):
            continue
        nm = db.scalar(select(ChatMessage.name).where(ChatMessage.thread == t, ChatMessage.name.is_not(None)).limit(1))
        if q and q.lower() not in (nm or "").lower() and q.lower() not in (m.text or "").lower():
            continue
        aid = m.appeal_id or db.scalar(select(ChatMessage.appeal_id).where(ChatMessage.thread == t, ChatMessage.appeal_id.is_not(None))
                                       .order_by(ChatMessage.id.desc()).limit(1))
        ap_ = db.get(Appeal, aid) if aid else None
        out.append({"thread": t, "channel": m.channel, "closed": bool(ap_ and ap_.closed_at), "appeal_status": ap_.status if ap_ else None, "name": nm or f"{CH_LABEL.get(m.channel, m.channel)} …{t[-4:]}", "last": m,
                    "unread": unread.get(t, 0), "patient_id": db.scalar(select(ChatMessage.patient_id).where(
                        ChatMessage.thread == t, ChatMessage.patient_id.is_not(None)).limit(1))})
    out.sort(key=lambda x: x["last"].at, reverse=True)
    return out


REPLY_KEY = "meta_chat_replies"          # ручні відповіді в чатах — окремо від автоматичних SMS/Viber/Telegram (за замовч. увімкнено)


def replies_enabled(db: Session) -> bool:
    from ..models import SyncState
    st = db.get(SyncState, REPLY_KEY)
    return not (st and st.value == "off")


def set_replies(db: Session, on: bool) -> None:
    from ..models import SyncState
    st = db.get(SyncState, REPLY_KEY) or SyncState(key=REPLY_KEY)
    st.value, st.updated_at = ("on" if on else "off"), datetime.utcnow()
    db.merge(st)
    db.commit()


def reply(db: Session, thread: str, text: str, user_id: int | None = None, http: httpx.Client | None = None) -> ChatMessage:
    """Ручна відповідь адміністратора в Messenger / Instagram (лише у відповідь пацієнту — тип RESPONSE, вікно 24 год).
    Автоматичні повідомлення (нагадування, розсилки) сюди не йдуть; окремий вимикач — replies_enabled."""
    last = db.scalars(select(ChatMessage).where(ChatMessage.thread == thread).order_by(ChatMessage.id.desc())).first()
    if not last:
        raise ValueError("Діалог не знайдено")
    m = ChatMessage(channel=last.channel, thread=thread, direction="out", text=text[:2000], user_id=user_id,
                    patient_id=last.patient_id, appeal_id=last.appeal_id, name=last.name)
    if not replies_enabled(db):
        m.status, m.error = "paused", "Відповіді в чатах вимкнено (Налаштування → Канали)"
    elif last.channel == "telegram":                    # Telegram Business — від імені акаунта клініки
        from . import tg_business as tb
        ok, mid, err = tb.send(db, thread, text, http)
        m.status, m.mid, m.error = ("sent" if ok else "failed"), mid, err
    elif last.channel == "instagram" and _ig_token(db):     # Instagram API з входом через Instagram
        try:
            js = (http or httpx.Client(timeout=20)).post(f"{IGRAPH}/me/messages", params={"access_token": _ig_token(db)},
                                                         json={"recipient": {"id": thread}, "message": {"text": text[:1000]}}).json()
        except Exception as e:  # noqa: BLE001
            js = {"error": {"message": str(e)[:200]}}
        if js.get("error"):
            m.status, m.error = "failed", (js["error"].get("message") or "помилка")[:300]
        else:
            m.status, m.mid = "sent", js.get("message_id")
    elif not _page_token(db):
        m.status, m.error = "failed", "Немає токена сторінки (Налаштування → Канали → Instagram та Facebook)"
    else:
        try:
            js = (http or httpx.Client(timeout=20)).post(f"{GRAPH}/me/messages", params={"access_token": _page_token(db)},
                                                         json={"recipient": {"id": thread}, "message": {"text": text[:2000]},
                                                               "messaging_type": "RESPONSE"}).json()
        except Exception as e:  # noqa: BLE001
            js = {"error": {"message": str(e)[:200]}}
        if js.get("error"):
            m.status, m.error = "failed", (js["error"].get("message") or "помилка")[:300]
        else:
            m.status, m.mid = "sent", js.get("message_id")
    db.add(m)
    db.commit()
    return m


def mark_read(db: Session, thread: str) -> None:
    now = datetime.utcnow()
    for m in db.scalars(select(ChatMessage).where(ChatMessage.thread == thread, ChatMessage.direction == "in", ChatMessage.read_at.is_(None))):
        m.read_at = now
    db.commit()


HOOK_KEY = "meta_webhook_log"           # останні виклики вебхука — діагностика в Налаштування → Канали


def hook_log(db: Session, kind: str, note: str = "") -> None:
    """Записати виклик вебхука (без вмісту повідомлень): час, тип, коротка примітка. Зберігаємо 15 останніх."""
    import json
    from ..models import SyncState
    try:
        st = db.get(SyncState, HOOK_KEY) or SyncState(key=HOOK_KEY)
        rows = json.loads(st.value or "[]")
        rows = ([{"at": datetime.utcnow().isoformat(timespec="seconds"), "kind": kind, "note": note[:160]}] + rows)[:15]
        st.value, st.updated_at = json.dumps(rows, ensure_ascii=False), datetime.utcnow()
        db.merge(st)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def hook_events(db: Session) -> list[dict]:
    import json
    from ..models import SyncState
    st = db.get(SyncState, HOOK_KEY)
    try:
        rows = json.loads(st.value) if st and st.value else []
    except ValueError:
        rows = []
    for r in rows:
        try:
            r["at"] = datetime.fromisoformat(r["at"])
        except (KeyError, ValueError):
            r["at"] = None
    return rows


def summarize(payload: dict) -> str:
    """Коротко про подію без тексту: object, скільки подій і яких типів."""
    kinds: dict[str, int] = {}
    for e in payload.get("entry") or []:
        for ev in e.get("messaging") or []:
            k = ("message" if is_direct(ev) else "ignored") if ev.get("message") else next(
                (x for x in ("reaction", "read", "delivery", "postback", "referral") if ev.get(x)), "other")
            kinds[k] = kinds.get(k, 0) + 1
        for c in e.get("changes") or []:
            k = f"change:{c.get('field')}"
            kinds[k] = kinds.get(k, 0) + 1
    return f"{payload.get('object') or '?'} · " + (", ".join(f"{k}×{v}" for k, v in kinds.items()) or "порожньо")


SUB_FIELDS = "messages,messaging_postbacks"


def subscription(db: Session, http: httpx.Client | None = None) -> tuple[bool | None, str]:
    """Чи підписана сторінка на наш застосунок (GET /me/subscribed_apps токеном сторінки)."""
    tok = _page_token(db)
    if not tok or tok.startswith("IG"):
        return None, ""
    try:
        js = (http or httpx.Client(timeout=15)).get(f"{GRAPH}/me/subscribed_apps", params={"access_token": tok}).json()
    except Exception as e:  # noqa: BLE001
        return None, f"не вдалося перевірити підписку ({str(e)[:80]})"
    if js.get("error"):
        return None, f"підписку не перевірено: {js['error'].get('message', '')[:120]}"
    apps = js.get("data") or []
    if not apps:
        return False, "сторінку НЕ підписано на вебхук — натисніть «Підписати сторінку»"
    flds = sorted({(f.get("name") or "") if isinstance(f, dict) else str(f)          # нові версії Graph: [{"name": "messages", ...}]
                   for a in apps for f in (a.get("subscribed_fields") or [])} - {""})
    ok = "messages" in flds
    return ok, ("сторінку підписано: " + (", ".join(flds) or "без полів") + ("" if ok else " — немає messages, натисніть «Підписати сторінку»"))


def subscribe(db: Session, http: httpx.Client | None = None) -> tuple[bool, str]:
    tok = _page_token(db)
    if not tok:
        return False, "Немає токена сторінки"
    try:
        js = (http or httpx.Client(timeout=15)).post(f"{GRAPH}/me/subscribed_apps",
                                                     params={"access_token": tok, "subscribed_fields": SUB_FIELDS}).json()
    except Exception as e:  # noqa: BLE001
        return False, f"Немає звʼязку: {str(e)[:120]}"
    if js.get("error"):
        m = js["error"].get("message", "помилка")[:200]
        if js["error"].get("code") in (10, 200):
            m += " — потрібен дозвіл pages_manage_metadata у токені сторінки"
        return False, f"Meta: {m}"
    return bool(js.get("success")), "Сторінку підписано на messages, messaging_postbacks" if js.get("success") else "Meta не підтвердила підписку"


_AV: dict[str, tuple[float, bytes, str]] = {}     # аватари співрозмовників: thread → (час, байти, тип) — у памʼяті, 12 год


def avatar(db: Session, thread: str, http: httpx.Client | None = None) -> tuple[bytes, str] | None:
    """Фото профілю співрозмовника (profile_pic через Graph токеном сторінки). Посилання Meta тимчасові — тож кешуємо байти."""
    import time
    if thread.startswith("tg:"):                         # Telegram Business — фото з Meta немає
        return None
    hit = _AV.get(thread)
    if hit and time.time() - hit[0] < 12 * 3600:
        return (hit[1], hit[2]) if hit[1] else None
    tok = _page_token(db)
    data, ctype = b"", ""
    if tok or _ig_token(db):
        cl = http or httpx.Client(timeout=10, follow_redirects=True)
        try:
            url = _ig_get(db, thread, "profile_pic", cl).get("profile_pic") if _ig_token(db) else None
            if not url and tok:
                js = cl.get(f"{GRAPH}/{thread}", params={"access_token": tok, "fields": "profile_pic"}).json()
                url = js.get("profile_pic") if not js.get("error") else None
            if url:
                r = cl.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) < 2_000_000:
                    data, ctype = r.content, r.headers["content-type"]
        except Exception:  # noqa: BLE001
            pass
    _AV[thread] = (time.time(), data, ctype)
    if len(_AV) > 500:
        _AV.pop(next(iter(_AV)))
    return (data, ctype) if data else None


_LAT = [("shch", "щ"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ch", "ч"), ("sh", "ш"), ("yu", "ю"), ("ya", "я"), ("ye", "є"),
        ("yi", "ї"), ("iu", "ю"), ("ia", "я"), ("ie", "є"), ("a", "а"), ("b", "б"), ("v", "в"), ("h", "г"), ("g", "г"), ("d", "д"),
        ("e", "е"), ("z", "з"), ("y", "и"), ("i", "і"), ("j", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
        ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"), ("c", "к"), ("w", "в"), ("x", "кс"), ("q", "к")]


def to_cyr(s: str) -> str:
    """Латиниця → кирилиця для пошуку (Vadym Bondar → вадим бондар). Кирилицю не чіпаємо."""
    s, out, i = (s or "").lower(), [], 0
    while i < len(s):
        for lat, cyr in _LAT:
            if s.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def suggest(db: Session, name: str | None, limit: int = 5) -> list[Patient]:
    """Схожі пацієнти за іменем співрозмовника (імʼя + прізвище, латиницю переводимо в кирилицю; @нік — за Instagram)."""
    from . import search as srch
    if not name or name[:1] == "…":
        return []
    out: list[Patient] = []
    if name.startswith("@") and (p := _patient_for(db, "instagram", name)):
        out.append(p)
        name = name.split("·", 1)[1] if "·" in name else ""
    raw = [w for w in (name or "").replace("@", " ").replace("·", " ").split() if len(w) >= 3 and not w.isdigit()][:3]
    if not raw:
        return out
    variants = {" ".join(raw), " ".join(to_cyr(w) for w in raw)}
    # «Vadym» → «вадим», «Bondar» → «бондар»; закінчення можуть відрізнятися — беремо основу слова
    stems = [srch.qwords(" ".join(w[:max(3, len(w) - 2)] for w in v.split())) for v in variants]
    for p in db.scalars(select(Patient).where(Patient.full_name.is_not(None))):
        if p in out:
            continue
        if any(st and srch.match(st, p.full_name) for st in stems):
            out.append(p)
            if len(out) >= limit:
                break
    return out


def close_thread(db: Session, thread: str, status: str, reason: str | None, user_id: int | None) -> int:
    """Закрити діалог = закрити відкриті звернення з нього вказаним статусом (Записаний / Відмова / Не цільове).
    Наступне вхідне повідомлення відкриє нове звернення. Повертає кількість закритих."""
    from . import appeals as ap
    ids = {i for (i,) in db.execute(select(ChatMessage.appeal_id).where(ChatMessage.thread == thread, ChatMessage.appeal_id.is_not(None)))}
    n = 0
    for a in db.scalars(select(Appeal).where(Appeal.id.in_(ids or {0}), Appeal.closed_at.is_(None))):
        ap.set_status(db, a, status, user_id, reason or None)
        n += 1
    last = db.scalars(select(ChatMessage).where(ChatMessage.thread == thread).order_by(ChatMessage.id.desc())).first()
    if last and last.direction != "sys":                 # межа діалогу: далі — новий діалог (у картці пацієнта окремо)
        db.add(ChatMessage(channel=last.channel, thread=thread, direction="sys", status="closed", user_id=user_id,
                           text=f"Діалог закрито · {status}" + (f" ({reason})" if reason else ""), name=last.name,
                           patient_id=last.patient_id, appeal_id=last.appeal_id))
        db.commit()
    mark_read(db, thread)
    return n


def sessions(msgs: list[ChatMessage], appeals: dict | None = None) -> list[dict]:
    """Розбити переписку на окремі діалоги «від відкриття до закриття»: межа — позначка закриття (sys)
    або зміна звернення. Повертає нові першими: {start, end, closed, result, msgs, channel, thread}."""
    appeals = appeals or {}
    out: list[dict] = []
    cur: dict | None = None
    for m in msgs:
        if m.direction == "sys":
            if cur:
                cur.update(end=m.at, closed=True, result=(m.text or "").replace("Діалог закрито · ", ""))
                cur = None
            continue
        if cur and m.appeal_id and cur["appeal_id"] and m.appeal_id != cur["appeal_id"]:
            a = appeals.get(cur["appeal_id"])
            cur.update(end=(a.closed_at if a and a.closed_at else cur["msgs"][-1].at), closed=bool(a and a.closed_at),
                       result=(a.status if a else ""))
            cur = None
        if cur is None:
            cur = {"start": m.at, "end": None, "closed": False, "result": "", "msgs": [], "channel": m.channel, "thread": m.thread,
                   "appeal_id": m.appeal_id, "name": m.name}
            out.append(cur)
        cur["msgs"].append(m)
        cur["appeal_id"] = cur["appeal_id"] or m.appeal_id
        cur["name"] = cur["name"] or m.name
    for c in out:                                   # старі закриті звернення без позначки
        a = appeals.get(c["appeal_id"])
        if not c["closed"] and a and a.closed_at and a.closed_at >= c["msgs"][-1].at:
            c.update(end=a.closed_at, closed=True, result=a.status)
    out.sort(key=lambda c: c["start"], reverse=True)
    return out


def refresh_names(db: Session, limit: int = 50, http: httpx.Client | None = None) -> int:
    """Діалоги Instagram без імені → @username та імʼя з Instagram API (після додавання IG-токена)."""
    if not _ig_token(db):
        return 0
    named = {t for (t,) in db.execute(select(ChatMessage.thread).where(ChatMessage.channel == "instagram", ChatMessage.name.is_not(None)).distinct())}
    todo = [t for (t,) in db.execute(select(ChatMessage.thread).where(ChatMessage.channel == "instagram").distinct()) if t not in named][:limit]
    n = 0
    for t in todo:
        nm = _profile(db, "instagram", t, http)
        if not nm:
            continue
        for m in db.scalars(select(ChatMessage).where(ChatMessage.thread == t)):
            m.name = nm
        p = _patient_for(db, "instagram", nm)
        if p:
            for m in db.scalars(select(ChatMessage).where(ChatMessage.thread == t, ChatMessage.patient_id.is_(None))):
                m.patient_id = p.id
        n += 1
    db.commit()
    return n


IG_REFRESH_KEY = "ig_token_refreshed"


def ig_refresh(db: Session, http: httpx.Client | None = None, force: bool = False) -> bool:
    """Довгостроковий IG-токен живе 60 днів — продовжуємо раз на тиждень (GET refresh_access_token)."""
    from ..models import SyncState
    tok, c = _ig_token(db), _raw_meta(db)
    if not tok or not c:
        return False
    st = db.get(SyncState, IG_REFRESH_KEY)
    if not force and st and st.updated_at and (datetime.utcnow() - st.updated_at) < timedelta(days=7):
        return False
    try:
        js = (http or httpx.Client(timeout=20)).get("https://graph.instagram.com/refresh_access_token",
                                                    params={"grant_type": "ig_refresh_token", "access_token": tok}).json()
    except Exception:  # noqa: BLE001
        return False
    new = js.get("access_token")
    if not new:
        log.warning("IG token refresh: %s", (js.get("error") or {}).get("message"))
        return False
    ch.update(db, c["id"], None, bool(c.get("on")), {}, {"ig_token": new})
    db.merge(SyncState(key=IG_REFRESH_KEY, value="ok", updated_at=datetime.utcnow()))
    db.commit()
    return True
