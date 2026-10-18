"""P4 acceptance (llm, calls Vertex): F4 parity on HH-DEMO-LIVESTOCK, F2 alert cites facts, explainability."""
import asyncio

import pytest

from core.verifier import check_texts
from features import f2_watch
from orchestrators import selector
from services import alerts


@pytest.mark.llm
def test_f4_parity_adk_vs_plain(monkeypatch):
    monkeypatch.setenv("ENGINE_FALLBACK", "off")
    monkeypatch.setenv("ENGINE_TIMEOUT_S", "90")
    out = {e: asyncio.run(selector.run("f4", "HH-DEMO-LIVESTOCK", engine=e)) for e in ("adk", "plain")}
    for e, r in out.items():
        assert r.orchestrator_used == e and r.verifier_status == "PASS", (e, r.fallback_reason, r.notes)
    assert out["adk"].recommended == out["plain"].recommended, {e: r.recommended for e, r in out.items()}
    assert out["adk"].recommended != "SELL_NOW"


@pytest.mark.llm
@pytest.mark.parametrize("feature,hh", [("f3", "HH-DEMO-FLOOR"), ("f4", "HH-DEMO-LIVESTOCK")])
def test_explainability_fields_present_and_verified(feature, hh, monkeypatch):
    monkeypatch.setenv("ENGINE_TIMEOUT_S", "90")
    r = asyncio.run(selector.run(feature, hh))
    assert r.verifier_status == "PASS", r.notes
    e = r.explain
    facts = {f.fact_id: f.model_dump() for f in r.facts}
    assert 1 <= len(e.why_recommended) <= 3 and all(d.fact_id in facts for d in e.why_recommended)
    others = {o.name for o in r.options} - {r.recommended}
    assert {w.option for w in e.why_not_others} >= others and all(w.fact_id in facts for w in e.why_not_others)
    assert e.what_would_change and e.what_would_change["text"]
    assert e.confidence_reason and isinstance(e.data_gaps, list)
    assert e.plain_explanation_mr and e.plain_explanation_hi
    assert check_texts({"mr": e.plain_explanation_mr, "hi": e.plain_explanation_hi}, facts) == []
    assert e.evidence["text"].startswith("evidence used: ")


@pytest.mark.llm
def test_f2_alert_cites_fact_ids_and_date(monkeypatch):
    sent = []

    class Capture(alerts.AlertSink):
        via = "local"

        def publish(self, alert):
            sent.append(alert)
            return {**alert, "created_at": "now"}

    monkeypatch.setattr(alerts, "recent_alert", lambda *a, **k: None)      # bypass 7-day dedupe for the test
    monkeypatch.setattr(alerts, "get_sink", lambda repo: Capture())
    ctx = f2_watch.build_context("PLOT-HH-DEMO-OK")
    r = asyncio.run(selector.execute(f2_watch, ctx, "f2", ctx.hh["hh_id"], use_result_cache=False))
    assert r.stress_level in ("MED", "HIGH") and r.verifier_status == "PASS", r.notes
    facts = {f.fact_id for f in r.facts}
    assert sent and sent[0]["fact_ids"] and set(sent[0]["fact_ids"]) <= facts
    as_of = next(f.value for f in r.facts if f.fact_id == "F-stress-asof")
    assert as_of in sent[0]["text_mr"] and as_of in sent[0]["text_hi"]
