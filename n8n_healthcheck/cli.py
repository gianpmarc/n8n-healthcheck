"""Command line: collect (or load) a Snapshot, run the rules, write the report."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from typing import Optional
from urllib.parse import urlsplit

from . import engine, profile, questions, report
from .models import Snapshot, Target

# Exit code when the check itself could not run (bad usage, I/O error, nothing
# collected). 0/1/2 are reserved for the result of a check that did run.
EXIT_ERROR = 3

# Target field -> the Snapshot section that proves the source was reached.
# --url also feeds "metrics", but that is legitimately empty when the metrics
# endpoint is disabled, so only "health" tells whether the URL worked; main()
# adds "metrics" to the failed sections separately.
SOURCE_SECTION = {
    "url": "health",
    "env_file": "env",
    "postgres_dsn": "postgres",
    "redis_url": "redis",
    "kube_namespace": "kubernetes",
}


def valid_url(url: str) -> bool:
    """True for an http(s) URL with a host. urllib would also open file:, ftp: and data:."""
    try:
        parts = urlsplit(url.strip())
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def json_safe(value):
    """A copy of JSON-like data with NaN and infinities, which are not valid JSON, as None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number (0-65535)")
    return port


# Flags added with the environment profile. An abbreviation that was unambiguous before
# them (`--save` for --save-snapshot, `--re` for --redis-url) keeps its meaning.
PROFILE_FLAGS = ("save_profile", "reason", "reference", "withhold", "show_profile")
# Flags added with the profile questions. They give way in the same manner, also to the
# profile flags: `--sh` stays --show-profile.
QUESTION_FLAGS = ("answer", "list_questions", "show_questions")


class _Parser(argparse.ArgumentParser):
    def _get_option_tuples(self, option_string):
        matches = super()._get_option_tuples(option_string)
        for newer in (QUESTION_FLAGS, QUESTION_FLAGS + PROFILE_FLAGS):
            older = [match for match in matches if match[0].dest not in newer]
            if len(matches) > 1 and len(older) == 1:
                return older
        return matches

    def error(self, message: str):
        # argparse exits 2 by default, which here means "critical finding".
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = _Parser(
        prog="n8n-healthcheck",
        description="Read-only health check for a self-hosted n8n instance. "
        "For a local web interface instead of these flags, run `n8n-healthcheck serve` "
        "(see `n8n-healthcheck serve --help`).",
        epilog="Exit codes: 0 no critical findings or warnings, 1 at least one warning, "
        "2 at least one critical finding, 3 the check could not run "
        "(usage error, unreadable or unwritable file, no requested source reachable, "
        "every rule failed to run). --show-profile and --show-questions exit 0, or 3 when the file "
        "is not a usable profile.",
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
    group = parser.add_argument_group(
        "environment profile",
        "One JSON file of facts about the environment, to review and share. "
        "It never holds a secret, host, URL, IP address, path or name. Withholding a fact "
        "does not remove the setting it was read from or a finding that mentions it: "
        "withhold those too.",
    )
    group.add_argument("--save-profile", metavar="FILE", help="write the environment profile as JSON")
    group.add_argument("--reason", choices=profile.REASONS, help="why the profile was taken (default: baseline)")
    group.add_argument("--reference", metavar="TEXT", help="free text stored in the profile; no secrets")
    group.add_argument(
        "--withhold",
        action="append",
        default=[],
        metavar="ID",
        help="leave this entry out of the profile: a fact id, setting:<scope>:<NAME> or check (repeatable)",
    )
    group.add_argument("--show-profile", metavar="FILE", help="print a saved profile as Markdown, then exit")
    group = parser.add_argument_group(
        "profile questions",
        "Closed questions no probe can answer. Every one is optional. An answer is written "
        'in the profile as "declared", and is dropped when the tool measured the fact itself.',
    )
    group.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="answer one question in the profile (repeatable); several values as ID=a,b",
    )
    group.add_argument("--list-questions", action="store_true", help="print the questions and their values, then exit")
    group.add_argument(
        "--show-questions", metavar="FILE", help="print what a saved profile leaves open, as Markdown, then exit"
    )
    args = parser.parse_args(argv)
    for flag in ("show_profile", "show_questions"):
        if getattr(args, flag) is not None:
            if [name for name, value in vars(args).items() if name not in (flag, "output") and value]:
                parser.error(f"--{flag.replace('_', '-')} takes no other flag except -o")
            return args
    if args.list_questions:
        if [name for name, value in vars(args).items() if name != "list_questions" and value]:
            parser.error("--list-questions takes no other flag")
        return args
    if args.save_profile is None:
        for flag in ("reason", "reference", "withhold", "answer"):
            if getattr(args, flag) not in (None, []):
                parser.error(f"--{flag} needs --save-profile")
    elif not args.save_profile.strip():
        parser.error("--save-profile must not be empty")
    elif args.snapshot:
        parser.error(
            "--save-profile cannot be combined with --snapshot: a saved snapshot does not "
            "record which sources failed or when it was taken"
        )
    try:
        args.reference = profile.check_reference(args.reference or "")
    except profile.ProfileError as exc:
        parser.error(f"--reference: {exc}")
    for item in args.withhold:
        # A setting id can only be checked once the environment has been read.
        if item not in profile.FACT_IDS and item != "check" and not item.startswith("setting:"):
            parser.error(f"--withhold: nothing in the profile has the id {item!r}")
    answers = {}
    for item in args.answer:
        fact_id, equals, value = (part.strip() for part in item.partition("="))
        if not equals:
            parser.error(f"--answer: {item!r} is not ID=VALUE")
        if fact_id in answers:
            parser.error(f"--answer: {fact_id!r} is answered twice")
        several = questions.OPERATOR_BY_ID.get(fact_id, {}).get("kind") == "multi"
        answers[fact_id] = [part.strip() for part in value.split(",")] if several else value
    try:
        profile.check_answers(answers)
    except profile.ProfileError as exc:
        parser.error(f"--answer: {exc} (see --list-questions)")
    args.answer = answers
    for source in SOURCE_SECTION:
        value = getattr(args, source)
        # The collectors test `is None`, so "" would be collected, not ignored.
        if value is not None and not value.strip():
            parser.error(f"--{source.replace('_', '-')} must not be empty")
    if args.url and not valid_url(args.url):
        parser.error("--url must be an http:// or https:// URL with a host")
    if not args.snapshot and not any(getattr(args, source) for source in SOURCE_SECTION):
        parser.error("nothing to inspect: give at least one source flag or --snapshot")
    return args


