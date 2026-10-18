# SeasonSaathi: 3-minute demo video script

Live URL: https://seasonsaathi-184136561293.asia-south1.run.app
Impact page: record from **local** http://localhost:8000/#/impact (cached KPIs live in local Postgres).

---

## A. Before recording (10 min)

1. Warm instance on: `gcloud run services update seasonsaathi --region asia-south1 --min-instances 1`
2. **Pre-warm run-through** on the live URL with `?demo=1`: click presenter steps 2, 3, 5, 6 once and wait for each
   to finish. Same inputs are then served from the Gemini cache on that instance, so the recording is fast.
   (Do NOT pre-run the learning demo or F1: they should run live.)
3. Local server running for the Impact page: `uvicorn app.main:app --port 8000`, open `#/impact`, check that
   K4 shows **20/20 · 10/10**.
4. Browser: Chrome, zoom 110%, window ~1280 px, close other tabs, notifications off. Language **EN** to start.
5. Mic allowed for the live URL (🔒 → Microphone → Allow). Speak the F1 sentence once to test, then **↺ Start again**.
6. Screen recorder ready (OBS / Loom / `Ctrl+Shift+Alt+R` on Ubuntu). Record at 1280×720 or 1920×1080.

If a live step takes > 10 s while recording: keep talking over the "Working on it…" timer (it is honest:
real agents running), or cut the wait in editing.

---

## B. Script (≈ 2:45) · simple words, short lines

Live URL with presenter bar: https://seasonsaathi-184136561293.asia-south1.run.app/?demo=1

**1 · Problem (0:00–0:15)** · Show: Households list.
> "Farmers in drought-hit Marathwada faces tough choices. They must decide what to sow and whether to sell their cattle.
> SeasonSaathi helps NGO field agents make the safest choice, for tenant farmers and women too."

**2 · Voice interview (0:15–0:45)** · New household → language हिंदी → ↺ Start again → 🎙️ Record voice → speak →
⏹ Stop. Show "Heard", the profile chips and the agent's question.
Speak: "मैं खुद खेती करती हूँ। 2 एकड़ ज़मीन बटाई पर है, सिर्फ़ बारिश का पानी है। पिछले खरीफ़ में ज्वार और तूर बोई थी।
एक गाय और चार बकरियाँ हैं, चारा सिर्फ़ 2 हफ़्ते का बचा है।"
> "The agent just talks to the farmer in Hindi or Marathi. Gemini fills the profile from the farmer's own words.
> Nothing is guessed. It asks only the questions that matter."

**3 · Crop plan (0:45–1:25)** · Language EN → presenter **5 · HH-DEMO-FLOOR**. Show the 3 cards and the red
"Safety rule applied" banner. Scroll to Agent Trace. Tap one reason so its fact lights up.
> "Here, AI agents plan the rabi crop. They pull real facts: satellite rain, crop greenness and soil moisture
> from Earth Engine, plus prices and similar farms. This farm has only 6 to 13 percent of the water a crop needs.
> Every reason points to a fact. Tap it and you see the source. A safety rule blocks risky advice."

**4 · Catching AI mistakes (1:25–1:45)** · Presenter **3 · Inject bad fact**. Show the red box, ❌ FAIL,
then ✅ PASS with "Regeneration #2".
> "AI can make mistakes, so we plant one on purpose. The verifier catches it, the AI fixes it,
> and only checked advice reaches the farmer."

**5 · Livestock (1:45–2:05)** · Presenter **6 · HH-DEMO-LIVESTOCK**. Show the rupee cards and support list.
Switch to हिंदी, press 🔊.
> "For cattle, it compares keeping, selling one, or selling now, in rupees from real prices.
> It reads the advice aloud in the farmer's language."

**6 · Learning (2:05–2:25)** · Presenter **7 · Learning demo** → click 🎓 twice. Show "What changed: High → Medium".
> "When results come back, it learns. After repeated losses, it trusts itself less and changes its advice."

**7 · Impact (2:25–2:45)** · Local http://localhost:8000/#/impact. Show K4 and K3 cards.
> "We test ourselves honestly. The verifier caught 20 out of 20 planted errors.
> Satellite data flagged stress 13 days before the drought date.
> Built on Gemini, ADK, Earth Engine, BigQuery, Pub/Sub and Cloud Run. Thank you."

## C. Do NOT show in the video
- HH-DEMO-OK crop plan cards (shows "ACT · High" with water short: known gap, fix after recording).
- K1 (−₹14,536: asset terms favour selling; milk/herd rebuilding not modelled). Keep for Q&A.
- Adding a village (roadmap).

## D. After recording
```bash
gcloud run services update seasonsaathi --region asia-south1 --min-instances 0
psql seasonsaathi -c "delete from outcomes where season like 'learning-demo-%'"
```
Learning-demo rows on BigQuery are stored with provenance `demo` (never counted as observed).

Then fix and redeploy:
1. Water short (code budget) → never ACT (closes the HH-DEMO-OK gap).
2. F1: ask about animals before fodder; grazing → no fodder question; value goats (survey, Odisha).

## E. Q&A cheat sheet
| Question | Answer |
|---|---|
| Is the data real? | Rain, NDVI, soil, soil moisture, mandi prices, MSP: real. Households, outcomes, plot locations, normal animal values: synthetic, badged. |
| Why trust the AI? | Facts from tools only, every claim cites a fact_id, code + Gemini verifier, safety floor in code, withheld when verification fails. 20/20 planted errors caught. |
| What's new vs advisory apps? | Household-level (tenant/woman), asks its own questions, verifies itself, lowers its own confidence after bad outcomes, and says "not established" instead of inventing numbers. |
| Scale? | One Cloud Run service; BigQuery; Earth Engine export per village; ~₹1 of Gemini per decision with Lite; impact replay is free (0 model calls). |
| Field validation? | Survey in Ganjam, Odisha: cattle are grazed, goats preferred (₹12,000 vs ₹6,000), moneylender loans, migration income, no insurance awareness → next version. |
| K1 negative? | In 12-week asset terms, selling often looks better; we don't model milk income or herd rebuilding yet, and we show it rather than hide it. |
