"""F1 interview (unit, no LLM): terms, ranges, completeness, consent, no-guess rule, questions, pipeline."""
import asyncio

import pytest

from core.schemas import ExtractedField, ExtractionOutput, HouseholdProfile, QuestionOutput, Turn
from features import f1_interview as f1
from orchestrators import selector
from tests.fakes import MemoryRepo, ScriptedEngine, demo_repo


def history(*texts):
    """Alternating farmer / agent turns, starting with the farmer."""
    return [Turn(role="user" if i % 2 == 0 else "agent", text=t) for i, t in enumerate(texts)]


def extraction(*items, terms=()):
    return ExtractionOutput(fields=[ExtractedField(field=f, value=v, evidence=e) for f, v, e in items],
                            local_terms=[{"term": t, "meaning": m} for t, m in terms])


# ---------- term normalisation ----------

def test_terms_yaml_hits_latin_and_devanagari():
    hits = {(h["term"], h["canonical"]) for h in f1.find_terms("आमची विहिरीचे पाणी आटले, bhadyane jamin, 2 gai")}
    assert ("विहिर", "well") in hits and ("bhadyane", "tenant") in hits and ("gai", "cow") in hits


def test_latin_terms_need_whole_words():
    assert not f1.find_terms("turmeric and saturday")      # 'tur' inside other words is not pigeon pea


def test_normalise_crops_and_enums():
    assert f1.normalise_text_value("Jowar, harbhara and kardai") == "sorghum, gram and safflower"
    assert f1.normalise_text_value("तूर") == "pigeon pea"
    assert f1.normalise_enum("tenure", "batai") == "sharecrop"
    assert f1.normalise_enum("irrigation", "vihir") == "well"
    assert f1.normalise_enum("irrigation", "canal") is None


# ---------- range validation ----------

def test_out_of_range_becomes_unknown_and_is_asked_first():
    h = history("I farm 80 acres of rented land")
    profile, report = f1.build_profile(extraction(("acres", "80", "80 acres"), ("tenure", "tenant", "rented land")), h)
    assert profile["acres"] is None and report["out_of_range"] == [{"field": "acres", "value": 80.0}]
    assert f1.next_target(profile, report["out_of_range"], h) == ("confirm", "acres")


@pytest.mark.parametrize("field,value", [("acres", 51), ("water_hours_per_week", 169), ("cattle_count", 51),
                                         ("goats_count", 201), ("fodder_weeks_left", 53)])
def test_pydantic_ranges(field, value):
    with pytest.raises(ValueError):
        HouseholdProfile(**{field: value})
    HouseholdProfile(**{field: value - 1})


# ---------- completeness ----------

def test_completeness_weights():
    assert f1.profile_completeness({}) == 0
    full = dict(irrigation="well", water_hours_per_week=4, fodder_weeks_left=3, cash_buffer_band="low",
                crops_last_season="soybean", cattle_count=2, tenure="tenant", acres=3)
    assert f1.profile_completeness(full) == 100
    assert f1.profile_completeness({"irrigation": "well", "tenure": "owner"}) == 30        # 20 + 10
    assert f1.profile_completeness({**full, "cash_buffer_band": None}) == 85              # 100 - 15


def test_completeness_skips_fodder_when_no_animals():
    p = dict(irrigation="borewell", water_hours_per_week=10, cash_buffer_band="low", crops_last_season="cotton",
             cattle_count=0, goats_count=0, tenure="sharecrop", acres=2)
    assert f1.profile_completeness(p) == 100 and "fodder_weeks_left" not in f1.unknown_fields(p)


def test_incomplete_flag_under_60():
    assert f1.flags_for(59) == ["incomplete"] and f1.flags_for(60) == []


# ---------- no-guess rule ----------

def test_field_without_farmer_evidence_is_dropped():
    h = history("We grow soybean.")
    profile, report = f1.build_profile(extraction(("irrigation", "well", "our well"), ("crops_last_season", "soybean", "soybean")), h)
    assert profile["irrigation"] is None and profile["crops_last_season"] == "soybean"
    assert report["dropped"][0]["field"] == "irrigation"


def test_agent_words_are_not_farmer_evidence():
    h = history("We grow soybean.", "Do you have a borewell?", "I don't know.")
    profile, _ = f1.build_profile(extraction(("irrigation", "borewell", "Do you have a borewell")), h)
    assert profile["irrigation"] is None


def test_number_must_match_evidence():
    h = history("Fodder will last 3 weeks")
    profile, report = f1.build_profile(extraction(("fodder_weeks_left", "6", "Fodder will last 3 weeks")), h)
    assert profile["fodder_weeks_left"] is None and "number not in evidence" in report["dropped"][0]["reason"]


