"""Собівартість послуги: розрахункова (з техкарти — норми списання × ціна закупівлі) або фіксована (задана в послузі)."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CbService, ProcedureNorm
from ..parsers.observ import norm


def techcard_costs(db: Session) -> dict[int, float]:
    """Матеріали на 1 послугу за нормами списання; послуга без власних норм бере норму своєї техкартки."""
    per: dict[int, float] = defaultdict(float)
    for n in db.scalars(select(ProcedureNorm)):
        per[n.service_id] += float(n.qty or 0) * float((n.product.cost if n.product else 0) or 0)
    svcs = db.scalars(select(CbService)).all()
    by_card: dict[str, float] = {}
    for sv in svcs:
        if sv.techcard and sv.id in per:
            by_card.setdefault(sv.techcard.strip().lower(), per[sv.id])
    for sv in svcs:
        if sv.id not in per and sv.techcard and sv.techcard.strip().lower() in by_card:
            per[sv.id] = by_card[sv.techcard.strip().lower()]
    return {k: round(v, 2) for k, v in per.items()}


class Costs:
    """Пошук собівартості позиції за назвою послуги (з чека) — обидва варіанти."""

    def __init__(self, db: Session):
        from .cb_services_import import service_index
        self.idx = service_index(db)
        self.tc = techcard_costs(db)

    def of(self, name: str) -> tuple[float, float]:
        sv = self.idx.get(norm(name or ""))
        if not sv:
            return 0.0, 0.0
        return self.tc.get(sv.id, 0.0), float(sv.cost_fixed or 0)
