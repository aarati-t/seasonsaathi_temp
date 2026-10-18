# P4 checks (F4, F7, explainability, learning loop, F2): commands and expected output

Run from the repo root with the venv active. Outputs are from real runs on 2026-10-18 (Vertex AI,
`gemini-3.5-flash`, ADK). Gemini wording and risk numbers vary; **option sets, ₹ figures (computed in code),
verifier status, rules applied, thresholds and counts must match.** Repeats come from the LLM cache.

Before the first run (after pulling P4): `python scripts/make_synthetic.py --n 25 --seed 42`,
`python scripts/load_data.py`, `python scripts/embed_profiles.py` (adds HH-DEMO-LIVESTOCK / -ONECOW / -NOCATTLE,
season_rain, the labelled calibration seed, and migrates outcomes to `outcome_id`).

---

## 0. Tests
| Command | Vertex? | Expected |
|---|---|---|
| `pytest -m unit` | no | `135 passed` |
| `pytest -m llm` | yes | `17 passed` (P2 parity ×3 + repeatability, P3 ×9, P4: F4 parity, F2 alert, explainability ×2) |

Unit tests cover: fodder cost maths, shortfall, option sets by cattle count, SELL_NOW rule, calibration downgrade and
n<5 rule, what_would_change maths, stress levels, dry spells, dedupe, Pub/Sub envelope decoding, term approval,
moneylender / support / 2019-camp-rate rules, peer outcome facts, feedback style retrieval.

---

## 1. F4 livestock: `python scripts/run_f4.py HH-DEMO-LIVESTOCK [--engine plain] [--inject-bad-fact]`
2 cows, 3 weeks fodder, low cash. Facts computed in code:
```
F-f4-shortfall-weeks   Fodder shortfall: 9 weeks (horizon 12 − 3 left)
F-f4-fodder-cost       2,268 kg (2 × 18 kg × 7 × 9) = ₹45,360 (range ₹38,556-₹56,700 at ₹17-25/kg)
F-f4-sale-now          ₹17,438 (range ₹16,875-₹18,000) = normal ₹22,500 with a 25-20% drought drop   [SYNTHETIC]
F-f4-camp              100 Rs per animal per day; 2019 rate; current rate not confirmed
```
```
Final:
  option                 risk conf  cash press. cash needed            value kept     sale now
  KEEP_AND_BUY_FODDER      60 HIGH  high        ₹38,556-56,700         ₹40,000-50,000 -
  SELL_ONE_KEEP_REST       40 HIGH  med         ₹19,278-28,350         ₹20,000-25,000 ₹16,875-18,000  <- recommended
  SELL_NOW                 80 HIGH  low         ₹0                     ₹0             ₹33,750-36,000
What would change it: If fodder_weeks_left were 11 or more (now 3), buying kadba for the rest of the 12-week
  horizon would cost at most ₹10,000 (the low cash-band limit, an assumption) ...
verifier_status=PASS  engine=adk
```
Check: SELL_NOW not recommended (risk not strictly lowest); every ₹ comes from a fact; explanations in Marathi/Hindi.

`python scripts/run_f4.py HH-DEMO-ONECOW` → options **KEEP_AND_BUY_FODDER | KEEP_WITH_SUPPORT | SELL_NOW**;
KEEP_WITH_SUPPORT (fodder_camp, silage) recommended; cash needed ₹0-34,650; every sentence that uses the camp
rate says "२०१९ चा दर; सध्याचा दर निश्चित नाही" / "२०१९ की दर; मौजूदा दर पक्की नहीं"; goats: "goat fodder not costed".

`python scripts/run_f4.py HH-DEMO-NOCATTLE` → `F4 not applicable for HH-DEMO-NOCATTLE: no large animals (no Gemini call)`.

## 2. F7 calibration: `python scripts/show_calibration.py --min-n 5`
```
Rule: HIGH and hit_rate < 0.7 and n >= 5 -> MED; n < 5 -> no downgrade
context_bucket                     conf  hit_rate    n  real  synth  effect on a HIGH call
15-30|rainfed|gram                 HIGH      0.67    6     0      6  DOWNGRADE to MED
15-30|rainfed|gram+safflower       HIGH      0.40    5     0      5  DOWNGRADE to MED
>30|rainfed|gram                   HIGH      0.50   14     0     14  DOWNGRADE to MED
>30|rainfed|gram+safflower         HIGH      1.00    5     0      5  keep HIGH
>30|rainfed|sorghum+fodder         HIGH      0.33    6     0      6  DOWNGRADE to MED
```
(>30|rainfed|gram includes the 7 labelled SEED-CAL rows.) API: `GET /api/calibration[?context=...]`.

