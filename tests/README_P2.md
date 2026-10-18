# P2 checks: how to run them and what you should see

All commands run from the repo root with the venv active (`source .venv/bin/activate`).
Outputs below are from real runs on 2026-10-17 (Vertex AI, `gemini-3.5-flash`, `GENAI_MODE=vertex`).
Gemini wording, failure-risk numbers and latency vary run to run; **signals, verifier status,
safety rule, caps and fact_ids should match.**

Before the first run (only after changing data): `python scripts/make_synthetic.py --n 25 --seed 42`,
`python scripts/load_data.py`, `python scripts/embed_profiles.py`.

---

## 0. Tests

| Command | Calls Vertex? | Expected |
|---|---|---|
| `pytest -m unit` | no | all pass (80 after P3) |
| `pytest` | no | all unit tests pass; llm tests skipped |
| `pytest -m llm` | **yes** (~30 calls first time; cached after) | P2 part: parity on SYN-001 / SYN-008 / SYN-018 + repeatability 3/3 (4 tests); P3 adds 7 (see README_P3.md) |

`unit` is applied automatically to every test not marked `llm`. `llm` tests run only with `-m llm`
(or `RUN_LIVE=1`). The repeatability test always makes real calls (cache off, 3 runs, ~12 calls).

---

## 1. `python scripts/show_facts.py HH-DEMO-FLOOR` (no Vertex calls)

Expected (abridged; 34 facts from 7 tool calls):
```
Household HH-DEMO-FLOOR: rainfed, cash low, village Chincholi (Dharashiv); calibration context: rainfed

fact_id                          name                                                  value     unit             source                          synthetic
F-hh-irrigation                  Irrigation source: rainfed                            rainfed                    household profile (SYNTHETIC)   SYNTHETIC
F-hh-water_hours_per_week        Water available: 0 hours per week                     0         hours per week   household profile (SYNTHETIC)   SYNTHETIC
F-hh-cash_buffer_band            Cash buffer: low                                      low                        household profile (SYNTHETIC)   SYNTHETIC
F-hh-fodder_weeks_left           Fodder left: 2 weeks for 3 cattle and 0 goats         2         weeks            household profile (SYNTHETIC)   SYNTHETIC
F-hh-completeness                Profile completeness: 100% of fields known            100       % of profile…    household profile (SYNTHETIC)   SYNTHETIC
F-rain-deficit                   Season rain is 41.9% below normal (cumulative since…  41.9      % below normal   CHIRPS daily (…) via Earth Engine
F-rain-dryspell                  Longest dry spell since 2026-06-01: 24 days (…)       24        days             CHIRPS daily (…)
F-peer-chana-only                Similar households grew Chana only in 4 seasons: 2…   2         good of 4 seasons embeddings + outcomes        SYNTHETIC
F-cal-rainfed-HIGH               In rainfed households, past HIGH-confidence calls w…  61        % right          outcomes table (calibration)    SYNTHETIC
F-cal-rainfed-cap                Calibration for rainfed households: HIGH calls righ…  MED       max confidence   outcomes table (calibration)    SYNTHETIC
F-price-gram-dharashiv-mandi     Agmarknet price ₹7,414/qtl in Dharashiv on 2026-10-…  7414.29   Rs/qtl           Agmarknet
F-price-gram-dharashiv-msp       MSP ₹5,958/qtl (RMS 2027-28)                          5958.0    Rs/qtl           PIB
F-price-safflower-dharashiv-msp  MSP ₹7,215/qtl (RMS 2027-28); current mandi price u…  7215.0    Rs/qtl           PIB
F-ndvi-pct-normal                Crop greenness is 114.6% of normal for the 10 days…   114.6     % of normal      Sentinel-2 SR (…); plot SYNTHETIC SYNTHETIC
...
34 facts from 7 tool calls

Top-3 similar households (cosine on profile embeddings, households with past outcomes only):
  SYN-002    cosine 0.9659  rainfed, cash high, 3.5 acres, tenant
  SYN-020    cosine 0.9609  rainfed, cash low, 3.0 acres, tenant
  SYN-010    cosine 0.9556  borewell, cash med, 2.0 acres, tenant
```
Check: every row has a fact_id and a source; household, peer, calibration and plot facts are SYNTHETIC;
rain, NDVI values and prices are real; safflower has only the MSP fact.

---

## 2. `python scripts/run_f3.py ...` (calls Vertex unless cached; ~4 calls per fresh run)

Trace order printed: 🧭 planner tools → 🛰️ tool calls → ⚖️ assessor → 🔎 verifier agent → 🛰️ facts →
✅/❌ code verifier → 📊 calibration / missing-data cap → 🛡️ safety floor → 💰 Gemini calls → Final table.

