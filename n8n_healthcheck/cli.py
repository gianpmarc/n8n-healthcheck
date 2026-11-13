"""Command line: collect (or load) a Snapshot, run the rules, write the report."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Optional

from . import engine, report
from .models import Snapshot, Target

# Exit code when the check itself could not run (bad usage, I/O error, nothing
# collected). 0/1/2 are reserved for the result of a check that did run.
EXIT_ERROR = 3

# Target field -> the Snapshot section that proves the source was reached.
# --url also feeds "metrics", but that is legitimately empty when the metrics
# endpoint is disabled, so only "health" tells whether the URL worked.
SOURCE_SECTION = {
    "url": "health",
    "env_file": "env",
    "postgres_dsn": "postgres",
    "redis_url": "redis",
    "kube_namespace": "kubernetes",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # argparse exits 2 by default, which here means "critical finding".
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = _Parser(
        prog="n8n-healthcheck",
        description="Read-only health check for a self-hosted n8n instance.",
        epilog="Exit codes: 0 no critical findings or warnings, 1 at least one warning, "
        "2 at least one critical finding, 3 the check could not run "
        "(usage error, unreadable or unwritable file, no requested source reachable).",
    )
    parser.add_argument("--url", help="n8n base URL (health endpoints and /metrics)")
    parser.add_argument("--env-file", help="KEY=VALUE file with the n8n environment")
    parser.add_argument("--postgres-dsn", help="Postgres connection string")
    parser.add_argument("--redis-url", help="Redis connection URL")
    parser.add_argument("--kube-namespace", help="Kubernetes namespace running n8n")
    parser.add_argument("--kube-context", help="kubectl context (default: current)")
    parser.add_argument("--snapshot", metavar="FILE", help="load a saved snapshot instead of collecting")
    parser.add_argument("--save-snapshot", metavar="FILE", help="write the collected snapshot as JSON")
    parser.add_argument("-o", "--output", metavar="FILE", help="write the report here (default: stdout)")
    args = parser.parse_args(argv)
    if not args.snapshot and not any(getattr(args, source) for source in SOURCE_SECTION):
        parser.error("nothing to inspect: give at least one source flag or --snapshot")
    return args


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    failed: list[str] = []  # sections requested on the command line but not collected

    if args.snapshot:
        try:
            with open(args.snapshot, encoding="utf-8") as fh:
                snapshot = Snapshot(**json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            return _error(f"cannot load snapshot {args.snapshot}: {exc}")
    else:
        # Imported here so --snapshot runs work without the collectors.
        from .collectors import collect

        target = Target(
            url=args.url,
            env_file=args.env_file,
            postgres_dsn=args.postgres_dsn,
            redis_url=args.redis_url,
            kube_namespace=args.kube_namespace,
            kube_context=args.kube_context,
        )
        try:
            snapshot = collect(target)
        except Exception as exc:
            # The DSN and Redis URL contain passwords: never echo them back.
            message = f"{type(exc).__name__}: {exc}"
            for secret in (args.postgres_dsn, args.redis_url):
                if secret:
                    message = message.replace(secret, "***")
            return _error(f"collection failed: {message}")

        # Collectors do not raise for an unreachable source: they leave its
        # section None. Tell that apart from a source nobody asked for.
        requested = [section for source, section in SOURCE_SECTION.items() if getattr(target, source)]
        failed = [section for section in requested if getattr(snapshot, section) is None]
        if failed == requested:
            return _error("none of the requested sources could be collected")
        if "health" in failed:
            failed.append("metrics")

    # Imported here so the rule set is read at run time, not at import time.
    from .rules import ALL_RULES

    findings = engine.run(snapshot, ALL_RULES)
    markdown = report.render_markdown(snapshot, findings, failed=tuple(failed))
    try:
        if args.save_snapshot:
            with open(args.save_snapshot, "w", encoding="utf-8") as fh:
                json.dump(asdict(snapshot), fh, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(markdown)
        else:
            sys.stdout.write(markdown)
    except OSError as exc:
        return _error(f"cannot write {exc.filename}: {exc.strerror}")

    severities = {f.severity for f in findings}
    if "critical" in severities:
        return 2
    if "warning" in severities:
        return 1
    return 0
