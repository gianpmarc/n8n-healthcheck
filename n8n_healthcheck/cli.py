"""Command line: collect (or load) a Snapshot, run the rules, write the report."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Optional

from . import engine, report
from .models import Snapshot, Target


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="n8n-healthcheck",
        description="Read-only health check for a self-hosted n8n instance.",
        epilog="Exit code: 2 if any critical finding, 1 if any warning, 0 otherwise; "
        "3 if the check itself could not run.",
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
    sources = (args.url, args.env_file, args.postgres_dsn, args.redis_url, args.kube_namespace)
    if not args.snapshot and not any(sources):
        parser.error("nothing to inspect: give at least one source flag or --snapshot")
    return args


def _collect(args: argparse.Namespace) -> Snapshot:
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
    return collect(target)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    if args.snapshot:
        try:
            with open(args.snapshot, encoding="utf-8") as fh:
                snapshot = Snapshot(**json.load(fh))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: cannot load snapshot {args.snapshot}: {exc}", file=sys.stderr)
            return 3
    else:
        try:
            snapshot = _collect(args)
        except Exception as exc:
            # The DSN and Redis URL contain passwords: never echo them back.
            message = f"{type(exc).__name__}: {exc}"
            for secret in (args.postgres_dsn, args.redis_url):
                if secret:
                    message = message.replace(secret, "***")
            print(f"error: collection failed: {message}", file=sys.stderr)
            return 3

    if args.save_snapshot:
        with open(args.save_snapshot, "w", encoding="utf-8") as fh:
            json.dump(asdict(snapshot), fh, indent=2)

    # Imported here so the rule set is read at run time, not at import time.
    from .rules import ALL_RULES

    findings = engine.run(snapshot, ALL_RULES)
    markdown = report.render_markdown(snapshot, findings)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(markdown)
    else:
        sys.stdout.write(markdown)

    severities = {f.severity for f in findings}
    if "critical" in severities:
        return 2
    if "warning" in severities:
        return 1
    return 0
