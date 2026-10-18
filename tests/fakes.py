"""Test doubles: an in-memory Repo with a tiny dataset, and a scripted Engine (no Gemini)."""
from core.schemas import AssessorOutput, Driver, Option, VerifierVerdict, WhyNot
from orchestrators.base import Engine, EngineOutput, EngineTask
from tools.water_budget import OPTION_CROPS, coverage_fact_id


class MemoryRepo:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.tables.setdefault("decision_log", [])

    def fetch_all(self, table):
        return [dict(r) for r in self.tables.get(table, [])]

    def fetch_where(self, table, **eq):
        return [dict(r) for r in self.tables.get(table, [])
                if all(str(r.get(k)).lower() == str(v).lower() for k, v in eq.items())]

    def insert(self, table, rows):
        key = {"households": "hh_id"}.get(table)
        existing = {r.get(key) for r in self.tables.get(table, [])} if key else set()
        if key and any(r[key] in existing for r in rows):
            raise ValueError("duplicate key")
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)
        return len(rows)

    def upsert(self, table, rows):
        self.tables.setdefault(table, []).extend(dict(r) for r in rows)
        return len(rows)


# Test-fixture price rows (same shape as data/raw/livestock_prices.csv; values chosen for easy maths).
def _lp(key, low, high, mid, unit="Rs per kg", basis="reported", synthetic=False):
    return {"item_key": key, "item": key, "unit": unit, "region": "D", "period": "test", "value_low": low,
            "value_high": high, "value_mid": mid, "basis": basis, "is_synthetic": synthetic, "note": None,
            "source_url": None if synthetic else "https://example.test"}


# Test-fixture calibration_v rows (rain_series fixture deficit 87.5% -> bucket >30).
CALIBRATION_V = [
    {"context_bucket": ">30|rainfed|gram", "confidence_level": "HIGH", "hit_rate": 0.33, "n": 6, "n_real": 1, "n_synthetic": 5},
    {"context_bucket": ">30|rainfed|gram+safflower", "confidence_level": "HIGH", "hit_rate": 0.25, "n": 4, "n_real": 0, "n_synthetic": 4},
    {"context_bucket": ">30|rainfed|sorghum+fodder", "confidence_level": "HIGH", "hit_rate": 0.8, "n": 5, "n_real": 0, "n_synthetic": 5},
]
F3_CONTEXTS = {"Chana only": ">30|rainfed|gram", "Chana + safflower": ">30|rainfed|gram+safflower",
               "Jowar + fodder": ">30|rainfed|sorghum+fodder"}


def cal_facts(irrigation="rainfed"):
    from tools.calibration import fact_id_for
    return {n: fact_id_for(c.replace("rainfed", irrigation)) for n, c in F3_CONTEXTS.items()}


LIVESTOCK_PRICES = [
    _lp("kadba_kg_now", 17, 25, 20), _lp("kadba_kg_normal", 8, 12.5, 10),
    _lp("cattle_price_drop_pct", -25, -20, -22.5, unit="percent"),
    _lp("cow_sale_now_example", 15625, 15625, 15625, unit="Rs per cow", basis="derived"),
    _lp("desi_cow_normal_value", 20000, 25000, 22500, unit="Rs per animal", basis="assumption", synthetic=True),
    _lp("silage_own_maize_kg", 3, 4, 3.5), _lp("silage_ready_kg", 7, 8, 7.5),
    _lp("fodder_camp_large_animal_day", 100, 100, 100, unit="Rs per animal per day"),
    _lp("fodder_intake_large_animal_day", 18, 18, 18, unit="kg per animal per day"),
]


def _hh(hh_id, irrigation, hours, cash, emb):
    return {"hh_id": hh_id, "village_id": 1, "tenure": "owner", "acres": 2.0, "irrigation": irrigation,
            "water_hours_per_week": hours, "crops_last_season": "soybean", "cattle_count": 2, "goats_count": 0,
            "fodder_weeks_left": 3, "off_farm_income": "none", "loan_due_month": None, "cash_buffer_band": cash,
            "primary_cultivator_gender": "man", "consent": True, "profile_text_norm": "x",
            "profile_embedding": emb, "is_synthetic": True}


