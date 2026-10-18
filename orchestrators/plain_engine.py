"""Plain engine (fallback): the same 3 roles as direct google-genai calls with function calling.

Every Gemini call goes through core.llm.generate (disk cache + backoff + usage stats).
"""
import asyncio
import json
import time

from google.genai import types

from core import llm, settings
from core.schemas import AssessorOutput, TraceStep, VerifierVerdict
from orchestrators.base import AgentSpec, Engine, EngineOutput, EngineTask, summarize, tool_trace

MAX_TOOL_TURNS = 6


def _config(**kw) -> types.GenerateContentConfig:
    model = llm.gemini_model()
    return types.GenerateContentConfig(temperature=0, seed=42, thinking_config=llm.thinking_config(model), **kw)


def _dump(contents: list[types.Content]) -> list:
    return [c.model_dump(mode="json", exclude_none=True) for c in contents]


def _turn(feature: str) -> types.Content:
    """Same framing as the ADK engine: role prompt as system instruction, a short user turn."""
    return types.Content(role="user", parts=[types.Part(text=f"Run {feature}.")])


def _json_call(role: str, prompt: str, schema, feature: str):
    resp = llm.generate([_turn(feature)],
                        _config(system_instruction=prompt, response_mime_type="application/json",
                                response_schema=schema),
                        key_parts=("plain", settings.thinking(), role, prompt, schema.__name__), role=role)
    return schema.model_validate_json(resp.text)


class PlainEngine(Engine):
    name = "plain"

    async def run(self, task: EngineTask) -> EngineOutput:
        return await asyncio.to_thread(self._run, task)

    async def run_agents(self, feature: str, specs: list[AgentSpec], state: dict) -> tuple[dict, list[TraceStep]]:
        return await asyncio.to_thread(self._run_agents, feature, specs, dict(state))

    def _run_agents(self, feature: str, specs: list[AgentSpec], state: dict) -> tuple[dict, list[TraceStep]]:
        outputs, trace = {}, []
        for spec in specs:
            t0 = time.perf_counter()
            out = _json_call(spec.name, spec.instruction(state), spec.output_schema, feature)
            state[spec.output_key] = out.model_dump()
            outputs[spec.output_key] = out
            trace.append(TraceStep(step=spec.name, title=spec.title or spec.name, detail=out.model_dump(),
                                   ms=round((time.perf_counter() - t0) * 1000)))
        return outputs, trace

    def _run(self, task: EngineTask) -> EngineOutput:
        trace = []
        if task.planner_prompt:
            trace += self._plan(task)
        facts = task.store.compact()

        t0 = time.perf_counter()
        assessment = _json_call("assessor", task.assessor_prompt(facts), task.assessor_schema, task.feature)
        trace.append(TraceStep(step="assessor", title=f"⚖️ Assessor (attempt {task.attempt})",
                               detail=summarize(assessment),
                               ms=round((time.perf_counter() - t0) * 1000)))
        verdict = None
        if task.verifier_prompt:
            t0 = time.perf_counter()
            verdict = _json_call("verifier", task.verifier_prompt(facts, assessment.model_dump_json()),
                                 VerifierVerdict, task.feature)
            trace.append(TraceStep(step="verifier_agent", title=f"Verifier agent: {verdict.status}",
                                   detail=verdict.model_dump(), ms=round((time.perf_counter() - t0) * 1000)))
        return EngineOutput(assessment=assessment, verdict=verdict, trace=trace)

    def _plan(self, task: EngineTask) -> list[TraceStep]:
        tools = {fn.__name__: fn for fn in task.tools}
        config = _config(system_instruction=task.planner_prompt, tools=task.tools,
                         automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        contents = [_turn(task.feature)]
        since, t0, text = len(task.store.calls), time.perf_counter(), ""
        for _ in range(MAX_TOOL_TURNS):
            resp = llm.generate(contents, config, key_parts=("plain", settings.thinking(), "planner", task.planner_prompt,
                                                             _dump(contents), sorted(tools)), role="planner")
            calls = resp.function_calls or []
            if not calls:
                text = resp.text or ""
                break
            contents.append(resp.candidates[0].content)
            parts = []
            for fc in calls:
                fn = tools.get(fc.name)
                result = fn(**(fc.args or {})) if fn else {"error": f"unknown tool {fc.name}"}
                parts.append(types.Part.from_function_response(name=fc.name, response=json.loads(
                    json.dumps(result, default=str))))
            contents.append(types.Content(role="user", parts=parts))
        steps = [TraceStep(step="planner", title="🧭 Planner chose tools",
                           detail={"tools": [c["tool"] for c in task.store.calls[since:]], "reasoning": text.strip()},
                           ms=round((time.perf_counter() - t0) * 1000))]
        return steps + tool_trace(task.store, since)
