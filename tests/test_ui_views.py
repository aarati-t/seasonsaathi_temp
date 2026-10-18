"""P5 read-only UI views (unit): stored rows only, demo households first, no embeddings sent to the browser."""
import asyncio
from pathlib import Path

import pytest

from app import views
from orchestrators import selector
from tests.fakes import ScriptedEngine, cal_facts, demo_repo, good_output

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def repo_with_runs():
    repo = demo_repo()
    repo.tables["households"].append({**repo.tables["households"][0], "hh_id": "HH-DEMO-X"})
    out = good_output(cal_facts(), signal="ACT", water="fit")
    asyncio.run(selector.run("f3", "T-3", engine="adk", repo=repo, engines={"adk": ScriptedEngine(outputs=[out, out])}))
    repo.tables["alerts"] = [{"alert_id": "AL-1", "hh_id": "T-1", "plot_id": "P-T-1", "stress_level": "HIGH",
                              "next_step": "ALERT_AGENT", "created_at": "2026-10-01", "via": "local"}]
    return repo


def test_households_demo_first_with_latest_signal_and_alert():
    rows = views.households(repo_with_runs())
    assert rows[0]["hh_id"] == "HH-DEMO-X" and rows[0]["is_demo"]
    by = {r["hh_id"]: r for r in rows}
    assert by["T-3"]["latest_f3"]["recommended"] and by["T-3"]["latest_f3"]["signal"] in ("ACT", "WAIT", "AVOID")
    assert by["T-1"]["latest_alert"]["stress_level"] == "HIGH" and by["T-2"]["latest_alert"] is None
    assert by["T-1"]["plot_ids"] == ["P-T-1"] and isinstance(by["T-1"]["completeness_pct"], int)
    assert all("profile_embedding" not in r for r in rows)


def test_household_detail_hides_embedding_and_404s():
    d = views.household(demo_repo(), "T-1")
    assert "profile_embedding" not in d["household"] and d["village"]["village_id"] == 1 and d["plots"]
    with pytest.raises(KeyError):
        views.household(demo_repo(), "NOPE")


def test_plot_series_returns_stored_rows_with_source():
    s = views.plot_series(demo_repo(), "P-T-1")
    assert len(s["rain"]) == 10 and s["rain"][0]["source"] == "CHIRPS" and s["ndvi"] == []
    with pytest.raises(KeyError):
        views.plot_series(demo_repo(), "P-NOPE")


def test_ui_config_and_support_come_from_config_files():
    cfg = views.ui_config()
    assert sum(cfg["profile"]["completeness_weights"].values()) == 100
    assert cfg["calibration"]["high_min_hit_rate"] == 0.70 and "loss" in cfg["outcome_results"]
    assert {"fodder_camp", "mgnrega", "silage"} <= set(views.support())


def test_ui_uses_relative_api_paths_only():
    js = (STATIC / "app.js").read_text()
    assert "http://" not in js.replace("http://www.w3.org", "") and "localhost" not in js
    assert "fetch(path" in js


@pytest.mark.parametrize("lang", ["en", "mr", "hi"])
def test_i18n_files_have_the_same_keys(lang):
    import json

    def keys(d, p=""):
        return {p + k for k, v in d.items() for _ in [0]} | {x for k, v in d.items() if isinstance(v, dict) for x in keys(v, p + k + ".")}
    en = keys(json.loads((STATIC / "i18n" / "en.json").read_text()))
    assert keys(json.loads((STATIC / "i18n" / f"{lang}.json").read_text())) == en


def test_adk_fail_flag_falls_back_to_plain_with_reason():
    out = good_output(cal_facts(), signal="ACT", water="fit")
    adk, plain = ScriptedEngine(outputs=[out, out]), ScriptedEngine(name="plain", outputs=[out, out])
    r = asyncio.run(selector.run("f3", "T-1", inject="adk_fail", engine="adk", repo=demo_repo(),
                                 engines={"adk": adk, "plain": plain}))
    assert r.orchestrator_used == "plain" and r.verifier_status == "PASS"
    assert "forced failure" in r.fallback_reason and not any(t.step == "inject" for t in r.trace) and not adk.runs


def test_ui_shows_force_fail_only_when_api_accepts_it():
    from app.main import app
    enum = app.openapi()["components"]["schemas"]["F3Request"]["properties"]["inject"]
    assert "adk_fail" in str(enum)


def test_demo_flag_is_never_answered_from_cache(monkeypatch):
    from core import settings
    repo = demo_repo()
    out = good_output(cal_facts(), signal="ACT", water="fit")
    asyncio.run(selector.run("f3", "T-1", engine="adk", repo=repo, engines={"adk": ScriptedEngine(outputs=[out, out])}))
    monkeypatch.setattr(settings, "llm_offline", lambda: True)
    plain = asyncio.run(selector.run("f3", "T-1", repo=repo))
    assert plain.orchestrator_used == "cache"
    again = asyncio.run(selector.run("f3", "T-1", repo=repo))          # cache replies are not cached again
    assert again.orchestrator_used == "cache" and sum(t.step == "usage" for t in again.trace) == 2
    flagged = asyncio.run(selector.run("f3", "T-1", inject="bad_fact_id", repo=repo))
    assert flagged.orchestrator_used == "rule"
    assert any(n.startswith("Demo flag inject=bad_fact_id not applied") for n in flagged.notes)


def test_saved_household_gets_created_at_loaded_rows_do_not():
    from features import f1_interview
    repo = demo_repo()
    profile = {"tenure": "tenant", "acres": 2, "irrigation": "rainfed", "water_hours_per_week": 0, "cash_buffer_band": "low",
               "crops_last_season": "soybean", "cattle_count": 1, "goats_count": 0, "fodder_weeks_left": 2}
    res = f1_interview.save_household(profile, 1, True, repo, embed=lambda texts: [[0.1, 0.2]])
    rows = {r["hh_id"]: r for r in views.households(repo)}
    assert rows[res["hh_id"]]["created_at"] and rows["T-1"]["created_at"] is None
    assert views.household(repo, res["hh_id"])["household"]["created_at"].startswith("20")
