import contextlib
import http.client
import io
import json
import os
import re
import socket
import stat
import threading
import unittest
from dataclasses import asdict
from unittest import mock

import n8n_healthcheck
from n8n_healthcheck import cli, engine, profile, questions
from n8n_healthcheck.models import Finding, Snapshot
from n8n_healthcheck.web import server as web

SNAPSHOT = Snapshot(health={"healthz": True, "readiness": True}, metrics={}, env={"EXECUTIONS_MODE": "regular"}, n8n_version="1.70.0")
PASSWORD = "s3cr3t-pw"
DSN = f"postgresql://n8n:{PASSWORD}@db.internal:5432/n8n"
REDIS_URL = f"redis://:{PASSWORD}@redis.internal:6379/0"


def rule(severity, area="scalability"):
    def fake_rule(snapshot):
        return Finding(
            rule_id=f"TEST-{severity}",
            area=area,
            severity=severity,
            title=f"A {severity} finding",
            evidence="EXECUTIONS_MODE=regular",
            impact="impact",
            recommendation="recommendation",
        )

    return fake_rule


RULES = [rule("info"), rule("critical"), rule("warning", "security")]


class WebTest(unittest.TestCase):
    def setUp(self):
        # Everything the server process writes to stderr (access log, collector warnings).
        self.log = io.StringIO()
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.start_server()

    def start_server(self, allow_hosts=()):
        self.server = web.make_server("127.0.0.1", 0, allow_hosts)
        self.port = self.server.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        """Return (status, headers, body bytes). `body` may be bytes or a JSON-able object."""
        headers = dict(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()

    def run_api(self, body, snapshot=SNAPSHOT, rules=RULES, headers=None):
        """POST /api/run with collect() and the rules faked; return (status, JSON, raw text)."""
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot) as self.collect:
            with mock.patch("n8n_healthcheck.rules.ALL_RULES", list(rules)):
                status, _, raw = self.request("POST", "/api/run", body, headers)
        return status, json.loads(raw), raw.decode()

    # --- meta and static files -------------------------------------------------

    def test_meta_reports_missing_binaries(self):
        with mock.patch("shutil.which", side_effect=lambda name: None if name == "psql" else "/usr/bin/" + name):
            status, headers, raw = self.request("GET", "/api/meta")
        meta = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(meta["version"], n8n_healthcheck.__version__)
        self.assertRegex(meta["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual([s["id"] for s in meta["sources"]], ["url", "env", "postgres", "redis", "kubernetes"])
        sources = {s["id"]: s for s in meta["sources"]}
        self.assertFalse(sources["postgres"]["available"])
        self.assertIn("psql was not found", sources["postgres"]["hint"])
        self.assertTrue(sources["kubernetes"]["available"])
        self.assertTrue(sources["url"]["available"])
        for source in meta["sources"]:
            self.assertEqual(set(source), {"id", "label", "available", "hint"})
            self.assertTrue(source["label"] and source["hint"])

    def test_meta_serves_the_catalogue_of_questions(self):
        served = json.loads(self.request("GET", "/api/meta")[2])["questions"]
        self.assertEqual(set(served), {"operator", "conversation", "measured"})
        self.assertEqual(
            [(q["id"], q["question"], q["kind"], tuple((v["value"], v["label"]) for v in q["values"])) for q in served["operator"]],
            [(q["id"], q["question"], q["kind"], q["values"]) for q in questions.OPERATOR],
        )
        for question in served["operator"]:
            self.assertEqual(set(question), {"id", "question", "kind", "values"})
        self.assertEqual([(q["question"], q["why"]) for q in served["conversation"]], list(questions.CONVERSATION))
        # Part 1 of the sheet, in its order: every fact of the catalogue that is not an operator question.
        self.assertEqual(
            [(q["id"], q["question"]) for q in served["measured"]],
            [(i, questions.sentence(i)) for i in profile.FACT_IDS if i not in questions.OPERATOR_BY_ID],
        )
        self.assertEqual(len(served["measured"]) + len(served["operator"]), len(profile.FACT_IDS))

    def test_index_and_static_file(self):
        with open(os.path.join(web.STATIC_DIR, "index.html"), "rb") as fh:
            expected = fh.read()
        for path in ("/", "/static/index.html", "/static/index.html?v=1"):
            status, headers, raw = self.request("GET", path)
            self.assertEqual((status, raw), (200, expected), path)
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIsNone(headers["Access-Control-Allow-Origin"])

    def test_static_content_types(self):
        with mock.patch.object(web, "STATIC_DIR", web.STATIC_DIR.parent):  # any directory with a .py file
            status, headers, _ = self.request("GET", "/static/server.py")
        self.assertEqual(status, 200)
        self.assertIn("python", headers["Content-Type"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_no_traversal_and_no_directory_listing(self):
        for path in (
            "/static/../cli.py",
            "/static/..%2fcli.py",
            "/static/%2e%2e/cli.py",
            "/static/%2e%2e%2fcli.py",
            "/static/..%252fcli.py",
            "/static/..\\cli.py",
            "/static/../server.py",
            "/static/../../cli.py",
            "/static//etc/passwd",
            "/static/%00",
            "/static/",
            "/static",
            "/static/.",
            "/../cli.py",
            "/cli.py",
        ):
            status, _, raw = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(json.loads(raw), {"error": "not found"}, path)

    # --- a run -----------------------------------------------------------------

    def test_run_returns_the_contract(self):
        status, result, _ = self.run_api({"url": "http://n8n.internal:5678"})
        self.assertEqual(status, 200)
        self.assertEqual(
            set(result),
            {"n8n_version", "exit_code", "summary", "partial", "scores", "not_assessed", "findings", "warnings", "markdown", "snapshot", "profile"},
        )
        self.assertEqual(result["n8n_version"], "1.70.0")
        self.assertEqual(result["exit_code"], 2)
        self.assertTrue(result["partial"])  # postgres, redis and kubernetes were not provided
        self.assertTrue(result["summary"].startswith("This health check found 1 critical issue, 1 warning"))
        self.assertEqual([f["severity"] for f in result["findings"]], ["critical", "warning", "info"])
        self.assertEqual(
            set(result["findings"][0]), {"rule_id", "area", "severity", "title", "evidence", "impact", "recommendation"}
        )
        self.assertEqual(
            result["scores"][0], {"area": "scalability", "score": 55, "partial": True, "findings": 2}
        )
        self.assertEqual(
            [s["area"] for s in result["scores"]],
            ["scalability", "reliability", "performance", "maintainability", "security"],
        )
        self.assertEqual(
            [(n["id"], n["reason"]) for n in result["not_assessed"]],
            [("postgres", "not_provided"), ("redis", "not_provided"), ("kubernetes", "not_provided")],
        )
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["snapshot"], asdict(SNAPSHOT))
        target = self.collect.call_args.args[0]
        self.assertEqual(target.url, "http://n8n.internal:5678")
        self.assertIsNone(target.postgres_dsn)

    def test_profile_is_the_one_profile_py_builds_with_nothing_withheld(self):
        _, result, _ = self.run_api({"url": "http://n8n.internal:5678"})
        built = result["profile"]
        self.assertIs(profile.validate(built), built)
        expected = profile.build_profile(SNAPSHOT, engine.run(SNAPSHOT, RULES), ())
        self.assertEqual({**built, "created_at": ""}, {**expected, "created_at": ""})
        self.assertEqual(built["run"], {"reason": "baseline", "reference": ""})
        self.assertNotIn("withheld", [fact["provenance"] for fact in built["facts"]])
        self.assertEqual([f["rule_id"] for f in built["check"]["findings"]], [f["rule_id"] for f in result["findings"]])
        self.assertEqual(built["sources"]["postgres"], "not_provided")

    def test_profile_marks_a_source_that_failed(self):
        with mock.patch("n8n_healthcheck.collectors.redis.collect", side_effect=RuntimeError("connection refused")):
            with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES):
                _, _, raw = self.request("POST", "/api/run", {"env_text": "EXECUTIONS_MODE=queue\n", "redis_url": REDIS_URL})
        built = json.loads(raw)["profile"]
        self.assertEqual(built["sources"]["redis"], "failed")
        self.assertEqual(built["sources"]["env"], "collected")

    def test_profile_holds_nothing_from_the_request_and_no_host_or_secret(self):
        mark = "zqleak"
        snapshot = Snapshot(
            health={"healthz": True, "readiness": True},
            env={
                "EXECUTIONS_MODE": "queue",
                "N8N_HOST": f"{mark}.internal",
                "WEBHOOK_URL": f"https://{mark}.internal/",
                "N8N_ENCRYPTION_KEY": "<redacted>",
                f"{mark.upper()}_COMPANY_FLAG": "1",
            },
            n8n_version="1.70.0",
        )
        body = {
            "url": f"http://{mark}-n8n.internal:5678",
            "env_text": f"N8N_HOST={mark}.internal\nN8N_ENCRYPTION_KEY={PASSWORD}\n",
            "postgres_dsn": DSN.replace("db.internal", f"{mark}-db.internal"),
            "redis_url": REDIS_URL.replace("redis.internal", f"{mark}-redis.internal"),
            "kube_namespace": f"{mark}-namespace",
            "kube_context": f"{mark}-context",
        }
        status, result, _ = self.run_api(body, snapshot=snapshot)
        self.assertEqual(status, 200)
        text = profile.to_json(result["profile"])
        for fragment in (mark, PASSWORD, "internal", "://", "<redacted>"):
            self.assertNotIn(fragment, text.lower())
        # The settings are still there, by name: the host as "set", the mode with its value.
        listed = {s["name"]: (s["state"], s["value"]) for s in result["profile"]["settings"]}
        self.assertEqual(listed["N8N_HOST"], ("set", None))
        self.assertEqual(listed["EXECUTIONS_MODE"], ("value", "queue"))
        self.assertEqual(result["profile"]["settings_not_listed"], 1)

    def test_profile_from_the_real_env_collector_has_no_secret(self):
        text = f"EXECUTIONS_MODE=regular\nN8N_ENCRYPTION_KEY={PASSWORD}\nN8N_HOST=n8n.internal\n"
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES):
            _, _, raw = self.request("POST", "/api/run", {"env_text": text})
        built = json.loads(raw)["profile"]
        self.assertNotIn(PASSWORD, json.dumps(built))
        self.assertNotIn("internal", json.dumps(built))
        self.assertEqual(profile.validate(built)["facts"][3]["value"], "regular")

    def test_markdown_equals_the_cli_output(self):
        _, result, _ = self.run_api({"url": "http://n8n.internal:5678"})
        out = io.StringIO()
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=SNAPSHOT):
            with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES), contextlib.redirect_stdout(out):
                code = cli.main(["--url", "http://n8n.internal:5678"])
        self.assertEqual(result["markdown"], out.getvalue())
        self.assertEqual(result["exit_code"], code)

    def test_not_assessed_reasons(self):
        kubernetes = {
            "deployments": [
                {
                    "name": "n8n",
                    "replicas": 1,
                    "ready_replicas": 1,
                    "containers": [{"name": "n8n", "image": "n8nio/n8n:1.70.0", "env": {}, "env_complete": True}],
                }
            ]
        }
        snapshot = Snapshot(health={"healthz": True, "readiness": True, "metrics_disabled": True}, kubernetes=kubernetes)

        def broken(snapshot):
            raise RuntimeError("boom")

        status, result, _ = self.run_api(
            {"url": "http://n8n.internal:5678", "kube_namespace": "n8n", "postgres_dsn": DSN}, snapshot, [broken, rule("info")]
        )
        self.assertEqual(status, 200)
        reasons = {n["id"]: n["reason"] for n in result["not_assessed"]}
        self.assertEqual(
            reasons,
            {
                "metrics": "disabled",
                "env": "from_kubernetes",
                "postgres": "failed",
                "redis": "not_provided",
                "ENGINE-SKIPPED": "rule_skipped",
            },
        )
        for entry in result["not_assessed"]:
            self.assertEqual(set(entry), {"id", "label", "reason", "detail"})
            # The Markdown is rendered from the same entries.
            self.assertIn(entry["label"], result["markdown"])
            self.assertIn(entry["detail"], result["markdown"])
        self.assertEqual(result["exit_code"], 0)

    def test_nothing_collected_is_exit_code_3_with_a_report(self):
        status, result, _ = self.run_api({"redis_url": REDIS_URL, "url": "http://n8n.internal"}, Snapshot(), [])
        self.assertEqual(status, 200)
        self.assertEqual(result["exit_code"], 3)
        self.assertTrue(
            result["summary"].startswith(
                "This health check could not run: none of the requested data sources could be collected "
                "(Health endpoints, Prometheus metrics, Redis)."
            )
        )
        self.assertNotIn("found 0", result["summary"])
        self.assertIn(result["summary"], result["markdown"])
        self.assertEqual({s["score"] for s in result["scores"]}, {None})
        self.assertIn(("redis", "failed"), [(n["id"], n["reason"]) for n in result["not_assessed"]])

    def test_every_rule_failing_is_exit_code_3(self):
        def broken(snapshot):
            raise RuntimeError("boom")

        _, result, _ = self.run_api({"url": "http://n8n.internal:5678"}, rules=[broken])
        self.assertEqual(result["exit_code"], 3)

    def test_empty_strings_are_absent(self):
        body = {"url": " http://n8n.internal:5678 ", "env_text": "  \n", "postgres_dsn": "", "redis_url": " ", "kube_namespace": "", "kube_context": None}
        status, _, _ = self.run_api(body)
        self.assertEqual(status, 200)
        target = self.collect.call_args.args[0]
        self.assertEqual(asdict(target), {**dict.fromkeys(asdict(target)), "url": "http://n8n.internal:5678"})

    def test_env_text_goes_through_a_private_file_that_is_removed(self):
        seen = {}

        def collect(target):
            seen["path"] = target.env_file
            seen["file_mode"] = stat.S_IMODE(os.stat(target.env_file).st_mode)
            seen["dir_mode"] = stat.S_IMODE(os.stat(os.path.dirname(target.env_file)).st_mode)
            with open(target.env_file, encoding="utf-8") as fh:
                seen["text"] = fh.read()
            raise RuntimeError("collector blew up")

        text = "EXECUTIONS_MODE=queue\nN8N_ENCRYPTION_KEY=" + PASSWORD + "\n"
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=collect):
            status, _, raw = self.request("POST", "/api/run", {"env_text": text})
        self.assertEqual(status, 500)
        self.assertEqual(seen["text"], text)
        self.assertEqual(seen["file_mode"], 0o600)
        self.assertEqual(seen["dir_mode"], 0o700)
        self.assertFalse(os.path.exists(os.path.dirname(seen["path"])))
        self.assertNotIn(PASSWORD, raw.decode())

    def test_env_text_values_are_redacted_by_the_real_collector(self):
        text = "EXECUTIONS_MODE=regular\nN8N_ENCRYPTION_KEY=" + PASSWORD + "\n"
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES):
            status, _, raw = self.request("POST", "/api/run", {"env_text": text})
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(result["snapshot"]["env"], {"EXECUTIONS_MODE": "regular", "N8N_ENCRYPTION_KEY": "<redacted>"})
        self.assertNotIn(PASSWORD, raw.decode())
        self.assertNotIn(PASSWORD, self.log.getvalue())

    def test_could_not_run_summary_gives_the_reasons(self):
        with mock.patch("n8n_healthcheck.collectors.redis.collect", side_effect=RuntimeError("connection refused")):
            with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES[:0]):
                _, _, raw = self.request("POST", "/api/run", {"redis_url": REDIS_URL})
        result = json.loads(raw)
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["warnings"], ["redis_url: not collected (connection refused)"])
        self.assertIn("could be collected (Redis). Reported: redis_url: not collected (connection refused).", result["summary"])
        self.assertIn(result["summary"], result["markdown"])
        self.assertNotIn(PASSWORD, raw.decode())

    def test_partial_failure_keeps_the_normal_summary(self):
        _, result, _ = self.run_api({"url": "http://n8n.internal", "redis_url": REDIS_URL})
        self.assertTrue(result["summary"].startswith("This health check found 1 critical issue"))

    def test_non_finite_metrics_are_null_in_valid_json(self):
        snapshot = Snapshot(health={}, metrics={"a": float("nan"), "b": float("inf"), "c": float("-inf"), "d": 1.5})
        status, _, raw = self.run_api({"url": "http://n8n.internal"}, snapshot)
        self.assertEqual(status, 200)

        def reject(constant):
            raise AssertionError(f"{constant} is not valid JSON")

        result = json.loads(raw, parse_constant=reject)
        self.assertEqual(result["snapshot"]["metrics"], {"a": None, "b": None, "c": None, "d": 1.5})

    # --- secrets ---------------------------------------------------------------

    def test_failed_collectors_are_warnings_without_secrets(self):
        def fail(target, *args):
            raise RuntimeError(f"could not connect to {target.postgres_dsn} or {target.redis_url}")

        body = {"postgres_dsn": DSN, "redis_url": REDIS_URL, "env_text": "DB_POSTGRESDB_PASSWORD=" + PASSWORD}
        with mock.patch("n8n_healthcheck.collectors.postgres.collect", side_effect=fail):
            with mock.patch("n8n_healthcheck.collectors.redis.collect", side_effect=fail):
                with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES):
                    status, _, raw = self.request("POST", "/api/run", body)
        result = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertTrue(result["warnings"][0].startswith("postgres_dsn: not collected ("))
        self.assertTrue(result["warnings"][1].startswith("redis_url: not collected ("))
        self.assertEqual(
            {n["id"]: n["reason"] for n in result["not_assessed"]}["postgres"], "failed"
        )
        # The warnings still reach the real stderr, and the access log is not mistaken for one.
        self.assertIn(result["warnings"][0], self.log.getvalue())
        self.assertIn("POST /api/run", self.log.getvalue())
        for text in (raw.decode(), self.log.getvalue()):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn("db.internal", text.replace(result["warnings"][0], "").replace(result["warnings"][1], ""))

    def test_unexpected_error_is_500_without_details(self):
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=RuntimeError(f"cannot reach {DSN}")):
            status, _, raw = self.request("POST", "/api/run", {"postgres_dsn": DSN, "redis_url": REDIS_URL})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(raw), {"error": "the health check failed unexpectedly (RuntimeError)"})
        self.assertNotIn("Traceback", raw.decode() + self.log.getvalue())
        self.assertNotIn(PASSWORD, raw.decode() + self.log.getvalue())
        self.assertNotIn("db.internal", raw.decode() + self.log.getvalue())
        # The lock is released: the next run is accepted.
        self.assertEqual(self.run_api({"url": "http://n8n.internal:5678"})[0], 200)

    def test_log_has_no_query_string_and_no_traceback(self):
        self.assertEqual(self.request("GET", f"/api/meta?token={PASSWORD}")[0], 200)
        self.assertEqual(self.request("GET", f"/static/index.html?{PASSWORD}")[0], 200)
        self.assertEqual(self.request("GET", f"/nope?{PASSWORD}", headers={"Host": "evil.example"})[0], 403)
        # A request line http.server rejects itself (it would log the raw line).
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as raw:
            raw.sendall(f"GET /?{PASSWORD} HTTP/9.9\r\nHost: {self.host}\r\n\r\n".encode())
            self.assertIn(b" 505 ", raw.makefile("rb").readline())
        # An unexpected exception in a handler: only its type is logged.
        with mock.patch.object(web, "meta", side_effect=RuntimeError(f"boom {PASSWORD}")):
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                self.request("GET", "/api/meta")
        log = self.log.getvalue()
        self.assertIn('"GET /api/meta" 200', log)
        self.assertIn('"GET /static/index.html" 200', log)
        self.assertIn('"GET /nope" 403', log)
        self.assertIn("failed: RuntimeError", log)
        self.assertNotIn(PASSWORD, log)
        self.assertNotIn("?", log)
        self.assertNotIn("Traceback", log)

    def test_html_gets_a_content_security_policy(self):
        for path in ("/", "/static/index.html"):
            _, headers, raw = self.request("GET", path)
            policy = headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", policy)
            self.assertIn("script-src 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertNotIn("unsafe", policy)
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            # The policy forbids inline scripts, styles and handlers, so the page must not have any.
            self.assertIsNone(re.search(rb"<script(?![^>]*\bsrc=)|<style|\son\w+=|\sstyle=", raw))
        status, headers, _ = self.request("GET", "/static/app.js")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"))
        _, headers, _ = self.request("GET", "/api/meta")
        self.assertEqual(headers["Content-Security-Policy"], web.CONTENT_SECURITY_POLICY)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_every_response_has_the_security_headers_once(self):
        expected = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": web.CONTENT_SECURITY_POLICY,
        }
        responses = {
            "GET /": self.request("GET", "/"),
            "GET /api/meta": self.request("GET", "/api/meta"),
            "404": self.request("GET", "/nope"),
            "403": self.request("GET", "/", headers={"Host": "evil.example"}),
            # http.server's own error pages
            "501 PUT": self.request("PUT", "/api/run", b"{}"),
            "501 OPTIONS": self.request("OPTIONS", "/api/run"),
            "414": self.request("GET", "/" + "a" * 70000),
        }
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as raw:
            raw.sendall(b"GET / HTTP/1.1 extra words\r\n\r\n")
            response = http.client.HTTPResponse(raw)
            response.begin()
            responses["400 malformed"] = (response.status, response.headers, response.read())
        self.assertEqual(
            [responses[name][0] for name in responses if name[0].isdigit()], [404, 403, 501, 501, 414, 400]
        )
        for name, (_, headers, _) in responses.items():
            for header, value in expected.items():
                self.assertEqual(headers.get_all(header), [value], (name, header))
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_head(self):
        _, get_headers, body = self.request("GET", "/")
        status, headers, raw = self.request("HEAD", "/")
        self.assertEqual((status, raw), (200, b""))
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(headers["Content-Type"], get_headers["Content-Type"])
        self.assertEqual(self.request("HEAD", "/api/meta")[0], 200)
        self.assertEqual(self.request("HEAD", "/static/../cli.py")[0], 404)
        self.assertEqual(self.request("HEAD", "/", headers={"Host": "evil.example"})[0], 403)

    # --- rejected requests -----------------------------------------------------

    def test_url_must_be_http_or_https(self):
        for url in (
            "file:///etc/hosts#",
            "FILE:///etc/hosts",
            "ftp://ftp.example/pub",
            "data:text/plain,up 1",
            "http://",
            "https:///metrics",
            "n8n.internal:5678",
            "//n8n.internal",
            "http://[bad",
        ):
            status, result, _ = self.run_api({"url": url, "env_text": "A=b"})
            self.assertEqual(status, 400, url)
            self.assertEqual(result["error"], "url must be an http:// or https:// URL with a host")
            self.collect.assert_not_called()
        for url in ("http://n8n.internal:5678", "HTTPS://n8n.example.com/n8n/", "http://[::1]:5678"):
            self.assertEqual(self.run_api({"url": url})[0], 200, url)

    def test_deeply_nested_json_is_a_bad_request(self):
        status, result, _ = self.run_api(b"[" * 200000, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertIn("not valid JSON", result["error"])
        self.assertEqual(self.request("GET", "/api/meta")[0], 200)

    def test_bad_requests(self):
        cases = [
            (b"{not json", "not valid JSON"),
            (b"\xff\xfe", "not valid JSON"),
            (b"[]", "must be a JSON object"),
            ({}, "nothing to inspect"),
            ({"url": "", "env_text": " "}, "nothing to inspect"),
            ({"kube_context": "prod"}, "nothing to inspect"),
            ({"url": "http://n8n.internal", "env_file": "/etc/passwd"}, "unknown field: env_file"),
            ({"url": 5678}, "url must be a string"),
            ({"env_text": ["A=b"]}, "env_text must be a string"),
        ]
        for body, message in cases:
            headers = {"Content-Type": "application/json"}
            status, result, _ = self.run_api(body, headers=headers)
            self.assertEqual(status, 400, body)
            self.assertIn(message, result["error"])
            self.collect.assert_not_called()

    def test_bad_request_does_not_echo_values(self):
        status, _, raw = self.run_api({"postgres_dsn": [DSN], "redis_url": REDIS_URL})
        self.assertEqual(status, 400)
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn(PASSWORD, self.log.getvalue())

    def test_content_type_must_be_json(self):
        body = json.dumps({"url": "http://n8n.internal:5678"}).encode()
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            headers = {"Content-Type": content_type} if content_type else {}
            status, result, _ = self.run_api(body, headers=headers)
            self.assertEqual(status, 415, content_type)
            self.collect.assert_not_called()
        status, _, _ = self.run_api(body, headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_body_size_limit(self):
        status, result, _ = self.run_api({"env_text": "A=" + "x" * web.MAX_BODY_BYTES})
        self.assertEqual(status, 413)
        self.assertIn("larger than", result["error"])
        self.collect.assert_not_called()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/run")
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()  # no Content-Length
        self.assertEqual(connection.getresponse().status, 400)

    def test_one_run_at_a_time(self):
        started, release = threading.Event(), threading.Event()

        def slow_collect(target):
            started.set()
            release.wait(10)
            return SNAPSHOT

        first = {}
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=slow_collect):
            with mock.patch("n8n_healthcheck.rules.ALL_RULES", RULES):
                thread = threading.Thread(
                    target=lambda: first.update(status=self.request("POST", "/api/run", {"url": "http://a.internal"})[0])
                )
                thread.start()
                self.assertTrue(started.wait(10))
                status, _, raw = self.request("POST", "/api/run", {"url": "http://b.internal"})
                # Reading is not blocked by a run.
                self.assertEqual(self.request("GET", "/api/meta")[0], 200)
                release.set()
                thread.join(10)
        self.assertEqual(status, 409)
        self.assertIn("already running", json.loads(raw)["error"])
        self.assertEqual(first["status"], 200)
        self.assertEqual(self.run_api({"url": "http://c.internal"})[0], 200)

    def test_host_header_guard(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", "localhost:1", f"127.0.0.1:{self.port}.evil.example", ""):
            for method, path in (("GET", "/"), ("GET", "/api/meta"), ("POST", "/api/run")):
                body = {"url": "http://n8n.internal"} if method == "POST" else None
                status, _, raw = self.request(method, path, body, {"Host": host})
                self.assertEqual(status, 403, (host, path))
                self.assertIn("Host header", json.loads(raw)["error"])
        for host in (f"localhost:{self.port}", f"LOCALHOST:{self.port}", f"[::1]:{self.port}", self.host):
            self.assertEqual(self.request("GET", "/api/meta", headers={"Host": host})[0], 200, host)

    def test_allow_host(self):
        self.server.shutdown()
        self.start_server(allow_hosts=["Healthcheck.Internal", "localhost:9000"])
        for host in (f"healthcheck.internal:{self.port}", "localhost:9000", f"localhost:{self.port}"):
            self.assertEqual(self.request("GET", "/api/meta", headers={"Host": host})[0], 200, host)
        for host in ("healthcheck.internal", "healthcheck.internal:9000", "evil.example:9000"):
            self.assertEqual(self.request("GET", "/api/meta", headers={"Host": host})[0], 403, host)

    def test_origin_must_match_the_host(self):
        body = {"url": "http://n8n.internal:5678"}
        for origin in ("http://evil.example", f"http://evil.example:{self.port}", f"http://localhost:{self.port}", "null", "http://[bad", self.host):
            status, result, _ = self.run_api(body, headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
            self.assertIn("cross-origin", result["error"])
            self.collect.assert_not_called()
        # Same host and port; https is what the browser sends behind a TLS proxy.
        for origin in (f"http://{self.host}", f"https://{self.host}", f"HTTP://{self.host}"):
            status, _, _ = self.run_api(body, headers={"Origin": origin})
            self.assertEqual(status, 200, origin)
        status, _, _ = self.run_api(body, headers={"Origin": f"http://localhost:{self.port}", "Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_no_cors_headers_and_no_preflight(self):
        status, headers, _ = self.request("OPTIONS", "/api/run", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 501)
        for status, headers, _ in (
            self.request("GET", "/api/meta", headers={"Origin": "http://evil.example"}),
            self.request("POST", "/api/run", {"url": "x"}, {"Origin": "http://evil.example"}),
        ):
            self.assertEqual([name for name in headers if name.lower().startswith("access-control")], [])

    def test_other_routes(self):
        self.assertEqual(self.request("GET", "/api/run")[0], 405)
        self.assertEqual(self.request("POST", "/api/meta", {"url": "x"})[0], 404)
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)


class ServeCommandTest(unittest.TestCase):
    def run_serve(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(web.ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
            with mock.patch("webbrowser.open") as browser, mock.patch("signal.signal"):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = cli.main(argv)
                    except SystemExit as exit_:
                        code = exit_.code
        return code, out.getvalue(), err.getvalue(), browser

    def test_defaults_open_the_browser_on_loopback(self):
        with mock.patch.object(web, "make_server", wraps=web.make_server) as make_server:
            code, out, err, browser = self.run_serve(["serve", "--port", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(make_server.call_args.args[:2], ("127.0.0.1", 0))
        port = out.strip().rsplit(":", 1)[1].split("/")[0]
        self.assertEqual(out, f"n8n-healthcheck is serving on http://127.0.0.1:{port}/ (Ctrl+C to stop)\n")
        self.assertEqual(err, "")
        browser.assert_called_once_with(f"http://127.0.0.1:{port}/")

    def test_default_port_is_8080(self):
        with mock.patch.object(web, "serve", return_value=0) as serve:
            self.assertEqual(cli.main(["serve"]), 0)
        serve.assert_called_once_with("127.0.0.1", 8080, True, [])

    def test_non_loopback_host_warns_and_no_browser(self):
        code, out, err, browser = self.run_serve(
            ["serve", "--host", "0.0.0.0", "--port", "0", "--no-browser", "--allow-host", "a.internal", "--allow-host", "b.internal:9000"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("http://localhost:", out)
        self.assertIn("no authentication", err)
        self.assertIn("simultaneous connections is not limited", err)
        browser.assert_not_called()

    def test_usage_error_and_busy_port(self):
        code, _, err, _ = self.run_serve(["serve", "--port", "abc"])
        self.assertEqual(code, 3)
        self.assertIn("usage: n8n-healthcheck serve", err)
        with mock.patch.object(web, "make_server", side_effect=OSError(48, "Address already in use")):
            code, _, err, _ = self.run_serve(["serve", "--no-browser"])
        self.assertEqual(code, 3)
        self.assertIn("cannot listen on 127.0.0.1:8080: Address already in use", err)

    def test_port_out_of_range_is_a_usage_error(self):
        for port in ("99999", "65536", "-1", "8080.5"):
            code, _, err, _ = self.run_serve(["serve", "--port", port, "--no-browser"])
            self.assertEqual(code, 3, port)
            self.assertIn("is not a port number (0-65535)", err)
            self.assertNotIn("Traceback", err)

    def test_bind_address_is_an_accepted_host(self):
        self.assertEqual(
            web.allowed_hosts("192.168.1.20", 8080),
            {"localhost:8080", "127.0.0.1:8080", "[::1]:8080", "192.168.1.20:8080"},
        )
        self.assertIn("[fe80::1]:8080", web.allowed_hosts("fe80::1", 8080))
        for wildcard in ("0.0.0.0", "::", ""):
            self.assertEqual(web.allowed_hosts(wildcard, 8080), {"localhost:8080", "127.0.0.1:8080", "[::1]:8080"})
        self.assertEqual(
            web.allowed_hosts("0.0.0.0", 80, ["hc.internal", "localhost:9000"]) - web.allowed_hosts("0.0.0.0", 80),
            {"hc.internal", "hc.internal:80", "localhost:9000"},
        )

    def test_main_help_mentions_serve(self):
        code, out, _, _ = self.run_serve(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("n8n-healthcheck serve", " ".join(out.split()))

    def test_serve_is_only_a_command_in_first_position(self):
        code, _, err, _ = self.run_serve(["--url", "http://n8n.internal", "serve"])
        self.assertEqual(code, 3)
        self.assertIn("unrecognized arguments: serve", err)


if __name__ == "__main__":
    unittest.main()
