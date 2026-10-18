"""Safety floor: water=short AND cash=low -> never ACT. Confidence caps only lower."""
from core.safety_floor import apply_confidence_cap, apply_safety_floor, lower_one
from tests.fakes import good_output


def test_blocks_act_when_water_short_and_cash_low():
    out = good_output("F-x", signal="ACT", water="short")
    notes = apply_safety_floor(out, "low")
    assert notes and all(o.signal != "ACT" for o in out.options)


def test_no_change_when_cash_not_low():
    out = good_output("F-x", signal="ACT", water="short")
    assert apply_safety_floor(out, "med") == [] and all(o.signal == "ACT" for o in out.options)


def test_no_change_when_water_not_short():
    out = good_output("F-x", signal="ACT", water="tight")
    assert apply_safety_floor(out, "low") == []


def test_recommended_moves_to_an_act_option():
    out = good_output("F-x", signal="ACT", water="fit")
    out.options[0].water_fit = "short"          # only the recommended option is blocked
    apply_safety_floor(out, "low")
    assert out.recommended != "Chana only" and out.options[0].signal == "WAIT"


def test_confidence_cap_and_lower_one():
    out = good_output("F-x")
    notes = apply_confidence_cap(out, "MED", "calibration")
    assert len(notes) == 3 and all(o.confidence == "MED" for o in out.options)
    assert lower_one("HIGH") == "MED" and lower_one("LOW") == "LOW"


def test_unknown_cash_counts_as_low():
    out = good_output("F-x", signal="ACT", water="short")
    notes = apply_safety_floor(out, None)
    assert notes and "cash buffer unknown" in notes[0] and all(o.signal != "ACT" for o in out.options)


def test_code_water_budget_short_blocks_act_for_any_cash_band():
    out = good_output("F-cal-x", signal="ACT", water="short")
    notes = apply_safety_floor(out, "med", budget_short={"Chana only": "covers 43-50% of need"})
    assert out.options[0].signal == "WAIT" and "water budget short: covers 43-50% of need" in notes[0]
    assert out.options[1].signal == "ACT"          # medium cash, no code budget for it: old rule does not apply
