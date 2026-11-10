"""Заявки з форм сайту → звернення в CRM з мітками реклами.

Приймає POST на /api/public/lead/{hook}: JSON, form-urlencoded (вебхук Tilda) або text/plain з JSON (скрипт з Tag Manager —
без CORS-preflight). Поля шукаємо за типовими назвами (Name/name/імʼя, Phone/phone/tel…). Мітки: utm_* і gclid з полів,
з cookie Tilda (TILDAUTM) або з адреси сторінки. Є мітки — звернення одразу точно привʼязане до кампанії;
немає — GA4 дозвяже його за часом відправки форми (подія ✉ у налаштуваннях GA4).
Повтор того самого телефону протягом 30 хв — коментар у наявному зверненні, не нове."""
from __future__ import annotations

import json
import re
import secrets as _secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appeal, SyncState
from . import appeals as ap

KEY = "site_leads"
DEFAULT = {"hook": "", "source": "Сайт · форма", "log": []}
UTM = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid")
NAME_KEYS = ("name", "імя", "імʼя", "ім'я", "имя", "fio", "піб", "your-name", "fullname", "first_name")
PHONE_KEYS = ("phone", "tel", "телефон", "mobile", "your-phone", "phone_number")
MSG_KEYS = ("textarea", "message", "comment", "comments", "коментар", "повідомлення", "питання", "question", "text", "your-message")
INTEREST_KEYS = ("service", "послуга", "procedure", "процедура", "interest", "direction", "напрямок")
SKIP = {"formid", "formname", "tranid", "cookies", "test", "page", "referer", "referrer", "url", "ga_client_id", "client_id", "form", "hook"}


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    out = {**DEFAULT, **d}
    if not out["hook"]:
        out["hook"] = _secrets.token_urlsafe(18)
        save_cfg(db, out)
    return out


def save_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False, default=str), updated_at=datetime.utcnow()))
    db.commit()


def parse_body(raw: bytes, ctype: str) -> dict:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            d = json.loads(text)
            return {str(k): v for k, v in d.items()} if isinstance(d, dict) else {}
        except ValueError:
            pass
    return dict(parse_qsl(text, keep_blank_values=True))


