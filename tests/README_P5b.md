# P5b checks: impact simulator + Impact page (evidence-safe, gaps A–F)

All KPI maths runs in code (`features/impact.py`, `features/verifier_bench.py`). Gemini never calculates a KPI.
A normal **replay** reads cached, eligibility-checked assessments and stored data: no model calls, no outcome writes,
one appended `impact_runs` row. **Refresh** (CLI only) builds missing cache entries and runs the K4 benchmark.

## Commands (actually supported)
```bash
python scripts/run_impact.py                       # cached replay, demo population (synthetic households)
python scripts/run_impact.py --refresh             # builds missing F3/F4 assessments + K4 benchmark (MODEL CALLS)
python scripts/run_impact.py --engine plain        # cache keys include the engine
python scripts/run_impact.py --households all      # LOCAL ONLY: includes real (F1-saved) households
python scripts/run_impact.py --export impact.json  # full run as JSON
pytest tests/test_impact.py tests/test_water_budget.py tests/test_learning.py -q
```
API: `GET /api/impact/latest`, `POST /api/impact/run` (cached replay only), `GET /api/impact/detail/{K1..K7}`.
The API replay saves a run only when the cache is complete for that backend's data (else 409 + the last run stays).

Impact on GCP (after a local refresh):
```bash
GOOGLE_CLOUD_PROJECT=$PROJECT BQ_DATASET=seasonsaathi BQ_LOCATION=$REGION python scripts/sync_impact_to_bq.py --check
```
Copies impact_cache / verifier_benchmark / impact_runs to BigQuery (append-only) and reports whether fact snapshots
match on BigQuery data (if not, cloud Re-run answers 409 and keeps showing the synced run).
Refresh is not exposed over HTTP: the API has no login.

Refresh needs `LLM_OFFLINE=false`; with offline on, every assessment is a rule result and is excluded (F).

## Results as of 2026-10-18 (replay, before any refresh)
`run IR-20261018T130008-f45a22 · 31 households (31 synthetic) · cache 0/55 · model calls 0 · 12.8 s`

| KPI | value | status | eligible/total | note |
|---|---|---|---|---|
| K1 | null | NOT_ESTABLISHED | 0/24 | no cached F4 assessments yet → run `--refresh` |
| K2 | null | NOT_ESTABLISHED | 0/31 | no cached F3 assessments yet |
| K3 | 13 days (median) | REPLAY | 31/31 plots | 31 detected; first HIGH: 16 plots 2026-09-16, 3 on 09-08, 12 in Jun–Jul (early-season stress) |
| K4 | null | NOT_RUN | – | benchmark runs only with `--refresh` |
| K5 | −0.114 | SIMULATED | 82/82 synthetic | gap 0.339 → 0.225, 31 records downgraded, in-sample; 0 observed outcomes |
| K6 | 48.4 % | MEASURED | 31/31 | tenant/sharecrop 14, woman 3 (union 15); fresh decisions p50 13.5 s, p90 26.6 s, 2.55 model calls/decision |
| K7 | 3 days | TARGET | – | not measured; F6 not built |

Deterministic K4 dry run (`semantic=False`, in tests): 15/15 number / missing-ID / invented-ID errors caught,
10/10 controls accepted, 5 unsupported claims `not_run` (only the semantic verifier agent can catch them).
The real K4 result (with the verifier agent) exists only after `--refresh`; it is never hardcoded.

## Metric definitions
- **K1** position = sale proceeds + retained animal value − fodder expenditure over the F4 horizon; improvement =
  recommended − SELL_NOW baseline (scenario). low/mid/high = every F4 range at its low end / central / high end.
  Mean, median, total, n_better. Combined F3+F4 feasibility: **unknown** (no crop cost, only cash bands).
- **K2** paired households only: baseline (Chana only, labelled assumption: no planned rabi crop field) vs recommended
  option. "Short" = code water-budget coverage entirely below 70 %; ranges straddling 70 % or unknown are excluded and
  counted. reduction_pp, resolved_short_pct (null with reason when no baseline is short).
- **K3** F2 thresholds replayed day by day with `get_plot_stress_facts(cutoff=…)`: rain days ≤ cutoff, NDVI windows
  ending by the cutoff, normals from earlier years, deficit ignored until 50 mm normal rain (same rule as the rain
  tool). lead_days = reference − first HIGH (negative kept; no HIGH → null). Reference 2026-09-29 is an
  **unverified candidate** (`config/impact.yaml`). Observation-date replay; publication delay not modelled.
- **K4** 20 constructed errors (5 changed numbers, 5 missing IDs, 5 invented IDs, 5 unsupported claims with genuine
  IDs) + 10 verbatim-fact controls appended to a frozen real F3 output (`tests/fixtures/verifier_bench_f3.json`).
  Production checks: code verifier + verifier agent. Crash / timeout = `error`, never a catch.
- **K5** gap = mean |mapping[confidence] − outcome rate of the record's (context, confidence) bucket|, before vs
  after the F7 rule, on the same synthetic records (in-sample). Mapping HIGH .85 / MED .60 / LOW .35 is illustrative.
  Correctness: ACT→good, AVOID→partial/loss, WAIT→partial (calibration_v).
- **K6** demo composition + decision_log telemetry (fresh vs cached/fallback separated). "₹ cost not measured".
- **K7** target only.

## A–F changes in shared code (this pass)
- **D** `outcomes.provenance` (observed / synthetic / demo); older rows derived: synthetic, `learning-demo-*` = demo,
  else `legacy_unverified` (never observed). Learning demo posts `demo: true`. `calibration_v.n_real` = observed only,
  plus `n_demo`, `n_unverified`. Peer outcome facts and the evidence line report provenance.
- **F** persistent verifier failure → `withheld: true` + reason on F3/F4; UI shows "Recommendation withheld" and no ★.
  KPI eligibility = verifier PASS + semantic agent PASS + not withheld + fresh engine (no cache/rule fallback).
- **B** failure_risk / risk shown as "AI relative score · not a probability", no colour bands; KPIs never use it.
- **E** Learning page: heuristic banner, observed / synthetic / demo columns.
- **A** water fit chip shows the coverage % or "not established (no water budget)"; K2 uses only code bands.
- **C** K1 reports `combined_feasibility: unknown` with the missing inputs.
- F2 stress facts gained a `cutoff` and the existing minimum-normal-rain rule.

## Demo walkthrough
1. `python scripts/run_impact.py --refresh` once (model calls; with Lite about ₹10-30), then Impact tab → Re-run.
2. K4 card: "X/20 errors caught · Y/10 valid controls accepted" → expand "All cases".
3. K1 chart: baseline vs recommended position per household; read the limitations aloud.
4. K2 bars: paired before/after with counts; K3: per-plot dates vs the unverified reference date.

## Remaining limitations
- Cached assessments do not exist until `--refresh` runs; K1/K2/K4 show NOT_ESTABLISHED / NOT_RUN until then.
- K4 uses one F3 fixture; F4 not benchmarked; must-cite rules not exercised.
- K2 baseline is an assumption for every household; gram/safflower water needs come from Rajasthan studies.
- K3 reference date unverified; plot locations synthetic; CHIRPS publication lag not modelled.
- K5 is in-sample on synthetic data; observed calibration not established (0 observed outcomes).
- The legacy learning-demo/REAL rows (if any) are derived as demo / legacy_unverified, never rewritten.
- BigQuery: new tables and the provenance column arrive on the next `load_data.py --backend bigquery` + redeploy.
