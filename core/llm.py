"""Shared google-genai access for both engines: client, retry, generation config, disk cache, usage stats.

GENAI_MODE=vertex -> Vertex AI (ADC locally, service account on Cloud Run).
GENAI_MODE=apikey -> AI Studio key (fallback mode).
Model names always come from env (GEMINI_MODEL, EMBED_MODEL). Switches: see core/settings.py.
"""
import contextvars
import hashlib
import json
import logging
import os
import random
import sys
import time
from functools import lru_cache

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from core import settings

load_dotenv()

RETRY_CODES = {429, 503, 504}  # rate limit, unavailable, deadline exceeded

log = logging.getLogger("seasonsaathi.llm")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


def genai_mode() -> str:
    mode = os.getenv("GENAI_MODE", "vertex").strip().lower()
    if mode not in ("vertex", "apikey"):
        raise ValueError(f"GENAI_MODE must be 'vertex' or 'apikey', got {mode!r}")
    return mode


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("<"):
        raise RuntimeError(f"{name} is not set in .env")
    return value


def gemini_model() -> str:
    return require_env("GEMINI_MODEL")


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    # SDK-level retries off: with_backoff below is the single, bounded retry policy.
    http = types.HttpOptions(timeout=int(settings.llm_timeout_s() * 1000),
                             retry_options=types.HttpRetryOptions(attempts=1))
    if genai_mode() == "vertex":
        return genai.Client(
            vertexai=True,
            project=require_env("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            http_options=http,
        )
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GENAI_MODE=apikey needs GOOGLE_API_KEY (or GEMINI_API_KEY) in .env")
    return genai.Client(api_key=api_key, http_options=http)


def client_backend(client: genai.Client) -> dict:
    """What a client will actually talk to (not what .env intends)."""
    vertex = bool(client.vertexai)
    api = client._api_client
    return {"backend": "vertex" if vertex else "apikey",
            "project": getattr(api, "project", None) if vertex else None,
            "location": getattr(api, "location", None) if vertex else None}


@lru_cache(maxsize=1)
def adk_backend() -> dict:
    """ADK's Gemini model builds its own client from env (GOOGLE_GENAI_USE_VERTEXAI, project, location)."""
    from google.adk.models.google_llm import Gemini

    return client_backend(Gemini(model=gemini_model()).api_client)


def plain_backend() -> dict:
    return client_backend(get_client())


def verify_backend(check_adk: bool = True) -> dict:
    """Fail fast if GENAI_MODE and the clients disagree. Prints the resolved backend once."""
    mode, plain = genai_mode(), plain_backend()
    if plain["backend"] != mode:
        raise RuntimeError(f"GENAI_MODE={mode} but the google-genai client is {plain['backend']}")
    if check_adk:
        want = "TRUE" if mode == "vertex" else "FALSE"
        try:
            adk = adk_backend()
        except Exception as exc:  # noqa: BLE001 - e.g. ADK fell back to API-key mode with no key
            raise RuntimeError(f"GENAI_MODE={mode} but ADK cannot build its Gemini client ({exc}); "
                               f"check GOOGLE_GENAI_USE_VERTEXAI={want} in .env") from None
        if adk["backend"] != mode:
            raise RuntimeError(f"GENAI_MODE={mode} but ADK would use {adk['backend']}: "
                               f"set GOOGLE_GENAI_USE_VERTEXAI={want} in .env")
        if mode == "vertex" and (adk["project"], adk["location"]) != (plain["project"], plain["location"]):
            raise RuntimeError(f"ADK project/location {adk['project']}/{adk['location']} differ from "
                               f"{plain['project']}/{plain['location']}: check GOOGLE_CLOUD_PROJECT/LOCATION")
    log.info("genai backend resolved: backend=%s project=%s location=%s model=%s embed_model=%s",
             plain["backend"], plain["project"], plain["location"], gemini_model(), os.getenv("EMBED_MODEL"))
    return plain


def token_counts(usage) -> tuple[int | None, int | None]:
    """(tokens_in, tokens_out). tokens_out includes thinking tokens, which are billed as output."""
    if usage is None:
        return None, None
    t_in = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
    t_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    return t_in, t_out


def record_call(engine: str, role: str, backend: dict, model: str, latency_ms: int, usage, cached: bool) -> None:
    """One log line per model call (cache hits are logged with cached=true and their original tokens)."""
    t_in, t_out = token_counts(usage)
    log.info("llm_call engine=%s role=%s backend=%s project=%s location=%s model=%s latency_ms=%d "
             "tokens_in=%s tokens_out=%s cached=%s", engine, role, backend["backend"], backend["project"],
             backend["location"], model, latency_ms, t_in, t_out, str(cached).lower())
    stats = _stats.get()
    if stats is not None:
        stats["calls"].append({"engine": engine, "role": role, **backend, "model": model, "latency_ms": latency_ms,
                               "tokens_in": t_in, "tokens_out": t_out, "cached": cached})


def with_backoff(fn, *args, retries: int | None = None, base_delay: float = 1.0, **kwargs):
    """Call fn, retrying with exponential backoff + jitter on 429 (RESOURCE_EXHAUSTED) / 503 / 504."""
    retries = settings.llm_max_retries() if retries is None else retries
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except errors.APIError as exc:
            # 429 is RESOURCE_EXHAUSTED (rate limit). 402 also says RESOURCE_EXHAUSTED but means
            # credits are depleted, which waiting does not fix, so match on the code only.
            if exc.code not in RETRY_CODES or attempt == retries:
                raise
            time.sleep(base_delay * 2**attempt + random.uniform(0, 0.5))


def thinking_config(model: str) -> types.ThinkingConfig | None:
    """GEMINI_THINKING=low|off|default. Gemini 2.x uses a token budget, 3.x a level."""
    mode = settings.thinking()
    if mode == "default":
        return None
    if model.startswith("gemini-2"):
        return types.ThinkingConfig(thinking_budget=0 if mode == "off" else 512)
    return types.ThinkingConfig(thinking_level="minimal" if mode == "off" else "low")


def retry_options() -> types.HttpRetryOptions:
    """For ADK's Gemini model, which builds its own client."""
    return types.HttpRetryOptions(attempts=settings.llm_max_retries() + 1, initial_delay=1.0,
                                  http_status_codes=sorted(RETRY_CODES))


# ---------- usage stats per request ----------

_stats: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_stats", default=None)


def start_stats() -> dict:
    stats = {"llm_calls": 0, "cache_hits": 0, "calls": []}
    _stats.set(stats)
    return stats


def count(kind: str) -> None:
    stats = _stats.get()
    if stats is not None:
        stats[kind] += 1


# ---------- disk cache ----------

def cache_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def cache_get(key: str) -> str | None:
    if not settings.llm_cache_enabled():
        return None
    path = settings.llm_cache_dir() / f"{key}.json"
    return path.read_text() if path.exists() else None


def cache_put(key: str, payload: str) -> None:
    if not settings.llm_cache_enabled():
        return
    d = settings.llm_cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{key}.tmp"
    tmp.write_text(payload)
    tmp.replace(d / f"{key}.json")


def embed(texts: list[str], task_type: str = "SEMANTIC_SIMILARITY") -> list[list[float]]:
    """EMBED_MODEL embeddings (Vertex in vertex mode), with backoff and one log line per call."""
    model = require_env("EMBED_MODEL")
    t0 = time.perf_counter()
    resp = with_backoff(get_client().models.embed_content, model=model, contents=texts,
                        config=types.EmbedContentConfig(task_type=task_type))
    count("llm_calls")
    record_call("embed", "embed", plain_backend(), model, round((time.perf_counter() - t0) * 1000), None, cached=False)
    return [list(e.values) for e in resp.embeddings]


def generate(contents: list[types.Content], config: types.GenerateContentConfig, key_parts: tuple,
             model: str | None = None, role: str = "llm", engine: str = "plain") -> types.GenerateContentResponse:
    """generate_content with disk cache + backoff + one log line. key_parts must identify the request fully."""
    model = model or gemini_model()
    key = cache_key(model, key_parts)
    cached = cache_get(key)
    if cached is not None:
        count("cache_hits")
        resp = types.GenerateContentResponse.model_validate_json(cached)
        record_call(engine, role, plain_backend(), model, 0, resp.usage_metadata, cached=True)
        return resp
    if settings.llm_offline():
        raise RuntimeError("LLM_OFFLINE=true and no cached response")
    count("llm_calls")
    t0 = time.perf_counter()
    resp = with_backoff(get_client().models.generate_content, model=model, contents=contents, config=config)
    record_call(engine, role, plain_backend(), model, round((time.perf_counter() - t0) * 1000),
                resp.usage_metadata, cached=False)
    cache_put(key, resp.model_dump_json(exclude_none=True))
    return resp