def test_allowed_conversions_and_sums():
    h = history("रोज़ 2 घंटा पानी, घरी २ गायी आणि १ म्हैस")
    profile, _ = f1.build_profile(extraction(("water_hours_per_week", "14", "रोज़ 2 घंटा पानी"),
                                             ("cattle_count", "3", "घरी २ गायी आणि १ म्हैस")), h)
    assert profile["water_hours_per_week"] == 14 and profile["cattle_count"] == 3


def test_rainfed_derives_zero_hours_and_says_so():
    h = history("only rain, no well")
    profile, report = f1.build_profile(extraction(("irrigation", "rainfed", "only rain")), h)
    assert profile["water_hours_per_week"] == 0 and "rainfed" in report["derived"][0]


# ---------- questions ----------

def test_priority_order_and_skips_known():
    p = {f: None for f in f1.FIELD_NAMES}
    assert f1.next_target(p, [], history("hi")) == ("water", "irrigation")
    p.update(irrigation="rainfed", water_hours_per_week=0)
    assert f1.next_target(p, [], history("hi")) == ("fodder", "cattle_count")   # animals first (survey)
    p.update(cattle_count=0, goats_count=0)                        # no animals: no fodder question
    assert f1.next_target(p, [], history("hi")) == ("cash", "cash_buffer_band")
    p.update(cash_buffer_band="low", loan_due_month=3)
    assert f1.next_target(p, [], history("hi")) == ("tenure", "tenure")
    p.update(tenure="owner")
    assert f1.next_target(p, [], history("hi")) is None


def test_group_not_asked_twice():
    h = [Turn(role="user", text="hi"), Turn(role="agent", text="water?", field="irrigation"),
         Turn(role="user", text="don't know")]
    assert f1.next_target({f: None for f in f1.FIELD_NAMES}, [], h) == ("fodder", "cattle_count")


# ---------- pipeline (scripted engine) ----------

def run_turn(turns, lang="en", **engines):
    ctx = f1.build_context(turns, lang)
    return asyncio.run(selector.execute(f1, ctx, "f1", None, engine="adk", repo=demo_repo(),
                                        engines=engines, use_result_cache=False))


def test_turn_returns_question_then_final_after_three():
    eng = ScriptedEngine(agent_outputs={"extraction": extraction(("crops_last_season", "soybean", "soybean")),
                                        "question": QuestionOutput(question="Where does your water come from?")})
    r = run_turn([{"role": "user", "text": "We grow soybean."}], adk=eng)
    assert r.type == "question" and r.field_targeted == "irrigation" and r.question.startswith("Where")
    assert r.profile_so_far == {"crops_last_season": "soybean"} and r.completeness_pct == 10

    turns = [{"role": "user", "text": "We grow soybean."}]
    for f in ("irrigation", "fodder_weeks_left", "cash_buffer_band"):
        turns += [{"role": "agent", "text": "?", "field": f}, {"role": "user", "text": "I don't know."}]
    r = run_turn(turns, adk=eng)
    assert r.type == "final" and r.questions_asked == 3 and "incomplete" in r.profile["flags"]
    assert "irrigation" in r.unknown_fields and r.profile["irrigation"] is None


def test_gemini_normalised_terms_are_logged_in_trace():
    eng = ScriptedEngine(agent_outputs={"extraction": extraction(("irrigation", "well", "कुएँ"), terms=[("कुएँ", "well"), ("तूर", "pigeon pea")]),
                                        "question": QuestionOutput(question="?")})
    r = run_turn([{"role": "user", "text": "पानी कुएँ से"}], lang="hi", adk=eng)
    step = next(t for t in r.trace if t.step == "terms")
    assert [t["term"] for t in step.detail["gemini_normalised_terms"]] == ["कुएँ"]   # तूर is in terms.yaml


def test_engines_down_falls_back_to_rule_with_template_question():
    down = dict(adk=ScriptedEngine(error=RuntimeError("down")), plain=ScriptedEngine(name="plain", error=RuntimeError("down")))
    r = run_turn([{"role": "user", "text": "माझ्याकडे ३ एकर भाड्याने, पाणी विहिरीचे"}], lang="mr", **down)
    assert r.orchestrator_used == "rule" and r.type == "question"
    assert r.profile_so_far["tenure"] == "tenant" and r.profile_so_far["irrigation"] == "well" and r.profile_so_far["acres"] == 3
    assert r.question == f1.TEMPLATES["water"]["mr"]


# ---------- saving ----------

