"""IP-телефонія (як у CleverBox): Binotel, Stream Telecom, Ringostat.
Налаштування — SyncState telephony_cfg (ключі зашифровано). Вхідний дзвінок з вебхука провайдера →
запис у журнал дзвінків, пацієнт за номером і (за бажанням) нове звернення з вибраним джерелом."""
from __future__ import annotations

import json
import re
import secrets as _secrets
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appeal, Call, SyncState
from . import appeals as ap

PROVIDERS = {
    "binotel": {"label": "Binotel", "site": "https://www.binotel.ua", "fields": [("key", "Ключ API (key)", False), ("secret", "Секрет API (secret)", True)],
                "hint": "Кабінет Binotel → Налаштування → API. Вебхук — у «API CALL COMPLETED»."},
    "stream": {"label": "Stream Telecom", "site": "https://stream-telecom.com.ua", "fields": [("login", "Логін API", False), ("token", "Токен API", True)],
               "hint": "Дані API — у менеджера Stream Telecom. Вебхук подій дзвінків — адреса нижче."},
    "ringostat": {"label": "Ringostat", "site": "https://ringostat.com", "fields": [("project_id", "Project ID", False), ("auth_key", "Auth-key", True)],
                  "hint": "Значення можна дізнатися в особистому кабінеті Ringostat у розділі Інтеграція → Ringostat API."},
}
SECRET = {f for p in PROVIDERS.values() for f, _, sec in p["fields"] if sec}
# що показувати у вікні «Вхідний дзвінок» (Налаштування → IP-телефонія)
POPUP_FIELDS = [("avatar", "Фото пацієнта"), ("tag", "Статус пацієнта (Первинний / Постійний / ФАНАТ)"), ("employee", "Хто приймає дзвінок"),
                ("appt", "Найближчий запис (дата, лікар, послуги)"), ("last", "Останній візит"), ("visits", "Кількість візитів"),
                ("tasks", "Відкриті задачі"), ("bonus", "Бонуси"), ("plan", "Активний бюті-план"), ("note", "Замітка з картки пацієнта"),
                ("birthday", "День народження (тиждень до / після)")]
# свої поля на картці дзвінка (без медичних даних): ключ → (назва, функція значення)
POPUP_EXTRA = [("groups", "Групи пацієнта"), ("mark", "Мітка VIP / BAD"), ("discount", "Персональна знижка"), ("balance", "Баланс рахунку"),
               ("ltv", "Сума покупок (LTV)"), ("avg", "Середній чек"), ("source", "Джерело залучення"), ("pref_doctor", "Улюблений лікар"),
               ("drink", "Улюблений напій"), ("directions", "Напрямки"), ("channel", "Канал звʼязку"), ("no_repeat", "Не дзвонити при повторному візиті"),
               ("first_visit", "Перший візит"), ("email", "Email"), ("instagram", "Instagram"), ("text", "Свій текст (підказка адміністратору)")]


def popup_extra_value(p, key: str, text: str = "") -> str | None:
    """Значення свого поля для пацієнта (рядок) або None, якщо порожньо."""
    def money(v):
        return f"{float(v):,.0f} ₴".replace(",", " ") if v else None
    if key == "text":
        return (text or "").strip()[:200] or None
    if p is None:
        return None
    v = {"groups": lambda: ", ".join(str(g.get("name") if isinstance(g, dict) else g) for g in (p.client_groups or [])) or None,
         "mark": lambda: {"vip": "VIP", "bad": "BAD"}.get((p.cb_mark or "").lower()),
         "discount": lambda: (" / ".join(x for x in [f"товари {p.disc_products_pct:g}%" if p.disc_products_pct else "",
                                                   f"послуги {p.disc_services_pct:g}%" if p.disc_services_pct else ""] if x)
                              or (f"{p.discount_pct:g}%" if p.discount_pct else None)),
         "balance": lambda: money(p.cb_balance), "ltv": lambda: money(p.cb_total), "avg": lambda: money(p.cb_average),
         "source": lambda: p.source, "pref_doctor": lambda: p.preferred_doctor, "drink": lambda: p.drink,
         "directions": lambda: ", ".join(p.directions or []) or None, "channel": lambda: p.communication_channel,
         "no_repeat": lambda: "так" if p.no_repeat_call else None,
         "first_visit": lambda: p.cb_first_visit.strftime("%d.%m.%Y") if p.cb_first_visit else None,
         "email": lambda: p.email, "instagram": lambda: p.instagram}.get(key)
    try:
        r = v() if v else None
    except Exception:          # noqa: BLE001
        r = None
    return str(r)[:160] if r else None


DEFAULT_CRITERIA = """Оціни розмову адміністратора медичного простору MOLODIST з пацієнтом за 100-бальною шкалою:
1. Привітання: назвала клініку й себе, звернулась на «ви» (10)
2. Виявила потребу: що турбує, яка процедура цікавить, чи був раніше (20)
3. Експертність і впевненість: пояснила процедуру, ціну, тривалість, підготовку (20)
4. Пропозиція запису: запропонувала конкретний час, лікаря (20)
5. Робота із запереченнями (ціна, страх, «подумаю») (15)
6. Завершення: підсумувала домовленість, попрощалась, ввічливість і тон (15)
Якщо пацієнта не записано — поясни чому і що треба було зробити інакше."""


_CRIT_RE = re.compile(r"^\s*\d+[.)]\s*(.+?)\s*\((\d+)\)\s*$")


def crit_rows(text: str) -> list[dict]:
    """Текст критеріїв → рядки [{title, desc, pts}] (рядки «N. Назва: опис (бали)»)."""
    out = []
    for ln in (text or "").splitlines():
        m = _CRIT_RE.match(ln)
        if not m:
            continue
        body, pts = m.group(1), int(m.group(2))
        t, _, dsc = body.partition(":")
        out.append({"title": t.strip(), "desc": dsc.strip(), "pts": pts})
    return out


