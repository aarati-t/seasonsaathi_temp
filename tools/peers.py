"""Tool: find_similar_households(hh_id). Cosine similarity on profile embeddings (VECTOR_MODE=python),
then the peers' past outcomes per crop mix. Facts only. Outcomes are SYNTHETIC in the demo data."""
import re
from collections import defaultdict

import numpy as np

from core.settings import thresholds
from repo.base import Repo, get_repo
from tools.calibration import outcome_provenance

SOURCE = "embeddings (EMBED_MODEL) + outcomes table"


def season_key(season: str) -> tuple[int, str]:
    """Order seasons by their first year ('rabi-2025-26' -> 2025), not alphabetically."""
    m = re.search(r"(19|20)\d\d", season or "")
    return (int(m.group()) if m else -1, season or "")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def similar(hh_id: str, repo: Repo, k: int) -> list[tuple[float, str]] | None:
    """Top-k (cosine, hh_id) among households that have an embedding AND past outcomes (a peer with no
    history says nothing). None if hh_id has no embedding."""
    households = {h["hh_id"]: h for h in repo.fetch_all("households")}
    me = households.get(hh_id)
    if not me or not me["profile_embedding"]:
        return None
    with_history = {o["hh_id"] for o in repo.fetch_all("outcomes")}
    v = np.array(me["profile_embedding"], dtype=float)
    scored = []
    for other_id, h in households.items():
        if other_id == hh_id or not h["profile_embedding"] or other_id not in with_history:
            continue
        w = np.array(h["profile_embedding"], dtype=float)
        scored.append((float(v @ w / (np.linalg.norm(v) * np.linalg.norm(w))), other_id))
    return sorted(scored, reverse=True)[:k]


def find_similar_households(hh_id: str, repo: Repo | None = None, k: int | None = None) -> list[dict]:
    repo = repo or get_repo()
    k = k or thresholds()["peers"]["k"]
    households = {h["hh_id"]: h for h in repo.fetch_all("households")}
    peers = similar(hh_id, repo, k)
    if peers is None:
        return [{"fact_id": "F-peer-none", "value": None, "unit": None, "source": SOURCE, "source_url": None,
                 "is_synthetic": False, "text": f"Similar households unavailable: {hh_id} has no profile embedding"}]
    peer_ids = [p for _, p in peers]
    synthetic = any(households[p]["is_synthetic"] for p in peer_ids)
    base = {"source": SOURCE, "source_url": None, "is_synthetic": synthetic}

    facts = [{**base, "fact_id": "F-peer-list", "value": peer_ids, "unit": None,
              "text": f"{len(peer_ids)} most similar households: "
                      + ", ".join(f"{p} (similarity {s:.2f})" for s, p in peers)}]
    by_mix = defaultdict(lambda: {"good": 0, "partial": 0, "loss": 0})
    for o in repo.fetch_all("outcomes"):  # every mix seen anywhere, so "0 seasons" is reported too
        by_mix[o["decision"]]
    for p in peer_ids:
        for o in repo.fetch_where("outcomes", hh_id=p):
            by_mix[o["decision"]][o["actual_result"]] += 1
    # Learning loop: each peer's most recent recorded outcome (real outcomes appear here automatically).
    latest = []
    for i, p in enumerate(peer_ids, start=1):
        rows = sorted(repo.fetch_where("outcomes", hh_id=p), key=lambda o: (season_key(o["season"]), o.get("outcome_id") or ""))
        if not rows:
            continue
        o = rows[-1]
        latest.append(o)
        sold = f"; {o['cattle_sold']} animal(s) sold" + (" in distress" if o.get("distress_sale") else "") \
            if o.get("cattle_sold") else ""
        prov = outcome_provenance(o)
        facts.append({"fact_id": f"F-peer-outcome-{i}", "value": o["actual_result"], "unit": None,
                      "source": "outcomes table", "source_url": None, "is_synthetic": prov != "observed",
                      "provenance": prov,
                      "text": f"Similar household {p}, {o['season']}: {o['decision']} -> {o['actual_result']}{sold}"
                              + (" (observed outcome)" if prov == "observed" else f" ({prov} outcome)")})
    if latest:
        season = max((o["season"] for o in latest), key=season_key)
        last = [o for o in latest if o["season"] == season]
        losses = sum(o["actual_result"] == "loss" for o in last)
        facts.append({"fact_id": "F-peer-lastseason", "value": losses, "unit": f"losses of {len(last)}",
                      "source": "outcomes table", "source_url": None,
                      "is_synthetic": any(outcome_provenance(o) != "observed" for o in last),
                      "text": f"{losses} of {len(last)} similar households had a loss in {season}"})
    for mix, c in sorted(by_mix.items()):
        n = sum(c.values())
        facts.append({**base, "fact_id": f"F-peer-{slug(mix)}", "value": c["good"], "unit": f"good of {n} seasons",
                      "text": f"Similar households grew {mix} in {n} seasons: {c['good']} good, "
                              f"{c['partial']} partial, {c['loss']} loss"})
    return facts
