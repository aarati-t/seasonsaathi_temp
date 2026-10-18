"""F1 Household Risk Profile (interview agent), SPEC section 6 + P3 refinements.

Stateless: each call gets the whole history and returns either the next question or the final profile.
Per turn (ADK primary, plain fallback via orchestrators/selector.execute):
  1. code: glossary hits from config/terms.yaml (applied before Gemini)
  2. extractor agent (Gemini): fields with the farmer's exact words as evidence + unlisted local terms
  3. code verifier: evidence must appear in what the farmer said (never guess), numbers must match the
     evidence, values normalised, Pydantic ranges (out of range -> unknown and asked)
  4. code: completeness (weighted), next question by priority, max 3 questions
  5. interviewer agent (Gemini): phrases that one question in the farmer's language
"""
import base64
from datetime import datetime, timezone
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from google.genai import types
from pydantic import ValidationError

from core import llm, settings
from core.schemas import (ExtractedField, ExtractionOutput, HouseholdProfile, InterviewResult, LocalTerm,
                          QuestionOutput, TraceStep, TranscriptOutput, Turn)
from orchestrators.base import AgentSpec, Engine, load_prompt

log = logging.getLogger("seasonsaathi.f1")
if not log.handlers:
    log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)

ROOT = Path(__file__).resolve().parent.parent
FEATURE = "f1"
LANGS = {"mr": "Marathi", "hi": "Hindi", "en": "English"}

PROFILE_FIELDS = [
    ("tenure", "tenure"),
    ("acres", "acres"),
    ("irrigation", "irrigation"),
    ("water_hours_per_week", "water hours per week"),
    ("crops_last_season", "crops last season"),
    ("cattle_count", "cattle"),
    ("goats_count", "goats"),
    ("fodder_weeks_left", "fodder weeks left"),
    ("off_farm_income", "off-farm income"),
    ("loan_due_month", "loan due month"),
    ("cash_buffer_band", "cash buffer"),
    ("primary_cultivator_gender", "primary cultivator"),
    ("soil_type_farmer", "soil type"),
]
OPTIONAL = {"soil_type_farmer"}   # P7: recorded only if the farmer says it; never asked, not in unknown_fields
FIELD_NAMES = [f for f, _ in PROFILE_FIELDS]
NUMERIC = {"acres", "water_hours_per_week", "fodder_weeks_left"}
INTEGER = {"cattle_count", "goats_count", "loan_due_month"}
ENUMS = {"tenure": {"owner", "tenant", "sharecrop"}, "irrigation": {"rainfed", "well", "borewell"},
         "cash_buffer_band": {"low", "med", "high"}, "primary_cultivator_gender": {"man", "woman", "both"}}
# Question priority (refinement): water source/hours -> fodder weeks -> cash buffer/loan month -> tenure.
# The fodder slot first asks WHICH animals (counts) when they are unknown, never "fodder left" for unknown animals
# (field survey: cattle are often grazed and goats preferred).
GROUPS = [("water", ["irrigation", "water_hours_per_week"]), ("fodder", ["cattle_count", "goats_count", "fodder_weeks_left"]),
          ("cash", ["cash_buffer_band", "loan_due_month"]), ("tenure", ["tenure"])]
FIELD_GROUP = {f: g for g, fs in GROUPS for f in fs}
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