def crit_text(rows: list[dict], tail: str = "") -> str:
    head = "Оціни розмову адміністратора медичного простору MOLODIST з пацієнтом за 100-бальною шкалою:"
    lines = [f"{i}. {r['title']}" + (f": {r['desc']}" if r.get("desc") else "") + f" ({int(r['pts'])})" for i, r in enumerate(rows, 1)]
    return "\n".join([head] + lines + ([tail] if tail else []))


def crit_tail(text: str) -> str:
    """Рядки після переліку критеріїв (напр. «Якщо пацієнта не записано…»)."""
    ls = (text or "").splitlines()
    last = max([i for i, ln in enumerate(ls) if _CRIT_RE.match(ln)], default=-1)
    return "\n".join(ls[last + 1:]).strip() if last >= 0 else ""


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def _raw(db: Session) -> dict:
    st = db.get(SyncState, "telephony_cfg")
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("provider", "")
    d.setdefault("source", "Дзвінок")
    d.setdefault("create_appeal", True)
    d.setdefault("missed_only", False)
    d.setdefault("p", {})
    d.setdefault("popup_users", [])
    d.setdefault("popup_fields", [k for k, _ in POPUP_FIELDS])          # кому показувати вікно «Вхідний дзвінок» (порожньо — усім з правом «Звернення» / «Запис»)
    d.setdefault("popup_custom", [])         # свої поля на картці дзвінка: [{key, label, text}]
    d.setdefault("ai_on", False)             # AI-оцінка діалогів (Claude) за критеріями
    d.setdefault("ai_criteria", DEFAULT_CRITERIA)
    d.setdefault("ai_alert_below", 60)       # оцінка нижче — повідомлення власнику в Telegram
    d.setdefault("ai_alert", True)
    d.setdefault("ai_task_to", "")           # відповідальний за обробку поганих дзвінків: id | "taker" | "resp" | "" (як раніше — повідомлення)
    d.setdefault("ai_task_tg", True)         # і повідомити його в Telegram
    d.setdefault("ai_alert_to", [])          # кому: id користувачів + "taker" (хто говорив) + "resp" (відповідальний за лінію); порожньо — власнику
    d.setdefault("staff", [])                # співробітники: {user_id, ext (ID у телефонії), alias (імʼя в телефонії)}
    d.setdefault("lines", [])                # лінії: {number, name, user_id (відповідальний)}
    d.setdefault("ai_engine", "crm")         # crm — оцінює Claude з CRM; agent — зовнішній агент (OpenClaw) через API v1 /calls
    if not d.get("hook"):
        d["hook"] = _secrets.token_urlsafe(18)
    return d


def _save(db: Session, d: dict) -> None:
    st = db.get(SyncState, "telephony_cfg") or SyncState(key="telephony_cfg")
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def public(db: Session) -> dict:
    d = _raw(db)
    if not (db.get(SyncState, "telephony_cfg")):
        _save(db, d)                                         # закріпити секрет вебхука
    out = {k: v for k, v in d.items() if k != "p"}
    out["p"] = {}
    for prov, vals in d["p"].items():
        out["p"][prov] = {k: (bool(v) if k in SECRET else v) for k, v in vals.items()}
    return out


def secret(d: dict, prov: str, key: str) -> str:
    v = (d["p"].get(prov) or {}).get(key)
    if not v:
        return ""
    if key not in SECRET:
        return v
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def save_extra(db: Session, popup_users: list[int], ai_on: bool, ai_criteria: str, ai_alert_below, ai_alert: bool,
               ai_engine: str = "crm", popup_fields: list[str] | None = None, ai_alert_to: list[str] | None = None,
               ai_task_to: str | None = None, ai_task_tg: bool | None = None, popup_custom: list[dict] | None = None) -> None:
    d = _raw(db)
    if ai_task_to is not None:
        d["ai_task_to"] = ai_task_to if ai_task_to in ("taker", "resp") or str(ai_task_to).isdigit() else ""
    if ai_task_tg is not None:
        d["ai_task_tg"] = bool(ai_task_tg)
    if ai_alert_to is not None:
        d["ai_alert_to"] = [x if x in ("taker", "resp") else int(x) for x in ai_alert_to if x in ("taker", "resp") or str(x).isdigit()]
    if popup_fields is not None:
        d["popup_fields"] = [k for k, _ in POPUP_FIELDS if k in popup_fields]
    if popup_custom is not None:
        keys = {k for k, _ in POPUP_EXTRA}
        d["popup_custom"] = [{"key": r["key"], "label": " ".join(str(r.get("label") or "").split())[:60],
                              "text": str(r.get("text") or "").strip()[:200]} for r in popup_custom if r.get("key") in keys][:20]
    try:
        thr = max(0, min(100, int(float(ai_alert_below))))
    except (TypeError, ValueError):
        thr = 60
    d.update(popup_users=[int(x) for x in popup_users if str(x).isdigit()], ai_on=bool(ai_on),
             ai_criteria=(ai_criteria or "").strip()[:4000] or DEFAULT_CRITERIA, ai_alert_below=thr, ai_alert=bool(ai_alert),
             ai_engine=ai_engine if ai_engine in ("crm", "agent") else "crm")
    _save(db, d)


def popup_for(db: Session, user) -> bool:
    from ..security import can
    d = _raw(db)
    if d.get("popup_users"):
        return user.id in d["popup_users"]
    return can(user, "appeals") or can(user, "book")


def save(db: Session, provider: str, vals: dict, source: str, create_appeal: bool, missed_only: bool) -> None:
    d = _raw(db)
    if provider and provider not in PROVIDERS:
        raise ValueError("Невідомий провайдер")
    d.update(provider=provider, source=source.strip()[:80] or "Дзвінок", create_appeal=create_appeal, missed_only=missed_only)
    if provider:
        cur = d["p"].setdefault(provider, {})
        for f, _, sec in PROVIDERS[provider]["fields"]:
            v = str(vals.get(f) or "").strip()
            if sec:
                if v:
                    cur[f] = _cipher().encrypt(v.encode()).decode()
            else:
                cur[f] = v[:120]
    _save(db, d)


