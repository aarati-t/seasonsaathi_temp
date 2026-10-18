"""Env switches that limit Vertex AI usage, cache results and control fallback. Read at call time.

LLM_CACHE=disk|off        cache every Gemini response on disk by request hash (repeat runs cost nothing)
LLM_CACHE_DIR=.cache/llm  where the cache lives (on Cloud Run: /tmp/llm-cache, lost on restart)
LLM_OFFLINE=false|true    never call Gemini: use the last good result, else the rule result (LOW)
GEMINI_THINKING=low|off|default  model thinking; low keeps latency and cost down
LLM_TIMEOUT_S=30          per-call HTTP timeout (plain engine)
LLM_MAX_RETRIES=3         retries on 429/503 with exponential backoff
ORCHESTRATOR=adk|plain    primary engine
ENGINE_FALLBACK=on|off    on ADK error/timeout/bad schema, re-run with the plain engine
ENGINE_TIMEOUT_S=20       ADK run time limit before falling back
VERIFIER_AGENT=on|off     LLM verifier call (the code verifier always runs)
MAX_REGENERATIONS=1       assessor re-runs after a verifier FAIL, then downgrade to LOW
RESULT_CACHE=on|off       if every engine fails, reuse the last PASS result from decision_log
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip().lower()


def on(name: str, default: str) -> bool:
    return _env(name, default) in ("on", "true", "1", "yes")


def num(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def llm_cache_enabled() -> bool:
    return _env("LLM_CACHE", "disk") == "disk"


def llm_cache_dir() -> Path:
    path = Path(os.getenv("LLM_CACHE_DIR", ".cache/llm"))
    return path if path.is_absolute() else ROOT / path


def llm_offline() -> bool:
    return on("LLM_OFFLINE", "false")


def thinking() -> str:
    return _env("GEMINI_THINKING", "low")


def orchestrator() -> str:
    return _env("ORCHESTRATOR", "adk")


def engine_fallback() -> bool:
    return on("ENGINE_FALLBACK", "on")


def engine_timeout_s() -> float:
    return num("ENGINE_TIMEOUT_S", 20)


def llm_timeout_s() -> float:
    return num("LLM_TIMEOUT_S", 30)


def llm_max_retries() -> int:
    return int(num("LLM_MAX_RETRIES", 3))


def verifier_agent() -> bool:
    return on("VERIFIER_AGENT", "on")


def max_regenerations() -> int:
    return int(num("MAX_REGENERATIONS", 1))


def result_cache() -> bool:
    return on("RESULT_CACHE", "on")


def thresholds() -> dict:
    import yaml

    return yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())
