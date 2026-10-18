"""SPEC section 3: 3 fixed households give the same signal and a verifier PASS on both engines (live).

"Same signal" = the same recommended option with the same signal. The engines frame the request
differently (ADK system instruction vs plain user turn), so the other options' signals may differ.
Uses the LLM disk cache: the first run costs Gemini calls, re-runs are free.
"""
import asyncio

import pytest

from orchestrators import selector

HOUSEHOLDS = ["SYN-001", "SYN-008", "SYN-018"]  # borewell/med cash, rainfed/low cash, well/high cash


@pytest.mark.llm
@pytest.mark.parametrize("hh_id", HOUSEHOLDS)
def test_engines_agree(hh_id, monkeypatch):
    monkeypatch.setenv("ENGINE_FALLBACK", "off")
    monkeypatch.setenv("ENGINE_TIMEOUT_S", "90")
    out = {e: asyncio.run(selector.run("f3", hh_id, engine=e)) for e in ("adk", "plain")}
    for e, r in out.items():
        assert r.orchestrator_used == e and r.verifier_status == "PASS", (e, r.fallback_reason, r.notes)
    rec = {e: (r.recommended, next(o.signal for o in r.options if o.name == r.recommended)) for e, r in out.items()}
    assert rec["adk"] == rec["plain"], rec
