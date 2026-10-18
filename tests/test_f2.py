"""F2 (unit, no LLM): dry spells, stress levels, LOW/dedupe paths, drone rule, alert checks, Pub/Sub, scan."""
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.schemas import F2Alert, F2Plan
from features import f2_watch as f2
from orchestrators import selector
from services import alerts
from tests.fakes import ScriptedEngine, demo_repo
from tools.rain import dry_spells
from tools.stress import get_plot_stress_facts, stress_level


def test_dry_spell_calculation():
    assert dry_spells([0, 1, 2.4, 3, 0, 0, None, 0], 2.5) == (3, 1)    # missing day breaks a run
    assert dry_spells([5, 5], 2.5) == (0, 0)


def facts(deficit=None, spell=None, ndvi=None):
    return [{"fact_id": "F-stress-deficit", "value": deficit}, {"fact_id": "F-stress-dryspell", "value": spell},
            {"fact_id": "F-stress-ndvi", "value": ndvi}]


@pytest.mark.parametrize("vals,level", [((26, 22, 79), "HIGH"), ((26, 22, 90), "HIGH"), ((26, 10, 90), "MED"),
                                        ((10, 10, 79), "MED"), ((25, 21, 80), "LOW"), ((None, None, None), "LOW")])
def test_stress_level_rules(vals, level):
    assert stress_level(facts(*vals))[0] == level


def repo_with(level_fixture="HIGH"):
    repo = demo_repo()   # rain fixture: 87.5% deficit, 10 dry days (1 mm/day)
    repo.tables["ndvi_series"] = [{"plot_id": "P-T-1", "date": "2026-10-01", "ndvi": 0.3, "ndvi_normal": 0.6,
                                   "source": "Sentinel-2"}]
    if level_fixture == "LOW":
        for r in repo.tables["rain_series"]:
            r.update(rain_mm=8.0, cum_deficit_pct=0.0)
        repo.tables["ndvi_series"][0]["ndvi"] = 0.6
    return repo


def good_alert(as_of="2026-06-10"):
    return F2Alert(text_mr=f"पाऊस 87.5% कमी.\nपावसाची माहिती {as_of} पर्यंत", text_hi=f"बारिश 87.5% कम.\nबारिश का डेटा {as_of} तक",
                   fact_ids=["F-stress-deficit", "F-stress-asof"])


def run(plot="P-T-1", repo=None, plan="ALERT_AGENT", alert=None, inject=None):
    repo = repo or repo_with()
    eng = ScriptedEngine(agent_outputs={"plan": F2Plan(next_step=plan, reason="r", fact_ids=["F-stress-deficit"]),
                                        "alert": alert or good_alert()})
    r = asyncio.run(selector.execute(f2, f2.build_context(plot, repo), "f2", "T-1", engine="adk", repo=repo,
                                     engines={"adk": eng}, inject=inject, use_result_cache=False))
    return r, repo, eng


def test_high_plot_alert_is_checked_and_stored():
    r, repo, eng = run()
    assert r.stress_level == "HIGH" and r.verifier_status == "PASS" and r.next_step == "ALERT_AGENT"
    stored = repo.tables["alerts"]
    assert len(stored) == 1 and stored[0]["via"] == "local" and json.loads(stored[0]["fact_ids"]) == ["F-stress-deficit", "F-stress-asof"]


def test_dedupe_within_7_days_makes_no_llm_call():
    _, repo, _ = run()
    r2, repo, eng = run(repo=repo)
    assert r2.deduped and eng.agent_runs == [] and len(repo.tables["alerts"]) == 1
    old = datetime.now(timezone.utc) - timedelta(days=8)
    repo.tables["alerts"][0]["created_at"] = old.isoformat()
    r3, repo, _ = run(repo=repo)
    assert not r3.deduped and len(repo.tables["alerts"]) == 2


def test_low_stress_no_llm_no_alert():
    r, repo, eng = run(repo=repo_with("LOW"))
    assert r.stress_level == "LOW" and r.next_step == "WAIT_NEXT_PASS" and eng.agent_runs == [] and not repo.tables.get("alerts")


