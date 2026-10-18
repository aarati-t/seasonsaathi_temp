"""FastAPI app: routes + static UI (+ /api/alerts/receive in build block 6)."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import llm, settings
from core.schemas import F2Result, F4Result, FeatureResult, InterviewResult, Turn
from features import f1_interview, f2_watch, learning
from app import views
from services import alerts as alert_service
from services.storage import get_storage
from orchestrators import selector
from repo.base import get_repo

load_dotenv()


def resolved() -> dict:
    """What this process really uses (resolved from the live objects, not just .env)."""
    info = llm.plain_backend()
    repo = get_repo()
    return {"genai_backend": info["backend"], "project": info["project"], "location": info["location"],
            "gemini_model": llm.gemini_model(), "embed_model": os.getenv("EMBED_MODEL"),
            "orchestrator": settings.orchestrator(),
            "data_backend": type(repo).__name__.removesuffix("Repo").lower(),
            "bq_dataset": getattr(repo, "dataset", None),
            "storage_backend": get_storage().backend, "alerts_mode": alert_service.get_sink(repo).via}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_repo().init_schema()  # idempotent: creates tables and adds new columns (decision_log etc.)
    # Fail fast: refuse to start if GENAI_MODE and the real clients (google-genai, ADK) disagree.
    if not settings.llm_offline():
        llm.verify_backend(check_adk=settings.orchestrator() == "adk")
    llm.log.info("startup: %s", " ".join(f"{k}={v}" for k, v in resolved().items()))
    yield


app = FastAPI(title="SeasonSaathi", lifespan=lifespan)


class F3Request(BaseModel):
    inject: Optional[Literal["bad_fact_id", "bad_number", "adk_fail"]] = None  # verifier demo; adk_fail = fallback demo
    engine: Optional[Literal["adk", "plain"]] = None                # override ORCHESTRATOR for one request


class F4Request(BaseModel):
    inject: Optional[Literal["bad_fact_id", "bad_number", "adk_fail"]] = None
    engine: Optional[Literal["adk", "plain"]] = None


class InterviewRequest(BaseModel):
    history: list[Turn]
    lang: Literal["mr", "hi", "en"] = "mr"
    engine: Optional[Literal["adk", "plain"]] = None


class AudioRequest(BaseModel):
    audio_b64: str                      # base64 of the recording (webm/ogg/wav/mp3, <= 2 MB)
    mime_type: str
    history: list[Turn] = []            # earlier turns; the transcript becomes the next farmer turn
    lang: Literal["mr", "hi", "en"] = "mr"
    engine: Optional[Literal["adk", "plain"]] = None


class OutcomeRequest(BaseModel):
    hh_id: str
    request_id: str
    season: str
    actual_result: Literal["good", "partial", "loss"]
    cattle_sold: Optional[int] = None
    distress_sale: Optional[bool] = None
    note: Optional[str] = None
    demo: bool = False   # learning demo: stored with provenance 'demo', never as an observed outcome


class FeedbackRequest(BaseModel):
    request_id: str
    rating: Literal["up", "down"]
    note: Optional[str] = None


class TermDecision(BaseModel):
    term: str
    status: Literal["approved", "rejected"] = "approved"
    canonical: Optional[str] = None
    category: Optional[str] = None
    note: Optional[str] = None


class SaveRequest(BaseModel):
    profile: dict[str, Any]
    village_id: int
    consent: bool = False


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "genai_mode": os.getenv("GENAI_MODE", "vertex"),
        "data_backend": os.getenv("DATA_BACKEND", "postgres"),
        "orchestrator": os.getenv("ORCHESTRATOR", "adk"),
    }


@app.get("/api/whoami")
def whoami() -> dict:
    """Which backend and models this process really uses (resolved from the client, not just .env)."""
    return resolved()


@app.get("/api/calibration")
def calibration_table(context: Optional[str] = None) -> dict:
    """F7 calibration_v table for the UI: hit_rate, n, n_real, n_synthetic per context and confidence level."""
    rows = get_repo().fetch_where("calibration_v", context_bucket=context) if context else get_repo().fetch_all("calibration_v")
    t = settings.thresholds()["calibration"]
    return {"rule": f"HIGH and hit_rate < {t['high_min_hit_rate']} and n >= {t['min_n']} -> MED; n < {t['min_n']} -> no downgrade",
            "rows": [{**r, "hit_rate": round(float(r["hit_rate"]), 3)} for r in rows]}


@app.post("/api/f3/{hh_id}", response_model=FeatureResult)
async def f3(hh_id: str, body: F3Request | None = None) -> FeatureResult:
    """F3 crop-mix plan: result + full Agent Trace + orchestrator_used."""
    body = body or F3Request()
    try:
        return await selector.run("f3", hh_id, inject=body.inject, engine=body.engine)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/f4/{hh_id}", response_model=F4Result)
async def f4(hh_id: str, body: F4Request | None = None) -> F4Result:
    """F4 livestock and fodder decision: options with ₹ from facts, explainability, full trace."""
    body = body or F4Request()
    try:
        return await selector.run("f4", hh_id, inject=body.inject, engine=body.engine)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _interview(history: list[Turn], lang: str, engine: str | None) -> InterviewResult:
    try:
        ctx = f1_interview.build_context(history, lang, get_repo())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await selector.execute(f1_interview, ctx, "f1", None, engine=engine, use_result_cache=False)


@app.post("/api/f1/interview", response_model=InterviewResult, response_model_exclude_none=True)
async def f1_interview_turn(body: InterviewRequest) -> InterviewResult:
    """Stateless F1 turn: send the whole history; get the next question or the final profile."""
    return await _interview(body.history, body.lang, body.engine)


async def _audio_request(request: Request) -> tuple[bytes, str, list[Turn], str, str | None]:
    """Either a file upload (multipart: file, history as JSON text, lang, engine) or JSON with audio_b64."""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart upload needs a 'file' field")
        if upload.size is not None and upload.size > f1_interview.MAX_AUDIO_BYTES:
            raise HTTPException(status_code=400, detail=f"audio is {upload.size / 1024 / 1024:.1f} MB; the limit is 2 MB")
        mime = str(form.get("mime_type") or upload.content_type or "")
        try:
            history = [Turn(**t) for t in json.loads(str(form.get("history") or "[]"))]
            body = AudioRequest(audio_b64="", mime_type=mime, history=history, lang=str(form.get("lang") or "mr"),
                                engine=form.get("engine") or None)
            data = f1_interview.check_audio(await upload.read(f1_interview.MAX_AUDIO_BYTES + 1), mime)
        except f1_interview.AudioError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"bad form field: {exc}") from exc
        return data, mime, body.history, body.lang, body.engine
    try:
        body = AudioRequest(**(await request.json()))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"send a file upload or JSON with audio_b64: {exc}") from exc
    try:
        data = f1_interview.decode_audio(body.audio_b64, body.mime_type)
    except f1_interview.AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data, body.mime_type, body.history, body.lang, body.engine


@app.post("/api/f1/audio", response_model=InterviewResult, response_model_exclude_none=True)
async def f1_audio_turn(request: Request) -> InterviewResult:
    """Audio turn: Gemini transcribes (multimodal), then the same flow as /api/f1/interview.
    Accepts a normal file upload (field 'file') or JSON {audio_b64, mime_type, history, lang}."""
    data, mime, history, lang, engine = await _audio_request(request)
    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3"}.get(
        mime.split(";")[0].strip().lower(), "wav")
    try:
        stored = get_storage().save("audio", data, mime.split(";")[0].strip(), ext)
    except Exception as exc:  # noqa: BLE001 - storing the recording must not block the interview
        stored = {"uri": None, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        transcript, step = f1_interview.transcribe(data, mime.split(";")[0].strip().lower(), lang)
    except f1_interview.NoSpeechError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except f1_interview.AudioError as exc:  # Gemini call failed
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = await _interview(list(history) + [Turn(role="user", text=transcript)], lang, engine)
    result.transcript = transcript
    step.detail["audio_uri"] = stored.get("uri")
    if stored.get("error"):
        step.detail["storage_error"] = stored["error"]
    result.trace.insert(0, step.model_copy(update={"backend": result.genai_backend or "vertex",
                                                   "model": llm.gemini_model()}))
    return result


@app.post("/api/households")
def save_household(body: SaveRequest) -> dict:
    """Save an F1 profile: consent required, village must exist, embedding stored in the same insert."""
    try:
        return f1_interview.save_household(body.profile, body.village_id, body.consent, get_repo())
    except f1_interview.ConsentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:  # unknown village or out-of-range profile values
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/f2/scan")
async def f2_scan() -> dict:
    """F2 for every plot: summary counts; households without plots are skipped ('no plot location')."""
    return await f2_watch.scan(get_repo())


@app.post("/api/f2/{plot_id}", response_model=F2Result)
async def f2(plot_id: str) -> F2Result:
    """F2 season risk watch for one plot: stress level in code, next step + alert by agents, dedupe 7 days."""
    try:
        ctx = f2_watch.build_context(plot_id, get_repo())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await selector.execute(f2_watch, ctx, "f2", ctx.hh["hh_id"], use_result_cache=False)


@app.get("/api/alerts")
def list_alerts(hh_id: Optional[str] = None) -> list[dict]:
    return alert_service.alerts_for(get_repo(), hh_id)


@app.post("/api/alerts/receive")
async def receive_alert(request: Request) -> dict:
    """Pub/Sub push endpoint (message.data = base64 JSON alert). Works locally too."""
    try:
        return alert_service.receive_push(await request.json(), get_repo())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/outcomes")
def add_outcome(body: OutcomeRequest) -> dict:
    """Learning loop 1: record what actually happened (single insert, is_synthetic=false)."""
    try:
        return learning.record_outcome(get_repo(), body.hh_id, body.request_id, body.season, body.actual_result,
                                       body.cattle_sold, body.distress_sale, body.note, demo=body.demo)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/feedback")
def add_feedback(body: FeedbackRequest) -> dict:
    """Learning loop 3: rate an explanation (insert only)."""
    try:
        return learning.record_feedback(get_repo(), body.request_id, body.rating, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/feedback/review")
def feedback_review() -> list[dict]:
    """'down' ratings with notes, newest first."""
    return learning.feedback_review(get_repo())


@app.get("/api/terms/review")
def terms_review() -> dict:
    """Learning loop 4: pending / approved / rejected local terms."""
    return learning.terms_review(get_repo())


@app.post("/api/terms/review")
def decide_term(body: TermDecision) -> dict:
    """Approve (with canonical + category) or reject a term; approved terms are used on the next F1 run."""
    try:
        return learning.approve_term(get_repo(), body.term, body.canonical, body.category, body.status, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------- P5b impact (KPIs computed in code; cached replay only, no model calls) ----------

@app.get("/api/impact/latest")
def impact_latest() -> dict:
    from features import impact
    out = impact.latest(get_repo())
    if out is None:
        raise HTTPException(status_code=404, detail="no impact run yet: POST /api/impact/run or scripts/run_impact.py")
    return out


@app.post("/api/impact/run")
def impact_run() -> dict:
    """Cached replay of the synthetic demo population. Refresh (model calls) is CLI-only: the API has no login."""
    from features import impact
    try:
        return impact.run(get_repo(), households="demo", do_refresh=False, require_complete=True)
    except impact.CacheIncomplete as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/impact/detail/{kpi}")
def impact_detail(kpi: str) -> dict:
    from features import impact
    out = impact.latest(get_repo())
    match = next((k for k in (out or {}).get("kpis", []) if k["id"].lower() == kpi.lower()), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"KPI {kpi} not in the latest impact run")
    return {"run_id": out["run_id"], "created_at": out["created_at"], "kpi": match}


# ---------- read-only views for the UI (stored rows only; no decisions) ----------

@app.get("/api/villages")
def list_villages() -> list[dict]:
    return sorted(get_repo().fetch_all("villages"), key=lambda v: v["village_id"])


@app.get("/api/households")
def list_households() -> list[dict]:
    """Household list: profile basics, completeness, plots, latest F3 signal and latest alert. Demo households first."""
    return views.households(get_repo())


@app.get("/api/households/{hh_id}")
def get_household(hh_id: str) -> dict:
    try:
        return views.household(get_repo(), hh_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/plots/{plot_id}/series")
def get_plot_series(plot_id: str) -> dict:
    """Stored rain_series (village) and ndvi_series (plot) rows for the F2 charts."""
    try:
        return views.plot_series(get_repo(), plot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/ui_config")
def get_ui_config() -> dict:
    return views.ui_config()


@app.get("/api/support_options")
def get_support_options() -> dict:
    return views.support()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
