"""Embed every household's profile_text_norm with EMBED_MODEL and store it in profile_embedding.

Usage: python scripts/embed_profiles.py [--force]
Uses GENAI_MODE from .env (vertex = Vertex AI embeddings; apikey = AI Studio fallback).
Run after load_data.py. Skips households that already have an embedding unless --force.
"""
import argparse

from google.genai import types

from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from core.llm import genai_mode, get_client, require_env, with_backoff
from repo.base import get_repo

BATCH = 20


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="re-embed households that already have one")
    args = ap.parse_args()

    model = require_env("EMBED_MODEL")
    repo = get_repo()
    todo = [
        h for h in repo.fetch_all("households")
        if h["profile_text_norm"] and (args.force or not h["profile_embedding"])
    ]
    print(f"GENAI_MODE={genai_mode()} model={model}: {len(todo)} households to embed")
    client = get_client()
    config = types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY")
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        resp = with_backoff(client.models.embed_content, model=model,
                            contents=[h["profile_text_norm"] for h in batch], config=config)
        for hh, emb in zip(batch, resp.embeddings, strict=True):
            repo.set_embedding(hh["hh_id"], list(emb.values), model)
        print(f"  embedded {i + len(batch)}/{len(todo)} (dim={len(resp.embeddings[0].values)})")


if __name__ == "__main__":
    main()
