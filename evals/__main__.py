"""CLI entry point: ``python -m evals``."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

# Importing the package redirects DATABASE_URL before `database` is loaded.
import evals  # noqa: F401
from evals.dataset import ALL_TAGS, CASES
from evals.runner import PASS_THRESHOLD, format_report, run_suite, select_cases


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="Run the ReliefAI evaluation suite.",
    )
    parser.add_argument("--case", action="append", help="Run only this case (repeatable)")
    parser.add_argument("--tag", action="append", help="Run only cases with this tag")
    parser.add_argument("--limit", type=int, help="Run at most N cases")
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the LLM judge (faster and cheaper; deterministic scorers only)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "Pause between cases. Useful on Groq's free tier, whose "
            "tokens-per-minute budget a full run otherwise saturates."
        ),
    )
    parser.add_argument("--json", metavar="PATH", help="Write the full report as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--list", action="store_true", help="List cases and exit")
    args = parser.parse_args()

    if args.list:
        print(f"{len(CASES)} cases   tags: {', '.join(ALL_TAGS)}\n")
        for case in CASES:
            print(f"  {case.name}")
            print(f"      scenario: {case.scenario}   tags: {', '.join(case.tags)}")
            print(f"      command : {case.command}")
        return 0

    cases = select_cases(names=args.case, tags=args.tag, limit=args.limit)
    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 2

    use_judge = not args.no_judge
    print(
        f"Running {len(cases)} case(s)"
        f"{'' if use_judge else ' without the LLM judge'}…\n",
        flush=True,
    )

    def _progress(result):
        mark = "PASS" if result["passed"] else "FAIL"
        detail = result.get("error", f"{result['score']:.2f}")
        print(f"  [{mark}] {result['case']:<42} {detail}", flush=True)

    report = asyncio.run(
        run_suite(cases, use_judge=use_judge, delay_s=args.delay, on_result=_progress)
    )

    print()
    print(format_report(report, verbose=args.verbose))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, default=str)
        print(f"\nJSON report written to {args.json}")

    # Exit codes let CI tell a quality regression apart from a flaky environment:
    #   0 = everything assessed passed
    #   1 = at least one case genuinely failed
    #   3 = nothing failed, but some cases could not be assessed
    if report["failed"]:
        return 1
    if report.get("errored"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
