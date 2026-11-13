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

    def test_no_source_is_a_usage_error(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main([])

    def test_collects_from_target_and_saves_snapshot(self):
        saved = os.path.join(self.dir, "saved.json")
        argv = ["--url", "https://n8n.example.com", "--kube-namespace", "n8n", "--save-snapshot", saved]
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=SNAPSHOT) as collect:
            code, out, _ = self.run_cli(argv, [rule("warning")])

        self.assertEqual(code, 1)
        target = collect.call_args.args[0]
        self.assertEqual((target.url, target.kube_namespace), ("https://n8n.example.com", "n8n"))
        self.assertIsNone(target.postgres_dsn)
        with open(saved, encoding="utf-8") as fh:
            self.assertEqual(Snapshot(**json.load(fh)), SNAPSHOT)

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
