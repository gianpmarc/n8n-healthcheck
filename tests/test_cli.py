import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from unittest import mock

from n8n_healthcheck import cli
from n8n_healthcheck.models import Finding, Snapshot

SNAPSHOT = Snapshot(env={"EXECUTIONS_MODE": "regular"}, n8n_version="1.70.0")


def rule(severity):
    """A fake rule that fires only on the test snapshot, proving the JSON was loaded."""

    def fake_rule(snapshot):
        if snapshot.env != SNAPSHOT.env:
            return None
        return Finding(
            rule_id=f"TEST-{severity}",
            area="scalability",
            severity=severity,
            title=f"A {severity} finding",
            evidence="EXECUTIONS_MODE=regular",
            impact="impact",
            recommendation="recommendation",
        )

    return fake_rule


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.snapshot_path = os.path.join(self.dir, "snapshot.json")
        with open(self.snapshot_path, "w", encoding="utf-8") as fh:
            json.dump(asdict(SNAPSHOT), fh)

    def run_cli(self, argv, rules=()):
        """Run main() with fake rules; return (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", list(rules)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_snapshot_end_to_end_prints_report(self):
        code, out, _ = self.run_cli(["--snapshot", self.snapshot_path], [rule("critical"), rule("info")])
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("# n8n health check report — n8n 1.70.0"))
        self.assertIn("#### TEST-critical — A critical finding", out)
        self.assertIn("EXECUTIONS_MODE=regular", out)
        self.assertIn("Postgres (`--postgres-dsn`)", out)  # not assessed

    def test_exit_codes(self):
        for rules, expected in (
            ([rule("critical"), rule("warning")], 2),
            ([rule("warning"), rule("info")], 1),
            ([rule("info")], 0),
            ([], 0),
        ):
            with self.subTest(expected=expected, rules=len(rules)):
                self.assertEqual(self.run_cli(["--snapshot", self.snapshot_path], rules)[0], expected)

    def test_no_rule_ran_cleanly_exits_3(self):
        def broken(snapshot):
            raise KeyError("version")

        code, out, err = self.run_cli(["--snapshot", self.snapshot_path], [broken, broken])
        self.assertEqual(code, 3)
        self.assertIn("Rule broken could not run", out)  # the report is still written
        self.assertIn("no rule could run", err)
        # Some rules crashed, others ran: the normal 2/1/0 logic applies.
        self.assertEqual(self.run_cli(["--snapshot", self.snapshot_path], [broken, rule("warning")])[0], 1)
        self.assertEqual(self.run_cli(["--snapshot", self.snapshot_path], [broken, lambda s: None])[0], 0)

    def test_empty_source_flag_is_a_usage_error(self):
        for flag in ("--url", "--env-file", "--postgres-dsn", "--redis-url", "--kube-namespace"):
            with self.subTest(flag=flag):
                err = io.StringIO()
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(err):
                    cli.main([flag, "", "--snapshot", self.snapshot_path])
                self.assertEqual(raised.exception.code, 3)
                self.assertIn(f"{flag} must not be empty", err.getvalue())

    def test_output_file(self):
        path = os.path.join(self.dir, "report.md")
        code, out, _ = self.run_cli(["--snapshot", self.snapshot_path, "-o", path])
        self.assertEqual((code, out), (0, ""))
        with open(path, encoding="utf-8") as fh:
            self.assertTrue(fh.read().startswith("# n8n health check report"))

    def test_unreadable_snapshot_exits_3(self):
        code, out, err = self.run_cli(["--snapshot", os.path.join(self.dir, "missing.json")])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("cannot load snapshot", err)

    def test_usage_errors_exit_3(self):
        for argv in ([], ["--no-such-flag"], ["--snapshot"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)
                self.assertEqual(raised.exception.code, 3)

    def test_help_documents_exit_codes(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stdout(out):
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("Exit codes:", out.getvalue())

    def test_unwritable_paths_exit_3(self):
        bad = os.path.join(self.dir, "no-such-dir", "file")
        for flag in ("-o", "--save-snapshot"):
            with self.subTest(flag=flag):
                code, _, err = self.run_cli(["--snapshot", self.snapshot_path, flag, bad], [rule("warning")])
                self.assertEqual(code, 3)
                self.assertIn("cannot write", err)

    def test_collects_from_target_and_saves_snapshot(self):
        saved = os.path.join(self.dir, "saved.json")
        argv = ["--url", "https://n8n.example.com", "--kube-namespace", "n8n", "--save-snapshot", saved]
        collected = Snapshot(health={"healthz": True}, metrics={}, env=SNAPSHOT.env, kubernetes={"deployments": []})
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=collected) as collect:
            code, out, _ = self.run_cli(argv, [rule("warning")])

        self.assertEqual(code, 1)
        target = collect.call_args.args[0]
        self.assertEqual((target.url, target.kube_namespace), ("https://n8n.example.com", "n8n"))
        self.assertIsNone(target.postgres_dsn)
        with open(saved, encoding="utf-8") as fh:
            self.assertEqual(Snapshot(**json.load(fh)), collected)
        self.assertNotIn("requested but could not be collected", out)

    def collect_returning(self, snapshot, argv):
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot):
            return self.run_cli(argv)

    def test_requested_source_that_failed_is_reported(self):
        argv = ["--url", "https://n8n.example.com", "--env-file", "n8n.env", "--redis-url", "redis://redis:6379"]
        code, out, _ = self.collect_returning(Snapshot(env={}), argv)
        self.assertEqual(code, 0)
        for flag in ("--url`)", "N8N_METRICS=true`)", "--redis-url`)"):
            self.assertIn(f"{flag} — requested but could not be collected", out)
        self.assertIn("(`--postgres-dsn`)\n", out)  # not requested: listed without the failure note

    def test_disabled_metrics_endpoint_is_not_a_failed_url(self):
        health = {"healthz": True, "metrics_disabled": True}
        code, out, _ = self.collect_returning(Snapshot(health=health), ["--url", "https://n8n.example.com"])
        self.assertEqual(code, 0)
        self.assertNotIn("requested but could not be collected", out)
        self.assertIn("- Prometheus metrics — endpoint disabled on the instance\n", out)

    def test_failed_metrics_scrape_is_reported_as_not_collected(self):
        # /metrics answered 403 or timed out (flag False), or the collector predates the flag.
        for health in ({"healthz": True, "metrics_disabled": False}, {"healthz": True}):
            with self.subTest(health=health):
                code, out, _ = self.collect_returning(Snapshot(health=health), ["--url", "https://n8n.example.com"])
                self.assertEqual(code, 0)
                self.assertIn("N8N_METRICS=true`) — requested but could not be collected", out)
                self.assertNotIn("endpoint disabled", out)
                self.assertNotIn("(`--url`) — requested", out)  # health itself was collected

    def test_nothing_requested_was_collected_exits_3(self):
        # metrics alone does not count: without health the URL is treated as failed.
        for snapshot in (Snapshot(), Snapshot(metrics={})):
            with self.subTest(snapshot=snapshot):
                argv = ["--url", "https://wrong.example.com", "--postgres-dsn", "postgresql://u:s3cret@db/n8n"]
                code, out, err = self.collect_returning(snapshot, argv)
                self.assertEqual((code, out), (3, ""))
                self.assertIn("none of the requested sources could be collected", err)
                self.assertNotIn("s3cret", err)

    def test_collection_failure_never_prints_connection_secrets(self):
        dsn = "postgresql://n8n:s3cret@db:5432/n8n"
        redis = "redis://:hunter2@redis:6379"
        error = ConnectionError(f"could not connect to {dsn} or {redis}")
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=error):
            code, out, err = self.run_cli(["--postgres-dsn", dsn, "--redis-url", redis])

        self.assertEqual(code, 3)
        self.assertIn("collection failed: ConnectionError", err)
        for secret in ("s3cret", "hunter2"):
            self.assertNotIn(secret, out + err)


if __name__ == "__main__":
    unittest.main()
