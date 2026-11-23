from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import io

STATIC = Path(__file__).resolve().parents[2] / "static"
ASSETS = STATIC / "m360" / "assets"
FONTS = STATIC / "fonts" / "pdf"


PAGE_W, PAGE_H = A4
MARGIN = 30
INK = colors.HexColor("#21103f")
MUTED = colors.HexColor("#746b82")
LILAC = colors.HexColor("#f1edfc")
GREEN = colors.HexColor("#47966d")
GOLD = colors.HexColor("#d8a844")
ORANGE = colors.HexColor("#d97942")
PURPLE = colors.HexColor("#7645e8")

MODULE_ICONS = {
    "recovery": "priority-sleep.jpg", "metabolic": "score-metabolic.jpg", "medical": "score-medical.jpg",
    "lifestyle": "score-lifestyle.jpg", "aesthetic": "score-aesthetics.jpg",
}
FACTOR_ICONS = {
    "sleep": "factor-sleep.jpg", "stress": "factor-stress.jpg", "activity": "factor-activity.jpg",
    "hydration": "factor-water.jpg", "checkup": "factor-checkup.jpg", "deficiencies": "factor-deficiencies.jpg",
    "hormones": "factor-hormones.jpg", "habits": "factor-habits.jpg", "spf": "factor-sunscreen.jpg",
    "homecare": "factor-home-care.jpg", "nutrition": "factor-nutrition.jpg", "genetics": "factor-genetics.jpg",
    "environment": "factor-environment.jpg",
}


def _register_fonts() -> None:
    if "MolodistSans" in pdfmetrics.getRegisteredFontNames():
        return
    reg, bold = str(FONTS / "DejaVuSans.ttf"), str(FONTS / "DejaVuSans-Bold.ttf")   # кирилиця; serif → той самий шрифт
    pdfmetrics.registerFont(TTFont("MolodistSans", reg))
    pdfmetrics.registerFont(TTFont("MolodistBold", bold))
    pdfmetrics.registerFont(TTFont("MolodistSerif", reg))
    pdfmetrics.registerFont(TTFont("MolodistSerifBold", bold))


def _status_color(score: float):
    if score >= 7:
        return GREEN
    if score >= 5:
        return GOLD
    return ORANGE


def _status_label(status: str) -> str:
    return {
        "strong": "Сильна сторона", "strong base": "Сильна сторона", "good": "Добра база",
        "attention": "Потребує уваги", "needs attention": "Потребує уваги",
        "risk": "Зона уваги", "high risk": "Зона уваги", "critical": "Пріоритетна увага",
    }.get((status or "").lower(), "Потребує уваги")


def _lines(c: canvas.Canvas, text: str, font: str, size: float, max_width: float) -> list[str]:
    words = (text or "").split()
    out, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and c.stringWidth(candidate, font, size) > max_width:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _draw_wrapped(c: canvas.Canvas, text: str, x: float, y: float, width: float, *, font="MolodistSans", size=9, leading=12, color=MUTED, max_lines=None):
    c.setFillColor(color)
    c.setFont(font, size)
    lines = _lines(c, text, font, size, width)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        while c.stringWidth(lines[-1] + "…", font, size) > width and lines[-1]:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _round(c: canvas.Canvas, x, y, w, h, radius=14, fill=colors.white, stroke=None, line=.7):
    c.setFillColor(fill)
    c.setStrokeColor(stroke or fill)
    c.setLineWidth(line)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=bool(stroke))


def _icon(c: canvas.Canvas, filename: str | None, x: float, y: float, size: float):
    if not filename:
        return
    path = ASSETS / filename
    if not path.exists():
        return
    c.saveState()
    clip = c.beginPath()
    clip.circle(x + size / 2, y + size / 2, size / 2)
    c.clipPath(clip, stroke=0, fill=0)
    c.drawImage(ImageReader(str(path)), x, y, width=size, height=size, preserveAspectRatio=True, anchor="c", mask="auto")
    c.restoreState()


