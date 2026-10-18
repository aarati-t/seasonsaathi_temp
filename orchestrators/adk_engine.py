"""ADK engine (primary): SequentialAgent[planner, assessor, verifier] of LlmAgents, run in-process via
Runner + InMemorySessionService. Session state carries facts between agents.

- planner: LlmAgent with the shared function tools; an after_tool_callback copies facts into state.
- assessor: LlmAgent, output_schema=AssessorOutput, sees facts only (include_contents='none').
- verifier: LlmAgent, output_schema=VerifierVerdict (skipped when VERIFIER_AGENT=off).
Model calls go through before/after_model_callback for the shared disk cache and usage stats.
"""
import time
import uuid

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.planners import BuiltInPlanner
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from core import llm, settings
from core.schemas import AssessorOutput, TraceStep, VerifierVerdict
from orchestrators.base import AgentSpec, Engine, EngineOutput, EngineTask, summarize, tool_trace

APP = "seasonsaathi"
_pending: dict[tuple[str, str], tuple[str, float]] = {}


def _cache_before(callback_context, llm_request):
    system = llm_request.config.system_instruction if llm_request.config else None
    schema = getattr(llm_request.config, "response_schema", None) if llm_request.config else None
    key = llm.cache_key("adk", llm_request.model, settings.thinking(), callback_context.agent_name, str(system),
                        [c.model_dump(mode="json", exclude_none=True) for c in llm_request.contents],
                        sorted(llm_request.tools_dict), getattr(schema, "__name__", str(schema)))
    cached = llm.cache_get(key)
    if cached is not None:
        llm.count("cache_hits")
        resp = LlmResponse.model_validate_json(cached)
        llm.record_call("adk", callback_context.agent_name, llm.adk_backend(), llm_request.model, 0,
                        resp.usage_metadata, cached=True)
        return resp
    if settings.llm_offline():
        raise RuntimeError("LLM_OFFLINE=true and no cached response")
    llm.count("llm_calls")
    _pending[(callback_context.invocation_id, callback_context.agent_name)] = (key, time.perf_counter())
    return None


def _cache_after(callback_context, llm_response):
    pending = _pending.pop((callback_context.invocation_id, callback_context.agent_name), None)
    if pending is None:
        return None
    key, t0 = pending
    llm.record_call("adk", callback_context.agent_name, llm.adk_backend(), llm.gemini_model(),
                    round((time.perf_counter() - t0) * 1000), llm_response.usage_metadata, cached=False)
    if llm_response.content and not llm_response.partial and not llm_response.error_code:
        llm.cache_put(key, llm_response.model_dump_json(exclude_none=True))
    return None


