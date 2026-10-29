"""IP-телефонія (як у CleverBox): Binotel, Stream Telecom, Ringostat.
Налаштування — SyncState telephony_cfg (ключі зашифровано). Вхідний дзвінок з вебхука провайдера →
запис у журнал дзвінків, пацієнт за номером і (за бажанням) нове звернення з вибраним джерелом."""
from __future__ import annotations

import json
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
            r = http.get("https://api.ringostat.net/calls/list", headers={"Auth-key": key, "x-project-id": pid},
                         params={"from": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"), "to": now.strftime("%Y-%m-%d %H:%M:%S"),
                                 "fields": "calldate"})
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


def _pick(data: dict, *keys):
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return v
    return None


def parse(provider: str, data: dict) -> dict:
    """Нормалізуємо вебхук будь-якого з провайдерів (поля з документації + типові назви)."""
    flat = dict(data)
    for k, v in data.items():                                # Binotel: callDetails[externalNumber]
        if k.startswith("callDetails[") and k.endswith("]"):
            flat[k[12:-1]] = v
    if isinstance(data.get("callDetails"), dict):
        flat.update(data["callDetails"])
    direction = "in"
    ct = str(_pick(flat, "callType", "call_type", "direction", "type") or "").lower()
    if ct in ("1", "out", "outgoing", "outbound", "outcoming"):
        direction = "out"
    disp = str(_pick(flat, "disposition", "status", "call_status") or "").upper()
    status = "answered" if disp in ("ANSWER", "ANSWERED", "PROPER", "SUCCESS") else ("missed" if disp else None)
    if disp in ("NOANSWER", "NO ANSWER", "BUSY", "CANCEL", "FAILED", "MISSED", "CHANUNAVAIL", "VM", "VM-SUCCESS"):
        status = "missed"
    try:
        dur = int(float(_pick(flat, "billsec", "duration", "talk_time") or 0))
    except (TypeError, ValueError):
        dur = None
    phone = _pick(flat, "externalNumber", "caller", "callerNumber", "phone", "src", "from", "client_phone")
    if direction == "out":
        phone = _pick(flat, "externalNumber", "dst", "to", "phone", "client_phone") or phone
    return {"ext_id": str(_pick(flat, "generalCallID", "callID", "uniqueid", "call_id", "id") or "")[:120] or None,
            "direction": direction, "phone": str(phone or "")[:64] or None, "status": status, "duration": dur,
            "employee": str(_pick(flat, "employeeName", "employee", "manager", "dst_name") or "")[:120] or None,
            "record_url": str(_pick(flat, "linkToCallRecordInMyBusiness", "recording", "record_url", "record") or "")[:500] or None}


def ingest(db: Session, provider: str, data: dict) -> Call:
    d = _raw(db)
    x = parse(provider, data)
    c = db.scalars(select(Call).where(Call.provider == provider, Call.ext_id == x["ext_id"])).first() if x["ext_id"] else None
    if not c:
        c = Call(provider=provider, at=datetime.now())
        db.add(c)
    for k, v in x.items():
        if v is not None:
            setattr(c, k, v)
    p = ap.find_patient(db, c.phone)
    c.patient_id = p.id if p else c.patient_id
    db.flush()
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
