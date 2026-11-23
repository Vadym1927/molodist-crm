from __future__ import annotations

from collections import defaultdict

from .questionnaire import FACTOR_LABELS, QUESTIONNAIRE

STATUS_RULES = [
    (90, "Strong base", "У вас вже є дуже сильна база. Більшість ключових факторів підтримані на високому рівні."),
    (75, "Good", "У вас хороша база, але є кілька зон, які ще стримують максимальний результат."),
    (60, "Needs attention", "У вас є хороші опори, але система ще нестабільна, і кілька факторів потребують уваги."),
    (40, "High risk", "Кілька ключових факторів вже негативно впливають на енергію, самопочуття та темп вікових змін."),
    (0, "Critical", "Поточний стан потребує пріоритетної уваги до базових факторів здоров’я, відновлення та способу життя."),
]

FACTOR_SUMMARIES = {
    "sleep": {
        "low": "Режим сну зараз не дає достатнього ресурсу для повноцінного відновлення.",
        "mid": "Сон частково підтримує відновлення, але ще не є стабільною сильною опорою.",
        "high": "Сон є сильною стороною і працює на ваше відновлення.",
    },
    "stress": {
        "low": "Рівень стресового навантаження зараз один із ключових факторів ризику.",
        "mid": "Стрес частково контрольований, але все ще впливає на загальний ресурс.",
        "high": "Рівень стресу загалом контрольований і не є головним обмеженням зараз.",
    },
    "nutrition": {
        "low": "Харчування зараз не дає тієї стабільної підтримки, яка потрібна для енергії та якості шкіри.",
        "mid": "Харчування частково підтримує вас, але ще є простір для системності.",
        "high": "Харчування є сильною опорою для метаболічного ресурсу.",
    },
    "activity": {
        "low": "Фізична активність зараз нижча за той рівень, який реально підтримує молодість і метаболізм.",
        "mid": "Рух є, але ще не достатньо стабільний або регулярний.",
        "high": "Рівень фізичної активності добре підтримує загальний стан.",
    },
    "hydration": {
        "low": "Водний баланс зараз не виглядає стабільною звичкою.",
        "mid": "Водний баланс частково контрольований, але ще потребує системності.",
        "high": "Водний баланс підтримується добре і стабільно.",
    },
    "checkup": {
        "low": "Базовий медичний контроль зараз недостатній, тому частина ризиків може залишатися поза увагою.",
        "mid": "Check-up є, але ще не на рівні регулярної системи контролю.",
        "high": "Медичний контроль достатньо добре вбудований у ваш спосіб життя.",
    },
    "deficiencies": {
        "low": "Є ризик, що дефіцити або пов’язані симптоми зараз помітно впливають на ресурс організму.",
        "mid": "Ситуація з дефіцитами частково контрольована, але не ідеальна.",
        "high": "Ризики, пов’язані з дефіцитами, зараз виглядають добре контрольованими.",
    },
    "hormones": {
        "low": "Гормонально чутливі процеси можуть бути однією з причин нестабільного самопочуття або стану шкіри.",
        "mid": "Гормональний блок потребує уточнення і більш системної уваги.",
        "high": "Гормональний блок не виглядає головною точкою напруги зараз.",
    },
    "habits": {
        "low": "Деякі звички зараз прямо послаблюють загальний ресурс і швидкість відновлення.",
        "mid": "Є звички, які вже частково контрольовані, але ще не закріплені системно.",
        "high": "Ваші звички загалом підтримують здоров’я, а не руйнують його.",
    },
    "spf": {
        "low": "Недостатній фотозахист зараз є одним із найсильніших зовнішніх факторів прискореного старіння шкіри.",
        "mid": "Фотозахист уже присутній, але ще не став стабільною системною звичкою.",
        "high": "Фотозахист добре інтегрований у ваш догляд і працює на профілактику вікових змін.",
    },
    "homecare": {
        "low": "Домашній догляд зараз не виглядає достатньо стабільним або прицільним.",
        "mid": "Догляд уже є, але ще не повністю відповідає потенціалу вашої шкіри.",
        "high": "Домашній догляд є сильною підтримкою для стабільного естетичного результату.",
    },
}

DEFAULT_SUMMARY = {
    "low": "Цей напрям зараз потребує пріоритетної уваги.",
    "mid": "У цьому напрямі є база, але ще не вистачає системності.",
    "high": "Цей напрям є однією з ваших сильних сторін.",
}

RED_FLAG_THRESHOLDS = {
    "sleep": 3.5,
    "stress": 3.5,
    "spf": 2.5,
    "deficiencies": 3.5,
}


def _summary_for_factor(factor_key: str, score: float) -> str:
    config = FACTOR_SUMMARIES.get(factor_key, DEFAULT_SUMMARY)
    if score < 4:
        return config["low"]
    if score < 7:
        return config["mid"]
    return config["high"]


