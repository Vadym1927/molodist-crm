from __future__ import annotations

from html import escape
from pathlib import Path

APP_BASE_URL = "/molodist-360"
from .questionnaire import QUESTION_LOOKUP


def build_answers_report(
    assessment_id: str,
    patient_name: str,
    answers: list[dict],
    photo_path: str | None = None,
) -> str:
    """Build the patient-facing, printable answer report."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for answer in answers:
        meta = QUESTION_LOOKUP.get(answer["question_key"], {})
        key = (meta.get("module_name", answer["module_key"]), meta.get("factor_name", answer["factor_key"]))
        groups.setdefault(key, []).append({
            "question_label": meta.get("question_label", answer["question_key"]),
            "answer_label": answer.get("answer_label") or answer.get("answer_value") or "",
            "answer_score": None if answer.get("answer_value") == "na" else answer.get("answer_score", 0),
        })

    logo_url = f"{APP_BASE_URL}/static/assets/molodist-360-score-logo.jpg"
    photo_url = f"{APP_BASE_URL}{photo_path}" if photo_path else None
    icon_by_module = {
        "Відновлення": "score-recovery.jpg",
        "Метаболічне здоров’я": "score-metabolic.jpg",
        "Медична база": "score-medical.jpg",
        "Спосіб життя": "score-lifestyle.jpg",
        "Естетика": "score-aesthetics.jpg",
    }
    avatar_markup = (
        f"<img src='{escape(photo_url)}' alt='Фото пацієнта' loading='lazy'>"
        if photo_url
        else escape((patient_name[:1] or "M").upper())
    )
    style = """
    :root{--ink:#211135;--violet:#6f23e9;--lilac:#f1ebff;--line:rgba(87,55,145,.11)}
    *{box-sizing:border-box} body{margin:0;color:var(--ink);font-family:Inter,Arial,sans-serif;background:#faf9ff}
    body:before,body:after{content:'';position:fixed;z-index:-1;width:420px;height:420px;border-radius:50%;opacity:.3;filter:blur(2px);pointer-events:none;background:radial-gradient(circle at 35% 35%,#fff 0 16%,#d9c7ff 17% 20%,transparent 45%)}
    body:before{top:-130px;left:-140px}body:after{right:-150px;bottom:-160px;transform:rotate(35deg)}
    .site-header{position:sticky;top:0;z-index:10;background:rgba(255,255,255,.9);border-bottom:1px solid rgba(87,55,145,.08);backdrop-filter:blur(14px)}.header-inner{max-width:1120px;min-height:74px;margin:0 auto;padding:10px 24px;display:flex;align-items:center;gap:25px}.brand{display:block;flex:0 0 auto}.brand img{display:block;width:178px;height:auto}.header-nav{display:flex;align-items:center;gap:22px;margin-left:auto}.header-nav a{color:var(--ink);font-size:13px;text-decoration:none;white-space:nowrap}.header-phone{color:var(--ink);font-size:13px;font-weight:700;text-decoration:none;white-space:nowrap}.print-btn{display:inline-flex;align-items:center;gap:8px;border:0;cursor:pointer;color:#fff;font:700 13px/1 Inter,Arial,sans-serif;white-space:nowrap;padding:8px 18px 8px 10px;border-radius:999px;background:linear-gradient(105deg,#3010b7,#681be8 54%,#8d34f6);box-shadow:0 10px 23px rgba(84,30,208,.24)}.print-btn img{width:28px;height:28px;border-radius:50%;object-fit:cover}
    .wrap{max-width:1040px;margin:0 auto;padding:34px 24px 72px}.report-top{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:25px}
    .card{background:rgba(255,255,255,.91);border:1px solid var(--line);border-radius:24px;box-shadow:0 14px 35px rgba(74,48,112,.07)}
    .patient{display:flex;align-items:center;gap:16px;padding:20px 24px;margin-bottom:20px}.avatar{display:grid;place-items:center;flex:0 0 64px;width:64px;height:64px;overflow:hidden;border-radius:50%;background:linear-gradient(145deg,#e5d7ff,#f7f1ff);color:#7635df;font-size:26px;font-weight:750;box-shadow:inset 0 0 0 1px rgba(103,48,220,.12)}.avatar img{width:100%;height:100%;object-fit:cover}.meta{margin:0;color:#756a82;font-size:15px;line-height:1.45}.meta strong{color:var(--ink)}
    .answer-card{padding:26px;margin-bottom:18px;break-inside:avoid}.module-head{display:flex;align-items:center;gap:14px;margin-bottom:18px}.module-icon{display:block;position:relative;overflow:hidden;flex:0 0 48px;width:48px;height:48px;border-radius:50%;background:#f2ebff}.module-icon img{position:absolute;left:50%;top:50%;width:210%;height:210%;max-width:none;transform:translate(-50%,-50%);object-fit:cover}.module-head h2{margin:0;font-size:29px;letter-spacing:-.045em;line-height:1.05}.module-head h3{margin:5px 0 0;color:#776c88;font-size:18px;font-weight:500}
    .q{margin:10px 0;padding:17px 20px;border-radius:16px;background:linear-gradient(105deg,#f8f5ff,#f3eeff);border:1px solid rgba(111,63,180,.055)}.question{font-size:16px;line-height:1.35}.a{margin-top:7px;color:#6e32dc;font-size:17px;font-weight:750}.score{margin-top:3px;color:#958aa1;font-size:13px}
    .report-footer{margin-top:0;padding:50px 24px 24px;color:rgba(255,255,255,.83);background:linear-gradient(135deg,#21172f 0%,#302147 56%,#4c3470 100%)}.footer-inner{max-width:1040px;margin:0 auto}.footer-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr) minmax(0,1fr);gap:44px;padding-bottom:34px}.footer-grid>*{min-width:0}.report-footer h2{margin:0 0 13px;color:#fff;font-size:14px}.report-footer p,.report-footer address,.report-footer a{font-size:14px;line-height:1.55}.report-footer p{margin:0 0 8px}.report-footer address{margin:0 0 10px;font-style:normal}.report-footer section>a{display:block;width:max-content;max-width:100%;margin-top:5px;color:#fff;text-decoration:none}.report-footer a:hover{color:#d7bcff}.report-footer .footer-logo{display:block;width:190px!important;max-width:100%;margin:0 0 16px;border-radius:10px;overflow:hidden;background:#fff;line-height:0}.report-footer .footer-logo img{display:block;width:100%!important;max-width:100%;height:auto}.footer-disclaimer{max-width:330px;color:rgba(255,255,255,.6);font-size:12px!important;line-height:1.5!important}.footer-bottom{display:flex;gap:28px;align-items:flex-start;justify-content:space-between;padding-top:20px;border-top:1px solid rgba(255,255,255,.16)}.footer-bottom p{max-width:700px;margin:0;color:rgba(255,255,255,.6);font-size:12px;line-height:1.5}.footer-bottom nav{display:flex;flex-wrap:wrap;gap:14px 20px}.footer-bottom nav a{color:#fff;white-space:nowrap}
    @media(max-width:920px){.header-inner{min-height:62px;padding:8px 14px}.brand img{width:148px}.header-nav,.header-phone{display:none}.print-btn{margin-left:auto;padding:12px 14px;font-size:12px}.wrap{padding:25px 14px 48px}.patient{padding:16px}.answer-card{padding:18px}.module-head h2{font-size:24px}.module-head h3{font-size:16px}.q{padding:14px}.question{font-size:15px}.a{font-size:16px}.report-footer{padding:38px 18px 22px}.footer-grid{grid-template-columns:1fr;gap:26px;padding-bottom:28px}.footer-bottom{display:grid;gap:18px}.footer-bottom nav{gap:12px 18px}}
    @media print{body{background:#fff}body:before,body:after,.site-header,.report-footer{display:none}.wrap{max-width:none;padding:0}.card{box-shadow:none}.answer-card{break-inside:avoid}}
    """
    parts = [
        "<!DOCTYPE html><html lang='uk'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>MOLODIST 360 · Відповіді — {escape(patient_name)}</title><style>{style}</style></head><body>",
        f"<header class='site-header'><div class='header-inner'><a class='brand' href='{APP_BASE_URL}/' aria-label='MOLODIST 360 Score'><img src='{escape(logo_url)}' alt='MOLODIST 360 Score'></a><nav class='header-nav'><a href='{APP_BASE_URL}/#methodology'>Методологія</a><a href='{APP_BASE_URL}/#assessment'>Оцінка</a><a href='/'>Про клініку</a><a href='/beautyplan'>Beauty Plan</a></nav><a class='header-phone' href='tel:+380933756437'>+38 093 37 56 437</a><button class='print-btn' onclick='window.print()'><img src='{APP_BASE_URL}/static/assets/print-pdf-icon.png' alt=''>Друк / Зберегти в PDF</button></div></header><main class='wrap'>",
        f"<section class='patient card'><span class='avatar'>{avatar_markup}</span><p class='meta'>Пацієнт: <strong>{escape(patient_name)}</strong><br>ID проходження: {escape(assessment_id)}</p></section>",
    ]
    for (module_name, factor_name), rows in groups.items():
        icon_asset = icon_by_module.get(module_name, "score-aesthetics.jpg")
        icon_url = f"{APP_BASE_URL}/static/assets/{icon_asset}"
        parts.append(f"<section class='answer-card card'><div class='module-head'><span class='module-icon'><img src='{escape(icon_url)}' alt='' aria-hidden='true'></span><div><h2>{escape(module_name)}</h2><h3>{escape(factor_name)}</h3></div></div>")
        for row in rows:
            score_text = "Не впливає на score" if row["answer_score"] is None else f"Бал відповіді: {row['answer_score']}"
            parts.append(f"<div class='q'><div class='question'><strong>{escape(row['question_label'])}</strong></div><div class='a'>Відповідь: {escape(str(row['answer_label']))}</div><div class='score'>{escape(score_text)}</div></div>")
        parts.append("</section>")
    parts.append(f"</main><footer class='report-footer'><div class='footer-inner'><div class='footer-grid'><section><a class='footer-logo' href='{APP_BASE_URL}/' aria-label='MOLODIST 360 Score'><img src='{escape(logo_url)}' alt='MOLODIST 360 Score'></a><p>ФОП Колган Дар’я Олександрівна</p><p class='footer-disclaimer'>Результат MOLODIST 360 має інформаційний характер і не є медичним діагнозом. Для медичних рішень зверніться до лікаря.</p></section><section><h2>Ліцензія</h2><p>Ліцензія МОЗ України на провадження господарської діяльності з медичної практики.</p><p>Наказ № 249 від 08.02.2023 року.</p></section><section><h2>Контактна інформація</h2><address>м. Київ, вул. Князя Романа Мстиславича, 28а<br>ЖК «Автограф»</address><a href='tel:+380933756437'>+38 (093) 375 64 37</a><a href='mailto:info.molodostua@gmail.com'>info.molodostua@gmail.com</a></section></div><div class='footer-bottom'><p>© 2026 MOLODIST. Усі права захищені.</p><nav aria-label='Правова інформація'><a href='{APP_BASE_URL}/privacy-policy'>Політика конфіденційності</a><a href='{APP_BASE_URL}/terms'>Умови користування</a></nav></div></div></footer></body></html>")
    return "".join(parts)
