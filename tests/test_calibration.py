"""F7 (unit): rain buckets, calibration facts from calibration_v, downgrade rule and the n < 5 rule."""
import pytest

from core.schemas import Driver, Option
from tests.fakes import demo_repo, good_output
from tools import calibration


@pytest.mark.parametrize("deficit,bucket", [(-5, "0-15"), (0, "0-15"), (14.9, "0-15"), (15, "15-30"), (30, "15-30"),
                                            (30.1, ">30"), (41.9, ">30"), (None, "unknown")])
def test_rain_buckets(deficit, bucket):
    assert calibration.rain_bucket(deficit) == bucket


def test_calibration_fact_from_view():
    f = calibration.get_calibration(">30|rainfed|gram", demo_repo())[0]
    assert f["fact_id"] == "F-cal-gt30-rainfed-gram"
    assert f["levels"]["HIGH"] == {"hit_rate": 0.33, "n": 6, "n_real": 1, "n_synthetic": 5, "n_demo": 0, "n_unverified": 0}
    assert "HIGH 33% right (n=6, 1 observed)" in f["text"] and f["levels"]["MED"]["n"] == 0


def _apply(conf, ctx):
    repo = demo_repo()
    facts = {f["fact_id"]: f for f in calibration.get_calibration(ctx, repo)}
    out = good_output("x", names=("Chana only",))
    out.options[0].confidence = conf
    notes, details = calibration.apply_calibration_rule(out, {"Chana only": ctx}, facts)
    return out.options[0].confidence, notes, details[0]


def test_high_with_low_hit_rate_and_enough_history_is_downgraded():
    conf, notes, d = _apply("HIGH", ">30|rainfed|gram")              # 0.33, n=6
    assert conf == "MED" and d["result"] == "downgraded to MED" and "calibration downgrade" in notes[0]


def test_insufficient_history_keeps_high_with_note():
    conf, notes, d = _apply("HIGH", ">30|rainfed|gram+safflower")    # 0.25 but n=4 < 5
    assert conf == "HIGH" and "insufficient history (n=4)" in notes[0]


def test_good_hit_rate_keeps_high_and_med_is_untouched():
    assert _apply("HIGH", ">30|rainfed|sorghum+fodder")[0] == "HIGH"   # 0.8
    conf, notes, d = _apply("MED", ">30|rainfed|gram")
    assert conf == "MED" and notes == [] and d["result"] == "not HIGH: rule not applied"


def test_unknown_context_has_no_history():
    conf, notes, _ = _apply("HIGH", "0-15|borewell|gram")
    assert conf == "HIGH" and "insufficient history (n=0)" in notes[0]