TEMPLATES = {  # rule fallback questions (no Gemini)
    "water": {"mr": "तुमच्याकडे पाणी कुठून येते — फक्त पाऊस, विहीर की बोअरवेल? आठवड्याला किती तास पाणी मिळते?",
              "hi": "आपके पास पानी कहाँ से आता है — सिर्फ बारिश, कुआँ या बोरवेल? हफ्ते में कितने घंटे पानी मिलता है?",
              "en": "Where does your water come from: rain only, a well or a borewell? How many hours of water do you get per week?"},
    "animals": {"mr": "घरी गायी, म्हशी किंवा शेळ्या आहेत का? किती? चारा किती आठवडे पुरेल, की जनावरे चरायला जातात?",
                "hi": "घर में गाय, भैंस या बकरियाँ हैं? कितनी? चारा कितने हफ्ते चलेगा, या पशु चरने जाते हैं?",
                "en": "Do you have cows, buffaloes or goats? How many? How many weeks will fodder last, or do they graze?"},
    "fodder": {"mr": "जनावरांसाठी चारा अजून किती आठवडे पुरेल?",
               "hi": "पशुओं के लिए चारा अभी कितने हफ्ते चलेगा?",
               "en": "How many more weeks will your fodder last for the animals?"},
    "cash": {"mr": "अडचणीच्या वेळी घरात काही बचत आहे का? कर्जाचा हप्ता कोणत्या महिन्यात आहे?",
             "hi": "मुश्किल समय के लिए घर में कुछ बचत है? कर्ज़ की किस्त किस महीने में है?",
             "en": "Do you have any savings for a bad month? In which month is a loan repayment due?"},
    "tenure": {"mr": "ही जमीन तुमची स्वतःची आहे, भाड्याने/खंडाने घेतली आहे की बटाईने करता?",
               "hi": "यह ज़मीन आपकी अपनी है, किराये/ठेके पर है या बटाई पर?",
               "en": "Is the land your own, rented, or sharecropped?"},
    "confirm": {"mr": "कृपया {label} पुन्हा सांगाल का? आधीचा आकडा जुळत नाही.",
                "hi": "कृपया {label} फिर से बताएँगे? पहले वाला आंकड़ा सही नहीं लगता.",
                "en": "Could you tell me the {label} again? The number before did not look right."},
}


# ---------- glossary (config/terms.yaml) ----------

@lru_cache(maxsize=1)
def load_terms() -> list[tuple[str, str, str]]:
    """[(alias, category, canonical)], longest aliases first."""
    data = yaml.safe_load((ROOT / "config" / "terms.yaml").read_text())
    out = [(alias, category, canonical) for category, entries in data.items()
           for canonical, aliases in entries.items() for alias in aliases]
    return sorted(out, key=lambda t: -len(t[0]))


def _alias_pattern(alias: str) -> str:
    # Latin: whole word. Devanagari: word start only, so inflections match (विहीर -> विहिरीचे needs its own alias).
    if re.search(r"[ऀ-ॿ]", alias):
        return r"(?<![ऀ-ॿ])" + re.escape(alias)
    return r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])"


def find_terms(text: str, extra: list | tuple = ()) -> list[dict]:
    """Glossary hits in text: [{term, category, canonical}] (each alias once).
    extra: human-approved terms from term_review (learning loop), used like config/terms.yaml."""
    hits, seen = [], set()
    for alias, category, canonical in sorted(list(load_terms()) + list(extra), key=lambda t: -len(t[0])):
        if alias.lower() in seen:
            continue
        if re.search(_alias_pattern(alias), text, flags=re.IGNORECASE):
            hits.append({"term": alias, "category": category, "canonical": canonical})
            seen.add(alias.lower())
    return hits


