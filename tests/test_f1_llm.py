"""F1 acceptance (llm, calls Vertex): 3 Marathi + 3 Hindi samples give a valid schema with <= 3 questions."""
import asyncio
from pathlib import Path

import pytest

from core.schemas import HouseholdProfile, InterviewResult, Turn
from features import f1_interview as f1
from orchestrators import selector

SAMPLES = Path(__file__).parent / "f1_samples"


def interview(path: Path) -> tuple[InterviewResult, int]:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    history, answers, questions = [Turn(role="user", text=lines[0])], lines[1:], 0
    for _ in range(5):
        lang = path.name[:2] if path.name[:2] in f1.LANGS else "en"
        r = asyncio.run(selector.execute(f1, f1.build_context(history, lang), "f1", None, use_result_cache=False))
        InterviewResult.model_validate(r.model_dump())
        if r.type == "final":
            return r, questions
        questions += 1
        history.append(Turn(role="agent", text=r.question, field=r.field_targeted))
        history.append(Turn(role="user", text=answers.pop(0) if answers else "माहीत नाही"))
    raise AssertionError("interview did not finish")


@pytest.mark.llm
@pytest.mark.parametrize("name", ["mr_1", "mr_2", "mr_3", "hi_1", "hi_2", "hi_3"])
def test_sample_gives_valid_profile_with_at_most_3_questions(name):
    r, questions = interview(SAMPLES / f"{name}.txt")
    assert r.orchestrator_used in ("adk", "plain"), r.fallback_reason
    assert questions <= 3 and r.questions_asked <= 3
    HouseholdProfile(**{k: r.profile[k] for k in f1.FIELD_NAMES})   # ranges hold
    assert set(r.unknown_fields) <= set(f1.FIELD_NAMES)
    assert all(r.profile[f] is None for f in r.unknown_fields)
    assert r.profile["flags"] == f1.flags_for(r.completeness_pct)


@pytest.mark.llm
def test_marathi_audio_sample_is_transcribed_and_interviewed():
    """tests/f1_samples/mr_1_audio.wav is SYNTHETIC speech (Gemini TTS) of mr_1.txt's opening line."""
    data = (SAMPLES / "mr_1_audio.wav").read_bytes()
    text, _ = f1.transcribe(data, "audio/wav", "mr")
    assert "एकर" in text and "भाड्याने" in text
    r = asyncio.run(selector.execute(f1, f1.build_context([Turn(role="user", text=text)], "mr"), "f1", None,
                                     use_result_cache=False))
    assert r.type == "question" and r.profile_so_far.get("tenure") == "tenant"


@pytest.mark.llm
def test_bad_range_values_are_asked_again():
    r, questions = interview(SAMPLES / "bad_range.txt")
    assert r.profile["acres"] == 5 and r.profile["cattle_count"] == 10 and questions <= 3


@pytest.mark.llm
def test_neighbours_well_is_not_rainfed_zero():
    r, _ = interview(SAMPLES / "neighbour_well.txt")
    assert r.profile["irrigation"] == "well" and r.profile["water_hours_per_week"] == 4
    assert not r.profile["derived"]
