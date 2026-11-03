"""Пошук пацієнтів незалежно від порядку слів: «Вероніка Дубовик Володимирівна» = «Дубовик Вероніка» = «дуб вер»."""
from __future__ import annotations

import re

from ..parsers.observ import norm


def qwords(q: str | None) -> list[str]:
    return norm((q or "").replace("ʼ", "").replace("'", "").replace("’", "")).split()


def match(q, *texts) -> bool:
    """Кожне слово запиту є частиною якогось слова в текстах (порядок слів не важливий). q — рядок або список qwords."""
    ws = qwords(q) if isinstance(q, str) else q
    if not ws:
        return True
    hay = norm(" ".join(str(t or "") for t in texts).replace("ʼ", "").replace("'", "").replace("’", "")).split()
    return all(any(w in h for h in hay) for w in ws)


def phone_match(q: str | None, phone: str | None) -> bool:
    """Запит із цифр (від 3) — збіг за номером: 0981234567 / 380981234567 / 98 123 — однаково."""
    d = re.sub(r"\D", "", q or "")
    if len(d) < 3 or re.search(r"[^\d\s+()\-]", q or ""):
        return False
    ph = re.sub(r"\D", "", phone or "")
    d9 = d.lstrip("0")[-9:] if len(d) >= 10 else d
    return bool(ph) and (d in ph or (bool(d9) and d9 in ph))


def patient(q: str | None, p) -> bool:
    """Пацієнт підходить під запит: ПІБ у будь-якому порядку або телефон."""
    if not (q or "").strip():
        return True
    return phone_match(q, getattr(p, "phone", None)) or match(q, getattr(p, "full_name", ""))