def test_drone_only_when_high():
    repo = repo_with()
    repo.tables["ndvi_series"][0]["ndvi"] = 0.6                         # NDVI fine -> only deficit crossed... and dry spell 10 < 21
    r, _, _ = run(repo=repo, plan="REQUEST_DRONE")
    assert r.stress_level == "MED" and r.next_step == "REQUEST_PHOTOS" and "only allowed when stress is HIGH" in r.notes[0]


def test_alert_with_bad_number_or_no_date_gets_regenerated_then_template():
    bad = F2Alert(text_mr="पाऊस 60% कमी", text_hi="बारिश 60% कम", fact_ids=["F-stress-deficit"])   # 60 is in no fact
    issues = f2.alert_issues(bad, get_plot_stress_facts("P-T-1", repo_with()), "2026-06-10")
    assert any("number 60" in i for i in issues) and any("must state the rain data date" in i for i in issues)
    r, _, _ = run(alert=bad)
    assert r.verifier_status == "FAIL" and "template alert" in r.notes[-1]
    assert "2026-06-10" in r.alert["text_mr"]


def test_pubsub_envelope_decoding_and_duplicate_delivery():
    repo = demo_repo()
    env = alerts.PubSubAlerts(project="p").envelope({"alert_id": "AL-1", "plot_id": "P-T-1", "stress_level": "HIGH",
                                                      "fact_ids": ["F-stress-deficit"]})
    assert alerts.decode_push(env)["alert_id"] == "AL-1"
    assert alerts.receive_push(env, repo) == {"stored": True, "alert_id": "AL-1", "message_id": "AL-1"}
    again = alerts.receive_push(env, repo)                                         # Pub/Sub redelivery
    assert again["stored"] is False and again["reason"] == "duplicate message_id"
    assert repo.tables["alerts"][0]["via"] == "pubsub"
    with pytest.raises(ValueError, match="not a Pub/Sub push envelope"):
        alerts.decode_push({"foo": 1})
    with pytest.raises(ValueError, match="base64-encoded JSON"):
        alerts.decode_push({"message": {"data": base64.b64encode(b"not json").decode()}})


def test_scan_skips_households_without_plots():
    repo = repo_with()
    repo.tables["plots"] = [p for p in repo.tables["plots"] if p["hh_id"] == "T-1"]
    eng = ScriptedEngine(agent_outputs={"plan": F2Plan(next_step="ALERT_AGENT", reason="r", fact_ids=[]),
                                        "alert": good_alert()})
    out = asyncio.run(f2.scan(repo, engine="adk", engines={"adk": eng}))
    assert out["summary"]["plots"] == 1 and out["summary"]["alerts_created"] == 1
    assert {s["hh_id"] for s in out["summary"]["skipped"]} == {"T-2", "T-3"}
    assert all(s["reason"] == "no plot location" for s in out["summary"]["skipped"])



def test_pubsub_publish_sends_to_topic_and_retries_are_deduped_on_message_id():
    class FakeFuture:
        def result(self, timeout=None):
            return "msg-123"

    class FakePublisher:
        sent = []

        def topic_path(self, project, topic):
            return f"projects/{project}/topics/{topic}"

        def publish(self, path, data, **attrs):
            self.sent.append((path, json.loads(data), attrs))
            return FakeFuture()

    pub = FakePublisher()
    sink = alerts.PubSubAlerts(topic="stress-alerts", project="p", publisher=pub)
    out = sink.publish({"alert_id": "AL-9", "plot_id": "P-T-1", "stress_level": "HIGH", "fact_ids": ["F-stress-deficit"]})
    assert out["message_id"] == "msg-123" and pub.sent[0][0] == "projects/p/topics/stress-alerts"
    assert pub.sent[0][1]["alert_id"] == "AL-9" and pub.sent[0][2]["plot_id"] == "P-T-1"
    # what the push subscription delivers: same data, Pub/Sub's messageId
    data = base64.b64encode(json.dumps(pub.sent[0][1]).encode()).decode()
    repo = demo_repo()
    for _ in range(2):   # retry
        alerts.receive_push({"message": {"data": data, "messageId": "msg-123"}}, repo)
    assert len(repo.tables["alerts"]) == 1 and repo.tables["alerts"][0]["message_id"] == "msg-123"
