"""The guided flow (0.7.0): the debug information is required for a profile, the n8n
version cannot be withheld, and the page reports how much a run decided.

The page itself is driven in tests/test_web_profile.py; the claims of
docs/what-it-does.md and of the first step are held in tests/test_claims.py.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import test_debug_info as pasted  # the synthetic debug information; imported as a module so its tests run once
from n8n_healthcheck import cli, profile
from n8n_healthcheck.models import Snapshot

COLLECTED = Snapshot(health={"healthz": True, "readiness": True, "metrics_disabled": True}, env={"EXECUTIONS_MODE": "queue"})


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.debug = self.path("debug.txt")
        with open(self.debug, "w", encoding="utf-8") as handle:
            handle.write(pasted.FULL)

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_cli(self, argv, collects=True):
        """(exit code, stdout, stderr). `collects=False`: collecting anything fails the test."""
        out, err = io.StringIO(), io.StringIO()
        collect = {"return_value": COLLECTED} if collects else {"side_effect": AssertionError("must not collect")}
        with mock.patch("n8n_healthcheck.collectors.collect", **collect):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(argv)
                except SystemExit as exit_:  # a usage error
                    code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_a_profile_without_the_debug_information_is_refused_before_anything_is_collected(self):
        for sources in (["--url", "https://n8n.example.com"], ["--env-file", "n8n.env", "--redis-url", "redis://cache.example:6379"], []):
            with self.subTest(sources=sources):
                code, out, err = self.run_cli(sources + ["--save-profile", self.path("p.json"), "-o", self.path("r.md")], collects=False)
                self.assertEqual((code, out), (3, ""))
                self.assertIn("--save-profile needs --debug-info", err)
                # The message says how to get the text.
                self.assertIn("Help > About n8n > Debug information > Copy", err)
                self.assertFalse(os.path.exists(self.path("p.json")))
                self.assertFalse(os.path.exists(self.path("r.md")))

    def test_the_report_alone_needs_no_debug_information(self):
        code, out, err = self.run_cli(["--url", "https://n8n.example.com"])
        self.assertIn(code, (0, 1, 2))  # the result of a check that ran, whatever it found
        self.assertEqual(err, "")
        self.assertIn("# n8n health check report", out)

    def test_with_the_debug_information_the_profile_is_written(self):
        code, _, err = self.run_cli(["--url", "https://n8n.example.com", "--debug-info", self.debug, "--save-profile", self.path("p.json")])
        self.assertIn(code, (0, 1, 2))
        self.assertEqual(err, "")
        self.assertEqual(profile.load_profile(self.path("p.json"))["format_version"], 1)

    def test_the_n8n_version_cannot_be_withheld(self):
        argv = ["--url", "https://n8n.example.com", "--debug-info", self.debug, "--save-profile", self.path("p.json")]
        code, out, err = self.run_cli(argv + ["--withhold", "n8n.version"], collects=False)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("n8n.version cannot be withheld: the n8n version is always part of the profile", err)
        self.assertFalse(os.path.exists(self.path("p.json")))
        # Next to an entry that can be: still refused, and nothing is written.
        code, _, err = self.run_cli(argv + ["--withhold", "executions.mode", "--withhold", "n8n.version"], collects=False)
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(self.path("p.json")))

    def test_every_other_entry_can_still_be_withheld(self):
        self.assertEqual(profile.ALWAYS_SHARED, "n8n.version")
        argv = ["--url", "https://n8n.example.com", "--debug-info", self.debug, "--save-profile", self.path("p.json")]
        others = [fact_id for fact_id in profile.FACT_IDS if fact_id != profile.ALWAYS_SHARED]
        withhold = [part for item in (*others, "setting:instance:EXECUTIONS_MODE", "check") for part in ("--withhold", item)]
        code, _, err = self.run_cli(argv + withhold)
        self.assertIn(code, (0, 1, 2), err)
        saved = profile.load_profile(self.path("p.json"))
        self.assertEqual([fact["id"] for fact in saved["facts"] if fact["provenance"] != "withheld"], ["n8n.version"])
        self.assertEqual(saved["check"], {"withheld": True})

    def test_a_run_with_the_real_debug_information_always_states_the_version(self):
        # Whatever else was or was not collected, the fact is measured: from the text when nothing else states it.
        for snapshot in (Snapshot(), COLLECTED):
            with mock.patch("n8n_healthcheck.collectors.collect", side_effect=lambda target, base=snapshot: Snapshot(**{**vars(base), "debug_info": target.debug_info})):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    cli.main(["--debug-info", self.debug, "--env-file", "n8n.env", "--save-profile", self.path("p.json")])
            fact = next(fact for fact in profile.load_profile(self.path("p.json"))["facts"] if fact["id"] == "n8n.version")
            self.assertEqual((fact["value"], fact["provenance"]), ("3.3.0", "measured"))

    def test_older_profiles_still_open(self):
        # Written without a debug information, and with the version withheld: both by 0.6.0 and before.
        older = profile.apply_withholding(profile.build_profile(COLLECTED, [], ()), ["n8n.version"])
        older["tool_version"] = "0.6.0"
        self.assertEqual(older["sources"]["debug_info"], "not_provided")
        with open(self.path("old.json"), "w", encoding="utf-8") as handle:
            handle.write(profile.to_json(older))
        for flag in ("--show-profile", "--show-sheet", "--show-questions"):
            with self.subTest(flag=flag):
                code, out, err = self.run_cli([flag, self.path("old.json")], collects=False)
                self.assertEqual((code, err), (0, ""))
                self.assertTrue(out.startswith("# "))
        self.assertIn("| n8n.version |  | withheld |", self.run_cli(["--show-profile", self.path("old.json")])[1])


if __name__ == "__main__":
    unittest.main()
