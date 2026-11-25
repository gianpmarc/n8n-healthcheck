"""HTTP server for `n8n-healthcheck serve`: static page plus /api/meta, /api/debug-info and /api/run.

The server has no authentication and can be told to connect to arbitrary hosts
and databases, so every request is checked against the Host header (DNS
rebinding) and a run also against Content-Type and Origin (cross-site
requests). Nothing from a request is logged, and nothing is written to disk
except the pasted env file, in a private temporary directory, for the duration
of the run. An n8n API key goes to the collector and nowhere else: it is not in the
response, a log line or an error. A pasted debug information is read in memory by debug_info.parse()
and only what the parser kept goes on: the text is never written, logged or
returned.

A profile is built only for a run that was given a debug information: without one the
answer of /api/run holds the report and no profile (the same rule as --save-profile on
the command line). /api/debug-info reads a pasted debug information before the run,
collects nothing, and says what was read and what the rules decide from it alone.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import n8n_healthcheck

from .. import cli, debug_info, engine, profile, questions, report, usage
from ..models import SOURCE_IDS, Snapshot, Target

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 1024 * 1024
LOCAL_NAMES = ("localhost", "127.0.0.1", "[::1]")
WILDCARD_HOSTS = ("", "0.0.0.0", "::")
# The same policy as the page's own <meta>, plus what only a header can say.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)

# Request field -> Target field. env_text is the content of an env file, never a path.
# debug_info_text is n8n's "Debug information" text; parse_run_request() replaces it with
# what debug_info.parse() kept of it.
RUN_FIELDS = {
    "debug_info_text": "debug_info",
    "url": "url",
    # An n8n API key, used with `url` to read usage numbers. Never logged or returned.
    "api_key": "api_key",
    "env_text": "env_file",
    "postgres_dsn": "postgres_dsn",
    "redis_url": "redis_url",
    "kube_namespace": "kube_namespace",
    "kube_context": "kube_context",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# The two requests that take a body. Both pass the same checks before it is read.
POST_ROUTES = ("/api/run", "/api/debug-info")


class BadRequest(Exception):
    """The request body is not a valid run request; the message is safe to return.

    `field` names the request field the page should show the message next to, if any.
    """

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def meta() -> dict:
    def source(id_, label, hint, binary=None):
        available = binary is None or shutil.which(binary) is not None
        if not available:
            hint = f"{binary} was not found on PATH, so {label} cannot be inspected from here."
        return {"id": id_, "label": label, "available": available, "hint": hint}

    return {
        "version": getattr(n8n_healthcheck, "__version__", "dev"),
        "sources": [
            source(
                "debug_info",
                "Debug information",
                "The text n8n gives under Help > About n8n > Debug information > Copy. "
                "The instance ids, host ids and the browser's user agent in it are read and discarded.",
            ),
            source("url", "n8n URL", "Base URL of the n8n instance; reads its health endpoints and /metrics."),
            # Not a fieldset of its own: the key is a field of the "n8n URL" source.
            source(
                "api",
                "n8n API key",
                "Optional, with the URL: reads executions per day, the failure rate, the active "
                "workflows and the users through n8n's public API, with GET requests only.",
            ),
            source("env", "Environment file", "Content of the n8n env file (KEY=VALUE lines); secret values are redacted."),
            source("postgres", "Postgres", "Connection string of the n8n database; read with the psql client.", "psql"),
            source("redis", "Redis", "Connection URL of the Redis that holds the n8n queue."),
            source("kubernetes", "Kubernetes", "Namespace running n8n; read with kubectl.", "kubectl"),
        ],
        # The catalogue of questions.py as data: the page holds no copy of it. `operator` is
        # asked in the share step; all three are needed to write the question sheet of an
        # opened profile, with `measured` in the order of the sheet.
        "questions": {
            "operator": [
                {**question, "values": [{"value": value, "label": label} for value, label in question["values"]]}
                for question in questions.OPERATOR
            ],
            "conversation": [{"question": question, "why": why} for question, why in questions.CONVERSATION],
            "measured": [
                {"id": fact_id, "question": questions.MEASURED[fact_id]}
                for fact_id in profile.FACT_IDS
                if fact_id not in questions.OPERATOR_BY_ID
            ],
        },
    }


def _json_object(body: bytes, known) -> dict:
    """The JSON object of a request body, with no field other than those in `known`."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):  # ValueError includes UnicodeDecodeError; RecursionError: deep nesting
        raise BadRequest("the request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("the request body must be a JSON object")
    unknown = sorted(set(data) - set(known))
    if unknown:
        # Names only, cut short: an unknown field is still client input.
        raise BadRequest("unknown field: " + ", ".join(name[:40] for name in unknown[:5]))
    return data


def parse_debug_info_request(body: bytes) -> dict:
    """Validate the JSON body of POST /api/debug-info; return what debug_info.parse() kept.

    The body holds `debug_info_text` and nothing else. As in parse_run_request(), the text
    does not leave this function, and a refusal is the parser's fixed sentence.
    """
    text = _json_object(body, ("debug_info_text",)).get("debug_info_text")
    if not isinstance(text, str) or not text.strip():
        raise BadRequest("debug_info_text must be a text that is not empty", "debug_info_text")
    try:
        return debug_info.parse(text)
    except debug_info.DebugInfoError as exc:
        raise BadRequest(str(exc), "debug_info_text") from None


def parse_run_request(body: bytes) -> dict:
    """Validate the JSON body of POST /api/run; return only the fields that are set.

    `debug_info_text` comes back as the dictionary debug_info.parse() made of it: the text
    itself does not leave this function. A text the parser refuses is a BadRequest with
    the parser's fixed sentence, which holds nothing of the text.
    """
    data = _json_object(body, RUN_FIELDS)
    fields = {}
    for name, value in data.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise BadRequest(f"{name} must be a string")
        if value.strip():
            fields[name] = value if name in ("env_text", "debug_info_text") else value.strip()
    # urllib would also open file:, ftp: and data: URLs, i.e. read local files.
    if "url" in fields and not cli.valid_url(fields["url"]):
        raise BadRequest("url must be an http:// or https:// URL with a host")
    if "api_key" in fields:
        if "url" not in fields:
            raise BadRequest("the API key needs the n8n URL: it is used on the API of that instance", "api_key")
        try:
            fields["api_key"] = usage.check_key(fields["api_key"])
        except usage.KeyRefused as exc:
            raise BadRequest(str(exc), "api_key") from None
    if not set(fields) - {"kube_context"}:
        raise BadRequest(
            "nothing to inspect: give at least one of debug_info_text, url, env_text, postgres_dsn, redis_url, kube_namespace"
        )
    if "debug_info_text" in fields:
        try:
            fields["debug_info_text"] = debug_info.parse(fields["debug_info_text"])
        except debug_info.DebugInfoError as exc:
            raise BadRequest(str(exc), "debug_info_text") from None
    return fields


class _Tee:
    """Stands in for sys.stderr during a run: passes everything on, keeps this thread's lines."""

    def __init__(self, real):
        self._real = real
        self._thread = threading.get_ident()
        self.captured: list[str] = []

    def write(self, text):
        if threading.get_ident() == self._thread:
            self.captured.append(text)
        return self._real.write(text)

    def __getattr__(self, name):
        return getattr(self._real, name)


def coverage(checks: list) -> dict:
    """How much of the check a run decided, counted from the `check.checks` of its profile.

    Nothing here is a prediction or a table: every number is a count of what the rules
    stated in that run. `decided` are the checks that ended as a finding, passed or not
    applicable. `waiting` counts, per source, the checks that were not assessed because no
    source that states what they read was given and that name the source among their
    `needs`; `failed` does the same for a source that was asked for and could not be
    collected. A check that names several sources is counted under each of them, so
    these numbers are not a sum. `unstated` are the checks for which a source was read
    and does not state the value, or holds it unreadable: no rule names a source for
    those. `disagree` and `errors` are two sources contradicting each other and a rule
    that failed to run.
    """
    waiting, failed = dict.fromkeys(SOURCE_IDS, 0), dict.fromkeys(SOURCE_IDS, 0)
    reasons = dict.fromkeys(("not_stated", "value_unreadable", "sources_disagree", "rule_error"), 0)
    decided = 0
    for entry in checks:
        if entry["outcome"] != "not_assessed":
            decided += 1
        elif entry["reason"] in ("source_missing", "source_failed"):
            counted = waiting if entry["reason"] == "source_missing" else failed
            for need in entry["needs"]:
                counted[need] += 1
        else:
            reasons[entry["reason"]] += 1
    return {
        "total": len(checks),
        "decided": decided,
        "waiting": waiting,
        "failed": failed,
        "unstated": reasons["not_stated"] + reasons["value_unreadable"],
        "disagree": reasons["sources_disagree"],
        "errors": reasons["rule_error"],
    }


def check_debug_info(read: dict) -> dict:
    """The response for POST /api/debug-info: what was read, and what it decides alone.

    `read` is what debug_info.parse() kept. Nothing is collected: the rules run on a
    snapshot that holds the debug information and nothing else, as in a run that is given
    no other source. `notes` are the sentences the report gives about the text (its age,
    a missing cluster section), from the report's own rule.
    """
    from ..rules import ALL_RULES  # read at run time, as in run_check()

    snapshot = Snapshot(debug_info=read)
    section = debug_info.of(snapshot)
    mode, multi_main, cluster = section["execution_mode"], section["multi_main"], section["cluster"]
    if cluster is None:
        instances = "not stated"
    else:
        counted = [f"{count} {name}" for name, count in cluster["instances"].items() if count]
        instances = f"{sum(cluster['instances'].values())} ({', '.join(counted)})" if counted else "none listed"
    built = profile.build_profile(snapshot, [], (), checks=engine.assess(snapshot, ALL_RULES))
    today = datetime.now(timezone.utc).date()
    return {
        "read": [
            {"label": "n8n version", "value": section["n8n_version"]},
            {
                "label": "Execution mode",
                "value": "not stated" if mode is None else mode + {True: " (multi-main)", False: " (single main)", None: ""}[multi_main],
            },
            {"label": "Instances", "value": instances},
            {"label": "Generated on", "value": section["generated_on"] or "date unknown"},
        ],
        # Without the first sentence, which only says that the text was read.
        "notes": report._debug_info_summary(section, False, (), today)[1:],
        "coverage": coverage(built["check"]["checks"]),
    }


def run_check(fields: dict) -> dict:
    """Collect, run the rules and build the response for POST /api/run.

    `profile` and `coverage` are None for a run without a debug information: the report
    needs none, a profile does.
    """
    # Imported here for the same reasons as in cli.main().
    from ..collectors import collect
    from ..rules import ALL_RULES

    target_fields = {RUN_FIELDS[name]: value for name, value in fields.items()}
    with contextlib.ExitStack() as stack:
        if "env_text" in fields:
            # The env collector reads a file, and Redis needs the env collected before it,
            # so the text goes through the same path as --env-file: a 0600 file in a
            # private (0700) directory that is removed when this block exits, error or not.
            private = stack.enter_context(tempfile.TemporaryDirectory(prefix="n8n-healthcheck-"))
            path = os.path.join(private, "n8n.env")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
                handle.write(fields["env_text"])
            target_fields["env_file"] = path
        target = Target(**target_fields)
        tee = _Tee(sys.stderr)
        with contextlib.redirect_stderr(tee):
            snapshot = collect(target)

    failed, nothing_collected = cli.failed_sections(target, snapshot)
    warnings = [line for line in "".join(tee.captured).splitlines() if line.strip()]
    checks = engine.assess(snapshot, ALL_RULES)
    findings = engine.findings_of(checks)
    # The CLI stops with exit code 3 before the report when no source answered; here a
    # report is still returned, saying that the check could not run and why.
    report_args = (snapshot, findings, tuple(failed), tuple(warnings) if nothing_collected else None)
    data = report.build_report(*report_args)
    data["findings"] = [asdict(finding) for finding in data["findings"]]
    # Default reason, no reference, nothing withheld: the page blanks entries itself.
    built = profile.build_profile(snapshot, findings, tuple(failed), checks=checks) if "debug_info_text" in fields else None
    return {
        **data,
        "exit_code": cli.EXIT_ERROR if nothing_collected else cli.result_code(findings, len(ALL_RULES)),
        "warnings": warnings,
        "markdown": report.render_markdown(*report_args),
        "snapshot": asdict(snapshot),
        "profile": built,
        "coverage": coverage(built["check"]["checks"]) if built else None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "n8n-healthcheck"
    sys_version = ""
    timeout = 30  # seconds a client may take to send its request

    def end_headers(self):
        # Here, not in _send(), so that http.server's own error pages (501, 414, a
        # malformed request) carry them too.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")  # with frame-ancestors: no clickjacking
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        super().end_headers()

    def log_request(self, code="-", size="-"):
        # Method, path and status only: a query string is client input and is never logged.
        path = re.sub(r"[^!-~]", "?", urlsplit(getattr(self, "path", "") or "").path)[:200]
        self.log_message('"%s %s" %s', self.command or "-", path or "-", getattr(code, "value", code))

    def log_error(self, format, *args):
        pass  # http.server quotes the raw request line here; log_request() records the status

    def do_GET(self):
        if not self._host_allowed():
            return
        path = urlsplit(self.path).path
        if path == "/api/meta":
            self._send_json(200, meta())
        elif path in POST_ROUTES:
            self._send_json(405, {"error": f"use POST for {path}"})
        elif path == "/":
            self._send_static("index.html")
        elif path.startswith("/static/"):
            self._send_static(unquote(path[len("/static/"):]))
        else:
            self._send_json(404, {"error": "not found"})

    do_HEAD = do_GET  # _send() leaves the body out

    def do_POST(self):
        if not self._host_allowed():
            return
        path = urlsplit(self.path).path
        if path not in POST_ROUTES:
            return self._send_json(404, {"error": "not found"})
        origin = self.headers.get("Origin")
        # Host was checked above, so an Origin on the same host and port is this page
        # (the scheme is not compared: a TLS proxy in front makes it https).
        try:
            same_origin = origin is None or urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()
        except ValueError:
            same_origin = False
        if not same_origin:
            return self._send_json(403, {"error": "cross-origin requests are not allowed"})
        if self.headers.get_content_type() != "application/json":  # text/plain when the header is absent
            return self._send_json(415, {"error": "Content-Type must be application/json"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            return self._send_json(400, {"error": "a Content-Length header is required"})
        if length > MAX_BODY_BYTES:
            # Discard (a bounded amount of) the body first: a client that is still
            # sending would otherwise see a connection reset instead of the 413.
            remaining = min(length, 16 * MAX_BODY_BYTES)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            return self._send_json(413, {"error": f"the request body is larger than {MAX_BODY_BYTES} bytes"})
        body = self.rfile.read(length)
        try:
            fields = parse_debug_info_request(body) if path == "/api/debug-info" else parse_run_request(body)
        except BadRequest as exc:
            return self._send_json(400, {"error": str(exc), **({"field": exc.field} if exc.field else {})})
        if path == "/api/debug-info":
            # Nothing is collected and nothing of the process is redirected: no lock is needed.
            try:
                return self._send_json(200, check_debug_info(fields))
            except Exception as exc:
                name = type(exc).__name__
                print(f"reading the debug information failed: {name}", file=sys.stderr)
                return self._send_json(500, {"error": f"the debug information could not be read ({name})"})
        if not self.server.run_lock.acquire(blocking=False):
            return self._send_json(409, {"error": "a health check is already running; wait for it to finish"})
        try:
            result = run_check(fields)
        except Exception as exc:
            # The message may quote a DSN or a URL with a password: keep only the type.
            name = type(exc).__name__
            print(f"run failed: {name}", file=sys.stderr)
            return self._send_json(500, {"error": f"the health check failed unexpectedly ({name})"})
        finally:
            self.server.run_lock.release()
        self._send_json(200, result)

    def _host_allowed(self) -> bool:
        if self.headers.get("Host", "").lower() in self.server.allowed_hosts:
            return True
        self._send_json(
            403,
            {"error": "this Host header is not accepted; open the page as localhost or start the server with --allow-host"},
        )
        return False

    def _send_static(self, relative: str) -> None:
        try:
            path = (STATIC_DIR / relative).resolve()
            found = STATIC_DIR in path.parents and path.is_file()
        except (OSError, ValueError):  # e.g. a NUL byte in the name
            found = False
        if not found:
            return self._send_json(404, {"error": "not found"})
        content_type = (
            CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        self._send(200, content_type, path.read_bytes())

    def _send_json(self, status: int, payload: dict) -> None:
        # A scraped metric can be NaN or +Inf, which json would write as invalid JSON.
        body = json.dumps(cli.json_safe(payload), allow_nan=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def allowed_hosts(host: str, port: int, allow_hosts=()) -> frozenset:
    """Host header values accepted by a server bound to host:port."""
    # A specific bind address is itself a name the page is opened with.
    bind = () if host in WILDCARD_HOSTS else (f"[{host}]" if ":" in host else host,)
    names = set()
    for name in (*LOCAL_NAMES, *bind, *allow_hosts):
        name = name.strip().lower()
        if re.search(r":\d+$", name):  # an explicit port, e.g. a different published port
            names.add(name)
        elif name:
            names.add(f"{name}:{port}")
            if port == 80:  # browsers leave the default port out of Host
                names.add(name)
    return frozenset(names)


def make_server(host: str, port: int, allow_hosts=()) -> ThreadingHTTPServer:
    """Bind the server (port 0 picks a free port). The caller runs serve_forever()."""

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

        def handle_error(self, request, client_address):
            # The default prints a traceback, whose message could quote request data.
            print(f"request from {client_address[0]} failed: {sys.exc_info()[0].__name__}", file=sys.stderr)

    server = Server((host, port), Handler)
    server.allowed_hosts = allowed_hosts(host, server.server_address[1], allow_hosts)
    server.run_lock = threading.Lock()  # one run at a time
    return server


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(host: str, port: int, open_browser: bool, allow_hosts=()) -> int:
    server = make_server(host, port, allow_hosts)
    shown = "localhost" if host in WILDCARD_HOSTS else f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"n8n-healthcheck is serving on {url} (Ctrl+C to stop)", flush=True)
    if not _is_loopback(host):
        print(
            f"warning: listening on {host}, not a loopback address. This server has no authentication: "
            "anyone who can reach it can make it connect to hosts and databases of their choice. "
            "The number of simultaneous connections is not limited either.",
            file=sys.stderr,
            flush=True,
        )

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)  # `docker stop`
    if open_browser:
        # The socket is already listening, so the page loads as soon as serve_forever() starts.
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