def _header(c: canvas.Canvas, page: int):
    c.setStrokeColor(colors.HexColor("#dad2eb"))
    c.setLineWidth(.7)
    c.line(MARGIN, PAGE_H - 37, PAGE_W - MARGIN, PAGE_H - 37)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 11)
    c.drawString(MARGIN, PAGE_H - 28, "MOLODIST 360")
    # Keep the report label clear of the monogram at the outer edge.
    monogram_x = PAGE_W - MARGIN - 8
    c.setFillColor(MUTED)
    c.setFont("MolodistSans", 6.5)
    c.drawRightString(monogram_x - 14, PAGE_H - 27, "ПЕРСОНАЛЬНИЙ ЗВІТ")
    c.setStrokeColor(colors.HexColor("#aa9ac9"))
    c.circle(monogram_x, PAGE_H - 25, 8, stroke=1, fill=0)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 8)
    c.drawCentredString(monogram_x, PAGE_H - 28, "M")
    c.setFillColor(MUTED)
    c.setFont("MolodistSans", 6.5)
    c.drawString(MARGIN, 17, "MOLODIST · Молодість 360")
    c.drawRightString(PAGE_W - MARGIN, 17, f"{page:02d} / 02")


def _module_row(c: canvas.Canvas, module: dict, y: float):
    x, w, h = MARGIN, PAGE_W - MARGIN * 2, 47
    _round(c, x, y, w, h, 10, fill=colors.HexColor("#f6f3fd"), stroke=colors.HexColor("#e5def3"), line=.45)
    _icon(c, MODULE_ICONS.get(module["module_key"]), x + 9, y + 7, 33)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 11)
    c.drawString(x + 53, y + 28, module["module_name"])
    c.setStrokeColor(colors.HexColor("#d6cee8"))
    c.line(x + 188, y + 8, x + 188, y + h - 8)
    c.setFillColor(INK)
    c.setFont("MolodistSerifBold", 16)
    c.drawRightString(x + 246, y + 25, f"{module['module_score']:.1f}")
    c.setFont("MolodistSerif", 9)
    c.drawString(x + 249, y + 25, "/10")
    c.line(x + 269, y + 8, x + 269, y + h - 8)
    c.setFillColor(_status_color(module["module_score"]))
    c.setFont("MolodistSans", 7.5)
    c.drawString(x + 279, y + 27, _status_label(module["module_status"]))
    _draw_wrapped(c, module["module_summary"], x + 279, y + 16, w - 290, size=6.7, leading=8, color=MUTED, max_lines=2)


def _factor_card(c: canvas.Canvas, factor: dict, x: float, y: float, w: float, h: float):
    _round(c, x, y, w, h, 10, fill=colors.HexColor("#f4f1fb"), stroke=colors.HexColor("#e3dcee"), line=.4)
    _icon(c, FACTOR_ICONS.get(factor["factor_key"]), x + 10, y + h - 41, 30)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 10.3)
    c.drawString(x + 47, y + h - 21, factor["factor_name"])
    badge = _status_color(factor["factor_score"])
    _round(c, x + w - 50, y + h - 31, 39, 18, 9, fill=colors.Color(badge.red, badge.green, badge.blue, alpha=.16))
    c.setFillColor(badge)
    c.setFont("MolodistSans", 7.1)
    c.drawCentredString(x + w - 30.5, y + h - 25, f"{factor['factor_score']:.1f}/10")
    _draw_wrapped(c, factor["factor_summary"], x + 12, y + h - 53, w - 24, size=7, leading=9, color=MUTED, max_lines=3)


