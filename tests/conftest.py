"""Markers: every test is 'unit' unless marked 'llm'. llm tests call Gemini on Vertex AI, so they are
skipped unless selected explicitly (pytest -m llm) or RUN_LIVE=1 is set."""
import os

import pytest

# Tests drive scripted fake engines (unit) or real Gemini (llm); a dev LLM_OFFLINE=true in .env must not
# leak in. Set before any module calls load_dotenv(), which never overrides an existing variable.
os.environ["LLM_OFFLINE"] = "false"


def pytest_collection_modifyitems(config, items):
    selected_llm = "llm" in (config.getoption("markexpr") or "")
    run_llm = selected_llm or os.getenv("RUN_LIVE") == "1"
    skip = pytest.mark.skip(reason="calls Vertex AI; run with: pytest -m llm")
    for item in items:
        if "llm" in item.keywords:
            if not run_llm:
                item.add_marker(skip)
        else:
            item.add_marker(pytest.mark.unit)
