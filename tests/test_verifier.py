"""Code verifier rejects missing/invalid fact_ids and unsupported numbers."""
from core.schemas import Driver
from core.verifier import numbers_in, verify
from tests.fakes import good_output

NAMES = ["Chana only", "Chana + safflower", "Jowar + fodder"]
FACTS = {
    "F-hh-irrigation": {"fact_id": "F-hh-irrigation", "text": "Irrigation source: rainfed", "value": "rainfed"},
    "F-rain-deficit": {"fact_id": "F-rain-deficit", "text": "Season rain is 41.9% below normal (to 2026-09-30)", "value": 41.9},
    "F-cal-rainfed-cap": {"fact_id": "F-cal-rainfed-cap", "text": "capped at MED", "value": "MED"},
    **{f"F-wb-coverage-{k}": {"fact_id": f"F-wb-coverage-{k}", "text": "coverage", "value": None}
       for k in ("gram", "gram-safflower", "sorghum")},
}


def test_valid_output_passes():
    assert verify(good_output("F-cal-rainfed-cap"), FACTS, NAMES) == []


def test_rejects_unknown_fact_id():
    out = good_output("F-cal-rainfed-cap")
    out.options[0].drivers[0].fact_id = "F-made-up-1"
    assert any("unknown fact_id F-made-up-1" in i for i in verify(out, FACTS, NAMES))


def test_rejects_missing_fact_id():
    out = good_output("F-cal-rainfed-cap")
    out.options[1].drivers[0].fact_id = ""
    assert any("has no fact_id" in i for i in verify(out, FACTS, NAMES))


def test_rejects_number_not_in_fact():
    out = good_output("F-cal-rainfed-cap")
    out.options[0].drivers.append(Driver(text="Rain is 60% below normal", fact_id="F-rain-deficit"))
    assert any("number 60" in i for i in verify(out, FACTS, NAMES))


def test_accepts_rounded_number():
    out = good_output("F-cal-rainfed-cap")
    out.options[0].drivers.append(Driver(text="Rain is about 42% below normal", fact_id="F-rain-deficit"))
    assert verify(out, FACTS, NAMES) == []


def test_requires_must_cite_and_exact_options():
    out = good_output("F-hh-irrigation", names=("Chana only", "Something else", "Jowar + fodder"))
    issues = verify(out, FACTS, NAMES, must_cite={n: ["F-cal-rainfed-cap"] for n in NAMES})
    assert any("options must be exactly" in i for i in issues)
    assert any("must cite F-cal-rainfed-cap" in i for i in issues)


def test_number_extraction_handles_dates_and_rupees():
    assert numbers_in("₹7,414/qtl on 2026-10-15, +24.4%") == [7414.0, 2026.0, 10.0, 15.0, 24.4]
