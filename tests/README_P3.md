# P3 checks (F1 interview): how to run them and what you should see

Run from the repo root with the venv active. Outputs are from real runs on 2026-10-18 (Vertex AI,
`gemini-3.5-flash`, ADK engine). Gemini's question wording varies; **fields, numbers, question count,
completeness, flags and unknown_fields should match.** Repeat runs are served from the LLM cache
(`Gemini calls: 0`); add `--no-cache` for live calls (~2 calls per turn: extractor + interviewer).

---

## 0. Tests

| Command | Calls Vertex? | Expected |
|---|---|---|
| `pytest -m unit` | no | `81 passed` (F1 adds 32: terms, ranges, completeness, consent, no-guess, questions, pipeline, audio checks) |
| `pytest -m llm tests/test_f1_llm.py` | **yes** (cached after first run) | `7 passed`: mr_1..3 + hi_1..3 valid schema with ≤ 3 questions, + the Marathi audio sample |

---

## 1. `python scripts/run_f1.py tests/f1_samples/mr_1.txt` (Marathi, 3 questions)
```
👩‍🌾 FARMER: माझ्याकडे ३ एकर भाड्याने घेतलेली जमीन आहे. खरीपात सोयाबीन आणि तूर घेतली. घरी २ गायी आणि १ म्हैस आहे. पाणी विहिरीचे आहे.
   🧾 Extractor (vertex/gemini-3.5-flash): tenure='tenant', acres='3', crops_last_season='soybean, pigeon pea', cattle_count='3', irrigation='well'
   📖 terms.yaml: भाड्याने→tenant, विहिर→well, म्हैस→buffalo, गायी→cow, तूर→pigeon pea, गाय→cow
   📖 normalised by Gemini (not in terms.yaml): सोयाबीन→soybean, खरीपात→in Kharif season
   ✅ No-guess check: all fields supported
   📊 Completeness 55% flags=['incomplete'] questions_asked=0 next=water:water_hours_per_week
🧑‍💼 AGENT (water_hours_per_week): तुमच्या विहिरीतून पिकाला आठवड्यातून साधारण किती तास पाणी मिळते?
👩‍🌾 FARMER: आता आठवड्याला फक्त ४ तास पाणी मिळते.
   📊 Completeness 70% ... next=fodder:fodder_weeks_left
🧑‍💼 AGENT (fodder_weeks_left): तुमच्याकडे असलेल्या जनावरांसाठी आता किती आठवडे पुरेल एवढा चारा शिल्लक आहे?
👩‍🌾 FARMER: चारा अजून ३ आठवडे पुरेल.
   📊 Completeness 85% ... next=cash:cash_buffer_band
🧑‍💼 AGENT (cash_buffer_band): अडचणीच्या काळासाठी तुमच्याकडे काही रोख रक्कम शिल्लक आहे का? ...
👩‍🌾 FARMER: बचत काहीच नाही, डिसेंबरमध्ये बँकेचा हप्ता आहे.
   📊 Completeness 100% flags=[] questions_asked=3 next=final

Final profile (questions asked: 3, completeness 100%, flags []):
   tenure tenant | acres 3.0 | irrigation well | water_hours_per_week 4.0 | crops_last_season soybean, pigeon pea
   cattle_count 3 (2 cows + 1 buffalo) | fodder_weeks_left 3.0 | loan_due_month 12 | cash_buffer_band low
   unknown_fields: ['goats_count', 'off_farm_income', 'primary_cultivator_gender']
   engine=adk backend=vertex model=gemini-3.5-flash fallback_reason=None
```
Check: questions in Marathi, in priority order (water → fodder → cash); goats/gender never asked and
left null (**not guessed**); terms.yaml hits listed; terms Gemini interpreted listed separately.

## 2. All samples (`python scripts/run_f1.py tests/f1_samples/<name>.txt`)

