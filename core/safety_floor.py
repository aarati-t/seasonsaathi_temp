"""Code rules applied after the assessor (SPEC F3 step 3, F1, F7). Gemini judges; these only limit.

- Safety floor: water_fit=short AND cash buffer low or unknown -> signal cannot be ACT (set to WAIT).
  P7/P5b: an option whose CODE water budget is short (whole coverage range below the tight band) cannot be ACT
  for any cash band: the budget is computed, not judged.
- Confidence cap: calibration cap (F7) or incomplete profile (F1) -> lower confidence to the cap.
- Downgrade: after a final verifier FAIL every option goes to LOW.
"""
from core.schemas import CONFIDENCE_ORDER, AssessorOutput

SIGNAL_RANK = {"ACT": 2, "WAIT": 1, "AVOID": 0}


def lower_one(level: str) -> str:
    return CONFIDENCE_ORDER[max(0, CONFIDENCE_ORDER.index(level) - 1)]


def apply_safety_floor(output: AssessorOutput, cash_buffer_band: str | None,
                       budget_short: dict[str, str] | None = None) -> list[str]:
    """Mutates output. Returns notes (empty = rule not applied).
    budget_short: option name -> coverage text, for options whose code water budget is short."""
    notes = []
    budget_short = budget_short or {}
    low_cash = cash_buffer_band in ("low", None)  # unknown cash is treated as low
    cash = "low" if cash_buffer_band == "low" else "unknown"
    for o in output.options:
        if o.signal != "ACT":
            continue
        if o.name in budget_short:
            o.signal = "WAIT"
            notes.append(f"Safety rule applied: {o.name} changed ACT -> WAIT (water budget short: {budget_short[o.name]})")
        elif o.water_fit == "short" and low_cash:
            o.signal = "WAIT"
            notes.append(f"Safety rule applied: {o.name} changed ACT -> WAIT (water short, cash buffer {cash})")
    if notes:
        _fix_recommended(output, notes)
    return notes


def apply_confidence_cap(output: AssessorOutput, cap: str, reason: str) -> list[str]:
    notes = []
    for o in output.options:
        if CONFIDENCE_ORDER.index(o.confidence) > CONFIDENCE_ORDER.index(cap):
            notes.append(f"Confidence lowered: {o.name} {o.confidence} -> {cap} ({reason})")
            o.confidence = cap
    return notes


def lower_all_one_level(output: AssessorOutput, reason: str) -> list[str]:
    notes = []
    for o in output.options:
        new = lower_one(o.confidence)
        if new != o.confidence:
            notes.append(f"Confidence lowered: {o.name} {o.confidence} -> {new} ({reason})")
            o.confidence = new
    return notes


def downgrade_all(output: AssessorOutput, reason: str) -> list[str]:
    for o in output.options:
        o.confidence = "LOW"
    return [f"Confidence lowered: all options set to LOW ({reason})"]


def _fix_recommended(output: AssessorOutput, notes: list[str]) -> None:
    by_name = {o.name: o for o in output.options}
    current = by_name.get(output.recommended)
    if current is None or current.signal == "ACT":
        return
    best = max(output.options, key=lambda o: (SIGNAL_RANK[o.signal], -(o.failure_risk or 100)))
    if best.name != output.recommended and SIGNAL_RANK[best.signal] > SIGNAL_RANK[current.signal]:
        notes.append(f"Recommended changed {output.recommended} -> {best.name} after the safety rule")
        output.recommended = best.name
