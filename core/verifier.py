"""Code verifier (SPEC F3 step 4, P4 global rule): every claim cites a fact_id that exists, and every number
in the output matches a fact value within tolerance (±1% or ±0.5 for rounding). Returns issues; [] = PASS.

Used by F3 and F4. Checks: option names, risk range, drivers (fact exists + numbers match the cited fact),
must-cite facts, why_recommended / why_not_others (when check_why), and free-text explanations (when
check_texts: every number must match some fact)."""
import re

from core.settings import thresholds

NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUMBER.findall((text or "").translate(DEVANAGARI_DIGITS)):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def fact_numbers(fact: dict) -> list[float]:
    nums = numbers_in(fact.get("text", ""))
    for key in ("value", "value_mid"):
        v = fact.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(float(v))
    for v in fact.get("range") or []:
        if isinstance(v, (int, float)):
            nums.append(float(v))
    return nums


def matches(n: float, candidates: list[float], rel: float, abs_tol: float) -> bool:
    return any(abs(n - c) <= max(abs_tol, rel * abs(c)) or abs(abs(n) - abs(c)) <= max(abs_tol, rel * abs(c))
               for c in candidates)


def _tol() -> tuple[float, float]:
    t = thresholds()["verifier"]
    return t["rel_tolerance"], t["abs_tolerance"]


def check_claim(where: str, text: str, fact_id: str, facts: dict[str, dict]) -> list[str]:
    rel, abs_tol = _tol()
    if not fact_id:
        return [f"{where}: '{text}' has no fact_id"]
    fact = facts.get(fact_id)
    if fact is None:
        return [f"{where}: cites unknown fact_id {fact_id}"]
    allowed = fact_numbers(fact)
    return [f"{where}: number {n:g} in '{text}' does not match fact {fact_id}"
            for n in numbers_in(text) if not matches(n, allowed, rel, abs_tol)]


def check_texts(texts: dict[str, str], facts: dict[str, dict]) -> list[str]:
    """Free text (explanations): every number must match a number in some fact."""
    rel, abs_tol = _tol()
    pool = [n for f in facts.values() for n in fact_numbers(f)]
    return [f"{name}: number {n:g} is not in any fact" for name, text in texts.items()
            for n in numbers_in(text) if not matches(n, pool, rel, abs_tol)]


def verify(output, facts: dict[str, dict], option_names: list[str], must_cite: dict[str, list[str]] | None = None,
           risk_field: str = "failure_risk", check_why: bool = False, check_explanations: bool = False) -> list[str]:
    """must_cite: option name -> fact_ids that option's drivers must cite (calibration, MSP-only price, ...)."""
    issues = []
    got = [o.name for o in output.options]
    if sorted(got) != sorted(option_names):
        issues.append(f"options must be exactly {option_names}, got {got}")
    if output.recommended not in got:
        issues.append(f"recommended '{output.recommended}' is not one of the options")
    for o in output.options:
        risk = getattr(o, risk_field, None)
        if risk is None or not 0 <= risk <= 100:
            issues.append(f"{o.name}: {risk_field} must be 0-100, got {risk}")
        if not o.drivers:
            issues.append(f"{o.name}: no drivers")
        for d in o.drivers:
            issues += check_claim(o.name, d.text, d.fact_id, facts)
        cited = {d.fact_id for d in o.drivers}
        for fid in (must_cite or {}).get(o.name, []):
            if fid not in cited:
                issues.append(f"{o.name}: drivers must cite {fid}")
    if check_why:
        why = getattr(output, "why_recommended", [])
        if not 1 <= len(why) <= 3:
            issues.append(f"why_recommended needs 1-3 drivers, got {len(why)}")
        for d in why:
            issues += check_claim("why_recommended", d.text, d.fact_id, facts)
        others = [n for n in got if n != output.recommended]
        explained = {w.option for w in getattr(output, "why_not_others", [])}
        for n in others:
            if n not in explained:
                issues.append(f"why_not_others: missing a line for {n}")
        for w in getattr(output, "why_not_others", []):
            if w.option not in others:
                issues.append(f"why_not_others: '{w.option}' is not a non-recommended option")
            issues += check_claim(f"why_not_others[{w.option}]", w.text, w.fact_id, facts)
    if check_explanations:
        issues += check_texts({"explanation_mr": output.explanation_mr, "explanation_hi": output.explanation_hi}, facts)
    return issues
