"""IP-телефонія (як у CleverBox): Binotel, Stream Telecom, Ringostat.
Налаштування — SyncState telephony_cfg (ключі зашифровано). Вхідний дзвінок з вебхука провайдера →
запис у журнал дзвінків, пацієнт за номером і (за бажанням) нове звернення з вибраним джерелом."""
from __future__ import annotations

import json
import re
import secrets as _secrets
from datetime import datetime, timedelta

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
DEFAULT_CRITERIA = """Оціни розмову адміністратора медичного простору MOLODIST з пацієнтом за 100-бальною шкалою:
1. Привітання: назвала клініку й себе, звернулась на «ви» (10)
2. Виявила потребу: що турбує, яка процедура цікавить, чи був раніше (20)
3. Експертність і впевненість: пояснила процедуру, ціну, тривалість, підготовку (20)
4. Пропозиція запису: запропонувала конкретний час, лікаря (20)
5. Робота із запереченнями (ціна, страх, «подумаю») (15)
6. Завершення: підсумувала домовленість, попрощалась, ввічливість і тон (15)
Якщо пацієнта не записано — поясни чому і що треба було зробити інакше."""


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
    d.setdefault("popup_users", [])          # кому показувати вікно «Вхідний дзвінок» (порожньо — усім з правом «Звернення» / «Запис»)
    d.setdefault("ai_on", False)             # AI-оцінка діалогів (Claude) за критеріями
    d.setdefault("ai_criteria", DEFAULT_CRITERIA)
    d.setdefault("ai_alert_below", 60)       # оцінка нижче — повідомлення власнику в Telegram
    d.setdefault("ai_alert", True)
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
               ai_engine: str = "crm") -> None:
    d = _raw(db)
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
    """Останні 30 вебхуків (для перевірки налаштування подій у кабінеті провайдера)."""
    st = db.get(SyncState, "telephony_hook_log") or SyncState(key="telephony_hook_log")
    try:
        xs = json.loads(st.value) if st.value else []
    except ValueError:
        xs = []
    xs.insert(0, {"at": datetime.now().strftime("%d.%m %H:%M:%S"), "provider": provider, "event": event or "—",
                  "data": {str(k)[:40]: str(v)[:200] for k, v in list(data.items())[:40]}})
    st.value, st.updated_at = json.dumps(xs[:30], ensure_ascii=False), datetime.utcnow()
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
                  '"employee_fio":"$employee_fio","record_link":"$recording_wav"}')
RINGOSTAT_EVENTS = [("ringing", "Перед дзвінком", "спливаюче вікно «Вхідний дзвінок» у CRM: хто дзвонить, картка пацієнта"),
                    ("answer", "У момент підняття трубки", "позначка, що дзвінок прийнято"),
                    ("end", "Після дзвінка", "журнал дзвінків, тривалість, запис розмови, звернення для нових номерів"),
                    ("end", "Після вихідного дзвінка", "вихідні дзвінки в журналі й історії пацієнта")]


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
    phone = _pick(flat, "externalNumber", "caller", "callerNumber", "phone", "src", "from", "client_phone")
    if direction == "out":
        phone = _pick(flat, "externalNumber", "dst", "callee", "to", "phone", "client_phone") or phone
    return {"ext_id": str(_pick(flat, "generalCallID", "callID", "uniqueid", "uid", "call_id", "id") or "")[:120] or None,
            "direction": direction, "phone": str(phone or "")[:64] or None, "status": status, "duration": dur,
            "employee": str(_pick(flat, "employeeName", "employee_fio", "employee", "manager", "dst_name") or "")[:120] or None,
            "record_url": str(_pick(flat, "linkToCallRecordInMyBusiness", "recording", "recording_wav", "record_link", "record_url", "record") or "")[:500] or None}


def ingest(db: Session, provider: str, data: dict, event: str = "") -> Call:
    """event: ringing (дзвінок іде — спливаюче вікно), answer (підняли трубку), end / порожньо (дзвінок завершено)."""
    d = _raw(db)
    event = (event or str(data.get("event") or "")).strip().lower()
    x = parse(provider, data)
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
    if c.employee and not c.user_id:
        c.user_id = _user_by_name(db, c.employee)
    db.flush()
    if event in ("ringing", "answer"):
        db.commit()
        return c
    want = d.get("create_appeal") and c.direction == "in" and c.phone and not c.appeal_id
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
        raise ValueError("Немає тексту діалогу — потрібна транскрипція (Ringostat AI)")
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
        alerts.send(f"📞 Низька оцінка дзвінка: <b>{c.ai_score:.0f}/100</b>\n{escape(c.employee or 'співробітник не вказаний')} · "
                    f"{c.at:%d.%m %H:%M} · {'вхідний' if c.direction == 'in' else 'вихідний'}\n{escape((c.ai_comment or '')[:500])}\n"
                    f'<a href="{settings.public_url.rstrip("/")}/telephony?call={c.id}">Відкрити дзвінок</a>')


def kpi(db: Session, d_from, d_to) -> list[dict]:
    """KPI по дзвінках за співробітниками: вхідні, прийняті, пропущені, вихідні, середня тривалість, середня AI-оцінка, нижче порогу."""
    from collections import defaultdict
    thr = float(_raw(db).get("ai_alert_below") or 0)
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    by: dict = defaultdict(lambda: {"in": 0, "ans": 0, "miss": 0, "out": 0, "dur": 0, "n_dur": 0, "scores": [], "low": 0, "booked": 0})
    for c in db.scalars(select(Call).where(Call.at >= a, Call.at <= b)):
        g = by[c.employee or ("Без співробітника" if c.status != "missed" else "Пропущені (нікому)")]
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
    c.ai_meta = meta
    if c.ai_score is not None:
        c.ai_at = datetime.utcnow()
    db.commit()
    alert_low(db, c)
    return c
