"""Smoke test: one Gemini call + one embedding call in whichever GENAI_MODE is set in .env.

Usage: python scripts/check_vertex.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import genai_mode, get_client, require_env, verify_backend, with_backoff  # noqa: E402


def main() -> int:
    mode = genai_mode()
    use_vertex_flag = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").strip().upper()
    print(f"GENAI_MODE={mode}  GOOGLE_GENAI_USE_VERTEXAI={use_vertex_flag or '(unset)'}")
    if (mode == "vertex") != (use_vertex_flag in ("TRUE", "1")):
        print("WARNING: GOOGLE_GENAI_USE_VERTEXAI does not match GENAI_MODE; ADK reads that flag.")
    if mode == "vertex":
        print(f"project={os.getenv('GOOGLE_CLOUD_PROJECT')}  location={os.getenv('GOOGLE_CLOUD_LOCATION', 'global')}")

    try:
        verify_backend()
    except RuntimeError as exc:
        print(f"[FAIL] Backend check: {exc}")
        return 1
    client = get_client()
    ok = True

    gemini_model = require_env("GEMINI_MODEL")
    try:
        resp = with_backoff(
            client.models.generate_content,
            model=gemini_model,
            contents="Reply with exactly: SeasonSaathi OK",
        )
        print(f"[PASS] Gemini ({gemini_model}): {resp.text.strip()!r}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Gemini ({gemini_model}): {exc}")

    try:
        embed_model = require_env("EMBED_MODEL")
        emb = with_backoff(
            client.models.embed_content,
            model=embed_model,
            contents="3 acre rented, soybean, 2 cows, well dried in August",
        )
        values = emb.embeddings[0].values
        print(f"[PASS] Embedding ({embed_model}): dim={len(values)} first3={[round(v, 4) for v in values[:3]]}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] Embedding: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