def failed_sections(target: Target, snapshot: Snapshot) -> tuple[list[str], bool]:
    """Sections that were requested but not collected, and whether that is all of them."""
    # Collectors do not raise for an unreachable source: they leave its
    # section None. Tell that apart from a source nobody asked for.
    requested = [section for source, section in SOURCE_SECTION.items() if getattr(target, source)]
    failed = [section for section in requested if getattr(snapshot, section) is None]
    nothing_collected = failed == requested
    # --url also asks for metrics; only a disabled endpoint (404) excuses it.
    if target.url and snapshot.metrics is None and not (snapshot.health or {}).get("metrics_disabled"):
        failed.append("metrics")
    return failed, nothing_collected


def result_code(findings: list, rule_count: int) -> int:
    """Exit code for a check that produced a report."""
    # Every rule crashed: the report is written, but it assessed nothing.
    skipped = sum(f.rule_id == engine.SKIPPED_RULE_ID for f in findings)
    if skipped and skipped == rule_count:
        return EXIT_ERROR
    severities = {f.severity for f in findings}
    if "critical" in severities:
        return 2
    if "warning" in severities:
        return 1
    return 0


def _serve(argv: list[str]) -> int:
    parser = _Parser(
        prog="n8n-healthcheck serve",
        description="Local web interface for the health check. The server has no "
        "authentication: keep it on a loopback address unless the network is trusted.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    parser.add_argument("--port", type=_port, default=8080, help="port to listen on (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the page in the default browser")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME[:PORT]",
        help="extra Host header value to accept besides localhost, 127.0.0.1 and [::1] "
        "(repeatable; without a port, the serving port is assumed)",
    )
    args = parser.parse_args(argv)
    from .web import server

    try:
        return server.serve(args.host, args.port, not args.no_browser, args.allow_host)
    except OSError as exc:
        return _error(f"cannot listen on {args.host}:{args.port}: {exc.strerror or exc}")


def _error(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _show_profile(path: str, output: Optional[str], render) -> int:
    """Print a saved profile through `render`: as it is, or as its question sheet."""
    try:
        markdown = render(profile.load_profile(path))
    except OSError as exc:
        return _error(f"cannot read profile {path}: {exc.strerror}")
    except profile.ProfileError as exc:
        return _error(f"cannot show profile {path}: {exc}")
    except Exception as exc:  # whatever a hand-edited file holds, never a traceback
        return _error(f"cannot show profile {path}: not a valid profile ({type(exc).__name__})")
    try:
        if output:
            with open(output, "w", encoding="utf-8") as fh:
                fh.write(markdown)
        else:
            sys.stdout.write(markdown)
    except OSError as exc:
        return _error(f"cannot write {exc.filename}: {exc.strerror}")
    except ValueError:  # text the output encoding cannot represent
        return _error(f"cannot show profile {path}: it holds text that cannot be written here")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "serve":
        return _serve(argv[1:])
    args = _parse_args(argv)
    if args.show_profile is not None:
        return _show_profile(args.show_profile, args.output, profile.render_markdown)
    if args.show_questions is not None:
        return _show_profile(args.show_questions, args.output, profile.render_questions)
    if args.list_questions:
        sys.stdout.write(questions.render_list())
        return 0
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

        failed, nothing_collected = failed_sections(target, snapshot)
        if nothing_collected:
            return _error("none of the requested sources could be collected")

    # Imported here so the rule set is read at run time, not at import time.
    from .rules import ALL_RULES

    findings = engine.run(snapshot, ALL_RULES)
    markdown = report.render_markdown(snapshot, findings, failed=tuple(failed))
    if args.save_profile is not None:
        try:
            # Probes, then answers, then withholding: a declared fact can be withheld too.
            saved_profile = profile.build_profile(
                snapshot, findings, tuple(failed), args.reason or "baseline", args.reference
            )
            saved_profile, dropped = profile.apply_answers(saved_profile, args.answer)
            saved_profile = profile.apply_withholding(saved_profile, args.withhold)
        except profile.ProfileError as exc:
            return _error(f"--withhold: {exc}")
        for fact_id in dropped:
            print(f"note: the answer to {fact_id} was dropped: the tool measured it", file=sys.stderr)
    try:
        if args.save_snapshot:
            with open(args.save_snapshot, "w", encoding="utf-8") as fh:
                # A metric can be NaN or +Inf, which json would write as invalid JSON.
                json.dump(json_safe(asdict(snapshot)), fh, indent=2, allow_nan=False)
        if args.save_profile is not None:
            with open(args.save_profile, "w", encoding="utf-8") as fh:
                fh.write(profile.to_json(saved_profile))
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(markdown)
        else:
            sys.stdout.write(markdown)
    except OSError as exc:
        return _error(f"cannot write {exc.filename}: {exc.strerror}")

    code = result_code(findings, len(ALL_RULES))
    if code == EXIT_ERROR:
        return _error("no rule could run; see \"Not assessed\" in the report")
    return code
