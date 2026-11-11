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

from urllib.parse import urlencode

from ..models import Appeal, Call, SiteClick, SyncState
from . import appeals as ap

KEY = "site_leads"
DEFAULT = {"hook": "", "source": "Сайт · форма", "log": [], "call_window": 5, "msg_window": 180}
KINDS = {"phone": "клік по номеру", "telegram": "Telegram", "viber": "Viber", "whatsapp": "WhatsApp", "booking": "запис онлайн",
         "instagram": "Instagram", "form": "форма"}
CHANNEL_SRC = {"telegram": ("telegram", "телеграм", "tg"), "viber": ("viber", "вайбер"), "whatsapp": ("whatsapp", "ватсап"),
               "instagram": ("instagram", "інстаграм"), "booking": ("сайт", "онлайн", "запис", "site", "booking"),
               "form": ("сайт", "форма", "site", "form")}
UTM = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid")
# поля, які тег Tag Manager додає до заявки: client_id GA4 (cid) тощо
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
    if not out["gclid"]:                                      # cookie Google Ads: _gcl_aw=GCL.<час>.<gclid>
        g = re.search(r"_gcl_aw=GCL\.\d+\.([^;\s]+)", ck)
        if g:
            out["gclid"] = unquote(g.group(1))
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


def is_paid(source: str | None, medium: str | None, gclid: str | None) -> bool:
    s_, m_ = (source or "").lower(), (medium or "").lower()
    return bool(gclid) or ("google" in s_ and m_ in ("cpc", "ppc", "paid"))


def click_ingest(db: Session, d: dict) -> dict:
    """Клік з тегу Tag Manager: {kind, href, page, gclid, utm_*, cid}. Зберігаємо лише кліки з реклами (gclid або google/cpc)."""
    kind = str(d.get("kind") or "").strip().lower()[:20]
    if kind not in KINDS:
        return {"ok": False, "error": "kind"}
    mk = {k: str(d.get(k) or "").strip()[:200] for k in UTM}
    if not is_paid(mk["utm_source"], mk["utm_medium"], mk["gclid"]):
        return {"ok": True, "skipped": "не реклама"}
    now = datetime.now()
    recent = db.scalars(select(SiteClick).where(SiteClick.at >= now - timedelta(seconds=20), SiteClick.kind == kind)).all()
    if any((x.gclid or "") == mk["gclid"] and (x.client_id or "") == str(d.get("cid") or "")[:80] for x in recent):
        return {"ok": True, "duplicate": True}                # подвійний клік
    sc = SiteClick(at=now, kind=kind, href=str(d.get("href") or "")[:300] or None, page=str(d.get("page") or "").split("?")[0][:300] or None,
                   gclid=mk["gclid"] or None, utm_source=mk["utm_source"] or ("google" if mk["gclid"] else None),
                   utm_medium=mk["utm_medium"] or ("cpc" if mk["gclid"] else None), utm_campaign=mk["utm_campaign"] or None,
                   utm_term=mk["utm_term"] or None, client_id=str(d.get("cid") or "")[:80] or None)
    db.add(sc)
    db.commit()
    return {"ok": True, "click_id": sc.id}


def _utm_str(sc: SiteClick, attr: str) -> str:
    q = {"utm_source": sc.utm_source or "google", "utm_medium": sc.utm_medium or "cpc", "utm_campaign": sc.utm_campaign or "",
         "utm_term": sc.utm_term or "", "gclid": sc.gclid or "", "attr": attr}
    return urlencode({k: v for k, v in q.items() if v})[:300]


def match_clicks(db: Session, d_from, d_to) -> dict:
    """Клік з реклами → дзвінок (клік по номеру, дзвінок за кілька хвилин) або звернення з того самого каналу (Telegram, Viber…,
    створене адміністратором протягом кількох годин). Найближчий попередній клік; кілька кампаній-кандидатів → «імовірно»."""
    from datetime import date as _d
    c = cfg(db)
    cw, mw = timedelta(minutes=float(c.get("call_window") or 5)), timedelta(minutes=float(c.get("msg_window") or 180))
    t0 = datetime.combine(d_from, datetime.min.time()) if isinstance(d_from, _d) else d_from
    t1 = datetime.combine(d_to, datetime.max.time()) if isinstance(d_to, _d) else d_to
    clicks = db.scalars(select(SiteClick).where(SiteClick.at >= t0 - mw, SiteClick.at <= t1).order_by(SiteClick.at)).all()
    rep = {"clicks": len(clicks), "calls": 0, "appeals": 0}
    if not clicks:
        return rep
    free = [x for x in clicks if not x.call_id and not x.appeal_id]
    for cl in db.scalars(select(Call).where(Call.at >= t0, Call.at <= t1, Call.direction == "in").order_by(Call.at)):
        if cl.utm_source and cl.attr not in ("ga4", "ga4?"):
            continue
        cand = [x for x in free if x.kind == "phone" and x.at - timedelta(minutes=1) <= cl.at <= x.at + cw]
        if not cand:
            continue
        best = min(cand, key=lambda x: abs((cl.at - x.at).total_seconds()))
        sure = len({x.utm_campaign for x in cand}) == 1
        cl.utm_source, cl.utm_medium = best.utm_source or "google", best.utm_medium or "cpc"
        cl.utm_campaign, cl.utm_term, cl.gclid = best.utm_campaign, best.utm_term, best.gclid
        cl.attr = "click" if sure else "click?"
        best.call_id = cl.id
        free.remove(best)
        rep["calls"] += 1
    shift = datetime.now() - datetime.utcnow()                # Appeal.created_at — UTC
    for a in db.scalars(select(Appeal).where(Appeal.created_at >= t0 - shift - timedelta(hours=1), Appeal.created_at <= t1 - shift + timedelta(hours=1),
                                             Appeal.deleted_at.is_(None)).order_by(Appeal.created_at)):
        u = a.utm or ""
        if u and "attr=ga4" not in u and "attr=click" not in u:
            continue
        if "attr=click" in u and "click%3F" not in u and "attr=click?" not in u:
            continue                                          # уже точно привʼязане
        at, src = a.created_at + shift, (a.source or "").lower()
        cand = [x for x in free if x.kind in CHANNEL_SRC and any(w in src for w in CHANNEL_SRC[x.kind])
                and x.at - timedelta(minutes=1) <= at <= x.at + mw]
        if not cand:
            continue
        best = max(cand, key=lambda x: x.at)                  # останній клік перед зверненням
        sure = len({x.utm_campaign for x in cand}) == 1
        a.utm = _utm_str(best, "click" if sure else "click?")
        best.appeal_id = a.id
        free.remove(best)
        rep["appeals"] += 1
    db.commit()
    return rep


