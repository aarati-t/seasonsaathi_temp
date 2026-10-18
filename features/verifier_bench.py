"""P5b K4: verifier discrimination benchmark on a frozen real F3 output (tests/fixtures/verifier_bench_f3.json).

Cases are built BY CONSTRUCTION, so the expected label never comes from the verifier being tested:
  changed_number (5)   a fact's own sentence with one number changed          -> invalid
  missing_fact_id (5)  a fact's own sentence with no fact_id                  -> invalid
  invented_fact_id (5) a fact's own sentence citing a fact_id that does not exist -> invalid
  unsupported_claim (5) a genuine fact_id attached to a conclusion the fact contradicts (no numbers) -> invalid
  valid_control (10)   a fact's own sentence, verbatim, citing that fact      -> valid
Each case appends ONE driver to the first option of the frozen assessment and runs the production checks:
the code verifier (core.verifier.verify, deterministic) and the verifier agent (Gemini, semantic, same prompt).
An exception or unavailable model is outcome 'error': never counted as a catch.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core import llm, settings
from core.schemas import AssessorOutput, Driver, VerifierVerdict
from core.verifier import verify
from orchestrators.base import FactStore, load_prompt

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "verifier_bench_f3.json"

CHANGED_NUMBERS = [   # (fact_id, sentence with one number changed vs the fact)
    ("F-rain-deficit", "Season rain is 61.9% below normal."),
    ("F-rain-dryspell", "The longest dry spell was 44 days."),
    ("F-rain-total", "Rain so far this season is 534.8 mm."),
    ("F-hh-acres", "The household farms 7 acres."),
    ("F-hh-cattle_count", "The household has 5 cattle."),
]
UNSUPPORTED = [       # genuine fact_id, conclusion the fact contradicts, no numbers (code checks cannot see it)
    ("F-rain-deficit", "Rain has been above normal this season, so water is plentiful."),
    ("F-hh-cash_buffer_band", "The household has no savings at all."),
    ("F-hh-irrigation", "The farm has no irrigation and depends only on rain."),
    ("F-peer-lastseason", "Most similar households suffered a crop loss last season."),
    ("F-peer-chana-only", "Similar households who grew Chana only mostly lost their crop."),
]
TEXT_FACTS = ["F-rain-deficit", "F-rain-dryspell", "F-rain-total", "F-hh-acres", "F-hh-cattle_count"]
CONTROL_FACTS = ["F-rain-deficit", "F-rain-dryspell", "F-rain-total", "F-hh-acres", "F-hh-cattle_count",
                 "F-hh-tenure", "F-hh-irrigation", "F-hh-cash_buffer_band", "F-peer-lastseason", "F-peer-chana-only"]


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def build_cases(fixture: dict) -> list[dict]:
    facts = {f["fact_id"]: f for f in fixture["facts"]}
    cases = []
    for i, (fid, text) in enumerate(CHANGED_NUMBERS, 1):
        cases.append({"case_id": f"num-{i}", "case_type": "changed_number", "expected": "invalid", "fact_id": fid, "text": text})
    for i, fid in enumerate(TEXT_FACTS, 1):
        cases.append({"case_id": f"missing-{i}", "case_type": "missing_fact_id", "expected": "invalid", "fact_id": "",
                      "text": facts[fid]["text"]})
    for i, fid in enumerate(TEXT_FACTS, 1):
        cases.append({"case_id": f"invented-{i}", "case_type": "invented_fact_id", "expected": "invalid",
                      "fact_id": f"F-invented-{i}", "text": facts[fid]["text"]})
    for i, (fid, text) in enumerate(UNSUPPORTED, 1):
        cases.append({"case_id": f"unsupported-{i}", "case_type": "unsupported_claim", "expected": "invalid",
                      "fact_id": fid, "text": text})
    for i, fid in enumerate(CONTROL_FACTS, 1):
        cases.append({"case_id": f"control-{i}", "case_type": "valid_control", "expected": "valid", "fact_id": fid,
                      "text": facts[fid]["text"]})
    return cases


def case_assessment(fixture: dict, case: dict) -> AssessorOutput:
    a = AssessorOutput.model_validate(fixture["assessment"])
    a.options[0].drivers.append(Driver(text=case["text"], fact_id=case["fact_id"]))
    return a


def semantic_check(fixture: dict, assessment: AssessorOutput) -> VerifierVerdict:
    """The production verifier agent prompt and schema, via core.llm (disk cache + backoff)."""
    from orchestrators.plain_engine import _json_call
    store = FactStore()
    store.add(fixture["facts"])
    return _json_call("verifier", load_prompt("verifier", FACTS=store.compact(), ASSESSMENT=assessment.model_dump_json()),
                      VerifierVerdict, "f3")


def run_case(fixture: dict, case: dict, semantic: bool = True) -> dict:
    facts = {f["fact_id"]: f for f in fixture["facts"]}
    names = [o["name"] for o in fixture["assessment"]["options"]]
    t0, stats = time.perf_counter(), llm.start_stats()
    detail = {"fact_id": case["fact_id"], "text": case["text"], "deterministic_issues": [], "semantic": None}
    try:
        a = case_assessment(fixture, case)
        detail["deterministic_issues"] = verify(a, facts, names, check_why=True, check_explanations=True)
        if semantic:
            v = semantic_check(fixture, a)
            detail["semantic"] = {"status": v.status, "issues": v.issues}
        flagged = bool(detail["deterministic_issues"]) or (detail["semantic"] or {}).get("status") == "FAIL"
        if semantic is False and case["expected"] == "invalid" and not flagged:
            outcome = "not_run"            # only the semantic check could catch it, and it did not run
        elif case["expected"] == "invalid":
            outcome = "caught" if flagged else "missed"
        else:
            outcome = "false_rejection" if flagged else "accepted"
    except Exception as exc:  # noqa: BLE001 - a crash is an execution error, never a catch
        outcome, detail["error"] = "error", f"{type(exc).__name__}: {str(exc)[:200]}"
    calls = sum(1 for c in stats["calls"] if not c["cached"])
    return {**case, "outcome": outcome, "detail": detail, "latency_ms": round((time.perf_counter() - t0) * 1000),
            "model_calls": calls, "cache_hits": stats["cache_hits"]}


def run_benchmark(repo, semantic: bool = True) -> dict:
    """Executes all cases and appends them to verifier_benchmark. Returns the summary (also used by K4)."""
    fixture = load_fixture()
    run_id = f"VB-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    versions = {"model": llm.gemini_model(), "verifier_agent": settings.verifier_agent() and semantic,
                "fixture_request_id": fixture["source_request_id"]}
    results = [run_case(fixture, c, semantic) for c in build_cases(fixture)]
    now = datetime.now(timezone.utc).isoformat()
    repo.insert("verifier_benchmark", [{
        "benchmark_run_id": run_id, "case_id": r["case_id"], "created_at": now, "case_type": r["case_type"],
        "expected": r["expected"], "outcome": r["outcome"], "detail_json": json.dumps(r["detail"], ensure_ascii=False),
        "latency_ms": r["latency_ms"], "model_calls": r["model_calls"], "version_metadata": json.dumps(versions)}
        for r in results])
    return summarize(run_id, results, versions)


def summarize(run_id: str | None, results: list[dict], versions: dict | None = None) -> dict:
    invalid = [r for r in results if r["expected"] == "invalid"]
    valid = [r for r in results if r["expected"] == "valid"]
    by_type = {}
    for r in invalid:
        t = by_type.setdefault(r["case_type"], {"planned": 0, "caught": 0, "missed": 0, "error": 0, "not_run": 0})
        t["planned"] += 1
        t[r["outcome"]] += 1
    return {"benchmark_run_id": run_id, "versions": versions or {},
            "errors_planned": len(invalid), "errors_caught": sum(r["outcome"] == "caught" for r in invalid),
            "errors_executed": sum(r["outcome"] in ("caught", "missed") for r in invalid),
            "controls_planned": len(valid), "controls_accepted": sum(r["outcome"] == "accepted" for r in valid),
            "controls_executed": sum(r["outcome"] in ("accepted", "false_rejection") for r in valid),
            "by_type": by_type, "misses": [r["case_id"] for r in results if r["outcome"] == "missed"],
            "false_rejections": [r["case_id"] for r in results if r["outcome"] == "false_rejection"],
            "errors": [r["case_id"] for r in results if r["outcome"] == "error"],
            "semantic_checks": any((r["detail"].get("semantic") is not None) for r in results),
            "model_calls": sum(r["model_calls"] for r in results),
            "cases": [{k: r[k] for k in ("case_id", "case_type", "expected", "outcome", "latency_ms")} |
                      {"text": r["detail"]["text"], "fact_id": r["detail"]["fact_id"],
                       "deterministic": bool(r["detail"]["deterministic_issues"]),
                       "semantic": (r["detail"].get("semantic") or {}).get("status")} for r in results]}


def latest_from_repo(repo) -> dict | None:
    """Stored benchmark (cached replay reads this; it never re-runs Gemini)."""
    rows = repo.fetch_all("verifier_benchmark")
    if not rows:
        return None
    run_id = max(rows, key=lambda r: str(r["created_at"]))["benchmark_run_id"]
    mine = [r for r in rows if r["benchmark_run_id"] == run_id]
    results = []
    for r in sorted(mine, key=lambda r: r["case_id"]):
        detail = json.loads(r["detail_json"] or "{}")
        results.append({"case_id": r["case_id"], "case_type": r["case_type"], "expected": r["expected"],
                        "outcome": r["outcome"], "detail": detail, "latency_ms": r["latency_ms"],
                        "model_calls": r["model_calls"] or 0})
    return summarize(run_id, results, json.loads(mine[0]["version_metadata"] or "{}"))
