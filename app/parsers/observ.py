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


NOPRICE_RE = re.compile(r"^(\d+)\s+(\S.*\S)$")


def _detail_price(pages: list[str], name: str) -> float | None:
    """Ціна засобу з його картки: «РЕЦЕПТ / 50 ml 4 000,00 ₴ 1»."""
    care = "\n".join(pages)
    pos = care.upper().find(name.upper()[:30])
    if pos < 0:
        return None
    r = re.search(r"РЕЦЕПТ\n[^\n]*?(\d{1,3}(?: \d{3})*,\d{2})\s*₴", care[pos:pos + 4000])
    return money(r.group(1)) if r else None


def parse_items(lines: list[str], pages: list[str] | None = None) -> tuple[list[PlanItem], float | None]:
    """Позиції блоку «Загальні витрати». Два відомі збої тексту PDF:
    • назва, що закінчується цифрою, злипається з ціною: «…MEDIK84 000,00 ₴» = «…MEDIK8» + «4 000,00 ₴» (а не 84 000);
    • у довгої назви ціна не вміщується в рядок — позиція без ціни.
    Звіряємось із рядком «Загалом»: підбираємо варіант, де сума позицій дорівнює підсумку; ціну без рядка беремо з різниці
    або з картки засобу."""
    raw, kind, total = [], None, None
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
        if not kind:
            continue
        m = ITEM_RE.match(s)
        if m:
            name, price = m.group(2).strip(), m.group(3)
            alt = None
            head = price.split(" ")[0].split(",")[0]
            if s[m.start(3) - 1] != " " and len(head) >= 2:
                alt = (name + price[0], price[1:])              # одна цифра ціни насправді належить назві
                if not re.fullmatch(r"\d{1,3}(?: \d{3})*,\d{2}", alt[1]):
                    alt = None
            raw.append({"kind": kind, "qty": int(m.group(1)), "name": name, "sum": money(price),
                        "alt": (alt[0], money(alt[1])) if alt else None})
        elif "₴" not in s:
            m2 = NOPRICE_RE.match(s)
            if m2 and kind == "product":
                raw.append({"kind": kind, "qty": int(m2.group(1)), "name": m2.group(2).strip(), "sum": None, "alt": None})
    glued = [r for r in raw if r["alt"]]
    missing = [r for r in raw if r["sum"] is None]
    chosen = None
    if total is not None and (glued or missing):
        from itertools import product as _prod
        for mask in sorted(_prod((0, 1), repeat=len(glued)), key=lambda t: -sum(t)):       # спершу — «всі злиплі розклеїти»
            sm = sum(r["sum"] for r in raw if r["sum"] is not None and not r["alt"])
            sm += sum((g["alt"][1] if on else g["sum"]) for g, on in zip(glued, mask))
            diff = round(total - sm, 2)
            if (not missing and abs(diff) < 0.01) or (len(missing) == 1 and diff > 0.009) or (len(missing) > 1 and diff > 0.009 and all(mask)):
                chosen = (mask, diff)
                break
    mask = chosen[0] if chosen else tuple(1 for _ in glued)       # без підсумку: ціна впритул до назви — завжди злипання
    for g, on in zip(glued, mask):
        if on:
            g["name"], g["sum"] = g["alt"]
    if missing:
        diff = chosen[1] if chosen else None
        for r in missing:
            dp = _detail_price(pages or [], r["name"])
            r["sum"] = (dp * r["qty"] if dp else None)
        unknown = [r for r in missing if r["sum"] is None]
        if len(missing) == 1 and diff is not None:
            missing[0]["sum"] = diff                              # підсумок точніший за картку
        elif len(unknown) == 1 and diff is not None:
            unknown[0]["sum"] = round(diff - sum(r["sum"] or 0 for r in missing), 2)
        for r in missing:
            r["sum"] = max(r["sum"] or 0.0, 0.0)
    items = [PlanItem(kind=r["kind"], name=r["name"], qty=r["qty"], unit_price=round(r["sum"] / r["qty"], 2), total=r["sum"]) for r in raw]
    return items, total


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
    items, total = parse_items(lines, pages)

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

    if session_at is None:                # дата сесії «злиплась» з текстом скарг — беремо дату створення PDF (звіт формують у день сесії)
        session_at = pdf_created(path)
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


def pdf_created(path) -> datetime | None:
    """Дата створення PDF з метаданих (D:YYYYMMDDHHMMSS…)."""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            raw = str((pdf.metadata or {}).get("CreationDate") or "")
        m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?", raw)
        if not m:
            return None
        y, mo, d, h, mi = (int(x) if x else 0 for x in m.groups())
        dt = datetime(y, mo, d, h, mi)
        return dt if 2015 <= y <= datetime.now().year + 1 else None
    except Exception:
        return None