def gtm_tag(hook_url: str) -> str:
    return GTM.replace("__HOOK__", hook_url)


GTM = r"""<script>
/* MOLODIST CRM v2 — заявки з форм і кліки (телефон, Telegram, Viber, запис) з реклами → CRM з мітками й gclid.
   Tag Manager: Теги → Новий → «Спеціальний HTML» → вставити → тригер «All Pages» (Усі сторінки) → Опублікувати. */
(function () {
  if (document.documentElement.hasAttribute('data-mlds')) return;   // сторінки сайту на VPS — CRM обробляє сама
  var HOOK = '__HOOK__', CLICK = HOOK.replace('/lead/', '/click/'), K = 'mlds_utm';
  function ck(n) { var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)')); return m ? decodeURIComponent(m[1]) : ''; }
  try {                                                     // запамʼятовуємо мітки з першої сторінки візиту (90 днів)
    var q = new URLSearchParams(location.search), m = {}, any = false;
    ['utm_source','utm_medium','utm_campaign','utm_term','utm_content','gclid'].forEach(function (k) { var v = q.get(k); if (v) { m[k] = v; any = true; } });
    if (any) localStorage.setItem(K, JSON.stringify({ m: m, t: Date.now() }));
  } catch (e) {}
  function saved() {
    var s = {}; try { var x = JSON.parse(localStorage.getItem(K) || 'null'); if (x && Date.now() - x.t < 90 * 864e5) s = x.m; } catch (e) {}
    if (!s.gclid) { var g = ck('_gcl_aw').split('.'); if (g.length > 2) s.gclid = g.slice(2).join('.'); }
    var ga = ck('_ga').split('.'); if (ga.length > 3) s.cid = ga[2] + '.' + ga[3];
    return s;
  }
  function beacon(url, d) {
    try { if (navigator.sendBeacon(url, new Blob([JSON.stringify(d)], { type: 'text/plain' }))) return; } catch (e) {}
    fetch(url, { method: 'POST', body: JSON.stringify(d), headers: { 'Content-Type': 'text/plain' }, keepalive: true });
  }
  function send(form) {
    var d = {}, fd = new FormData(form);
    fd.forEach(function (v, k) { if (typeof v === 'string' && v.trim() && !/^(g-recaptcha|tildaspec|form-spec|_)/i.test(k)) d[k] = v.trim(); });
    if (!Object.keys(d).length) return;
    var s = saved(); for (var k in s) if (!d[k]) d[k] = s[k];
    d.page = location.href; d.formname = form.getAttribute('data-tilda-form-name') || form.getAttribute('name') || form.id || document.title;
    beacon(HOOK, d);
  }
  function kind(a) {
    var h = (a.getAttribute('href') || '').toLowerCase(), t = (a.textContent || '').toLowerCase();
    if (h.indexOf('tel:') === 0) return 'phone';
    if (/t\.me\/|telegram\.me|tg:\/\//.test(h)) return 'telegram';
    if (/viber:|viber\.com|vb\.me/.test(h)) return 'viber';
    if (/wa\.me|whatsapp/.test(h)) return 'whatsapp';
    if (/instagram\.com|ig\.me/.test(h)) return 'instagram';
    if (/cbox|booking|zapys|zapis|online-?zapis|запис/.test(h) || /записатис|онлайн.запис|записатися/.test(t)) return 'booking';
    return null;
  }
  document.addEventListener('submit', function (e) { if (e.target && e.target.tagName === 'FORM') send(e.target); }, true);
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a') : null; if (!a) return;
    var k = kind(a); if (!k) return;
    var d = saved(); if (!d.gclid && !/google/i.test(d.utm_source || '')) return;   // лише з реклами
    d.kind = k; d.href = a.href; d.page = location.href; beacon(CLICK, d);
  }, true);
})();
</script>"""