def test(db: Session, http: httpx.Client | None = None) -> tuple[bool, str]:
    d = _raw(db)
    prov = d.get("provider")
    if not prov:
        return False, "Оберіть провайдера"
    http = http or httpx.Client(timeout=20)
    try:
        if prov == "ringostat":
            pid, key = secret(d, prov, "project_id"), secret(d, prov, "auth_key")
            if not (pid and key):
                return False, "Вкажіть Project ID і Auth-key"
            now = datetime.now()
            kw = dict(headers={"Auth-key": key, "x-project-id": pid},
                      params={"from": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "to": now.strftime("%Y-%m-%d %H:%M:%S"),
                              "fields": "calldate"})
            try:
                r = http.get("https://api.ringostat.net/calls/list", **kw)
            except httpx.ConnectError:
                try:                                          # IPv6 / мережа контейнера — пробуємо явно через IPv4
                    with httpx.Client(timeout=20, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as h4:
                        r = h4.get("https://api.ringostat.net/calls/list", **kw)
                except httpx.ConnectError as e:
                    return False, (f"Ключі збережено. API Ringostat недоступний із сервера CRM ({str(e)[:80]}) — це не заважає дзвінкам: "
                                   "вони приходять у CRM вебхуками. Перевірка ключів спрацює, коли сервер матиме доступ до api.ringostat.net.")
            if r.status_code >= 400:
                return False, f"Ringostat: {r.status_code} — перевірте Project ID / Auth-key"
            try:
                n = len(r.json())
            except ValueError:
                n = 0
            return True, f"Підключено · дзвінків за добу: {n}"
        if prov == "binotel":
            key, sec = secret(d, prov, "key"), secret(d, prov, "secret")
            if not (key and sec):
                return False, "Вкажіть key і secret"
            js = http.post("https://api.binotel.com/api/4.0/settings/list-of-employees.json", json={"key": key, "secret": sec}).json()
            if js.get("status") != "success":
                return False, f"Binotel: {js.get('message') or 'помилка'}"
            return True, f"Підключено · співробітників: {len(js.get('listOfEmployees') or {})}"
        if prov == "stream":
            if not secret(d, prov, "token"):
                return False, "Вкажіть токен"
            return True, "Дані збережено · підключення перевіримо за першим вебхуком дзвінка"
    except Exception as e:  # noqa: BLE001
        return False, f"Немає звʼязку: {str(e)[:150]}"
    return False, "?"


def log_hook(db: Session, provider: str, event: str, data: dict) -> None:
    """Останні 100 вебхуків (для перевірки налаштування подій у кабінеті провайдера)."""
    st = db.get(SyncState, "telephony_hook_log") or SyncState(key="telephony_hook_log")
    try:
        xs = json.loads(st.value) if st.value else []
    except ValueError:
        xs = []
    xs.insert(0, {"at": datetime.now().strftime("%d.%m %H:%M:%S"), "provider": provider, "event": event or "—",
                  "data": {str(k)[:40]: str(v)[:200] for k, v in list(data.items())[:40]}})
    st.value, st.updated_at = json.dumps(xs[:100], ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def hook_log(db: Session) -> list[dict]:
    st = db.get(SyncState, "telephony_hook_log")
    try:
        return json.loads(st.value) if st and st.value else []
    except ValueError:
        return []


def live(db: Session, seconds: int = 120) -> list[Call]:
    """Дзвінки, що зараз ідуть (подія «перед дзвінком» / «підняття трубки») — для спливаючого вікна."""
    return db.scalars(select(Call).where(Call.status.in_(("ringing", "answer")), Call.at >= datetime.now() - timedelta(seconds=seconds))
                      .order_by(Call.at.desc()).limit(5)).all()


def _pick(data: dict, *keys):
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return v
    return None


# Тіло вебхука для Ringostat (Webhooks 2.0 → JSON, змінні з «$»). event: ringing (перед дзвінком), answer (підняття трубки), end (після дзвінка)
RINGOSTAT_BODY = ('{"event":"%s","call_id":"$cdr_id","uid":"$uniqueid","caller":"$userfield","callee":"$dst","status":"$disposition",'
                  '"date":"$calldate","call_duration":"$duration","dialog":"$billsec","type":"$call_type","manager":"$staffid",'
                  '"employee_fio":"$employee_fio","record_link":"$recording_wav",'
                  '"utm_source":"$utm_source","utm_medium":"$utm_medium","utm_campaign":"$utm_campaign","utm_term":"$utm_term",'
                  '"utm_content":"$utm_content","gclid":"$gclid","landing":"$landing","pool":"$pool_name"}')
RINGOSTAT_EVENTS = [("ringing", "Перед дзвінком", "спливаюче вікно «Вхідний дзвінок» у CRM: хто дзвонить, картка пацієнта"),
                    ("answer", "У момент підняття трубки", "позначка, що дзвінок прийнято"),
                    ("end", "Після дзвінка", "журнал дзвінків, тривалість, запис розмови, звернення для нових номерів"),
                    ("end", "Після вихідного дзвінка", "вихідні дзвінки в журналі й історії пацієнта")]


def _digits(v) -> str | None:
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[-12:] if len(d) >= 5 else None


def _emp(v) -> str | None:
    """Імʼя співробітника з вебхука; «0», «-», порожнє — ніхто не прийняв."""
    v = " ".join(str(v or "").split())[:120]
    return None if v in ("", "0", "-", "—", "null", "None") or v.startswith("$") else v


def _utm(v, n: int = 120) -> str | None:
    v = str(v or "").strip()
    return None if not v or v.startswith("$") or v.lower() in ("null", "none", "undefined", "(not set)", "-") else v[:n]


def parse(provider: str, data: dict) -> dict:
    """Нормалізуємо вебхук будь-якого з провайдерів (поля з документації + типові назви)."""
    data = {k: v for k, v in data.items() if not (isinstance(v, str) and v.startswith("$"))}   # незаповнені змінні Ringostat
    flat = dict(data)
    for k, v in data.items():                                # Binotel: callDetails[externalNumber]
        if k.startswith("callDetails[") and k.endswith("]"):
            flat[k[12:-1]] = v
    if isinstance(data.get("callDetails"), dict):
        flat.update(data["callDetails"])
    direction = "in"
    ct = str(_pick(flat, "callType", "call_type", "direction", "type") or "").lower()
    if ct in ("1", "out", "outgoing", "outbound", "outcoming", "callback"):
        direction = "out"
    disp = str(_pick(flat, "disposition", "status", "call_status") or "").upper()
    status = "answered" if disp in ("ANSWER", "ANSWERED", "PROPER", "SUCCESS", "PROPER_ANSWERED") else ("missed" if disp else None)
    if disp in ("NOANSWER", "NO ANSWER", "BUSY", "CANCEL", "FAILED", "MISSED", "CHANUNAVAIL", "VM", "VM-SUCCESS"):
        status = "missed"
    try:
        dur = int(float(_pick(flat, "billsec", "dialog", "talk_time", "duration", "call_duration") or 0))
    except (TypeError, ValueError):
        dur = None
    phone = _pick(flat, "externalNumber", "caller", "call_number", "callerNumber", "phone", "src", "from", "client_phone")
    if direction == "out":
        phone = _pick(flat, "externalNumber", "dst", "callee", "recive_number", "receive_number", "to", "phone", "client_phone") or phone
    return {"ext_id": str(_pick(flat, "generalCallID", "callID", "uniqueid", "uid", "call_id", "id", "ID") or "")[:120] or None,
            "direction": direction, "phone": str(phone or "")[:64] or None, "status": status, "duration": dur,
            "employee": _emp(_pick(flat, "employeeName", "employee_fio", "employee", "connected", "reciver", "receiver", "dst_name")),
            "agent_ext": _emp(_pick(flat, "manager", "staffid", "staff_id", "employee_id", "internalNumber", "internal")),
            "line": _digits(_pick(flat, "callee", "dst", "did", "source", "recive_number", "receive_number", "pbxNumber", "line")
                            if direction == "in" else _pick(flat, "caller", "src", "pbxNumber", "line")),
            "record_url": str(_pick(flat, "linkToCallRecordInMyBusiness", "recording", "recording_wav", "record_link", "record_url", "record") or "")[:500] or None,
            # колтрекінг: джерело дзвінка (Ringostat підставляє utm-мітки сеансу; Binotel GetCall — utm у callDetails)
            "utm_source": _utm(_pick(flat, "utm_source", "utmSource", "source_utm")), "utm_medium": _utm(_pick(flat, "utm_medium", "utmMedium")),
            "utm_campaign": _utm(_pick(flat, "utm_campaign", "utmCampaign", "campaign"), 300), "utm_term": _utm(_pick(flat, "utm_term", "utmTerm", "keyword"), 300),
            "utm_content": _utm(_pick(flat, "utm_content", "utmContent"), 300), "gclid": _utm(_pick(flat, "gclid", "clientid_google", "google_click_id"), 300),
            "landing": _utm(_pick(flat, "landing", "landing_page", "fullUrl", "page"), 500)}


def ingest(db: Session, provider: str, data: dict, event: str = "") -> Call:
    """event: ringing (дзвінок іде — спливаюче вікно), answer (підняли трубку), end / порожньо (дзвінок завершено)."""
    d = _raw(db)
    event = (event or str(data.get("event") or "")).strip().lower()
    if event.startswith("$"):
        event = ""
    x = parse(provider, data)
    if not event and x["status"] is None and not x["duration"] and not x["record_url"]:
        event = "ringing"                 # подія без ?event=: немає статусу й тривалості — дзвінок ще йде
    if event == "ai":
        return ingest_ai(db, provider, data)
    if event in ("ringing", "answer"):
        x["status"] = event
        x["duration"] = None
    c = db.scalars(select(Call).where(Call.provider == provider, Call.ext_id == x["ext_id"])).first() if x["ext_id"] else None
    if not c and x["phone"]:                                  # той самий дзвінок з попередньої події (інший id у Ringostat)
        k_ = ap.phone_key(x["phone"])
        c = next((z for z in db.scalars(select(Call).where(Call.provider == provider, Call.status.in_(("ringing", "answer")),
                                                         Call.at >= datetime.now() - timedelta(minutes=30))).all()
                  if ap.phone_key(z.phone) == k_), None)
    if not c:
        c = Call(provider=provider, at=datetime.now())
        db.add(c)
    for k, v in x.items():
        if v is not None and not (k == "ext_id" and c.ext_id):
            setattr(c, k, v)
    if event == "end" and c.status in ("ringing", "answer"):
        c.status = "answered" if c.status == "answer" or (c.duration or 0) > 0 else "missed"
    p = ap.find_patient(db, c.phone)
    c.patient_id = p.id if p else c.patient_id
    if not c.user_id:
        c.user_id = staff_user(d, c.agent_ext, c.employee) or (_user_by_name(db, c.employee) if c.employee else None)
    db.flush()
    if event in ("ringing", "answer"):
        db.commit()
        return c
    want = d.get("create_appeal") and c.direction == "in" and c.phone and not c.appeal_id and not ap.is_ignored(db, c.phone)
    if want and d.get("missed_only") and c.status != "missed":
        want = False
    if want:
        k = ap.phone_key(c.phone)
        since = datetime.utcnow() - timedelta(hours=24)
        recent = [a for a in db.scalars(select(Appeal).where(Appeal.created_at >= since, Appeal.deleted_at.is_(None),
                                                             Appeal.phone.is_not(None))) if ap.phone_key(a.phone) == k]
        if recent:
            c.appeal_id = recent[0].id
        else:
            a = ap.create(db, {"phone": c.phone, "name": p.full_name if p else None, "source": d.get("source") or "Дзвінок",
                               "comment": f"Вхідний дзвінок ({PROVIDERS.get(provider, {}).get('label', provider)})"
                                          + (" — пропущений" if c.status == "missed" else "")})
            c.appeal_id = a.id
    db.commit()
    return c


# ---------------- співробітники й лінії ----------------
def _lk(v) -> str:
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[-9:]


def staff_user(d: dict, ext: str | None, name: str | None) -> int | None:
    """Хто взяв дзвінок: за ID у телефонії, потім за імʼям у телефонії (з налаштувань «Співробітники»)."""
    from .search import qwords
    for r in d.get("staff") or []:
        if ext and str(r.get("ext") or "").strip() and str(r["ext"]).strip() == str(ext).strip():
            return int(r["user_id"])
    nm = set(qwords(name or ""))
    for r in d.get("staff") or []:
        al = set(qwords(r.get("alias") or ""))
        if nm and al and al <= nm:
            return int(r["user_id"])
    return None


def line_info(d: dict, number: str | None) -> dict | None:
    k = _lk(number)
    if not k:
        return None
    for r in d.get("lines") or []:
        if _lk(r.get("number")) == k:
            return r
    return None


def save_staff(db: Session, staff: list[dict], lines: list[dict]) -> None:
    d = _raw(db)
    d["staff"] = [{"user_id": int(r["user_id"]), "ext": str(r.get("ext") or "").strip()[:40], "alias": " ".join(str(r.get("alias") or "").split())[:120]}
                  for r in staff if str(r.get("user_id") or "").isdigit()]
    d["lines"] = [{"number": "".join(ch for ch in str(r.get("number") or "") if ch.isdigit() or ch == "+")[:20],
                   "name": " ".join(str(r.get("name") or "").split())[:60],
                   "user_id": int(r["user_id"]) if str(r.get("user_id") or "").isdigit() else None}
                  for r in lines if _lk(r.get("number"))]
    _save(db, d)


def relink(db: Session, days: int = 60) -> int:
    """Після зміни співробітників — перепривʼязати недавні дзвінки до користувачів CRM."""
    d = _raw(db)
    n = 0
    for c in db.scalars(select(Call).where(Call.at >= datetime.now() - timedelta(days=days))):
        u = staff_user(d, c.agent_ext, c.employee)
        if u and c.user_id != u:
            c.user_id, n = u, n + 1
    db.commit()
    return n


def discovered(db: Session, days: int = 30) -> dict:
    """Що вже приходило з телефонії: співробітники (ID + імʼя) і лінії — для швидкого додавання."""
    from collections import Counter
    emp, lines = Counter(), Counter()
    for c in db.scalars(select(Call).where(Call.at >= datetime.now() - timedelta(days=days))):
        if c.agent_ext or _emp(c.employee):
            emp[(c.agent_ext or "", _emp(c.employee) or "")] += 1
        if c.line:
            lines[c.line] += 1
    return {"staff": [{"ext": e, "alias": a, "n": n} for (e, a), n in emp.most_common(20)],
            "lines": [{"number": l, "n": n} for l, n in lines.most_common(20)]}


# ---------------- співробітник, AI ----------------
def _user_by_name(db: Session, name: str | None) -> int | None:
    from ..models import User
    from .search import match
    if not name:
        return None
    for u in db.scalars(select(User).where(User.active.is_(True))):
        if match(name, u.name) or match(u.name, name):
            return u.id
    return None


ADMIN_WORDS = ("адмін", "адміністратор", "admin", "оператор", "менеджер", "співробітник", "клініка", "molodist")
PATIENT_WORDS = ("пацієнт", "пацієнтка", "клієнт", "клієнтка", "абонент", "client", "caller", "співрозмовник")


def dialog(transcript: str | None, employee: str | None = None) -> dict:
    """Транскрипт → репліки з ролями для показу «чатом»: адміністратор праворуч, пацієнт ліворуч.
    Мітки «Адміністратор: / Пацієнт:» (або імʼя співробітника) — точно; без міток — чергуємо, починаючи з того,
    хто представився клінікою (зазвичай адміністратор бере трубку), і позначаємо guessed=True."""
    lines = [ln.strip() for ln in (transcript or "").splitlines() if ln.strip()]
    emp = (employee or "").strip().lower()
    out, labelled = [], 0
    for ln in lines:
        sp, sep, tx = ln.partition(":")
        role = None
        if sep and tx.strip() and len(sp) < 30:
            s = sp.strip().lower()
            if any(w in s for w in ADMIN_WORDS) or (emp and (s in emp or emp.split()[0] in s)):
                role = "a"
            elif any(w in s for w in PATIENT_WORDS):
                role = "p"
            elif re.fullmatch(r"(speaker|спікер|голос)\s*[_ ]?\d+", s):
                role = "s" + re.sub(r"\D", "", s)
        if role:
            labelled += 1
            out.append({"role": role, "text": tx.strip()})
        else:
            out.append({"role": None, "text": ln})
    guessed = labelled < max(1, len(out)) // 2
    if guessed:                                              # без міток — чергуємо
        first_admin = not out or any(w in out[0]["text"].lower() for w in ("molodist", "молодіст", "адміністратор", "слухаю", "клініка"))
        cur = "a" if first_admin else "p"
        for x in out:
            x["role"] = cur
            cur = "p" if cur == "a" else "a"
    else:                                                    # «Speaker 1/2» — хто перший і представився клінікою, той адміністратор
        spk = [x for x in out if x["role"] and x["role"].startswith("s")]
        if spk:
            adm = next((x["role"] for x in spk if any(w in x["text"].lower() for w in ("molodist", "молодіст", "адміністратор"))), spk[0]["role"])
            for x in spk:
                x["role"] = "a" if x["role"] == adm else "p"
        last = "a"
        for x in out:
            if not x["role"]:
                x["role"] = last
            last = x["role"]
    for x in out:
        x["label"] = "Адміністратор" + (f" · {employee}" if employee else "") if x["role"] == "a" else "Пацієнт"
    return {"lines": out, "guessed": guessed and len(out) > 1}


def _vtt_text(raw: str) -> str:
    """WEBVTT → «Спікер: репліка» рядками (без таймкодів)."""
    out = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln or ln.upper().startswith("WEBVTT") or "-->" in ln or ln.isdigit() or ln.startswith("NOTE"):
            continue
        ln = re.sub(r"<v\s+([^>]+)>", r"\1: ", ln)
        ln = re.sub(r"<[^>]+>", "", ln)
        out.append(ln)
    return "\n".join(out)


def ingest_ai(db: Session, provider: str, data: dict, http: httpx.Client | None = None) -> Call:
    """Подія Ringostat «Дзвінок оброблено AI»: короткий зміст, настрій, рекомендації, файл діалогу (VTT) → дзвінок; далі — AI-оцінка."""
    data = {k: v for k, v in data.items() if not (isinstance(v, str) and v.startswith("$"))}
    x = parse(provider, data)
    c = db.scalars(select(Call).where(Call.provider == provider, Call.ext_id == x["ext_id"])).first() if x["ext_id"] else None
    if not c and x["phone"]:
        k_ = ap.phone_key(x["phone"])
        c = next((z for z in db.scalars(select(Call).where(Call.provider == provider, Call.at >= datetime.now() - timedelta(hours=6))
                                       .order_by(Call.at.desc())).all() if ap.phone_key(z.phone) == k_), None)
    if not c:
        c = Call(provider=provider, at=datetime.now(), ext_id=x["ext_id"], phone=x["phone"], direction=x["direction"])
        db.add(c)
    pick = lambda *ks: next((str(data[k]) for k in ks if data.get(k) not in (None, "")), None)   # noqa: E731
    c.summary = pick("summary", "brief_summary", "ai_summary", "short_summary") or c.summary
    txt = pick("transcript", "transcription", "dialog_text", "text")
    vtt = pick("vtt", "vtt_file", "transcript_file", "ai_file")
    if not txt and vtt and vtt.startswith("http"):
        try:
            r = (http or httpx.Client(timeout=30)).get(vtt)
            if r.status_code < 400:
                txt = _vtt_text(r.text)
        except Exception:  # noqa: BLE001
            pass
    elif txt and "-->" in txt:
        txt = _vtt_text(txt)
    if txt:
        c.transcript = txt[:60000]
    meta = dict(c.ai_meta or {})
    for k in ("client_mood", "employee_mood", "overall_mood", "recommendations", "recommended_actions"):
        if data.get(k):
            meta[k] = str(data[k])[:1000]
    c.ai_meta = meta or None
    p = ap.find_patient(db, c.phone)
    c.patient_id = p.id if p else c.patient_id
    db.commit()
    if _raw(db).get("ai_on") and _raw(db).get("ai_engine", "crm") == "crm" and c.transcript and c.ai_score is None:
        try:
            evaluate(db, c, http=http)
        except Exception:  # noqa: BLE001
            pass
    return c


def evaluate(db: Session, c: Call, http: httpx.Client | None = None, claude=None) -> Call:
    """AI-оцінка діалогу за критеріями з налаштувань (Claude API). Нижче порогу — повідомлення власнику."""
    from . import invoice_ai
    d = _raw(db)
    if not c.transcript:
        raise ValueError("Немає тексту діалогу: Ringostat AI його не надіслав. Щоб AI розбирав сам запис розмови, увімкніть «Агент на VPS» у Налаштування → IP-телефонія → AI-оцінка.")
    prompt = (d.get("ai_criteria") or DEFAULT_CRITERIA) + """

Відповідай ЛИШЕ JSON: {"score": число 0-100, "comment": "2-4 речення українською: що добре, що погано, що порадити",
"booked": true/false — чи записано пацієнта}. Не згадуй діагнози й медичні подробиці пацієнта.

Діалог:
""" + c.transcript[:30000]
    if claude:
        j = claude(prompt)
    else:
        key = invoice_ai.api_key(db)
        if not key:
            raise ValueError("Не задано ключ Claude API («Налаштування → Розпізнавання накладних»)")
        r = (http or httpx.Client(timeout=90)).post(invoice_ai.API, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                                                            "content-type": "application/json"},
                                                   json={"model": invoice_ai.model(), "max_tokens": 1200,
                                                         "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Claude API {r.status_code}")
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", out, re.S)
        j = json.loads(m.group(0)) if m else {}
    try:
        c.ai_score = max(0.0, min(100.0, float(j.get("score"))))
    except (TypeError, ValueError):
        c.ai_score = None
    c.ai_comment = str(j.get("comment") or "")[:2000] or None
    meta = dict(c.ai_meta or {})
    if "booked" in j:
        meta["booked"] = bool(j.get("booked"))
    c.ai_meta = meta
    c.ai_at = datetime.utcnow()
    db.commit()
    alert_low(db, c)
    return c


def alert_low(db: Session, c: Call) -> None:
    d = _raw(db)
    if c.ai_score is not None and d.get("ai_alert") and c.ai_score < float(d.get("ai_alert_below") or 0):
        from . import alerts
        from ..config import settings
        from html import escape
        from ..models import User
        from ..models import Patient
        taker = db.get(User, c.user_id) if c.user_id else None
        base = settings.public_url.rstrip("/")
        adm = (taker.name if taker else None) or c.employee or "не визначено"
        if taker and c.employee and c.employee.strip().lower() != (taker.name or "").strip().lower():
            adm += f" (у телефонії: {c.employee})"
        ln = line_info(d, c.line) if c.line else None
        line_txt = f" · лінія {escape((ln or {}).get('name') or c.line)}" if c.line else ""
        pt = db.get(Patient, c.patient_id) if c.patient_id else None
        if pt:
            pat = f'<a href="{base}/patients/{pt.id}">{escape(pt.full_name or "пацієнт")}</a>' + (f" · {escape(c.phone)}" if c.phone else "")
        else:
            pat = f"не знайдено в базі · {escape(c.phone or 'номер невідомий')}"
        text = (f"📞 Низька оцінка дзвінка: <b>{c.ai_score:.0f}/100</b>\n"
                f"{c.at:%d.%m %H:%M} · {'вхідний' if c.direction == 'in' else 'вихідний'}{line_txt}\n"
                f"👩‍💼 Адміністратор: <b>{escape(adm)}</b>\n👤 Пацієнт: {pat}\n"
                f"{escape((c.ai_comment or '')[:500])}\n"
                f'<a href="{base}/telephony?call={c.id}">Відкрити дзвінок</a>')
        pat_plain = (pt.full_name if pt else "новий номер") + (f" · {c.phone}" if c.phone else "")
        tt = str(d.get("ai_task_to") or "")
        if tt:
            uid = c.user_id if tt == "taker" else (int((line_info(d, c.line) or {}).get("user_id") or 0) or None) if tt == "resp" else int(tt)
            u = db.get(User, uid) if uid else None
            from ..models import Task
            key = f"call_low:{c.id}"
            if not db.scalar(select(Task.id).where(Task.source_key == key)):
                db.add(Task(title=f"Розібрати дзвінок · оцінка {c.ai_score:.0f}/100 · {adm} → {pt.full_name if pt else (c.phone or 'пацієнт')}"[:300],
                            description=(f"{c.at:%d.%m.%Y %H:%M} · {'вхідний' if c.direction == 'in' else 'вихідний'}\n"
                                         f"Адміністратор: {adm}\nПацієнт: {pat_plain}\n"
                                         f"{c.ai_comment or ''}\n\nДзвінок: {settings.public_url.rstrip('/')}/telephony?call={c.id}"),
                            kind="call_review", source_key=key, patient_id=c.patient_id, assignee_id=u.id if u else None,
                            assignee_role=None if u else "owner", due_date=date.today(), priority="high"))
                db.commit()
            if d.get("ai_task_tg", True):
                if u and u.telegram_chat_id and u.active:
                    alerts._api("sendMessage", data={"chat_id": u.telegram_chat_id, "text": (text + "\n📝 Створено задачу «Розібрати дзвінок»")[:4000],
                                                     "parse_mode": "HTML", "disable_web_page_preview": "true"})
                else:
                    alerts.send(text + f"\n📝 Задача: {escape(u.name) if u else 'власнику'}")
            return
        to = d.get("ai_alert_to") or []
        if not to:
            alerts.send(text)                      # як раніше — власнику
            return
        ids = {int(x) for x in to if str(x).isdigit()}
        if "taker" in to and c.user_id:
            ids.add(c.user_id)
        li = line_info(d, c.line)
        if "resp" in to and li and li.get("user_id"):
            ids.add(int(li["user_id"]))
        chats = {u.telegram_chat_id for u in db.scalars(select(User).where(User.id.in_(ids or [0]), User.active.is_(True))) if u.telegram_chat_id}
        for ch in chats:
            alerts._api("sendMessage", data={"chat_id": ch, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if not chats:
            alerts.send(text)                      # нікому з обраних не підключено Telegram — щоб не загубилось, власнику


def kpi(db: Session, d_from, d_to) -> list[dict]:
    """KPI по дзвінках за співробітниками: вхідні, прийняті, пропущені, вихідні, середня тривалість, середня AI-оцінка, нижче порогу."""
    from collections import defaultdict
    thr = float(_raw(db).get("ai_alert_below") or 0)
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    by: dict = defaultdict(lambda: {"in": 0, "ans": 0, "miss": 0, "out": 0, "dur": 0, "n_dur": 0, "scores": [], "low": 0, "booked": 0})
    from ..models import User
    d = _raw(db)
    unames = {u.id: u.name for u in db.scalars(select(User))}
    for c in db.scalars(select(Call).where(Call.at >= a, Call.at <= b)):
        who = unames.get(c.user_id) or _emp(c.employee)
        if not who and c.status == "missed":
            li = line_info(d, c.line)
            who = unames.get(li.get("user_id")) if li and li.get("user_id") in unames else "Пропущені (нікому)"   # пропущений — на відповідального за лінію
        g = by[who or "Без співробітника"]
        if c.direction == "in":
            g["in"] += 1
            g["miss" if c.status == "missed" else "ans"] += 1
        else:
            g["out"] += 1
        if c.duration:
            g["dur"] += c.duration
            g["n_dur"] += 1
        if c.ai_score is not None:
            g["scores"].append(c.ai_score)
            g["low"] += c.ai_score < thr
        if (c.ai_meta or {}).get("booked"):
            g["booked"] += 1
    out = []
    for k, g in sorted(by.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
        out.append({"name": k, "in": g["in"], "ans": g["ans"], "miss": g["miss"], "out": g["out"],
                    "avg_dur": round(g["dur"] / g["n_dur"]) if g["n_dur"] else 0,
                    "score": round(sum(g["scores"]) / len(g["scores"]), 1) if g["scores"] else None,
                    "rated": len(g["scores"]), "low": g["low"], "booked": g["booked"]})
    return out


def requeue(db: Session, c: Call) -> None:
    """Повернути дзвінок у чергу агента (кнопка «Переоцінити AI», коли розбирає агент на VPS)."""
    c.ai_score = c.ai_comment = None
    c.ai_at = None
    c.ai_meta = {**{k: v for k, v in (c.ai_meta or {}).items() if k not in ("fails",)}, "manual": True}   # вручну — агент бере і старі дзвінки
    db.commit()


def agent_result(db: Session, c: Call, data: dict) -> Call:
    """Результат AI-розбору від зовнішнього агента: текст діалогу, зміст, оцінка 0–100, коментар, чи записано."""
    if data.get("transcript"):
        t = str(data["transcript"])
        c.transcript = (_vtt_text(t) if "-->" in t else t)[:60000]
    if data.get("summary"):
        c.summary = str(data["summary"])[:4000]
    if data.get("score") is not None:
        try:
            c.ai_score = max(0.0, min(100.0, float(data["score"])))
        except (TypeError, ValueError):
            pass
    if data.get("comment"):
        c.ai_comment = str(data["comment"])[:2000]
    meta = dict(c.ai_meta or {})
    for k in ("booked", "client_mood", "employee_mood", "recommendations", "criteria"):
        if k in data:
            meta[k] = data[k] if k in ("booked", "criteria") else str(data[k])[:1000]
    meta["engine"] = "agent"
    failed = not data.get("transcript") and data.get("score") is None and bool(c.record_url) and not data.get("final")
    if failed:                                     # агент не зміг дістати запис — до 3 спроб, далі зупиняємось
        meta["fails"] = int(meta.get("fails") or 0) + 1
    c.ai_meta = meta
    c.ai_at = None if failed and meta["fails"] < 3 else datetime.utcnow()   # None — дзвінок повернеться в чергу агента
    db.commit()
    alert_low(db, c)
    return c


# ---------------- Ringostat API: мітки реклами (utm) з журналу дзвінків ----------------
UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
_RS_FIELDS = ["calldate,caller,dst,disposition,billsec,uniqueid,call_type,recording," + ",".join(UTM_FIELDS) + ",landing,pool_name",
              "calldate,caller,dst,disposition,billsec,uniqueid,call_type," + ",".join(UTM_FIELDS),
              "calldate,caller,dst,disposition,billsec," + ",".join(UTM_FIELDS)]


def _rs_get(http, params: dict, headers: dict):
    try:
        return http.get("https://api.ringostat.net/calls/list", params=params, headers=headers)
    except httpx.ConnectError:
        with httpx.Client(timeout=60, transport=httpx.HTTPTransport(local_address="0.0.0.0")) as h4:
            return h4.get("https://api.ringostat.net/calls/list", params=params, headers=headers)


def sync_utm(db: Session, d_from: date, d_to: date, http: httpx.Client | None = None, create: bool = True) -> dict:
    """Журнал дзвінків Ringostat (API calls/list) → utm-мітки в дзвінках CRM. Дзвінки, яких у CRM немає (до підключення вебхуків),
    створюються. Запит — по 7 днів (обмеження Ringostat: до 6500 дзвінків за запит)."""
    d = _raw(db)
    pid, key = secret(d, "ringostat", "project_id"), secret(d, "ringostat", "auth_key")
    if not key:
        raise ValueError("У Налаштування → IP-телефонія не вказано Auth-key Ringostat")
    headers = {"Auth-key": key, "x-project-id": pid or "", "Content-Type": "application/json"}
    http = http or httpx.Client(timeout=60)
    rep = {"fetched": 0, "updated": 0, "created": 0, "with_utm": 0, "fields": None}
    fields = None
    a = d_from
    while a <= d_to:
        b = min(a + timedelta(days=6), d_to)
        params = {"export_type": "json", "from": f"{a} 00:00:00", "to": f"{b + timedelta(days=1)} 00:00:00"}
        rows = None
        for fset in ([fields] if fields else _RS_FIELDS):
            r = _rs_get(http, {**params, "fields": fset}, headers)
            if r.status_code == 401:
                raise ValueError("Ringostat: ключ не прийнято (401) — перевірте Auth-key")
            if r.status_code < 400:
                try:
                    js = r.json()
                except ValueError:
                    js = None
                if isinstance(js, list):
                    rows, fields = js, fset
                    break
        if rows is None:
            raise ValueError(f"Ringostat API не віддав журнал дзвінків ({r.status_code}: {r.text[:120]})")
        rep["fields"] = fields
        rep["fetched"] += len(rows)
        for x in rows:                                        # діагностика: які джерела й пули номерів узагалі є в журналі
            if isinstance(x, dict):
                rep.setdefault("keys", sorted(x.keys())[:30])
                for f_, name in (("utm_source", "sources"), ("pool_name", "pools"), ("utm_medium", "mediums")):
                    v = str(x.get(f_) if x.get(f_) is not None else "∅")[:40] or "(порожньо)"
                    rep.setdefault(name, {})
                    rep[name][v] = rep[name].get(v, 0) + 1
        t0, t1 = datetime.combine(a, datetime.min.time()) - timedelta(hours=1), datetime.combine(b, datetime.max.time()) + timedelta(hours=1)
        have = db.scalars(select(Call).where(Call.at >= t0, Call.at <= t1)).all()
        by_ext = {c.ext_id: c for c in have if c.ext_id}
        by_phone: dict = {}
        for c in have:
            by_phone.setdefault(ap.phone_key(c.phone), []).append(c)
        for x in rows:
            if not isinstance(x, dict):
                continue
            utm = {k: _utm(x.get(k), 300 if k != "utm_source" and k != "utm_medium" else 120) for k in UTM_FIELDS}
            try:
                at = datetime.strptime(str(x.get("calldate"))[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            out = str(x.get("call_type") or "").lower() in ("out", "outgoing", "callback")
            phone = x.get("dst") if out else x.get("caller")
            k = ap.phone_key(str(phone or ""))
            c = by_ext.get(str(x.get("uniqueid") or "")) or min(
                (z for z in by_phone.get(k, []) if abs((z.at - at).total_seconds()) <= 600 and (z.direction == "out") == out),
                key=lambda z: abs((z.at - at).total_seconds()), default=None) if k else None
            if c is None:
                if not create or not k:
                    continue
                disp = str(x.get("disposition") or "").upper()
                c = Call(provider="ringostat", at=at, ext_id=str(x.get("uniqueid") or "")[:120] or None, direction="out" if out else "in",
                         phone=str(phone)[:64], status="answered" if disp in ("ANSWERED", "ANSWER", "PROPER") else "missed",
                         duration=int(float(x.get("billsec") or 0)), line=_digits(x.get("dst") if not out else x.get("caller")),
                         record_url=str(x.get("recording") or "")[:500] or None)
                p = ap.find_patient(db, c.phone)
                c.patient_id = p.id if p else None
                db.add(c)
                by_phone.setdefault(k, []).append(c)
                rep["created"] += 1
            changed = False
            for f_, v in utm.items():
                if v and getattr(c, f_) != v:
                    setattr(c, f_, v)
                    changed = True
            land = _utm(x.get("landing"), 500)
            if land and not c.landing:
                c.landing = land
            if any(utm.values()):
                rep["with_utm"] += 1
            rep["updated"] += changed
        db.commit()
        a = b + timedelta(days=1)
    return rep