def _pick(d: dict, keys) -> str:
    low = {k.lower().strip(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k)
        if v not in (None, ""):
            return str(v).strip()
    for k, v in low.items():                                  # «Phone_2», «name-1» тощо
        if v not in (None, "") and any(k.startswith(x) for x in keys):
            return str(v).strip()
    return ""


def marks(d: dict) -> dict:
    """utm_* і gclid: поля форми → cookie TILDAUTM → адреса сторінки."""
    low = {k.lower(): v for k, v in d.items()}
    out = {k: str(low.get(k) or "").strip() for k in UTM}
    ck = str(low.get("cookies") or "")
    m = re.search(r"TILDAUTM=([^;]+)", ck)
    if m:                                                     # Tilda: utm_source=google|||utm_medium=cpc|||…
        for part in unquote(m.group(1)).split("|||"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k in out and not out[k]:
                    out[k] = v.strip()
    for u in (low.get("page"), low.get("url"), low.get("referer"), low.get("referrer")):
        if u and "?" in str(u):
            q = dict(parse_qsl(urlparse(str(u)).query))
            for k in UTM:
                if not out[k] and q.get(k):
                    out[k] = q[k].strip()
    return {k: v[:200] for k, v in out.items() if v}


def ingest(db: Session, d: dict, origin: str = "") -> dict:
    c = cfg(db)
    if not d or set(k.lower() for k in d) <= {"test"}:       # перевірка вебхука Tilda («test=test»)
        return {"ok": True, "test": True}
    name, phone = _pick(d, NAME_KEYS)[:200], _pick(d, PHONE_KEYS)[:64]
    msg, interest = _pick(d, MSG_KEYS), _pick(d, INTEREST_KEYS)[:200]
    form = str(d.get("formname") or d.get("form") or d.get("formid") or "").strip()[:80]
    page = str(d.get("page") or d.get("url") or d.get("referer") or "").strip()[:300]
    if not (phone or name or msg):
        return {"ok": False, "error": "порожня заявка"}
    mk = marks(d)
    utm = "&".join(f"{k}={v}" for k, v in mk.items())
    extra = {k: v for k, v in d.items() if k.lower() not in SKIP and k.lower() not in UTM and str(v).strip()
             and str(v).strip() not in (name, phone, msg, interest) and not k.lower().startswith("utm")}
    lines = [f"Заявка з сайту{(' · ' + form) if form else ''}"]
    if msg:
        lines.append(msg[:1500])
    for k, v in list(extra.items())[:12]:
        lines.append(f"{k}: {str(v)[:200]}")
    if page:
        lines.append(f"Сторінка: {page.split('?')[0]}")
    text = "\n".join(lines)
    k_ = ap.phone_key(phone)
    if len(k_) >= 9:
        since = datetime.utcnow() - timedelta(minutes=30)
        dup = next((a for a in db.scalars(select(Appeal).where(Appeal.created_at >= since, Appeal.deleted_at.is_(None), Appeal.phone.is_not(None)))
                    if ap.phone_key(a.phone) == k_), None)
        if dup:
            ap.note(db, dup, "Повторна заявка з сайту:\n" + text, None, "system")
            if utm and not dup.utm:
                dup.utm = utm[:300]
            db.commit()
            _log(db, c, form, True, dup.id, bool(mk))
            return {"ok": True, "appeal_id": dup.id, "duplicate": True}
    a = ap.create(db, {"name": name, "phone": phone, "source": c.get("source") or "Сайт · форма", "interest": interest or None,
                       "comment": text, "utm": utm})
    _log(db, c, form, False, a.id, bool(mk))
    return {"ok": True, "appeal_id": a.id}


def _log(db: Session, c: dict, form: str, dup: bool, appeal_id: int, has_utm: bool) -> None:
    c = cfg(db)
    c["log"] = ([{"at": datetime.now().isoformat(timespec="seconds"), "form": form, "appeal": appeal_id, "dup": dup, "utm": has_utm}]
                + (c.get("log") or []))[:30]
    save_cfg(db, c)


def gtm_tag(hook_url: str) -> str:
    return GTM.replace("__HOOK__", hook_url)


GTM = r"""<script>
/* MOLODIST CRM — заявки з форм сайту → звернення в CRM з мітками реклами.
   Tag Manager: Теги → Новий → «Спеціальний HTML» → вставити → тригер «All Pages» (Усі сторінки) → Опублікувати. */
(function () {
  var HOOK = '__HOOK__', K = 'mlds_utm';
  try {                                                     // запамʼятовуємо мітки з першої сторінки візиту (30 днів)
    var q = new URLSearchParams(location.search), m = {}, any = false;
    ['utm_source','utm_medium','utm_campaign','utm_term','utm_content','gclid'].forEach(function (k) { var v = q.get(k); if (v) { m[k] = v; any = true; } });
    if (any) localStorage.setItem(K, JSON.stringify({ m: m, t: Date.now() }));
  } catch (e) {}
  function saved() { try { var s = JSON.parse(localStorage.getItem(K) || 'null'); return s && Date.now() - s.t < 30 * 864e5 ? s.m : {}; } catch (e) { return {}; } }
  function send(form) {
    var d = {}, fd = new FormData(form);
    fd.forEach(function (v, k) { if (typeof v === 'string' && v.trim() && !/^(g-recaptcha|tildaspec|form-spec|_)/i.test(k)) d[k] = v.trim(); });
    if (!Object.keys(d).length) return;
    var s = saved(); for (var k in s) if (!d[k]) d[k] = s[k];
    d.page = location.href; d.formname = form.getAttribute('data-tilda-form-name') || form.getAttribute('name') || form.id || document.title;
    try { navigator.sendBeacon(HOOK, new Blob([JSON.stringify(d)], { type: 'text/plain' })); }
    catch (e) { fetch(HOOK, { method: 'POST', body: JSON.stringify(d), headers: { 'Content-Type': 'text/plain' }, keepalive: true }); }
  }
  document.addEventListener('submit', function (e) { if (e.target && e.target.tagName === 'FORM') send(e.target); }, true);
})();
</script>"""