MODULE_SUMMARIES = {
    "recovery": {
        "low": "Відновлення зараз є однією з головних зон уваги. Без стабільного сну і кращого контролю стресу інші зміни можуть працювати слабше.",
        "mid": "У блоці відновлення є база, але її ще потрібно стабілізувати.",
        "high": "Блок відновлення зараз працює на вас і підтримує загальний ресурс.",
    },
    "metabolic": {
        "low": "Метаболічний блок зараз не дає достатньої опори для енергії, ваги та якості відновлення.",
        "mid": "Метаболічне здоров’я частково підтримане, але потребує системності.",
        "high": "Метаболічний блок є сильною базою для стабільного самопочуття.",
    },
    "medical": {
        "low": "Медична база зараз потребує уваги, щоб не пропускати фактори, які можуть гальмувати результат.",
        "mid": "Медична база частково контрольована, але ще не повністю закрита системно.",
        "high": "Медична база виглядає достатньо добре контрольованою.",
    },
    "lifestyle": {
        "low": "Спосіб життя зараз містить фактори, які послаблюють стійкість організму.",
        "mid": "У способі життя є як сильні, так і нестабільні елементи.",
        "high": "Ваш повсякденний ритм загалом підтримує довгостроковий результат.",
    },
    "aesthetic": {
        "low": "Естетичний блок зараз не дає достатньої профілактики вікових змін шкіри.",
        "mid": "Естетична база є, але ще потребує більш системного підходу.",
        "high": "Естетичний блок зараз добре підтримує якість шкіри та профілактику.",
    },
}


def _module_status(module_score: float) -> str:
    if module_score >= 8.5:
        return "strong"
    if module_score >= 7:
        return "good"
    if module_score >= 5:
        return "attention"
    return "risk"


MODULE_MAP = {module["key"]: module for module in QUESTIONNAIRE["modules"]}


def evaluate(answer_rows: list[dict]) -> dict:
    """Score each module against its own applicable questions.

    Every one of the five modules is worth 20 points.  ``N/A`` choices are
    intentionally excluded from both numerator and denominator, so medical
    questions that do not apply never reduce a participant's result.
    """
    factor_meta: dict[str, dict] = {}
    for module in QUESTIONNAIRE["modules"]:
        for factor in module["factors"]:
            factor_meta[factor["key"]] = {
                "module_key": module["key"], "module_name": module["name"],
                "factor_name": factor["name"], "informational": bool(factor.get("informational", False)),
            }

    rows_by_factor: dict[str, list[dict]] = defaultdict(list)
    rows_by_module: dict[str, list[dict]] = defaultdict(list)
    for row in answer_rows:
        meta = factor_meta.get(row["factor_key"])
        if not meta:
            continue
        rows_by_factor[row["factor_key"]].append(row)
        if not meta["informational"] and row.get("answer_value") != "na":
            rows_by_module[meta["module_key"]].append(row)

    module_details: dict[str, dict] = {}
    weighted_total = 0.0
    for module in QUESTIONNAIRE["modules"]:
        applicable = rows_by_module.get(module["key"], [])
        raw = sum(float(row.get("answer_score") or 0) for row in applicable)
        maximum = 10 * len(applicable)
        contribution = (raw / maximum * float(module["weight"])) if maximum else 0.0
        module_details[module["key"]] = {"raw": raw, "maximum": maximum, "contribution": contribution}
        weighted_total += contribution

    factors = []
    top_candidates = []
    for factor_key, meta in factor_meta.items():
        rows = rows_by_factor.get(factor_key, [])
        applicable = [row for row in rows if row.get("answer_value") != "na"]
        if applicable:
            raw = sum(float(row.get("answer_score") or 0) for row in applicable)
            factor_score = round(raw / len(applicable), 1)
        else:
            raw = 0.0
            factor_score = 0.0
        module_detail = module_details[meta["module_key"]]
        contribution = 0.0 if meta["informational"] or not module_detail["maximum"] else round(raw / module_detail["maximum"] * 20, 2)
        # Dynamic factor weight reports its current share of the fixed 20-point module.
        factor_weight = 0.0 if meta["informational"] else round(len(applicable) / (module_detail["maximum"] / 10) * 20, 2) if module_detail["maximum"] else 0.0
        summary = _summary_for_factor(factor_key, factor_score)
        is_red_flag = bool(applicable) and factor_score <= RED_FLAG_THRESHOLDS.get(factor_key, -1)
        if not meta["informational"] and applicable:
            top_candidates.append((factor_score, meta["factor_name"]))
        factors.append({
            "factor_key": factor_key, "factor_name": meta["factor_name"],
            "module_key": meta["module_key"], "module_name": meta["module_name"],
            "factor_score": factor_score, "factor_weight": factor_weight,
            "factor_contribution": contribution, "factor_summary": summary,
            "informational": meta["informational"], "is_red_flag": is_red_flag,
        })

    overall_score = max(0, min(100, round(weighted_total)))
    status = next((row for row in STATUS_RULES if overall_score >= row[0]), STATUS_RULES[-1])

    modules = []
    for module in QUESTIONNAIRE["modules"]:
        detail = module_details[module["key"]]
        module_score = round(detail["raw"] / detail["maximum"] * 10, 1) if detail["maximum"] else 0
        status_key = _module_status(module_score)
        summary_pool = MODULE_SUMMARIES[module["key"]]
        summary = summary_pool["high"] if status_key in {"strong", "good"} else summary_pool["mid"] if status_key == "attention" else summary_pool["low"]
        modules.append({
            "module_key": module["key"],
            "module_name": module["name"],
            "module_score": module_score,
            "module_status": status_key,
            "module_summary": summary,
        })

    top_risks = [name for _, name in sorted(top_candidates, key=lambda x: x[0])[:3]]
    overall_summary = status[2]
    if any(f["is_red_flag"] for f in factors):
        overall_summary += " У вас також є фактори високого ризику, які потребують пріоритетної уваги навіть за відносно кращого загального індексу."

    return {
        "overall_score": overall_score,
        "overall_status": status[1],
        "overall_summary": overall_summary,
        "modules": modules,
        "factors": factors,
        "top_risks": top_risks,
    }