| Sample | Questions | Completeness | Points to check |
|---|---|---|---|
| mr_1 | 3 | 100% | as above |
| mr_2 | 2 (fodder, cash) | 100% | rainfed → `derived: water_hours_per_week = 0`; ज्वारी/हरभरा → sorghum, gram; "दोन आठवडे" (word) → 2 |
| mr_3 | 1 (cash) | 100% | बटाईने → sharecrop; "गाय, म्हैस, शेळी काहीच नाही" → cattle 0, goats 0, so **no fodder question** and fodder not counted |
| hi_1 | 3 | 100% | कुएँ → well (logged as Gemini-normalised); "एक महीना" → 4 weeks |
| hi_2 | 2 (fodder, cash) | 90% | ज्वार/चना → sorghum, gram (terms.yaml); "थोड़ी बचत" → cash low; cattle unknown → in unknown_fields |
| hi_3 | 2 (fodder, cash) | 100% | ठेके पर → tenant (Gemini); "रोज़ 1 घंटा" → 7 h/week (x7 conversion accepted by the no-guess check) |
| en_1 | 3 | 100% | tur → pigeon pea (terms.yaml) |
| thin_1 | 3 | **10%**, flags `['incomplete']` | "I don't know" ×3 → nothing filled in; 11 unknown_fields |
| bad_range | 3 (confirm acres, confirm cattle, fodder) | 85% | "500 acres", "100 cows" → out of range (limits 50 / 50) → unknown and **asked again first**; answers "5 acres", "10 cows" accepted |
| neighbour_well | 2 (fodder, cash) | 100% | "mostly rain, but neighbour's well 4 h/week" → **irrigation well, 4 h/week** (not rainfed / 0); "a little savings" → cash low. If Gemini ever returns rainfed + hours > 0, code drops irrigation as a contradiction and asks the water question again |

## 3. Save: `python scripts/run_f1.py tests/f1_samples/mr_1.txt --save --consent --village V01`
```
Saved: {"hh_id": "HH-NEW-001", "completeness_pct": 100, "flags": [], "embed_model": "gemini-embedding-001",
        "embedding_dim": 3072, "plot": null, "note": "no plot created: crop health (NDVI) unavailable for this household"}
```
- Without `--consent`: `--save needs --consent (farmer gave consent) and --village (e.g. --village V01)`.
- DB row: `HH-NEW-001 | 1 | well | 3 | is_synthetic=f | consent=t | gemini-embedding-001 | 3072`, no row in `plots`.
- Remove a test save: `psql seasonsaathi -c "delete from households where hh_id='HH-NEW-001'"`.

Then `python scripts/run_f3.py HH-NEW-001`:
```
📊 Confidence lowered: crop health (NDVI) unavailable: crop health (NDVI) unavailable for this household (no plot location on record)
  note: Confidence lowered: Chana only HIGH -> MED (crop health (NDVI) unavailable for this household)
```

## 4. API (`uvicorn app.main:app --port 8080`)
```bash
curl -X POST localhost:8080/api/f1/interview -H 'content-type: application/json' \
  -d '{"history":[{"role":"user","text":"We grow soybean."}],"lang":"en"}'
# -> {"type":"question","field_targeted":"irrigation","completeness_pct":10,"profile_so_far":{"crops_last_season":"soybean"},...}

curl -X POST localhost:8080/api/households -H 'content-type: application/json' \
  -d '{"profile":{"irrigation":"well"},"village_id":1,"consent":false}'
# -> 400 {"detail":"consent must be true to save a profile"}
# village_id 99 -> 422 {"detail":"village_id 99 does not exist (villages are entered by hand; none are created)"}
```

## 5. Audio: `POST /api/f1/audio` (file upload or JSON + base64; webm/ogg/wav/mp3 ≤ 2 MB)
`tests/f1_samples/mr_1_audio.wav` is **SYNTHETIC speech** (Gemini TTS reading mr_1's opening line).
```bash
# file upload (optional fields: history as JSON text, lang, engine)
curl -X POST localhost:8080/api/f1/audio -F "file=@tests/f1_samples/mr_1_audio.wav;type=audio/wav" -F lang=mr
# or JSON + base64
python -c "import base64,json; print(json.dumps({'audio_b64': base64.b64encode(open('tests/f1_samples/mr_1_audio.wav','rb').read()).decode(), 'mime_type':'audio/wav', 'lang':'mr'}))" > /tmp/a.json
curl -X POST localhost:8080/api/f1/audio -H 'content-type: application/json' --data @/tmp/a.json
# -> {"type":"question","transcript":"माझ्याकडे तीन एकर भाड्याने घेतलेली जमीन आहे. ... पाणी विहिरीचे आहे.",
#     "field_targeted":"water_hours_per_week","profile_so_far":{"tenure":"tenant","acres":3.0,...,"cattle_count":3}}
```
| Input | Expected |
|---|---|
| `mime_type: video/mp4` (or upload a .txt) | 400 `unsupported audio type ...; use webm, ogg, wav or mp3` |
| upload without a `file` field | 400 `multipart upload needs a 'file' field` |
| 3 MB file | 400 `audio is 3.0 MB; the limit is 2 MB` |
| silent WAV | 422 `no clear speech found in the audio ...` (rejected in code, no Gemini call) |
| white noise WAV | 422 same message (Gemini reports no speech) |
| Gemini call fails | 502 `audio transcription failed: ...`; `/api/f1/interview` keeps working |
