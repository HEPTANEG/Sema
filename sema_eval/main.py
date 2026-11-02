import argparse
from pathlib import Path
import sys

if __package__:
    from . import runner
    from .benchmarks import longmemeval
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sema_eval import runner
    from sema_eval.benchmarks import longmemeval


SUITE_CHOICES = [
    "smoke",
    "regression",
    "tool",
    "file",
    "recovery",
    "conversation",
    "hard",
    "path",
    "scenario",
    "research",
    "memory_engine",
    "all",
    "failed",
]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _interactive()

    if argv[0] == "longmemeval":
        return longmemeval.main(argv[1:])

    if argv[0] == "halumem":
        return _run_halumem(argv[1:])

    if argv[0] == "report":
        parser = argparse.ArgumentParser(prog="sema-eval report")
        parser.add_argument("--for", dest="target", choices=["codex"], default="codex")
        parser.parse_args(argv[1:])
        path = runner.regenerate_codex_report()
        print(f"Codex brief regenerated: {path}")
        return 0

    if argv[0] == "clear":
        result = runner.clear_eval_results()
        print(f"Eval results cleared: {result['removed_count']} item(s)")
        return 0

    parser = argparse.ArgumentParser(prog="sema-eval")
    parser.add_argument("suite", choices=SUITE_CHOICES)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--keep-failed-workspaces", action="store_true")
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--failed-only", action="store_true")
    parser.add_argument("--judge", dest="judge", action="store_true", default=True)
    parser.add_argument("--no-judge", dest="judge", action="store_false")
    parser.add_argument("--sema-command", help="Command used to launch Sema. Defaults to SEMA_COMMAND or `sema`.")
    parser.add_argument("--turn-timeout", type=int, default=runner.DEFAULT_TURN_TIMEOUT_SECONDS)
    parser.add_argument("--no-transcript-stream", action="store_true")
    parser.add_argument("--tool-result-chars", type=int, default=runner.DEFAULT_TOOL_RESULT_CHARS)
    args = parser.parse_args(argv)

    if args.trials < 1:
        raise RuntimeError("--trials must be at least 1.")

    latest = runner.run_benchmark(
        args.suite,
        trials=args.trials,
        keep_failed_workspaces=args.keep_failed_workspaces,
        keep_workspaces=args.keep_workspaces,
        judge=args.judge,
        failed_only=args.failed_only,
        sema_command=args.sema_command,
        turn_timeout=args.turn_timeout,
        transcript_stream=not args.no_transcript_stream,
        tool_result_chars=args.tool_result_chars,
    )
    runner.print_run_summary(latest)
    return 0


def _run_halumem(argv):
    try:
        if __package__:
            from .benchmarks import halumem
        else:
            from sema_eval.benchmarks import halumem
    except FileNotFoundError as error:
        raise RuntimeError(
            "HaluMem support requires official evaluator sources that are not distributed with Sema."
        ) from error
    return halumem.main(argv)


def _interactive():
    counts = runner.list_suites()
    print("Sema Eval suites:")
    for suite in SUITE_CHOICES:
        if suite == "failed":
            continue
        count = sum(counts.values()) if suite == "all" else counts.get(suite, 0)
        print(f"- {suite}: {count}")
    print()
    print("Usage: sema-eval <suite> [--trials N] [--keep-failed-workspaces] [--sema-command sema]")
    print("       [--turn-timeout 180] [--no-transcript-stream] [--tool-result-chars 600]")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