class AdkEngine(Engine):
    name = "adk"

    def _agent(self, name: str, instruction, **kw) -> LlmAgent:
        model_name = llm.gemini_model()
        thinking = llm.thinking_config(model_name)
        return LlmAgent(
            name=name,
            model=Gemini(model=model_name, retry_options=llm.retry_options()),
            instruction=instruction,  # a callable: bypasses ADK {state} templating, so JSON braces are safe
            generate_content_config=types.GenerateContentConfig(temperature=0, seed=42),
            planner=BuiltInPlanner(thinking_config=thinking) if thinking else None,
            before_model_callback=_cache_before,
            after_model_callback=_cache_after,
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
            **kw,
        )

    async def run_agents(self, feature: str, specs: list[AgentSpec], state: dict) -> tuple[dict, list[TraceStep]]:
        """Generic SequentialAgent of LlmAgents with output schemas; state carries outputs between agents."""
        agents = [self._agent(s.name, (lambda ctx, s=s: s.instruction(dict(ctx.state))),
                              output_schema=s.output_schema, output_key=s.output_key, include_contents="none")
                  for s in specs]
        pipeline = SequentialAgent(name=f"{feature}_agents", sub_agents=agents)
        sessions = InMemorySessionService()
        runner = Runner(app_name=APP, agent=pipeline, session_service=sessions)
        user_id, session_id = "field-agent", uuid.uuid4().hex
        await sessions.create_session(app_name=APP, user_id=user_id, session_id=session_id, state=dict(state))
        last, timings = time.perf_counter(), {}
        message = types.Content(role="user", parts=[types.Part(text=f"Run {feature}.")])
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
            if event.is_final_response():
                now = time.perf_counter()
                timings[event.author] = round((now - last) * 1000)
                last = now
        final = (await sessions.get_session(app_name=APP, user_id=user_id, session_id=session_id)).state
        outputs, trace = {}, []
        for s in specs:
            if s.output_key not in final:
                raise RuntimeError(f"ADK agent {s.name} produced no output")
            out = s.output_schema.model_validate(final[s.output_key])
            outputs[s.output_key] = out
            trace.append(TraceStep(step=s.name, title=s.title or s.name, detail=out.model_dump(), ms=timings.get(s.name)))
        return outputs, trace

    async def run(self, task: EngineTask) -> EngineOutput:
        store = task.store

        def sync_facts(tool, args, tool_context, tool_response):
            tool_context.state["facts"] = store.compact()
            return None

        agents = []
        if task.planner_prompt:
            agents.append(self._agent("planner", lambda ctx: task.planner_prompt, tools=task.tools,
                                      after_tool_callback=sync_facts))
        agents.append(self._agent("assessor", lambda ctx: task.assessor_prompt(ctx.state["facts"]),
                                  output_schema=task.assessor_schema, output_key="assessment", include_contents="none"))
        if task.verifier_prompt:
            agents.append(self._agent(
                "verifier",
                lambda ctx: task.verifier_prompt(ctx.state["facts"],
                                                 task.assessor_schema.model_validate(ctx.state["assessment"]).model_dump_json()),
                output_schema=VerifierVerdict, output_key="verdict", include_contents="none"))
        pipeline = SequentialAgent(name=f"{task.feature}_pipeline", sub_agents=agents)

        sessions = InMemorySessionService()
        runner = Runner(app_name=APP, agent=pipeline, session_service=sessions)
        user_id, session_id = "field-agent", uuid.uuid4().hex
        await sessions.create_session(app_name=APP, user_id=user_id, session_id=session_id,
                                      state={"facts": store.compact()})

        since, t0, last = len(store.calls), time.perf_counter(), time.perf_counter()
        planner_text, timings = "", {}
        message = types.Content(role="user", parts=[types.Part(text=f"Run {task.feature}.")])
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
            if event.is_final_response():
                now = time.perf_counter()
                timings[event.author] = round((now - last) * 1000)
                last = now
                if event.author == "planner" and event.content and event.content.parts:
                    planner_text = "".join(p.text or "" for p in event.content.parts)

        state = (await sessions.get_session(app_name=APP, user_id=user_id, session_id=session_id)).state
        if "assessment" not in state:
            raise RuntimeError("ADK assessor produced no output")
        assessment = task.assessor_schema.model_validate(state["assessment"])
        verdict = VerifierVerdict.model_validate(state["verdict"]) if "verdict" in state else None

        trace = []
        if task.planner_prompt:
            trace.append(TraceStep(step="planner", title="🧭 Planner chose tools",
                                   detail={"tools": [c["tool"] for c in store.calls[since:]],
                                           "reasoning": planner_text.strip()},
                                   ms=timings.get("planner")))
            trace += tool_trace(store, since)
        trace.append(TraceStep(step="assessor", title=f"⚖️ Assessor (attempt {task.attempt})",
                               detail=summarize(assessment), ms=timings.get("assessor")))
        if verdict:
            trace.append(TraceStep(step="verifier_agent", title=f"Verifier agent: {verdict.status}",
                                   detail=verdict.model_dump(), ms=timings.get("verifier")))
        return EngineOutput(assessment=assessment, verdict=verdict, trace=trace)