PROFILE = dict(tenure="tenant", acres=3, irrigation="well", water_hours_per_week=4, crops_last_season="soybean",
               cattle_count=3, fodder_weeks_left=3, cash_buffer_band="low", loan_due_month=12)


def fake_embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.mark.parametrize("consent", [False, None, "yes", 1])
def test_save_rejects_without_consent(consent):
    with pytest.raises(f1.ConsentError):
        f1.save_household(PROFILE, 1, consent, demo_repo(), embed=fake_embed)


def test_save_rejects_unknown_village_and_out_of_range():
    with pytest.raises(ValueError, match="does not exist"):
        f1.save_household(PROFILE, 999, True, demo_repo(), embed=fake_embed)
    with pytest.raises(ValueError):
        f1.save_household({**PROFILE, "acres": 80}, 1, True, demo_repo(), embed=fake_embed)


def test_save_is_one_insert_with_embedding_and_no_plot(monkeypatch):
    monkeypatch.setenv("EMBED_MODEL", "test-embed")
    repo = demo_repo()
    calls = []
    original = repo.insert
    repo.insert = lambda table, rows: calls.append((table, rows)) or original(table, rows)
    out = f1.save_household(PROFILE, 1, True, repo, embed=fake_embed)
    assert out["hh_id"] == "HH-NEW-001" and out["plot"] is None and "NDVI" in out["note"]
    assert len(calls) == 1 and calls[0][0] == "households"
    row = calls[0][1][0]
    assert row["profile_embedding"] == [0.1, 0.2, 0.3] and row["embed_model"] == "test-embed"
    assert row["is_synthetic"] is False and row["consent"] is True and row["profile_text_norm"]
    assert not repo.fetch_where("plots", hh_id="HH-NEW-001")
    assert f1.next_hh_id(["HH-NEW-001", "HH-NEW-007", "SYN-001"]) == "HH-NEW-008"


# ---------- audio input checks ----------

def test_audio_rejects_bad_type_and_size():
    import base64
    with pytest.raises(f1.AudioError, match="unsupported audio type"):
        f1.decode_audio(base64.b64encode(b"x").decode(), "video/mp4")
    with pytest.raises(f1.AudioError, match="limit is 2 MB"):
        f1.decode_audio(base64.b64encode(b"0" * (2 * 1024 * 1024 + 1)).decode(), "audio/wav")
    assert f1.decode_audio(base64.b64encode(b"RIFF").decode(), "audio/wav") == b"RIFF"


def _wav(samples):
    import array
    import io
    import wave
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(array.array("h", samples).tobytes())
    return b.getvalue()


def test_silent_wav_is_rejected_before_gemini():
    import math
    assert f1.wav_is_silent(_wav([0] * 16000)) is True
    assert f1.wav_is_silent(_wav([int(3000 * math.sin(i / 5)) for i in range(16000)])) is False
    assert f1.wav_is_silent(b"not a wav") is None
    with pytest.raises(f1.AudioError, match="no clear speech"):
        f1.transcribe(_wav([0] * 16000), "audio/wav", "mr")


def test_chana_is_gram_and_upload_type_check():
    assert f1.normalise_text_value("चना, chana") == "gram, gram"
    assert f1.check_audio(b"RIFF", "audio/webm;codecs=opus") == b"RIFF"
    with pytest.raises(f1.AudioError, match="unsupported"):
        f1.check_audio(b"x", "text/plain")


def test_rainfed_with_irrigation_hours_is_a_contradiction_not_zero():
    h = history("Mostly on rain, but in dry spells we take water from the neighbour's well, about 4 hours a week")
    profile, report = f1.build_profile(extraction(("irrigation", "rainfed", "Mostly on rain"),
                                                  ("water_hours_per_week", "4", "about 4 hours a week")), h)
    assert profile["irrigation"] is None and profile["water_hours_per_week"] == 4 and not report["derived"]
    assert "contradicts" in report["dropped"][-1]["reason"]
    assert f1.next_target(profile, [], h) == ("water", "irrigation")


def test_fodder_slot_asks_about_animals_first_when_counts_unknown():
    from features.f1_interview import next_target, template_question
    profile = {"irrigation": "rainfed", "water_hours_per_week": 0.0}
    group, field = next_target(profile, [], [])
    assert (group, field) == ("fodder", "cattle_count")
    assert "goats" in template_question("en", group, field) and "graze" in template_question("en", group, field)
    assert next_target({**profile, "cattle_count": 2, "goats_count": 0}, [], [])[1] == "fodder_weeks_left"
    assert next_target({**profile, "cattle_count": 0, "goats_count": 0}, [], [])[0] == "cash"   # no animals: skip
