"""Explainability helpers shared by F3 and F4. Everything here is computed in code from facts and notes;
Gemini's why_* drivers are verified separately by core.verifier."""
from orchestrators.base import FactStore

REASON_KEYS = [
    ("calibration", "calibration"),
    ("profile incomplete", "missing data (profile incomplete)"),
    ("missing data", "missing data"),
    ("NDVI", "crop health (NDVI) unavailable"),
    ("Safety rule", "safety rule"),
    ("verifier failed", "verifier failed after regeneration"),
]


def confidence_reason(notes: list[str], status: str) -> list[str]:
    """Which rules set the confidence, in plain words (from the notes the rules wrote)."""
    out = []
    for key, label in REASON_KEYS:
        hits = [n for n in notes if key.lower() in n.lower()]
        if hits:
            first = hits[0] if hits[0].lower().startswith(label.split(" ")[0].lower()) else f"{label}: {hits[0]}"
            out.append(first + (f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""))
    if status == "RULE":
        out.append("rule result: AI unavailable, confidence LOW")
    return out or ["assessor judgement; no calibration, missing-data, safety or NDVI rule changed it"]


def cited_ids(assessment) -> set[str]:
    ids = {d.fact_id for o in assessment.options for d in o.drivers}
    ids |= {d.fact_id for d in getattr(assessment, "why_recommended", [])}
    ids |= {w.fact_id for w in getattr(assessment, "why_not_others", [])}
    return ids


def data_gaps(store: FactStore, cited: set[str]) -> list[str]:
    """Unknown profile inputs + synthetic facts the answer relied on."""
    gaps = [f"unknown: {f['text'].split(':')[0]}" for f in store.as_list()
            if f["fact_id"].startswith("F-hh-") and f.get("value") is None]
    gaps += [f"no data: {f['text']}" for f in store.as_list() if f["fact_id"].endswith("-none") or
             f["fact_id"] == "F-ndvi-unavailable"]
    gaps += [f"synthetic: {fid} ({store.facts[fid].get('name') or store.facts[fid]['text'][:60]})"
             for fid in sorted(cited) if fid in store.facts and store.facts[fid].get("is_synthetic")]
    return gaps


def evidence(store: FactStore, rated_examples: int = 0) -> dict:
    """'evidence used: n outcomes (x real), y peer outcomes, z rated examples' (P4 learning loop)."""
    cal = [f for f in store.as_list() if f["fact_id"].startswith("F-cal-")]
    n = sum(lvl.get("n", 0) for f in cal for lvl in (f.get("levels") or {}).values())
    real = sum(lvl.get("n_real", 0) for f in cal for lvl in (f.get("levels") or {}).values())
    demo = sum(lvl.get("n_demo", 0) for f in cal for lvl in (f.get("levels") or {}).values())
    peer = [f for f in store.as_list() if f["fact_id"].startswith("F-peer-outcome-")]
    peer_real = sum(1 for f in peer if not f.get("is_synthetic"))
    return {"outcomes": n, "outcomes_real": real, "outcomes_demo": demo, "peer_outcomes": len(peer),
            "peer_outcomes_real": peer_real, "rated_examples": rated_examples,
            "text": f"evidence used: {n} outcomes ({real} observed, {demo} demo, rest synthetic), {len(peer)} peer "
                    f"outcomes, {rated_examples} rated examples"}
