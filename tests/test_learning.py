"""Learning loop (unit): term approval, real outcomes, feedback style examples, peer outcome facts."""
import asyncio
import json

import pytest

from core.schemas import ExtractedField, ExtractionOutput, QuestionOutput
from features import f1_interview as f1
from features import learning
from orchestrators import selector
from tests.fakes import ScriptedEngine, cal_facts, demo_repo, good_output
from tools.peers import find_similar_households


# ---------- vocabulary ----------

def f1_turn(repo, text="पानी कुएँ से आता है"):
    eng = ScriptedEngine(agent_outputs={
        "extraction": ExtractionOutput(fields=[ExtractedField(field="irrigation", value="well", evidence="कुएँ")],
                                       local_terms=[{"term": "कुएँ", "meaning": "well"}]),
        "question": QuestionOutput(question="?")})
    ctx = f1.build_context([{"role": "user", "text": text}], "hi", repo)
    return asyncio.run(selector.execute(f1, ctx, "f1", None, engine="adk", repo=repo, engines={"adk": eng},
                                        use_result_cache=False))


def test_unknown_term_is_queued_then_used_after_approval():
    repo = demo_repo()
    r1 = f1_turn(repo)
    terms = next(t for t in r1.trace if t.step == "terms").detail
    assert [t["term"] for t in terms["gemini_normalised_terms"]] == ["कुएँ"] and terms["queued_for_review"] == ["कुएँ"]
    assert [t["term"] for t in learning.terms_review(repo)["pending"]] == ["कुएँ"]

    f1_turn(repo)                                                     # already queued: not inserted twice
    assert len(repo.tables["term_review"]) == 1

    learning.approve_term(repo, "कुएँ", "well", "irrigation")
    r2 = f1_turn(repo)
    terms = next(t for t in r2.trace if t.step == "terms").detail
    assert {"term": "कुएँ", "category": "irrigation", "canonical": "well"} in terms["terms_yaml"]
    assert terms["gemini_normalised_terms"] == []                     # no longer needs Gemini's reading
    assert learning.terms_review(repo)["approved"][0]["canonical"] == "well"


def test_term_approval_validates_category():
    with pytest.raises(ValueError):
        learning.approve_term(demo_repo(), "x", "well", "not-a-category")


# ---------- outcomes + feedback ----------

def run_f3(repo):
    return asyncio.run(selector.run("f3", "T-2", engine="adk", repo=repo,
                                    engines={"adk": ScriptedEngine(outputs=[good_output(cal_facts(), signal="WAIT")])}))


def test_real_outcome_is_one_insert_with_context():
    repo = demo_repo()
    r = run_f3(repo)
    before = len(repo.tables["outcomes"])
    row = learning.record_outcome(repo, "T-2", r.request_id, "rabi-2026-27", "loss", note="field visit")
    assert len(repo.tables["outcomes"]) == before + 1
    assert row["is_synthetic"] is False and row["decision"] == r.recommended and row["rain_deficit_bucket"] == ">30"
    from features.f3_cropmix import CROP_KEYS
    assert row["crop_key"] == CROP_KEYS[r.recommended] and row["predicted_signal"] == "WAIT"
    with pytest.raises(ValueError, match="is for T-2"):
        learning.record_outcome(repo, "T-1", r.request_id, "rabi-2026-27", "loss")


def test_up_rated_explanations_become_style_examples_only():
    repo = demo_repo()
    r = run_f3(repo)
    learning.record_feedback(repo, r.request_id, "up")
    learning.record_feedback(repo, r.request_id, "down", note="too long")
    block, n = learning.style_block(repo, "f3", r.context_bucket)
    assert n == 1 and "STYLE EXAMPLES" in block and "never use their numbers" in block
    assert learning.style_block(repo, "f3", "0-15|well")[1] == 0          # other context: none
    assert learning.feedback_review(repo)[0]["note"] == "too long"
    r2 = run_f3(repo)
    assert r2.explain.evidence["rated_examples"] == 1
    assert "1 rated examples" in next(t for t in r2.trace if t.step == "evidence").title


def test_peer_outcomes_are_facts_and_count_real():
    repo = demo_repo()
    row = {"outcome_id": "REAL-1", "hh_id": "T-2", "season": "rabi-2026-27", "decision": "Chana only",
           "predicted_signal": "WAIT", "predicted_confidence": "MED", "actual_result": "loss", "cattle_sold": 1,
           "distress_sale": True, "is_synthetic": False}
    repo.tables["outcomes"].append(row)                       # older row, no provenance: never "observed" (P5b D)
    fact = {f["fact_id"]: f for f in find_similar_households("T-1", repo)}["F-peer-outcome-1"]
    assert "(legacy_unverified outcome)" in fact["text"] and fact["is_synthetic"]
    repo.tables["outcomes"][-1] = {**row, "provenance": "observed"}
    facts = {f["fact_id"]: f for f in find_similar_households("T-1", repo)}
    assert "(observed outcome)" in facts["F-peer-outcome-1"]["text"] and not facts["F-peer-outcome-1"]["is_synthetic"]
    assert "distress" in facts["F-peer-outcome-1"]["text"]
    assert facts["F-peer-lastseason"]["text"] == "1 of 1 similar households had a loss in rabi-2026-27"


def test_season_order_uses_the_year():
    from tools.peers import season_key
    seasons = ["rabi-2025-26", "kharif-2026", "rabi-2023-24"]
    assert max(seasons, key=season_key) == "kharif-2026"


def test_learning_demo_outcomes_are_never_observed():
    from features.learning import record_outcome
    from tools.calibration import outcome_provenance
    repo = demo_repo()
    repo.tables["decision_log"] = [{"request_id": "R1", "hh_id": "T-1", "feature": "f3", "verifier_status": "PASS",
                                    "output_json": {"recommended": "Chana only", "options": [], "context_bucket": ">30|rainfed"}}]
    demo = record_outcome(repo, "T-1", "R1", "learning-demo-r1-1", "loss", demo=True)
    real = record_outcome(repo, "T-1", "R1", "rabi-2026-27", "good")
    assert demo["provenance"] == "demo" and real["provenance"] == "observed"
    assert outcome_provenance({"is_synthetic": False, "season": "learning-demo-1"}) == "demo"
    assert outcome_provenance({"is_synthetic": False, "season": "rabi-2025-26"}) == "legacy_unverified"
