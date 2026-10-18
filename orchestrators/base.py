"""Shared by both engines (SPEC section 3): fact store, LLM tool wrappers, prompt loading, engine interface.

Tools, prompts, schemas and the verifier are defined once and used by ADK and plain engines alike.
"""
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.schemas import AssessorOutput, TraceStep, VerifierVerdict
from repo.base import Repo
from tools import calibration, livestock, ndvi, peers, prices, rain, soil, water_budget

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str, **values: str) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    for k, v in values.items():
        text = text.replace(f"<<{k}>>", v)
    return text


class FactStore:
    """Request-scoped: every fact a tool returned, plus a per-request cache of tool calls."""

    def __init__(self):
        self.facts: dict[str, dict] = {}
        self.calls: list[dict] = []
        self._memo: dict[str, list[dict]] = {}

    def add(self, facts: list[dict]) -> None:
        for f in facts:
            self.facts[f["fact_id"]] = f

    def call(self, tool: str, fn: Callable, *args, **kwargs) -> list[dict]:
        key = json.dumps([tool, args, {k: v for k, v in kwargs.items() if k != "repo"}], default=str)
        t0 = time.perf_counter()
        cached = key in self._memo
        if not cached:
            self._memo[key] = fn(*args, **kwargs)
        facts = self._memo[key]
        self.add(facts)
        self.calls.append({"tool": tool, "args": list(args), "fact_ids": [f["fact_id"] for f in facts],
                           "cached": cached, "ms": round((time.perf_counter() - t0) * 1000)})
        return facts

    def called(self, tool: str) -> bool:
        return any(c["tool"] == tool for c in self.calls)

    def as_list(self) -> list[dict]:
        return list(self.facts.values())

    def compact(self) -> str:
        """Facts as the LLM sees them: compact JSON, no internal-only keys."""
        keep = ("fact_id", "text", "value", "unit", "range", "source", "as_of", "is_synthetic", "caveat", "mandi_available",
                "available")
        return json.dumps([{k: f[k] for k in keep if f.get(k) is not None} for f in self.as_list()],
                          ensure_ascii=False, default=str)


def make_tools(store: FactStore, repo: Repo) -> list[Callable]:
    """Plain Python functions with typed args + docstrings; both engines expose them to Gemini."""

    def get_rain_status(village_id: int) -> dict:
        """Season rainfall facts for a village (CHIRPS): deficit vs normal, totals, dry spells."""
        return {"facts": store.call("get_rain_status", rain.get_rain_status, int(village_id), repo=repo)}

    def get_crop_health(plot_id: str) -> dict:
        """Crop greenness facts for a plot (Sentinel-2 NDVI vs normal)."""
        return {"facts": store.call("get_crop_health", ndvi.get_crop_health, plot_id, repo=repo)}

    def find_similar_households(hh_id: str) -> dict:
        """The most similar households (profile embeddings) and how their past crop mixes turned out."""
        return {"facts": store.call("find_similar_households", peers.find_similar_households, hh_id, repo=repo)}

    def get_prices(commodity: str, district: str) -> dict:
        """Latest mandi price and MSP facts for a commodity key (gram, safflower, jowar, soybean, tur, wheat)."""
        return {"facts": store.call("get_prices", prices.get_prices, commodity, district, repo=repo)}

    def get_calibration(context: str) -> dict:
        """How often past calls at each confidence level were right, for context 'rainfed' or 'irrigated'."""
        return {"facts": store.call("get_calibration", calibration.get_calibration, context, repo=repo)}

    def get_livestock_facts(hh_id: str) -> dict:
        """F4 livestock facts for a household: fodder shortfall, fodder cost, sale values, per-option cash (₹, in code)."""
        return {"facts": store.call("get_livestock_facts", livestock.get_livestock_facts, hh_id, repo=repo)}

    def get_soil_status(hh_id: str) -> dict:
        """Soil facts for a household: soil map at the plot (texture, clay, organic carbon, pH), root-zone and topsoil
        moisture (SMAP), and the soil type in the farmer's own words."""
        return {"facts": store.call("get_soil_status", soil.get_soil_status, hh_id, repo=repo)}

    def get_water_budget(hh_id: str) -> dict:
        """Per crop-mix option: water available (root-zone water + irrigation) as % of the crop's sourced water need."""
        return {"facts": store.call("get_water_budget", water_budget.get_water_budget, hh_id, repo=repo)}

    return [get_rain_status, get_crop_health, find_similar_households, get_prices, get_calibration, get_livestock_facts,
            get_soil_status, get_water_budget]


@dataclass
class EngineTask:
    """One engine run. planner_prompt=None skips the planner (regeneration with known facts)."""
    feature: str
    planner_prompt: Optional[str]
    assessor_prompt: Callable[[str], str]                 # facts JSON -> prompt
    verifier_prompt: Optional[Callable[[str, str], str]]  # facts JSON, assessment JSON -> prompt
    tools: list[Callable]
    store: FactStore
    attempt: int = 1
    assessor_schema: type = AssessorOutput   # F3 default; F4 passes its own schema


def summarize(assessment) -> dict:
    """Short assessor summary for the trace (works for F3 and F4 option shapes)."""
    keys = {"name", "signal", "risk", "confidence"}
    return {"options": [o.model_dump(include=keys) for o in assessment.options],
            "recommended": assessment.recommended}


@dataclass
class AgentSpec:
    """One agent in a generic sequential pipeline (used by F1). instruction gets the current state dict;
    the agent's structured output is stored in state[output_key]."""
    name: str
    instruction: Callable[[dict], str]
    output_schema: type
    output_key: str
    title: str = ""


@dataclass
class EngineOutput:
    assessment: AssessorOutput
    verdict: Optional[VerifierVerdict]
    trace: list[TraceStep] = field(default_factory=list)


class Engine(ABC):
    name: str

    def backend(self) -> dict:
        """The backend this engine's Gemini calls really use: {backend, project, location}."""
        from core import llm
        return llm.adk_backend() if self.name == "adk" else llm.plain_backend()

    @abstractmethod
    async def run(self, task: EngineTask) -> EngineOutput:
        """Planner (tools) -> assessor (AssessorOutput) -> verifier agent (VerifierVerdict, optional)."""

    @abstractmethod
    async def run_agents(self, feature: str, specs: list[AgentSpec], state: dict) -> tuple[dict, list[TraceStep]]:
        """Run specs in order; returns ({output_key: validated model}, trace steps)."""


def tool_trace(store: FactStore, since: int) -> list[TraceStep]:
    return [TraceStep(step="tool", title=f"🛰️ {c['tool']}({', '.join(map(str, c['args']))})",
                      detail=c, ms=c["ms"]) for c in store.calls[since:]]
