"""Run an F1 interview from a sample file and print each turn, the final profile, completeness and trace.

Usage: python scripts/run_f1.py FILE [--engine adk|plain] [--lang mr|hi|en] [--save --consent --village V01] [--no-cache]
  FILE: first line = farmer's opening message; following lines = the farmer's answers, in order.
  --lang     defaults to the file name prefix (mr_ / hi_ / en_), else en
  --save     save the final profile as a new household (HH-NEW-nnn); needs --consent and --village
  --consent  confirms the farmer gave consent (saving is refused without it)
  --village  village_id from data/villages.csv (1 or V01)
"""
import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from common import ROOT  # noqa: F401  (puts the repo root on sys.path)
from core import llm
from core.schemas import InterviewResult, Turn
from features import f1_interview
from orchestrators import selector
from repo.base import get_repo


def show_trace(r: InterviewResult) -> None:
    for t in r.trace:
        d = t.detail
        ms = f"  [{t.ms} ms]" if t.ms is not None else ""
        tag = f"  ({t.backend}{'/' + t.model if t.model else ''})"
        if t.step == "engine":
            print(f"   {t.title}")
        elif t.step == "extractor":
            fields = ", ".join(f"{f['field']}={f['value']!r}" for f in d.get("fields", []))
            print(f"   🧾 Extractor{ms}{tag}: {fields or '(nothing)'}")
        elif t.step == "terms":
            gl = ", ".join(f"{g['term']}→{g['canonical']}" for g in d["terms_yaml"])
            print(f"   📖 terms.yaml: {gl or '(none)'}")
            if d["gemini_normalised_terms"]:
                print("   📖 normalised by Gemini (not in terms.yaml): "
                      + ", ".join(f"{g['term']}→{g['meaning']}" for g in d["gemini_normalised_terms"]))
        elif t.step == "verifier_code":
            print(f"   {t.title}")
            for x in d["dropped"]:
                print(f"      dropped {x['field']}={x['value']!r}: {x['reason']}")
            for x in d["out_of_range"]:
                print(f"      out of range {x['field']}={x['value']!r} -> unknown, will ask")
            for x in d["derived"]:
                print(f"      derived: {x}")
        elif t.step == "completeness":
            nxt = d["next"]
            print(f"   {t.title} flags={d['flags']} questions_asked={d['questions_asked']} "
                  f"next={nxt['group'] + ':' + nxt['field'] if nxt else 'final'}")
        elif t.step == "interviewer":
            print(f"   ❓ Interviewer{ms}{tag}")
        elif t.step == "usage":
            print(f"   💰 {t.title}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("--engine", choices=["adk", "plain"])
    ap.add_argument("--lang", choices=["mr", "hi", "en"])
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--consent", action="store_true")
    ap.add_argument("--village")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    if not args.file.exists():
        raise SystemExit(f"File not found: {args.file}")
    if args.save and not (args.consent and args.village):
        raise SystemExit("--save needs --consent (farmer gave consent) and --village (e.g. --village V01)")
    if args.no_cache:
        os.environ["LLM_CACHE"] = "off"
    lang = args.lang or (args.file.name[:2] if args.file.name[:2] in f1_interview.LANGS else "en")
    lines = [ln.strip() for ln in args.file.read_text().splitlines() if ln.strip()]
    if not lines:
        raise SystemExit(f"{args.file} is empty")
    try:
        llm.verify_backend(check_adk=args.engine != "plain")
    except RuntimeError as exc:
        raise SystemExit(f"Backend check failed: {exc}") from None
    get_repo().init_schema()

    history = [Turn(role="user", text=lines[0])]
    answers = lines[1:]
    print(f"F1 interview: {args.file.name} (lang={lang})\n")
    print(f"👩‍🌾 FARMER: {lines[0]}")
    result = None
    for _ in range(f1_interview.settings.thresholds()["interview"]["max_questions"] + 1):
        ctx = f1_interview.build_context(history, lang, get_repo())
        result = asyncio.run(selector.execute(f1_interview, ctx, "f1", None, engine=args.engine, use_result_cache=False))
        show_trace(result)
        if result.type == "final":
            break
        print(f"\n🧑‍💼 AGENT ({result.field_targeted}): {result.question}")
        history.append(Turn(role="agent", text=result.question, field=result.field_targeted))
        if not answers:
            print("   (no more answers in the file; stopping before the interview finished)")
            break
        answer = answers.pop(0)
        print(f"👩‍🌾 FARMER: {answer}")
        history.append(Turn(role="user", text=answer))

    if result is None or result.type != "final":
        return
    print(f"\nFinal profile (questions asked: {result.questions_asked}, completeness {result.completeness_pct}%, "
          f"flags {result.profile['flags']}):")
    for f in f1_interview.FIELD_NAMES:
        print(f"   {f:26s} {result.profile.get(f)}")
    for d in result.profile.get("derived", []):
        print(f"   derived: {d}")
    print(f"   unknown_fields: {result.unknown_fields}")
    print(f"   engine={result.orchestrator_used} backend={result.genai_backend} model={result.gemini_model} "
          f"fallback_reason={result.fallback_reason}")

    if args.save:
        village = int(re.sub(r"\D", "", args.village) or 0)
        try:
            saved = f1_interview.save_household(result.profile, village, args.consent, get_repo())
        except ValueError as exc:
            raise SystemExit(f"Not saved: {exc}") from None
        print("\nSaved:", json.dumps(saved, ensure_ascii=False))


if __name__ == "__main__":
    main()