def normalise_text_value(value: str, extra: list | tuple = ()) -> str:
    """Replace glossary terms in a free-text value (e.g. crops) by their canonical names."""
    out = value
    for alias, _cat, canonical in sorted(list(load_terms()) + list(extra), key=lambda t: -len(t[0])):
        out = re.sub(_alias_pattern(alias), canonical, out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip().lower()


def normalise_enum(field: str, value: str, extra: list | tuple = ()) -> str | None:
    v = value.strip().lower()
    if v in ENUMS[field]:
        return v
    for hit in find_terms(v, extra):
        if hit["canonical"] in ENUMS[field]:
            return hit["canonical"]
    return None


# ---------- no-guess verifier ----------

def _clean(text: str) -> str:
    text = (text or "").translate(DEVANAGARI_DIGITS).lower()
    text = re.sub(r"[\"'“”‘’.,;:!?()\[\]{}।॥\-–—/]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_supported(evidence: str, farmer_text: str) -> bool:
    """Evidence must be words the farmer actually said: a substring, or 80% of its words present."""
    ev, said = _clean(evidence), _clean(farmer_text)
    if not ev:
        return False
    if ev in said:
        return True
    words = ev.split()
    said_words = set(said.split())
    return sum(w in said_words for w in words) / len(words) >= 0.8


def numbers_in(text: str) -> list[float]:
    return [float(n) for n in re.findall(r"\d+(?:\.\d+)?", (text or "").translate(DEVANAGARI_DIGITS))]


def number_matches_evidence(value: float, evidence: str) -> bool:
    """If the evidence has digits, the value must be one of them, their sum (cattle = cows + buffaloes),
    or a per-day x7 / per-month x4-4.3 conversion (e.g. '2 तास रोज' -> 14 h/week).
    Evidence with number words only is accepted."""
    nums = numbers_in(evidence)
    if not nums:
        return True
    candidates = [c for n in nums for c in (n, n * 7, n * 4, n * 4.3)] + [sum(nums)]
    return any(abs(value - c) <= 0.5 for c in candidates)


def farmer_text(history: list[Turn]) -> str:
    return "\n".join(t.text for t in history if t.role == "user")


def build_profile(extraction: ExtractionOutput, history: list[Turn], extra_terms: list | tuple = ()) -> tuple[dict, dict]:
    """Validate Gemini's extraction in code. Returns (profile, report)."""
    said = farmer_text(history)
    profile: dict = {f: None for f in FIELD_NAMES}
    report = {"accepted": {}, "dropped": [], "out_of_range": [], "derived": []}
    for item in extraction.fields:  # later items (later answers) override earlier ones
        f, raw = item.field, (item.value or "").strip()
        if not raw or raw.lower() in ("null", "none", "unknown"):
            continue
        if not evidence_supported(item.evidence, said):
            report["dropped"].append({"field": f, "value": raw, "reason": f"evidence not in farmer's words: {item.evidence!r}"})
            continue
        if f in ENUMS:
            value = normalise_enum(f, raw, extra_terms)
            if value is None:
                report["dropped"].append({"field": f, "value": raw, "reason": f"not one of {sorted(ENUMS[f])}"})
                continue
        elif f in NUMERIC or f in INTEGER:
            nums = numbers_in(raw)
            if not nums:
                report["dropped"].append({"field": f, "value": raw, "reason": "not a number"})
                continue
            value = nums[0]
            if not number_matches_evidence(value, item.evidence):
                report["dropped"].append({"field": f, "value": raw, "reason": f"number not in evidence {item.evidence!r}"})
                continue
            if f in INTEGER:
                if value != int(value):
                    report["dropped"].append({"field": f, "value": raw, "reason": "must be a whole number"})
                    continue
                value = int(value)
        elif f in ("crops_last_season", "soil_type_farmer"):
            value = normalise_text_value(raw, extra_terms)
        else:
            value = raw
        try:
            HouseholdProfile(**{f: value})
        except ValidationError:
            profile[f] = None
            report["out_of_range"].append({"field": f, "value": value})
            report["accepted"].pop(f, None)
            continue
        profile[f] = value
        report["accepted"][f] = {"value": value, "evidence": item.evidence}
        report["out_of_range"] = [o for o in report["out_of_range"] if o["field"] != f]
    if profile["irrigation"] == "rainfed" and (profile["water_hours_per_week"] or 0) > 0:
        # Contradiction (e.g. "rain only, but neighbour's well 4 h/week"): do not pick one; ask again.
        report["dropped"].append({"field": "irrigation", "value": "rainfed",
                                  "reason": f"rainfed contradicts {profile['water_hours_per_week']:g} irrigation hours/week; asking again"})
        report["accepted"].pop("irrigation", None)
        profile["irrigation"] = None
    if profile["irrigation"] == "rainfed" and profile["water_hours_per_week"] is None:
        profile["water_hours_per_week"] = 0.0
        report["derived"].append("water_hours_per_week = 0 because irrigation is rainfed (no irrigation hours)")
    return profile, report


# ---------- completeness and questions ----------

def profile_completeness(hh: dict) -> int:
    """Weighted share of known fields (weights in config/thresholds.yaml). Fodder is not applicable, and
    left out of the denominator, when the household is known to have no cattle and no goats."""
    weights = dict(settings.thresholds()["profile"]["completeness_weights"])
    if hh.get("cattle_count") == 0 and hh.get("goats_count") == 0:
        weights.pop("fodder_weeks_left", None)
    total = sum(weights.values())
    known = sum(w for f, w in weights.items() if hh.get(f) is not None and hh.get(f) == hh.get(f))
    return round(known / total * 100) if total else 0


def flags_for(pct: int) -> list[str]:
    return ["incomplete"] if pct < settings.thresholds()["profile"]["min_completeness_pct"] else []


def asked_groups(history: list[Turn]) -> set[str]:
    return {FIELD_GROUP.get(t.field, t.field) for t in history if t.role == "agent" and t.field}


def next_target(profile: dict, out_of_range: list[dict], history: list[Turn]) -> tuple[str, str] | None:
    """(group, field) to ask next, or None. Out-of-range values first, then the priority groups."""
    done = asked_groups(history)
    for o in out_of_range:
        if f"confirm:{o['field']}" not in {t.field for t in history if t.role == "agent"}:
            return "confirm", o["field"]
    no_animals = profile.get("cattle_count") == 0 and profile.get("goats_count") == 0
    for group, fields in GROUPS:
        if group in done or (group == "fodder" and no_animals):
            continue
        unknown = [f for f in fields if profile.get(f) is None]
        if unknown:
            return group, unknown[0]
    return None


def unknown_fields(profile: dict) -> list[str]:
    no_animals = profile.get("cattle_count") == 0 and profile.get("goats_count") == 0
    return [f for f in FIELD_NAMES if profile.get(f) is None and f not in OPTIONAL
            and not (f == "fodder_weeks_left" and no_animals)]


# ---------- context, prompts, agents ----------

@dataclass
class F1Context:
    history: list[Turn]
    lang: str
    repo: object = None   # for learning-loop vocabulary (term_review); None = glossary file only


def build_context(history: list[dict | Turn], lang: str, repo=None) -> F1Context:
    turns = [t if isinstance(t, Turn) else Turn(**t) for t in history]
    if not turns or turns[0].role != "user":
        raise ValueError("history must start with the farmer's opening message (role 'user')")
    if lang not in LANGS:
        raise ValueError(f"lang must be one of {sorted(LANGS)}")
    return F1Context(history=turns, lang=lang, repo=repo)


def _history_text(history: list[Turn]) -> str:
    return "\n".join(f"{'FARMER' if t.role == 'user' else 'AGENT'}: {t.text}" for t in history)


def extractor_spec(ctx: F1Context, glossary: list[dict]) -> AgentSpec:
    gloss = "\n".join(f"- {g['term']} = {g['canonical']} ({g['category']})" for g in glossary) or "- (none found)"
    prompt = load_prompt("extractor", HISTORY=_history_text(ctx.history), GLOSSARY=gloss,
                         FIELDS=", ".join(FIELD_NAMES))
    return AgentSpec(name="extractor", instruction=lambda state: prompt, output_schema=ExtractionOutput,
                     output_key="extraction", title="🧾 Extractor")


def interviewer_spec(ctx: F1Context, group: str, field: str, profile: dict) -> AgentSpec:
    known = {k: v for k, v in profile.items() if v is not None}
    labels = dict(PROFILE_FIELDS)
    target = (f"confirm the value of '{labels[field]}' (the number given was not plausible)" if group == "confirm"
              else f"{group}: " + ", ".join(labels[f] for f in dict(GROUPS)[group] if profile.get(f) is None))
    prompt = load_prompt("interviewer", LANGUAGE=LANGS[ctx.lang], TARGET=target, KNOWN=str(known),
                         HISTORY=_history_text(ctx.history))
    return AgentSpec(name="interviewer", instruction=lambda state: prompt, output_schema=QuestionOutput,
                     output_key="question", title="❓ Interviewer")


def template_question(lang: str, group: str, field: str) -> str:
    if group == "fodder" and field in ("cattle_count", "goats_count"):
        group = "animals"
    return TEMPLATES[group][lang].format(label=dict(PROFILE_FIELDS).get(field, field))


# ---------- the turn ----------

def _finish(ctx: F1Context, profile: dict, report: dict, glossary: list[dict], gemini_terms: list[dict],
            trace: list[TraceStep], orchestrator: str) -> tuple[InterviewResult, tuple | None]:
    pct = profile_completeness(profile)
    asked = sum(t.role == "agent" for t in ctx.history)
    target = next_target(profile, report["out_of_range"], ctx.history) \
        if asked < settings.thresholds()["interview"]["max_questions"] else None
    trace.append(TraceStep(step="verifier_code", backend="code",
                           title="✅ No-guess check: " + (f"{len(report['dropped'])} field(s) dropped" if report["dropped"] else "all fields supported"),
                           detail={"accepted": report["accepted"], "dropped": report["dropped"],
                                   "out_of_range": report["out_of_range"], "derived": report["derived"]}))
    trace.append(TraceStep(step="completeness", backend="code", title=f"📊 Completeness {pct}%",
                           detail={"completeness_pct": pct, "flags": flags_for(pct), "questions_asked": asked,
                                   "next": {"group": target[0], "field": target[1]} if target else None}))
    if gemini_terms:
        log.info("f1 gemini_normalised_terms=%s", [t["term"] for t in gemini_terms])
    meta = dict(trace=trace, orchestrator_used=orchestrator, completeness_pct=pct, questions_asked=asked)
    known = {k: v for k, v in profile.items() if v is not None}
    if target:
        return InterviewResult(type="question", field_targeted=target[1] if target[0] != "confirm" else f"confirm:{target[1]}",
                               profile_so_far=known, **meta), target
    final = {**profile, "flags": flags_for(pct), "derived": report["derived"]}
    return InterviewResult(type="final", profile=final, unknown_fields=unknown_fields(profile), **meta), None


def _glossary_step(glossary: list[dict], gemini_terms: list[dict]) -> TraceStep:
    return TraceStep(step="terms", backend="code", title=f"📖 Glossary: {len(glossary)} term(s) from terms.yaml"
                     + (f", {len(gemini_terms)} normalised by Gemini" if gemini_terms else ""),
                     detail={"terms_yaml": glossary, "gemini_normalised_terms": gemini_terms})


async def run_with_engine(engine: Engine, ctx: F1Context, inject: str | None = None) -> InterviewResult:
    from features import learning

    approved = learning.approved_terms(ctx.repo)
    glossary = find_terms(farmer_text(ctx.history), approved)
    trace = [TraceStep(step="engine", title=f"Engine: {engine.name}")]
    out, steps = await engine.run_agents(FEATURE, [extractor_spec(ctx, glossary)], {})
    extraction: ExtractionOutput = out["extraction"]
    trace += steps
    gemini_terms = [t.model_dump() for t in extraction.local_terms if not find_terms(t.term, approved)]
    queued = learning.record_pending_terms(ctx.repo, gemini_terms) if ctx.repo is not None else []
    trace.append(_glossary_step(glossary, gemini_terms))
    if queued:
        trace[-1].detail["queued_for_review"] = queued
    profile, report = build_profile(extraction, ctx.history, approved)
    result, target = _finish(ctx, profile, report, glossary, gemini_terms, trace, engine.name)
    if target:
        out, steps = await engine.run_agents(FEATURE, [interviewer_spec(ctx, target[0], target[1], profile)], {})
        result.trace[len(result.trace):] = steps
        question = (out["question"].question or "").strip()
        result.question = question or template_question(ctx.lang, *target)
    return result


ANIMAL_FIELDS = {"cow": "cattle_count", "buffalo": "cattle_count", "goat": "goats_count"}


def rule_extract(history: list[Turn]) -> ExtractionOutput:
    """No-Gemini fallback: glossary terms and simple number patterns only; evidence = the matched words."""
    fields = []
    for t in history:
        if t.role != "user":
            continue
        text = t.text.translate(DEVANAGARI_DIGITS)
        for hit in find_terms(text):
            if hit["category"] == "irrigation":
                fields.append(ExtractedField(field="irrigation", value=hit["canonical"], evidence=hit["term"]))
            elif hit["category"] == "tenure":
                fields.append(ExtractedField(field="tenure", value=hit["canonical"], evidence=hit["term"]))
            elif hit["category"] == "crops":
                fields.append(ExtractedField(field="crops_last_season", value=hit["canonical"], evidence=hit["term"]))
            elif hit["category"] == "soil":
                fields.append(ExtractedField(field="soil_type_farmer", value=hit["canonical"], evidence=hit["term"]))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(acres?|एकर|एकड़)", text, flags=re.IGNORECASE)
        if m:
            fields.append(ExtractedField(field="acres", value=m.group(1), evidence=m.group(0)))
        counts: dict[str, int] = {}
        for alias, category, canonical in load_terms():
            if category != "animals":
                continue
            for m in re.finditer(r"(\d+)\s*" + re.escape(alias), text, flags=re.IGNORECASE):
                counts[ANIMAL_FIELDS[canonical]] = counts.get(ANIMAL_FIELDS[canonical], 0) + int(m.group(1))
                evidence = m.group(0)
        for f, n in counts.items():
            fields.append(ExtractedField(field=f, value=str(n), evidence=evidence if len(counts) == 1 else text))
    return ExtractionOutput(fields=fields, local_terms=[])


def rule_result(ctx: F1Context) -> InterviewResult:
    glossary = find_terms(farmer_text(ctx.history))
    trace = [TraceStep(step="engine", backend="code", title="Engine: rule fallback (no AI)"),
             _glossary_step(glossary, [])]
    profile, report = build_profile(rule_extract(ctx.history), ctx.history)
    result, target = _finish(ctx, profile, report, glossary, [], trace, "rule")
    if target:
        result.question = template_question(ctx.lang, *target)
    result.notes.append("Rule result: AI unavailable; only glossary terms and simple numbers were read")
    return result


# ---------- audio ----------

AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3"}
MAX_AUDIO_BYTES = 2 * 1024 * 1024


class AudioError(ValueError):
    pass


class NoSpeechError(AudioError):
    """The recording has no usable speech (client problem, HTTP 422)."""


def check_audio(data: bytes, mime_type: str) -> bytes:
    """Type and size rules for raw audio bytes (file upload path)."""
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    if mime_type not in AUDIO_TYPES:
        raise AudioError(f"unsupported audio type {mime_type!r}; use webm, ogg, wav or mp3")
    if not data:
        raise AudioError("audio is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError(f"audio is {len(data) / 1024 / 1024:.1f} MB; the limit is 2 MB")
    return data


def decode_audio(audio_b64: str, mime_type: str) -> bytes:
    mime_type = (mime_type or "").split(";")[0].strip().lower()
    if mime_type not in AUDIO_TYPES:
        raise AudioError(f"unsupported audio type {mime_type!r}; use webm, ogg, wav or mp3")
    try:
        data = base64.b64decode(audio_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise AudioError(f"audio is not valid base64: {exc}") from None
    if not data:
        raise AudioError("audio is empty")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError(f"audio is {len(data) / 1024 / 1024:.1f} MB; the limit is 2 MB")
    return data


WAV_SILENCE_RMS = 100  # 16-bit PCM; quiet room noise is a few hundred, digital silence is ~0


def wav_is_silent(data: bytes) -> bool | None:
    """True/False for PCM WAV; None if not a WAV we can read (webm/ogg/mp3 are checked by Gemini only)."""
    import array
    import io
    import math
    import wave

    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                return None
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        return None
    samples = array.array("h", frames[: len(frames) // 2 * 2])
    if not samples:
        return True
    return math.sqrt(sum(s * s for s in samples) / len(samples)) < WAV_SILENCE_RMS


def transcribe(data: bytes, mime_type: str, lang: str) -> tuple[str, TraceStep]:
    """Gemini on Vertex as multimodal input. Raises AudioError with a clear message on failure."""
    no_speech = "no clear speech found in the audio (silent, noise or too unclear); please record again or type the answer"
    if wav_is_silent(data):
        raise NoSpeechError(no_speech)
    prompt = load_prompt("transcribe", LANGUAGE=LANGS[lang])
    model = llm.gemini_model()
    config = types.GenerateContentConfig(temperature=0, seed=42, thinking_config=llm.thinking_config(model),
                                         response_mime_type="application/json", response_schema=TranscriptOutput)
    contents = [types.Content(role="user", parts=[types.Part.from_bytes(data=data, mime_type=mime_type),
                                                  types.Part(text=prompt)])]
    t0 = time.perf_counter()
    try:
        resp = llm.generate(contents, config, key_parts=("transcribe", hashlib.sha256(data).hexdigest(), lang, prompt),
                            role="transcribe")
        out = TranscriptOutput.model_validate_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        raise AudioError(f"audio transcription failed: {type(exc).__name__}: {str(exc)[:200]}") from None
    text = out.transcript.strip()
    if not out.speech_detected or not text:
        raise NoSpeechError(no_speech)
    return text, TraceStep(step="transcribe", title="🎙️ Transcribed audio", detail={"transcript": text, "bytes": len(data)},
                           ms=round((time.perf_counter() - t0) * 1000))


# ---------- saving ----------

class ConsentError(ValueError):
    pass


def next_hh_id(existing: list[str]) -> str:
    nums = [int(m.group(1)) for i in existing if (m := re.fullmatch(r"HH-NEW-(\d+)", i))]
    return f"HH-NEW-{max(nums, default=0) + 1:03d}"


def save_household(profile: dict, village_id, consent, repo, embed=None) -> dict:
    """Validate, embed, then ONE insert (embedding and embed_model included). No plot is created."""
    if consent is not True:
        raise ConsentError("consent must be true to save a profile")
    if not repo.fetch_where("villages", village_id=village_id):
        raise ValueError(f"village_id {village_id} does not exist (villages are entered by hand; none are created)")
    clean = HouseholdProfile(**{k: profile.get(k) for k in FIELD_NAMES}).model_dump()  # raises on out-of-range
    text = build_profile_text(clean)
    embed = embed or llm.embed
    model = llm.require_env("EMBED_MODEL")
    vector = embed([text])[0]
    hh_id = next_hh_id([h["hh_id"] for h in repo.fetch_all("households")])
    row = {"hh_id": hh_id, "village_id": int(village_id), **clean, "consent": True, "profile_text_norm": text,
           "profile_embedding": list(vector), "embed_model": model, "is_synthetic": False,
           "created_at": datetime.now(timezone.utc).isoformat()}
    repo.insert("households", [row])
    pct = profile_completeness(clean)
    return {"hh_id": hh_id, "completeness_pct": pct, "flags": flags_for(pct), "embed_model": model,
            "embedding_dim": len(vector), "plot": None,
            "note": "no plot created: crop health (NDVI) unavailable for this household"}


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and value != value) or value == ""


def build_profile_text(hh: dict) -> str:
    """Deterministic English profile text used for embeddings. Unknown fields say 'unknown'; optional fields
    (soil type) are left out when unknown, so profiles saved before P7 keep the same text."""
    parts = []
    for field, label in PROFILE_FIELDS:
        value = hh.get(field)
        if _is_missing(value) and field in OPTIONAL:
            continue
        if _is_missing(value):
            value = "unknown"
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        parts.append(f"{label}: {value}")
    return "; ".join(parts)