def demo_repo() -> MemoryRepo:
    """Test fixture data: placeholder village, 3 households, 10 rain days, outcomes for calibration."""
    outcomes = []
    for i, (sig, conf, res) in enumerate([("ACT", "HIGH", "loss")] * 4 + [("ACT", "HIGH", "good")] * 2):
        outcomes.append({"hh_id": "T-2", "season": f"s{i}", "decision": "Chana only", "predicted_signal": sig,
                         "predicted_confidence": conf, "actual_result": res, "cattle_sold": 0,
                         "distress_sale": False, "is_synthetic": True})
    return MemoryRepo({
        "villages": [{"village_id": 1, "name": "TestVillage", "taluka": "T", "district": "D", "lat": 0.0, "lon": 0.0}],
        "households": [_hh("T-1", "rainfed", 0, "low", [1.0, 0.0]), _hh("T-2", "rainfed", 0, "med", [0.9, 0.1]),
                       _hh("T-3", "well", 8, "high", [0.0, 1.0])],
        "rain_series": [{"village_id": 1, "date": f"2026-06-{d:02d}", "rain_mm": 1.0, "normal_mm": 8.0,
                         "cum_deficit_pct": 87.5, "source": "CHIRPS"} for d in range(1, 11)],
        "outcomes": outcomes,
        "price_summary": [], "ndvi_series": [],
        "livestock_prices": LIVESTOCK_PRICES,
        "calibration_v": CALIBRATION_V,
        "plots": [{"plot_id": f"P-{h}", "hh_id": h, "lat": 0.0, "lon": 0.0, "is_synthetic": True}
                  for h in ("T-1", "T-2", "T-3")],
        # P7 water budget inputs. The wide need range keeps every option's coverage spanning short..fit, so tests
        # written before P7 still exercise their own rule; tests/test_water_budget.py covers the budget itself.
        "soil_moisture": [{"village_id": 1, "date": "2026-10-05", "sm_surface": 0.2, "sm_rootzone": 0.25}],
        "soil_props": [{"plot_id": f"P-{h}", "texture_class": "loam", "clay_pct": 20.0, "sand_pct": 40.0,
                        "organic_carbon_pct": 0.5, "ph": 7.8, "water_fc_pct": 30.0, "depth_cm": 30,
                        "source": "test soil map", "source_url": None, "is_synthetic": True} for h in ("T-1", "T-2", "T-3")],
        "crop_water_need": [{"crop_key": c, "crop": c, "season": "rabi", "need_mm_low": 50, "need_mm_high": 400,
                             "root_depth_m_low": 0.6, "root_depth_m_high": 1.0, "depletion_p": 0.5,
                             "critical_stage": None, "region": "test", "basis": "assumption", "is_synthetic": True,
                             "note": "test row", "source_url": None, "root_source_url": None}
                            for c in ("gram", "safflower", "sorghum")],
    })


def good_output(cal_fact, signal="ACT", water="fit", names=("Chana only", "Chana + safflower", "Jowar + fodder")):
    """cal_fact: one fact_id for every option, or {option name: fact_id}."""
    per = cal_fact if isinstance(cal_fact, dict) else {n: cal_fact for n in names}
    return AssessorOutput(
        options=[Option(name=n, signal=signal, failure_risk=30, water_fit=water, cash_at_risk="med", confidence="HIGH",
                        drivers=[Driver(text="Water source is rainfed", fact_id="F-hh-irrigation"),
                                 Driver(text="Calibration applies", fact_id=per.get(n, "F-hh-irrigation")),
                                 *([Driver(text="Water budget applies", fact_id=coverage_fact_id(n))]
                                   if n in OPTION_CROPS else [])])
                 for n in names],
        recommended=names[0],
        why_recommended=[Driver(text="Water source is rainfed", fact_id="F-hh-irrigation")],
        why_not_others=[WhyNot(option=n, text="Water source is rainfed", fact_id="F-hh-irrigation") for n in names[1:]],
        explanation_mr="mr", explanation_hi="hi")


class ScriptedEngine(Engine):
    """Calls the planner's tools itself, then returns scripted outputs per attempt."""

    def __init__(self, name="adk", outputs=None, error: Exception | None = None, call_tools=True, agent_outputs=None,
                 tool_calls=None):
        self.name, self.outputs, self.error, self.call_tools = name, outputs or [], error, call_tools
        self.tool_calls = tool_calls or [("get_rain_status", (1,)), ("find_similar_households", ("T-1",))] + \
            [("get_calibration", (c,)) for c in F3_CONTEXTS.values()]
        self.agent_outputs = agent_outputs or {}   # output_key -> model (or callable(state) -> model)
        self.runs: list[EngineTask] = []
        self.agent_runs: list[tuple[list, dict]] = []

    def backend(self) -> dict:
        return {"backend": "test", "project": None, "location": None}

    async def run(self, task: EngineTask) -> EngineOutput:
        self.runs.append(task)
        if self.error:
            raise self.error
        if task.planner_prompt and self.call_tools:
            tools = {fn.__name__: fn for fn in task.tools}
            for name, args in self.tool_calls:
                tools[name](*args)
        out = self.outputs[min(len(self.runs), len(self.outputs)) - 1]
        verdict = VerifierVerdict(status="PASS", issues=[]) if task.verifier_prompt else None
        return EngineOutput(assessment=out.model_copy(deep=True), verdict=verdict)

    async def run_agents(self, feature, specs, state):
        if self.error:
            raise self.error
        self.agent_runs.append(([sp.name for sp in specs], dict(state)))
        outputs = {}
        for sp in specs:
            sp.instruction(dict(state))  # prompts must build without errors
            out = self.agent_outputs[sp.output_key]
            out = out(state) if callable(out) else out
            outputs[sp.output_key] = out
            state[sp.output_key] = out.model_dump()
        return outputs, []
