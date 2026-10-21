"""Парсер PDF бюті-планів з OBSERV 520x.

Структура звіту (стабільна у Observ): шапка (клініка, лікар, пацієнт, дата сесії, скарги,
коментарі) -> протокол процедур -> домашній догляд -> розпорядок дня -> «Загальні витрати».
Головне джерело цифр — блок «Загальні витрати», бо там кожна позиція в один рядок.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import pdfplumber

PRICE_RE = re.compile(r"(\d{1,3}(?: \d{3})*,\d{2})\s*₴\s*$")
ITEM_RE = re.compile(r"^(\d+)\s+(.+?)\s*(\d{1,3}(?: \d{3})*,\d{2})\s*₴\s*$")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4}),?\s*(\d{2}:\d{2})?")
VOLUME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мл|ml|г|g)\b", re.I)


def money(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def norm(s: str) -> str:
    """Нормалізація назви для зіставлення (регістр, латинська/кирилична Т, пробіли)."""
    s = s.upper().replace("Т", "T").replace(" ", " ")
    s = re.sub(r"[^\w%]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class PlanItem:
    kind: str               # "procedure" | "product"
    name: str
    qty: int
    unit_price: float
    total: float
    volume: str | None = None      # "245 мл" для засобів
    usage: list[str] = field(default_factory=list)  # ["morning", "evening"]
    url: str | None = None
    brand: str | None = None


@dataclass
class ParsedPlan:
    patient_name: str
    doctor_name: str | None
    session_at: datetime | None
    complaints: str | None
    comments: str | None
    routine_comment: str | None
    items: list[PlanItem]
    total: float
    clinic_phone: str | None = None
    patient_phone: str | None = None
    patient_email: str | None = None
    plan_title: str | None = None

    @property
    def procedures(self):
        return [i for i in self.items if i.kind == "procedure"]

    @property
    def products(self):
        return [i for i in self.items if i.kind == "product"]

    def to_dict(self):
        d = asdict(self)
        d["session_at"] = self.session_at.isoformat() if self.session_at else None
        return d


class ObservParseError(ValueError):
    pass


def _after(lines: list[str], label: str, start: int = 0) -> tuple[str | None, int]:
    """Перший непорожній рядок після мітки (поки не зустрілась наступна мітка-заголовок)."""
    for i in range(start, len(lines)):
        if lines[i].strip() == label:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt and not nxt.isupper():
                return nxt, i + 1
            return None, i
    return None, start


def parse_observ_pdf(path: str | Path, plan_title: str | None = None) -> ParsedPlan:
    with pdfplumber.open(str(path)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
        links = []
        for p in pdf.pages:
            for h in (p.hyperlinks or []):
                if h.get("uri"):
                    links.append(h["uri"])

    full = "\n".join(pages)
    if "Загальні витрати" not in full:
        raise ObservParseError("Це не схоже на бюті-план Observ: немає блоку «Загальні витрати»")

    header = pages[0].splitlines()

    # --- Лікар: рядок після «КЛІНІКА ПРАКТИКУЮЧИЙ» має вигляд «<клініка> <лікар>»
    doctor = None
    for i, ln in enumerate(header):
        if "ПРАКТИКУЮЧИЙ" in ln and i + 1 < len(header):
            parts = header[i + 1].split()
            # перший токен — клініка (MOLODIST), решта — ПІБ лікаря
            doctor = " ".join(parts[1:]) if len(parts) > 1 else None
            break

    # --- Пацієнт
    patient = None
    for i, ln in enumerate(header):
        if ln.strip() == "Інформація про клієнта":
            for j in range(i + 1, min(i + 4, len(header))):
                if header[j].strip() == "НАЗВА" and j + 1 < len(header):
                    patient = header[j + 1].strip()
            break
    if not patient:
        raise ObservParseError("Не знайшов ім'я пацієнта")

    # телефони/email: у блоці клієнта після мітки ТЕЛЕФОН значення може бути порожнім
    def field_in_block(block_start: str, label: str) -> str | None:
        try:
            s = next(i for i, l in enumerate(header) if l.strip() == block_start)
        except StopIteration:
            return None
        for j in range(s, len(header)):
            if header[j].strip() == label:
                v = header[j + 1].strip() if j + 1 < len(header) else ""
                if re.fullmatch(r"[+\d][\d\s()\-]{6,}", v) or "@" in v:
                    return v
                return None
        return None

    clinic_phone = field_in_block("Деталі клініки", "ТЕЛЕФОН")
    patient_phone = field_in_block("Інформація про клієнта", "ТЕЛЕФОН")
    patient_email = field_in_block("Інформація про клієнта", "ЕЛЕКТРОННА ПОШТА")

    # --- Сесія: «<скарги> 10.10.2026, 17:22»
    session_at, complaints = None, None
    for i, ln in enumerate(header):
        if ln.startswith("НАЗВА") and "ДАТА" in ln and i + 1 < len(header):
            row = header[i + 1]
            m = DATE_RE.search(row)
            if m:
                fmt = "%d.%m.%Y %H:%M" if m.group(2) else "%d.%m.%Y"
                session_at = datetime.strptime(" ".join(g for g in m.groups() if g), fmt)
                complaints = row[: m.start()].strip() or None
            else:
                complaints = row.strip()
            break

    comments = None
    if "КОМЕНТАРІ" in header:
        k = header.index("КОМЕНТАРІ")
        comments = "\n".join(l for l in header[k + 1:] if l.strip()).strip() or None

    # --- Загальні витрати
    summary_page = next(p for p in pages if p.lstrip().startswith("Загальні витрати") or "Загальні витрати" in p)
    lines = summary_page.splitlines()
    items: list[PlanItem] = []
    kind = None
    total = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s == "МЕТОДИ ЛІКУВАННЯ":
            kind = "procedure"; continue
        if s == "ДОМАШНІЙ ДОГЛЯД":
            kind = "product"; continue
        if s == "Загалом":
            m = PRICE_RE.search(lines[i + 1]) if i + 1 < len(lines) else None
            total = money(m.group(1)) if m else None
            kind = None; continue
        if kind:
            m = ITEM_RE.match(s)
            if m:
                qty = int(m.group(1)); name = m.group(2).strip(); line_total = money(m.group(3))
                items.append(PlanItem(kind=kind, name=name, qty=qty,
                                      unit_price=round(line_total / qty, 2), total=line_total))

    if not items:
        raise ObservParseError("Не знайшов жодної позиції у «Загальні витрати»")

    # --- Збагачення засобів: об'єм, ранок/вечір, посилання, бренд (зі сторінок «Домашній догляд»)
    _enrich_products(items, pages, links)

    calc_total = round(sum(i.total for i in items), 2)
    if total is None:
        total = calc_total

    routine_comment = None
    for p in pages:
        if p.lstrip().startswith("Розпорядок дня") and "КОМЕНТАРІ" in p:
            routine_comment = p.split("КОМЕНТАРІ", 1)[1].strip() or None

    return ParsedPlan(patient_name=patient, doctor_name=doctor, session_at=session_at,
                      complaints=complaints, comments=comments, routine_comment=routine_comment,
                      items=items, total=total, clinic_phone=clinic_phone,
                      patient_phone=patient_phone, patient_email=patient_email,
                      plan_title=plan_title)


def _extract_store_urls(pages: list[str]) -> list[str]:
    """URL після «ВЕБ-ПОСИЛАННЯ» розбиті на кілька рядків (а між ними може влізти бренд)."""
    urls = []
    text = "\n".join(pages)
    for m in re.finditer(r"ВЕБ-ПОСИЛАННЯ\n", text):
        parts = []
        for ln in text[m.end():].splitlines()[:5]:
            s = re.sub(r"^[A-Za-z]+\s+(?=https?://)", "", ln.strip())   # «Agskin https://…»
            if not parts and not s.startswith("http"):
                if re.fullmatch(r"[A-Za-z]+", s):
                    continue
                break
            if re.fullmatch(r"[a-z0-9\-/:._?=&%]+", s):
                parts.append(s)
                if s.endswith("/"):
                    break
            elif parts and re.fullmatch(r"[A-Za-z]+", s):
                continue          # бренд-мітка з лівої колонки
            elif parts:
                break
        if parts:
            urls.append("".join(parts))
    return urls


def _routine_usage(pages: list[str]) -> dict[str, list[str]]:
    """Розпорядок дня: які засоби в РАНОК / ВЕЧІР. Повертає {нормалізований рядок: [morning, evening]}."""
    page = next((p for p in pages if p.lstrip().startswith("Розпорядок дня")), "")
    body = page.split("КОМЕНТАРІ", 1)[0]
    res: dict[str, list[str]] = {"morning": [], "evening": []}
    cur = None
    for ln in body.splitlines()[1:]:
        s = ln.strip()
        if s.startswith("РАНОК"):
            cur = "morning"; s = s[5:]
        elif s.startswith("ВЕЧІР"):
            cur = "evening"; s = s[5:]
        if cur and s:
            res[cur].append(norm(s))
    return {k: [" ".join(v)] for k, v in res.items()}


def _overlap(a: str, b: str) -> float:
    wa = {w for w in norm(a).split() if len(w) > 2}
    wb = set(norm(b).split())
    return len(wa & wb) / max(len(wa), 1)


def _enrich_products(items: list[PlanItem], pages: list[str], links: list[str]) -> None:
    products = [i for i in items if i.kind == "product"]
    urls = [u for u in (links or []) if "molodist.store" in u] or _extract_store_urls(pages)
    routine = _routine_usage(pages)

    for idx, it in enumerate(products):
        vm = VOLUME_RE.search(it.name)
        if vm:
            it.volume = f"{vm.group(1)} {'мл' if vm.group(2).lower() in ('мл', 'ml') else 'г'}"

        # ранок/вечір: засіб присутній у тексті відповідного блоку розпорядку
        # беремо лише «розпізнавальні» слова — яких немає в назвах інших засобів плану
        others = set()
        for o in products:
            if o is not it:
                others |= set(norm(o.name).split())
        words = [w for w in norm(it.name).split() if len(w) > 2 and w not in others and not w.isdigit()]
        for slot, texts in routine.items():
            blob = set((texts[0] if texts else "").split())
            if words and sum(w in blob for w in words) / len(words) >= 0.5:
                it.usage.append(slot)

        # посилання: порядок карток = порядок у «Загальних витратах»; перевіряємо латинськими словами
        latin = [w.lower() for w in re.findall(r"[A-Za-z0-9]{2,}", it.name)]
        def score(u):
            return sum(w in u.lower() for w in latin) / max(len(latin), 1)
        cand = urls[idx] if idx < len(urls) else None
        if cand and score(cand) >= 0.3:
            it.url = cand
        else:
            best = max(urls, key=score, default=None)
            if best and score(best) >= 0.4:
                it.url = best

        # обʼєм для засобів без нього в назві (напр. SPF-крем) — з картки «РЕЦЕПТ <обʼєм>»
        if not it.volume:
            care = "\n".join(pages)
            pos = care.upper().find(it.name.upper()[:30])
            if pos >= 0:
                r = re.search(r"РЕЦЕПТ\n\s*(\d+)\s*(ml|мл|g|г)\b", care[pos:], re.I)
                if r:
                    it.volume = f"{r.group(1)} {'мл' if r.group(2).lower() in ('ml','мл') else 'г'}"


if __name__ == "__main__":
    import json, sys
    plan = parse_observ_pdf(sys.argv[1])
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