def build_pdf(assessment_id: str, patient_name: str, payload: dict) -> bytes:
    """Build the two-page branded MOLODIST 360 personal report."""
    target = io.BytesIO()
    _register_fonts()
    c = canvas.Canvas(target, pagesize=A4, pageCompression=1)
    c.setTitle(f"MOLODIST 360 — {patient_name}")
    c.setAuthor("MOLODIST")

    c.setFillColor(colors.HexColor("#fcfbff"))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    _header(c, 1)
    c.setFillColor(MUTED)
    c.setFont("MolodistSans", 7)
    c.drawString(MARGIN, PAGE_H - 60, "ПАЦІЄНТ")
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 19)
    c.drawString(MARGIN, PAGE_H - 82, patient_name or "Пацієнт")
    _round(c, MARGIN, PAGE_H - 230, PAGE_W - 2 * MARGIN, 120, 14, fill=LILAC)
    # The score is a true progress dial, not a full purple ring.
    score_center_x = MARGIN + 86
    score_center_y = PAGE_H - 170
    score_radius = 43
    c.setStrokeColor(colors.HexColor("#ddd6eb"))
    c.setLineWidth(5)
    c.circle(score_center_x, score_center_y, score_radius, stroke=1, fill=0)
    c.setStrokeColor(PURPLE)
    c.setLineCap(1)
    c.arc(
        score_center_x - score_radius,
        score_center_y - score_radius,
        score_center_x + score_radius,
        score_center_y + score_radius,
        startAng=90,
        extent=-(360 * max(0, min(100, payload["overall_score"])) / 100),
    )
    c.setFillColor(INK)
    c.setFont("MolodistSerifBold", 42)
    c.drawCentredString(score_center_x, PAGE_H - 183, str(payload["overall_score"]))
    c.setFont("MolodistSerif", 22)
    c.drawString(score_center_x + score_radius + 7, PAGE_H - 179, "/100")
    c.setFillColor(MUTED)
    # The index caption and status badge deliberately sit on separate lines.
    c.setFont("MolodistSans", 5.4)
    c.drawCentredString(score_center_x, PAGE_H - 198, "ЗАГАЛЬНИЙ ІНДЕКС МОЛОДОСТІ")
    score_color = _status_color(payload["overall_score"])
    _round(c, MARGIN + 28, PAGE_H - 228, 116, 16, 8, fill=colors.Color(score_color.red, score_color.green, score_color.blue, alpha=.18))
    c.setFillColor(score_color)
    c.setFont("MolodistSans", 7.2)
    c.drawCentredString(score_center_x, PAGE_H - 222, _status_label(payload.get("overall_status", "")))
    _icon(c, "consultation-next-step.jpg", MARGIN + 181, PAGE_H - 167, 40)
    _draw_wrapped(c, payload["overall_summary"], MARGIN + 231, PAGE_H - 143, PAGE_W - MARGIN - (MARGIN + 241), font="MolodistSerif", size=12, leading=15, color=INK, max_lines=5)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 17)
    c.drawString(MARGIN, PAGE_H - 258, "Ваші 5 модулів")
    c.setStrokeColor(colors.HexColor("#b9afce"))
    c.setLineWidth(.6)
    c.line(MARGIN + 145, PAGE_H - 254, PAGE_W - MARGIN, PAGE_H - 254)
    y = PAGE_H - 312
    for module in payload["modules"]:
        _module_row(c, module, y)
        y -= 52
    _round(c, MARGIN, 46, PAGE_W - MARGIN * 2, 53, 11, fill=colors.HexColor("#f0ecfa"))
    _icon(c, "action-pdf.jpg", MARGIN + 10, 56, 33)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 12)
    c.drawString(MARGIN + 54, 79, "Короткий висновок")
    _draw_wrapped(c, "Звіт показує ваші сильні сторони та напрями, які варто розібрати глибше на консультації.", MARGIN + 54, 65, PAGE_W - MARGIN * 2 - 65, size=7.6, leading=9, color=MUTED, max_lines=2)
    c.showPage()

    c.setFillColor(colors.HexColor("#fcfbff"))
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    _header(c, 2)
    c.setFillColor(MUTED)
    c.setFont("MolodistSans", 7)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 59, "MOLODIST 360")
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 27)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 92, "Деталізація факторів")
    c.setFillColor(MUTED)
    c.setFont("MolodistSerif", 12)
    c.drawCentredString(PAGE_W / 2, PAGE_H - 111, "Що підтримує вашу молодість")
    factors = [item for item in payload["factors"] if not item.get("informational")]
    card_w, card_h = (PAGE_W - MARGIN * 2 - 10) / 2, 83
    start_y = PAGE_H - 210
    for index, factor in enumerate(factors):
        col, row = index % 2, index // 2
        x = MARGIN + col * (card_w + 10)
        y = start_y - row * (card_h + 8)
        _factor_card(c, factor, x, y, card_w, card_h)
    y_cta = 58
    _round(c, MARGIN, y_cta, PAGE_W - MARGIN * 2, 58, 11, fill=LILAC)
    _icon(c, "consultation-next-step.jpg", MARGIN + 12, y_cta + 12, 34)
    c.setFillColor(INK)
    c.setFont("MolodistSerif", 13)
    c.drawString(MARGIN + 57, y_cta + 37, "Наступний крок")
    _draw_wrapped(c, "Обговоріть результат на консультації MOLODIST, щоб сформувати персональний Beauty Plan і відстежувати зміни.", MARGIN + 57, y_cta + 24, PAGE_W - MARGIN * 2 - 70, size=7.4, leading=9, color=MUTED, max_lines=2)
    c.setFillColor(MUTED)
    c.setFont("MolodistSans", 5.8)
    c.drawString(MARGIN, 34, "Цей звіт не є медичним діагнозом. Це інструмент первинної комплексної оцінки факторів ризику, способу життя та зон уваги.")
    c.save()
    return target.getvalue()