Downgrade in F3: `python scripts/run_f3.py HH-DEMO-FLOOR`
```
🧭 Planner chose: ..., get_calibration, get_calibration, get_calibration, ...
📊 calibration downgrade: Chana only, Jowar + fodder
  note: calibration downgrade: Chana only HIGH -> MED (past HIGH calls in >30|rainfed|gram right 50%, n=14; below 70%)
```
A HIGH call in a context with n < 5 keeps HIGH with the note "insufficient history (n=X)".

## 3. Explainability (F3 and F4, in the same run_f3 / run_f4 output)
```
Why recommended:      2-3 lines, each [fact_id]       (verified like drivers)
Why not the others:   one line per other option [fact_id]
What would change it: If the season rain deficit were below 25% (now 41.9%), a rainfed farm's water would not be
                      rated short, so the safety rule would not block ACT for Jowar + fodder     (computed in code)
Confidence reason:    calibration downgrade: ... ; Safety rule applied: ...
Data gaps:            synthetic: F-cal-..., synthetic: F-hh-cash_buffer_band, ...
मराठी / हिंदी:         3 short sentences, numbers checked against facts
📚 evidence used: 37 outcomes (0 real), 3 peer outcomes, 0 rated examples
```

## 4. Learning loop
Use a scratch copy so demo outcomes do not count as real data in your main DB:
```bash
createdb seasonsaathi_demo && pg_dump seasonsaathi | psql -q seasonsaathi_demo
export DATABASE_URL=postgresql:///seasonsaathi_demo
python scripts/add_outcome.py HH-DEMO-FLOOR --result loss --n 3
```
```
Recorded 3 REAL outcome(s) for HH-DEMO-FLOOR (request ...): Jowar + fodder predicted WAIT/MED -> actual loss
Calibration for >30|rainfed|sorghum+fodder: HIGH 33% right (n=6, 0 real); MED 25% right (n=4, 3 real); ...
```
(MED row moves from n=1, 100% to n=4, 25%, 3 real.) Then with `uvicorn app.main:app --port 8080`:
| Call | Expected |
|---|---|
| `POST /api/feedback {"request_id": "<F3 request>", "rating": "up"}` | `{rating: up, context_bucket: ">30\|rainfed"}` |
| `POST /api/feedback {... "rating": "down", "note": "too long"}` then `GET /api/feedback/review` | the down rating with its note |
| `POST /api/f3/HH-DEMO-FLOOR` | `evidence used: ... 1 rated examples` |
| `POST /api/f1/interview` (hi_1 opening, mentions कुएँ) then `GET /api/terms/review` | `pending: [सोयाबीन, कुएँ, भैंस]` |
| `POST /api/terms/review {"term":"कुएँ","status":"approved","canonical":"well","category":"irrigation"}` | approved |
| same F1 call again | trace: कुएँ in the glossary, not in "normalised by Gemini" |
| approve with `"category":"bogus"` | 422 `approving needs canonical and category in [...]` |
| `POST /api/outcomes {hh_id, request_id, season, actual_result}` | the inserted row, `is_synthetic: false` |

## 5. F2 season risk watch
`python scripts/run_f2.py PLOT-HH-DEMO-FLOOR`
```
Plot PLOT-HH-DEMO-FLOOR: stress HIGH; crossed: ['rain deficit 41.9% > 25%', 'dry spell 24 days > 21 days']
   F-stress-asof      Rain data up to 2026-09-30 (last available CHIRPS date)
   F-stress-deficit   Season rain is 41.9% below normal ...
   F-stress-dryspell  Longest dry spell: 24 days (days with rain under 2.5 mm) ...
   F-stress-ndvi      Crop greenness (NDVI) is 114.4% of normal ...
   🧭 Planner: REQUEST_DRONE - ...
   ✅ Alert check: PASS (attempt 1)
   📣 Alert AL-... via local
   मराठी: पाऊस सरासरीपेक्षा 41.9% कमी आहे, ...\n पुढील पाऊल: ... (पावसाची माहिती 2026-09-30 पर्यंत).
   fact_ids: ['F-stress-deficit', 'F-stress-dryspell', 'F-stress-ndvi', 'F-stress-asof']
```
Run it again → `⏭️ Duplicate: same plot + level alerted within 7 days (no LLM call)`.

`python scripts/run_f2.py --scan` (first time ~60 Gemini calls; all plots are HIGH on the real 2026 data)
```
Scanned 31 plots: HIGH 31, MED 0, LOW 0; alerts created 30, deduped 1; skipped 1
   skipped HH-NEW-001: no plot location
```
Second scan: `alerts created 0, deduped 31`.

API: `POST /api/f2/{plot_id}`, `POST /api/f2/scan`, `GET /api/alerts?hh_id=HH-DEMO-FLOOR`,
`POST /api/alerts/receive` with a Pub/Sub push envelope `{"message": {"data": "<base64 JSON alert>"}}` →
`{"stored": true}`; same alert again → `{"stored": false, "reason": "duplicate alert_id"}`; bad envelope → 400.
