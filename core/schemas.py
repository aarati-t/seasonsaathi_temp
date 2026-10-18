"""Pydantic schemas shared by both engines (SPEC section 6, F3 output).

LLM output schemas (AssessorOutput, VerifierVerdict) avoid numeric constraints so they work as
Gemini response schemas; range checks happen in core/verifier.py.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Signal = Literal["ACT", "WAIT", "AVOID"]
Confidence = Literal["HIGH", "MED", "LOW"]
WaterFit = Literal["fit", "tight", "short"]
CashRisk = Literal["low", "med", "high"]

CONFIDENCE_ORDER: list[str] = ["LOW", "MED", "HIGH"]


class Fact(BaseModel):
    """What every tool returns. Tools never return decisions. Extra keys (range, caveat...) are kept."""
    model_config = ConfigDict(extra="allow")

    fact_id: str
    value: Any = None
    unit: Optional[str] = None
    text: str
    source: Optional[str] = None
    source_url: Optional[str] = None
    as_of: Optional[str] = None
    is_synthetic: bool = False


class Driver(BaseModel):
    text: str
    fact_id: str


class Option(BaseModel):
    name: str
    signal: Signal
    failure_risk: Optional[int] = None
    drivers: list[Driver]
    water_fit: WaterFit
    cash_at_risk: CashRisk
    confidence: Confidence


class WhyNot(BaseModel):
    option: str
    text: str
    fact_id: str


class AssessorOutput(BaseModel):
    """Gemini response schema for the assessor agent (F3)."""
    options: list[Option]
    recommended: str
    why_recommended: list[Driver] = Field(default_factory=list)
    why_not_others: list[WhyNot] = Field(default_factory=list)
    explanation_mr: str
    explanation_hi: str


class VerifierVerdict(BaseModel):
    """Gemini response schema for the verifier agent."""
    status: Literal["PASS", "FAIL"]
    issues: list[str]


class TraceStep(BaseModel):
    step: str          # planner | tool | facts | assessor | verifier_code | verifier_agent | safety | calibration | engine
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)
    ms: Optional[int] = None
    backend: Optional[str] = None   # vertex | apikey for Gemini steps; code for rule/tool steps
    model: Optional[str] = None     # Gemini model for Gemini steps


class ResultMeta(BaseModel):
    """Fields every feature result carries (engine, fallback, latency, trace)."""
    trace: list[TraceStep] = Field(default_factory=list)
    orchestrator_used: str = ""
    genai_backend: Optional[str] = None
    gemini_model: Optional[str] = None
    fallback_reason: Optional[str] = None
    notes: list[str] = Field(default_factory=list)
    request_id: Optional[str] = None
    latency_ms: Optional[int] = None
    # P5b F: after persistent verifier failure the recommendation is withheld (LOW confidence is not a substitute).
    withheld: bool = False
    withheld_reason: Optional[str] = None


class FeatureResult(ResultMeta):
    """F3 output (SPEC section 6)."""
    hh_id: str
    options: list[Option]
    recommended: str
    verifier_status: Literal["PASS", "FAIL", "RULE"]
    safety_rule_applied: bool = False
    explanation_mr: str = ""
    explanation_hi: str = ""
    facts: list[Fact] = Field(default_factory=list)
    explain: "Explain" = Field(default_factory=lambda: Explain())
    context_bucket: Optional[str] = None


# ---------- F1 interview ----------

ProfileField = Literal["tenure", "acres", "irrigation", "water_hours_per_week", "crops_last_season", "cattle_count",
                       "goats_count", "fodder_weeks_left", "off_farm_income", "loan_due_month", "cash_buffer_band",
                       "primary_cultivator_gender", "soil_type_farmer"]


class HouseholdProfile(BaseModel):
    """Validated profile. Ranges per F1 refinement; a value outside them is treated as unknown and asked."""
    tenure: Optional[Literal["owner", "tenant", "sharecrop"]] = None
    acres: Optional[float] = Field(None, ge=0, le=50)
    irrigation: Optional[Literal["rainfed", "well", "borewell"]] = None
    water_hours_per_week: Optional[float] = Field(None, ge=0, le=168)
    crops_last_season: Optional[str] = None
    cattle_count: Optional[int] = Field(None, ge=0, le=50)
    goats_count: Optional[int] = Field(None, ge=0, le=200)
    fodder_weeks_left: Optional[float] = Field(None, ge=0, le=52)
    off_farm_income: Optional[str] = None
    loan_due_month: Optional[int] = Field(None, ge=1, le=12)
    cash_buffer_band: Optional[Literal["low", "med", "high"]] = None
    primary_cultivator_gender: Optional[Literal["man", "woman", "both"]] = None
    soil_type_farmer: Optional[str] = None   # P7: only when the farmer states it; never asked, never inferred


class ExtractedField(BaseModel):
    field: ProfileField
    value: str
    evidence: str   # exact words the farmer used; checked against the conversation in code


class LocalTerm(BaseModel):
    term: str
    meaning: str


class ExtractionOutput(BaseModel):
    """Gemini response schema for the F1 extractor agent."""
    fields: list[ExtractedField]
    local_terms: list[LocalTerm]


class QuestionOutput(BaseModel):
    """Gemini response schema for the F1 interviewer agent."""
    question: str


class TranscriptOutput(BaseModel):
    """Gemini response schema for audio transcription."""
    speech_detected: bool
    transcript: str


class Turn(BaseModel):
    role: Literal["user", "agent"]
    text: str
    field: Optional[str] = None   # agent turns may echo the field they targeted (keeps the API stateless)


class InterviewResult(ResultMeta):
    type: Literal["question", "final"]
    question: Optional[str] = None
    field_targeted: Optional[str] = None
    completeness_pct: int = 0
    profile_so_far: Optional[dict[str, Any]] = None
    profile: Optional[dict[str, Any]] = None
    unknown_fields: Optional[list[str]] = None
    questions_asked: int = 0
    transcript: Optional[str] = None


# ---------- F4 livestock ----------

F4Name = Literal["KEEP_AND_BUY_FODDER", "KEEP_WITH_SUPPORT", "SELL_ONE_KEEP_REST", "SELL_NOW"]


class F4Option(BaseModel):
    """What Gemini judges per option. ₹ amounts are NOT here: code fills them from facts."""
    name: F4Name
    cash_pressure: Literal["low", "med", "high"]   # vs cash_buffer_band only; never exact affordability
    risk: int                                      # 0-100 judgement
    drivers: list[Driver]
    confidence: Confidence
    support_ids: list[str] = Field(default_factory=list)   # ids from config/support_options.yaml only


class F4AssessorOutput(BaseModel):
    """Gemini response schema for the F4 assessor agent."""
    options: list[F4Option]
    recommended: str
    why_recommended: list[Driver] = Field(default_factory=list)
    why_not_others: list[WhyNot] = Field(default_factory=list)
    explanation_mr: str
    explanation_hi: str


class Money(BaseModel):
    value: Optional[int] = None
    low: Optional[int] = None
    high: Optional[int] = None
    fact_id: Optional[str] = None


class F4ResultOption(F4Option):
    cash_needed: Money
    asset_value_kept: Money
    sale_proceeds: Optional[Money] = None


class Explain(BaseModel):
    """Explainability block shared by F3 and F4 (all fields verifier-checked or computed in code)."""
    why_recommended: list[Driver] = Field(default_factory=list)
    why_not_others: list[WhyNot] = Field(default_factory=list)
    what_would_change: Optional[dict[str, Any]] = None
    confidence_reason: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    plain_explanation_mr: str = ""
    plain_explanation_hi: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class F4Result(ResultMeta):
    hh_id: str
    applicable: bool = True
    reason: Optional[str] = None
    options: list[F4ResultOption] = Field(default_factory=list)
    recommended: Optional[str] = None
    verifier_status: Literal["PASS", "FAIL", "RULE", "NA"] = "NA"
    explain: Explain = Field(default_factory=Explain)
    facts: list[Fact] = Field(default_factory=list)
    context_bucket: Optional[str] = None


FeatureResult.model_rebuild()


# ---------- F2 season risk watch ----------

NextStep = Literal["WAIT_NEXT_PASS", "REQUEST_PHOTOS", "REQUEST_DRONE", "ALERT_AGENT"]


class F2Plan(BaseModel):
    """Gemini response schema for the F2 planner agent."""
    next_step: NextStep
    reason: str
    fact_ids: list[str]


class F2Alert(BaseModel):
    """Gemini response schema for the F2 alert writer: 2 lines each, citing fact_ids."""
    text_mr: str
    text_hi: str
    fact_ids: list[str]


class F2Result(ResultMeta):
    plot_id: str
    hh_id: Optional[str] = None
    stress_level: Optional[Literal["LOW", "MED", "HIGH"]] = None
    crossed: list[str] = Field(default_factory=list)
    next_step: Optional[NextStep] = None
    alert: Optional[dict[str, Any]] = None
    deduped: bool = False
    skipped: Optional[str] = None
    verifier_status: Literal["PASS", "FAIL", "RULE", "NA"] = "NA"
    facts: list[Fact] = Field(default_factory=list)
