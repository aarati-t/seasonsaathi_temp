"""Engine selector (SPEC section 3): ORCHESTRATOR=adk|plain with auto-fallback, never blocking the demo.

Chain (SPEC rule 6): ADK engine -> plain engine -> cached result -> rule result (confidence LOW).
ADK falls back on error, timeout > ENGINE_TIMEOUT_S, or schema-invalid output. Every request is
written to decision_log with orchestrator_used and fallback_reason.
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from core import llm, settings
from core.schemas import F4Result, FeatureResult, ResultMeta, TraceStep
from features import f3_cropmix, f4_livestock
from orchestrators.base import Engine
from repo.base import Repo, get_repo

FEATURES = {"f3": f3_cropmix, "f4": f4_livestock}
RESULT_TYPES = {"f3": FeatureResult, "f4": F4Result}
ADK_FAIL = "adk_fail"   # demo flag: skip ADK as if it failed, so the fallback to plain is visible
LLM_STEPS = {"planner", "assessor", "verifier_agent", "extractor", "interviewer", "transcribe", "writer"}


def stamp_backend(result: ResultMeta, engine: Engine | None) -> None:
    """Every trace step gets backend + model: Gemini steps the engine's real backend, the rest 'code'."""
    if engine is not None:
        result.genai_backend, result.gemini_model = engine.backend()["backend"], llm.gemini_model()
    for t in result.trace:
        is_llm = t.step in LLM_STEPS and "Required tools called by code" not in t.title
        if is_llm and result.genai_backend:
            t.backend, t.model = t.backend or result.genai_backend, t.model or result.gemini_model
        elif not t.backend:
            t.backend = "code"


def get_engine(name: str) -> Engine:
    if name == "adk":
        from orchestrators.adk_engine import AdkEngine  # heavy import, only when used
        return AdkEngine()
    if name == "plain":
        from orchestrators.plain_engine import PlainEngine
        return PlainEngine()
    raise ValueError(f"ORCHESTRATOR must be adk|plain, got {name!r}")


def engine_order(primary: str | None = None) -> list[str]:
    if settings.llm_offline():
        return []
    primary = primary or settings.orchestrator()
    order = [primary]
    if primary == "adk" and settings.engine_fallback():
        order.append("plain")
    return order


def cached_result(repo: Repo, feature: str, hh_id: str) -> ResultMeta | None:
    rows = [r for r in repo.fetch_where("decision_log", hh_id=hh_id, feature=feature)   # real engine runs only, never
            if r["verifier_status"] == "PASS" and r["output_json"]                        # an earlier cache/rule reply
            and r.get("orchestrator_used") in ("adk", "plain")]
    if not rows:
        return None
    row = max(rows, key=lambda r: r["request_id"])
    data = row["output_json"] if isinstance(row["output_json"], dict) else json.loads(row["output_json"])
    result = RESULT_TYPES.get(feature, FeatureResult).model_validate(data)
    result.notes = result.notes + [f"Cached result from request {row['request_id']}"]
    return result


async def run(feature: str, hh_id: str, inject: str | None = None, engine: str | None = None,
              repo: Repo | None = None, engines: dict[str, Engine] | None = None) -> FeatureResult:
    """engines: test hook to supply engine objects by name."""
    module = FEATURES[feature]
    repo = repo or get_repo()
    ctx = module.build_context(hh_id, repo)  # KeyError if the household does not exist
    return await execute(module, ctx, feature, hh_id, inject=inject, engine=engine, repo=repo, engines=engines)


async def execute(module, ctx, feature: str, hh_id: str | None, inject: str | None = None, engine: str | None = None,
                  repo: Repo | None = None, engines: dict[str, Engine] | None = None,
                  use_result_cache: bool = True) -> ResultMeta:
    """Shared fallback chain for any feature module (run_with_engine, rule_result)."""
    repo = repo or get_repo()
    t0, stats = time.perf_counter(), llm.start_stats()
    request_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"

    result, reasons, used = None, [], None
    demo_flag = inject
    force_fail, inject = inject == ADK_FAIL, (None if inject == ADK_FAIL else inject)
    for name in engine_order(engine):
        if force_fail and name == "adk":
            reasons.append("adk: forced failure (demo flag inject=adk_fail)")
            continue
        eng = (engines or {}).get(name) or get_engine(name)
        timeout = settings.engine_timeout_s() if name == "adk" else None
        try:
            result = await asyncio.wait_for(module.run_with_engine(eng, ctx, inject), timeout)
            used = eng
            break
        except asyncio.TimeoutError:
            reasons.append(f"{name}: timeout > {timeout:g} s")
        except Exception as exc:  # noqa: BLE001 - any engine failure falls back
            reasons.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
    # A demo flag must never be answered with an old cached reply: it would look like the flag ran.
    if result is None and use_result_cache and hh_id and settings.result_cache() and not demo_flag:
        result = cached_result(repo, feature, hh_id)
        if result is not None:
            result.orchestrator_used = "cache"
    if result is None:
        result = module.rule_result(ctx)
    if settings.llm_offline():
        reasons.append("LLM_OFFLINE=true")
    if demo_flag and used is None:
        result.notes = result.notes + [f"Demo flag inject={demo_flag} not applied: no AI engine ran (needs LLM_OFFLINE=false)"]

    result.fallback_reason = "; ".join(reasons) or None
    result.request_id = request_id
    result.latency_ms = round((time.perf_counter() - t0) * 1000)
    t_in = sum(c["tokens_in"] or 0 for c in stats["calls"] if not c["cached"])
    t_out = sum(c["tokens_out"] or 0 for c in stats["calls"] if not c["cached"])
    result.trace.append(TraceStep(step="usage", title=f"Gemini calls: {stats['llm_calls']}, cache hits: {stats['cache_hits']}, "
                                                      f"tokens in/out: {t_in}/{t_out}", detail=dict(stats)))
    stamp_backend(result, used)
    repo.insert("decision_log", [{   # single insert (BigQuery-safe); request_id is unique
        "request_id": request_id, "hh_id": hh_id, "feature": feature,
        "facts_used": json.dumps([f.model_dump() for f in getattr(result, "facts", [])], ensure_ascii=False, default=str),
        "output_json": result.model_dump_json(),
        "verifier_status": getattr(result, "verifier_status", None), "orchestrator_used": result.orchestrator_used,
        "genai_backend": result.genai_backend, "gemini_model": result.gemini_model,
        "fallback_reason": result.fallback_reason, "latency_ms": result.latency_ms,
    }])
    return result
