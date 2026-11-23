"""Тест «Молодість 360» у CRM (перенесено з окремого сервісу на порту 8094).

Публічні сторінки — molodist.com.ua/molodist-360/ (у CRM: /_site/molodist-360/…). Пацієнт реєструється (імʼя, телефон,
згода), відповідає на запитання, отримує бал і PDF. Анкета привʼязується до картки пацієнта за телефоном (або створює
картку). Власна міні-CRM старого сервісу (плани, візити, фінанси) не переноситься — це вже є в CRM."""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import M360Assessment, Patient
from .questionnaire import QUESTION_LOOKUP, QUESTIONNAIRE
from .scoring import evaluate

SOURCE = "Сайт / Молодість 360"


class M360Error(ValueError):
    pass


def _phone(v: str) -> str:
    d = re.sub(r"\D", "", v or "")
    if len(d) == 9:
        d = "380" + d
    elif len(d) == 10 and d.startswith("0"):
        d = "38" + d
    return "+" + d if len(d) == 12 and d.startswith("380") else ""


def by_token(db: Session, token: str) -> M360Assessment | None:
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", token):
        return None
    return db.scalar(select(M360Assessment).where(M360Assessment.token == token))


def register(db: Session, d: dict, source: str = "") -> M360Assessment:
    if str(d.get("consent_personal_data") or "").lower() not in ("1", "true", "on", "yes"):
        raise M360Error("Потрібна згода на обробку персональних даних")
    first, last = str(d.get("first_name") or "").strip()[:100], str(d.get("last_name") or "").strip()[:100]
    phone = _phone(str(d.get("phone") or ""))
    if not first or not phone:
        raise M360Error("Вкажіть імʼя та номер телефону у форматі +380…")
    bd = None
    if d.get("birth_date"):
        try:
            bd = date.fromisoformat(str(d["birth_date"])[:10])
        except ValueError:
            bd = None
    from ..appeals import find_patient
    p = find_patient(db, phone)
    if not p:
        p = Patient(full_name=f"{last} {first}".strip(), phone=phone, source=SOURCE,
                    notes="Картку створено з тесту «Молодість 360» на сайті")
        db.add(p)
        db.flush()
    email = str(d.get("email") or "").strip()[:200] or None
    if email and not p.email:
        p.email = email
    if bd and not p.birthday:
        p.birthday = bd
    a = M360Assessment(token=secrets.token_urlsafe(18), patient_id=p.id, first_name=first, last_name=last, phone=phone, email=email,
                       birth_date=bd, gender=str(d.get("gender") or "")[:20] or None, city=str(d.get("city") or "")[:100] or None,
                       consent_results=str(d.get("consent_results") or "").lower() in ("1", "true", "on", "yes"),
                       answers={}, status="draft", source=(source or "")[:300] or None)
    db.add(a)
    db.commit()
    return a


def _option(question_key: str, value: str) -> dict | None:
    for m in QUESTIONNAIRE["modules"]:
        for f in m["factors"]:
            for q in f["questions"]:
                if q["key"] == question_key:
                    return next(({**o, "module_key": m["key"], "factor_key": f["key"]} for o in q["options"] if o["value"] == value), None)
    return None


def save_answer(db: Session, a: M360Assessment, d: dict) -> None:
    """Бал беремо з анкети на сервері (не з браузера), щоб результат не можна було підробити."""
    if a.status == "completed":
        raise M360Error("Оцінку вже завершено")
    qk, val = str(d.get("question_key") or ""), str(d.get("answer_value") or "")
    opt = _option(qk, val)
    if not opt:
        raise M360Error("Невідоме запитання або відповідь")
    ans = dict(a.answers or {})
    ans[qk] = {"module_key": opt["module_key"], "factor_key": opt["factor_key"], "answer_value": val,
               "answer_label": opt["label"], "answer_score": opt["score"] if opt["score"] is not None else 0}
    a.answers = ans
    db.commit()


def rows(a: M360Assessment) -> list[dict]:
    return [{"question_key": k, **v} for k, v in (a.answers or {}).items()]


def submit(db: Session, a: M360Assessment) -> dict:
    if not a.answers:
        raise M360Error("Немає відповідей")
    if a.status != "completed":
        res = evaluate(rows(a))
        a.result, a.overall_score, a.overall_status = res, res["overall_score"], res["overall_status"]
        a.status, a.submitted_at = "completed", datetime.utcnow()
        db.commit()
    return serialize(a)


def serialize(a: M360Assessment) -> dict:
    r = a.result or {}
    return {"assessment_id": a.token, "overall_score": r.get("overall_score"), "overall_status": r.get("overall_status"),
            "overall_summary": r.get("overall_summary"), "top_risks": (r.get("top_risks") or [])[:3],
            "modules": r.get("modules") or [],
            "factors": [{**f, "informational": f.get("factor_weight") == 0} for f in (r.get("factors") or [])],
            "pdf_url": f"/assets/pdfs/{a.token}.pdf", "answers_report_url": f"/assets/answers/{a.token}.html",
            "consultation_status": "new", "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None}


def name(a: M360Assessment) -> str:
    return f"{a.first_name or ''} {a.last_name or ''}".strip()


def pdf(a: M360Assessment) -> bytes:
    from .pdf_report import build_pdf
    return build_pdf(a.token, name(a), a.result or {})


def answers_html(a: M360Assessment) -> str:
    from .answer_report import build_answers_report
    return build_answers_report(a.token, name(a), rows(a))


def for_patient(db: Session, pid: int) -> list[M360Assessment]:
    return list(db.scalars(select(M360Assessment).where(M360Assessment.patient_id == pid).order_by(M360Assessment.id.desc())))


def alert_text(a: M360Assessment, event: str, base: str) -> str:
    """Telegram у чат заявок: хто почав / завершив тест. Без відповідей і медичних подробиць — лише бал і посилання на картку."""
    from html import escape
    b = (base or "").rstrip("/")
    head = "🧪 <b>Почали тест «Молодість 360»</b>" if event == "started" else "✅ <b>Завершили тест «Молодість 360»</b>"
    L = [head, "", f"👤 <b>{escape(name(a)) or 'без імені'}</b>", f"📞 {escape(a.phone or '—')}"]
    if event == "completed" and a.overall_score is not None:
        L.append(f"📊 Бал: <b>{a.overall_score:g}</b>")
    if a.patient_id:
        L += ["", f"➡️ <a href=\"{b}/patients/{a.patient_id}#t-m360\">Картка пацієнта</a>"]
    return "\n".join(L)


def notify(db: Session, a: M360Assessment, event: str, base: str) -> None:
    from .. import alerts, site_leads as sl
    text = alert_text(a, event, base)
    chat = str(sl.cfg(db).get("tg_chat") or "").strip()
    if chat:
        alerts._api("sendMessage", data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"})
    else:
        alerts.send(text)


__all__ = ["M360Error", "by_token", "register", "save_answer", "submit", "serialize", "pdf", "answers_html", "for_patient", "notify",
           "QUESTIONNAIRE", "QUESTION_LOOKUP"]