### 2a. `python scripts/run_f3.py HH-DEMO-OK` (well water, med cash)
```
🧭 Planner chose: get_rain_status, find_similar_households, get_calibration
⚖️ Assessor (attempt 1): Chana only WAIT/HIGH; Chana + safflower ACT/HIGH; Jowar + fodder ACT/HIGH
🔎 Verifier agent: PASS
✅ Code verifier: PASS (attempt 1)
📊 Calibration cap: HIGH: ... irrigated households: no cap
🛡️ Safety rule not needed
💰 Gemini calls: 4, cache hits: 0
Final:
  Chana only           WAIT   HIGH  tight  med   50
  Chana + safflower    ACT    HIGH  fit    med   20
  Jowar + fodder       ACT    HIGH  fit    med   15  <- recommended
  verifier_status=PASS  safety_rule_applied=False  engine=adk  fallback_reason=None  latency≈17000 ms
```
Check: no get_prices (cash is not low); PASS; no caps.

### 2b. `python scripts/run_f3.py HH-DEMO-FLOOR` (rainfed, water short, low cash)
```
🧭 Planner chose: get_rain_status, find_similar_households, get_calibration, get_prices, get_prices, get_prices
⚖️ Assessor (attempt 1): Chana only AVOID/MED; Chana + safflower WAIT/LOW; Jowar + fodder ACT/MED
📊 Calibration cap: MED: Calibration for rainfed households: HIGH calls right 61% (below 70%) ...
🛡️ Safety rule applied
     - Safety rule applied: Jowar + fodder changed ACT -> WAIT (water short, cash buffer low)
Final:
  Chana only           AVOID  MED   short  high  60
  Chana + safflower    WAIT   LOW   short  high  50
  Jowar + fodder       WAIT   MED   short  high  35  <- recommended
  verifier_status=PASS  safety_rule_applied=True  engine=adk
```
Check: get_prices called 3 times (low cash); **no option is ACT**; safety rule applied; confidence never
above MED (rainfed calibration cap); safflower option LOW and its drivers cite F-price-safflower-dharashiv-msp.

### 2c. `python scripts/run_f3.py HH-DEMO-THIN` (profile 45% complete, weighted)
```
📊 Calibration cap: MED: ...
📊 Confidence lowered: profile incomplete: Profile completeness: 45% (weighted); profile incomplete (below 60%), confidence capped at LOW
🛡️ Safety rule applied
     - Safety rule applied: Jowar + fodder changed ACT -> WAIT (water short, cash buffer unknown)
Final:
  Chana only           WAIT   LOW   short  high  65
  Chana + safflower    WAIT   LOW   short  high  60
  Jowar + fodder       WAIT   LOW   short  med   45  <- recommended
  verifier_status=PASS  safety_rule_applied=True
```
Check: **every option LOW** with the "profile incomplete" reason, and **no option is ACT**: unknown cash
counts as low for the safety floor.

### 2d. `python scripts/run_f3.py HH-DEMO-OK --inject-bad-fact` (verifier demo)
```
💉 Injected error: Chana only: driver fact_id replaced with F-corrupted-999
❌ Code verifier: FAIL (attempt 1)
     - Chana only: driver cites unknown fact_id F-corrupted-999
⚖️ Assessor (attempt 2): ...
✅ Code verifier: PASS (attempt 2)
💰 Gemini calls: 2, cache hits: 4
  verifier_status=PASS  engine=adk
```
Check: ❌ then ✅; exactly one regeneration (planner not re-run).

### 2e. `python scripts/run_f3.py HH-DEMO-OK --force-adk-fail` (fallback demo)
```
Engine: plain
🧭 Planner chose: get_rain_status, find_similar_households, get_calibration
✅ Code verifier: PASS (attempt 1)
  verifier_status=PASS  engine=plain  fallback_reason=adk: RuntimeError: forced ADK failure (--force-adk-fail)
```
Check: engine=plain, fallback_reason names the ADK failure; same signals as 2a.

### 2f. `python scripts/run_f3.py HH-DEMO-FLOOR --runs 3 --no-cache` (repeatability, ~12 real calls)
```
Signals matched in 3/3 runs: YES
  run 1: recommended Jowar + fodder; Chana + safflower WAIT, Chana only WAIT, Jowar + fodder WAIT
  run 2: recommended Jowar + fodder; Chana + safflower WAIT, Chana only WAIT, Jowar + fodder WAIT
  run 3: recommended Jowar + fodder; Chana + safflower WAIT, Chana only WAIT, Jowar + fodder WAIT
```
Without `--no-cache`, runs 2-3 are cache hits (0 calls) and always match.

---

## Known behaviour to be aware of
- Fresh runs take 10-22 s (target 15 s). ADK falls back to plain after `ENGINE_TIMEOUT_S=20`.
  Cached runs take under 4 s.
- Signals in a fresh run may differ slightly from the outputs above when facts change (new data); the
  checks marked "Check:" are what must hold.
- Safety floor treats unknown cash buffer as low (HH-DEMO-THIN triggers it).
