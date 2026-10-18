"""Self-learning loop (no model training): updated evidence + retrieval + human-approved vocabulary.

1. Outcomes: record_outcome() inserts a real outcome (is_synthetic=false) linked to a decision_log request;
   calibration_v counts it automatically.
2. Peer evidence: tools/peers.py returns peers' recorded outcomes as facts (F-peer-outcome-*).
3. Feedback: record_feedback() inserts up/down ratings; style_block() retrieves the top 2 'up'-rated
   explanations for the same feature + context bucket as STYLE examples only (never facts).
4. Vocabulary: F1 terms Gemini had to interpret go to term_review as 'pending'; approve_term() inserts a
   decision; approved terms join the glossary on the next run (no Gemini needed for them).
All writes are single inserts (BigQuery-safe); the latest term_review row per term wins.
"""
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from repo.base import Repo

TERM_CATEGORIES = {"crops", "soil", "irrigation", "animals", "tenure"}
RESULTS = {"good", "partial", "loss"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decision(repo: Repo, request_id: str) -> dict:
    rows = repo.fetch_where("decision_log", request_id=request_id)
    if not rows:
        raise KeyError(f"request_id {request_id} not found in decision_log")
    row = rows[0]
    out = row["output_json"]
    row["output"] = out if isinstance(out, dict) else json.loads(out)
    return row


# ---------- 1. outcomes ----------

def record_outcome(repo: Repo, hh_id: str, request_id: str, season: str, actual_result: str,
                   cattle_sold: int | None = None, distress_sale: bool | None = None, note: str | None = None,
                   demo: bool = False) -> dict:
    """demo=True (learning demo): provenance 'demo', never counted as an observed outcome."""
    if actual_result not in RESULTS:
        raise ValueError(f"actual_result must be one of {sorted(RESULTS)}")
    d = _decision(repo, request_id)
    if d["hh_id"] != hh_id:
        raise ValueError(f"request {request_id} is for {d['hh_id']}, not {hh_id}")
    out = d["output"]
    rec = out.get("recommended")
    signal = conf = bucket = crop = None
    if d["feature"] == "f3":
        from features.f3_cropmix import CROP_KEYS
        opt = next((o for o in out.get("options", []) if o["name"] == rec), {})
        signal, conf = opt.get("signal"), opt.get("confidence")
        bucket = (out.get("context_bucket") or "unknown|").split("|")[0]
        crop = CROP_KEYS.get(rec)
    row = {"outcome_id": f"REAL-{uuid.uuid4().hex[:12]}", "hh_id": hh_id, "season": season, "decision": rec,
           "predicted_signal": signal, "predicted_confidence": conf, "actual_result": actual_result,
           "cattle_sold": cattle_sold, "distress_sale": distress_sale, "request_id": request_id,
           "rain_deficit_bucket": bucket if bucket not in (None, "unknown") else None, "crop_key": crop,
           "note": note, "is_synthetic": False, "provenance": "demo" if demo else "observed"}
    repo.insert("outcomes", [row])
    return row


# ---------- 3. feedback + style examples ----------

def record_feedback(repo: Repo, request_id: str, rating: str, note: str | None = None) -> dict:
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    d = _decision(repo, request_id)
    row = {"feedback_id": f"FB-{uuid.uuid4().hex[:12]}", "request_id": request_id, "feature": d["feature"],
           "context_bucket": d["output"].get("context_bucket"), "rating": rating, "note": note, "created_at": _now()}
    repo.insert("feedback", [row])
    return row


def style_examples(repo: Repo, feature: str, context_bucket: str | None, k: int = 2) -> list[dict]:
    """Top-k 'up'-rated explanations for the same feature + context bucket (most up votes first)."""
    ups = [f for f in repo.fetch_where("feedback", feature=feature, rating="up")
           if (f.get("context_bucket") or None) == (context_bucket or None)]
    out = []
    for request_id, _n in Counter(f["request_id"] for f in ups).most_common():
        try:
            e = _decision(repo, request_id)["output"].get("explain") or {}
        except KeyError:
            continue
        if e.get("plain_explanation_mr"):
            out.append({"request_id": request_id, "mr": e["plain_explanation_mr"], "hi": e.get("plain_explanation_hi", "")})
        if len(out) == k:
            break
    return out


def style_block(repo: Repo, feature: str, context_bucket: str | None) -> tuple[str, int]:
    """Prompt text with the style examples, and how many were used."""
    ex = style_examples(repo, feature, context_bucket)
    if not ex:
        return "", 0
    lines = ["STYLE EXAMPLES rated helpful by field agents (copy their tone and simplicity ONLY; never use their "
             "numbers or claims, they are about other households):"]
    lines += [f"- mr: {e['mr']}\n  hi: {e['hi']}" for e in ex]
    return "\n".join(lines), len(ex)


def feedback_review(repo: Repo) -> list[dict]:
    return sorted(repo.fetch_where("feedback", rating="down"), key=lambda r: str(r.get("created_at")), reverse=True)


# ---------- 4. vocabulary ----------

def _latest_by_term(repo: Repo) -> dict[str, dict]:
    latest = {}
    for r in sorted(repo.fetch_all("term_review"), key=lambda r: (str(r.get("created_at")), r["review_id"])):
        latest[r["term"].strip().lower()] = r
    return latest


def record_pending_terms(repo: Repo, terms: list[dict], request_id: str | None = None) -> list[str]:
    """Insert unseen Gemini-normalised F1 terms as 'pending' (terms already reviewed are skipped)."""
    seen = _latest_by_term(repo)
    rows = [{"review_id": f"TR-{uuid.uuid4().hex[:12]}", "term": t["term"].strip(), "meaning": t.get("meaning"),
             "category": None, "canonical": None, "status": "pending", "request_id": request_id, "created_at": _now()}
            for t in terms if t.get("term") and t["term"].strip().lower() not in seen]
    if rows:
        repo.insert("term_review", rows)
    return [r["term"] for r in rows]


def approve_term(repo: Repo, term: str, canonical: str | None, category: str | None, status: str = "approved",
                 note: str | None = None) -> dict:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be 'approved' or 'rejected'")
    if status == "approved" and (category not in TERM_CATEGORIES or not canonical):
        raise ValueError(f"approving needs canonical and category in {sorted(TERM_CATEGORIES)}")
    row = {"review_id": f"TR-{uuid.uuid4().hex[:12]}", "term": term.strip(), "meaning": note, "category": category,
           "canonical": canonical, "status": status, "request_id": None, "created_at": _now()}
    repo.insert("term_review", [row])
    _approved_cache.clear()
    return row


def terms_review(repo: Repo) -> dict:
    latest = _latest_by_term(repo)
    by = {"pending": [], "approved": [], "rejected": []}
    for r in latest.values():
        by[r["status"]].append({k: r.get(k) for k in ("term", "meaning", "category", "canonical", "status")})
    return by


_approved_cache: dict = {}


def approved_terms(repo: Repo | None, ttl_s: float = 10.0) -> list[tuple[str, str, str]]:
    """[(alias, category, canonical)] approved by a human; used by F1 together with config/terms.yaml."""
    if repo is None:
        return []
    hit = _approved_cache.get(id(repo))
    if hit and time.time() - hit[0] < ttl_s:
        return hit[1]
    terms = [(r["term"], r["category"], r["canonical"]) for r in _latest_by_term(repo).values()
             if r["status"] == "approved"]
    _approved_cache[id(repo)] = (time.time(), terms)
    return terms
