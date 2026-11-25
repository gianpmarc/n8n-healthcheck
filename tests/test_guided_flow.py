"""The guided flow (0.7.0): the debug information is required for a profile, the n8n
version cannot be withheld, and the page reports how much a run decided.

The page itself is driven in tests/test_web_profile.py; the claims of
docs/what-it-does.md and of the first step are held in tests/test_claims.py.
"""

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import test_debug_info as pasted  # the synthetic debug information; imported as a module so its tests run once
from n8n_healthcheck import cli, debug_info, engine, profile, report
from n8n_healthcheck.models import SOURCE_IDS, Snapshot
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.web import server as web

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


def waiting_for(checks, source):
    """Counted here from what the rules stated, not through server.coverage()."""
    return sum(check.outcome == "not_assessed" and check.reason == "source_missing" and source in check.needs for check in checks)


class CoverageTest(unittest.TestCase):
    """The numbers of steps 3 and 5 against the rules themselves."""

    def test_the_numbers_of_the_debug_information_alone_are_what_the_rules_state(self):
        for label, text in (("with a cluster section", pasted.FULL), ("an older n8n, no cluster section", pasted.NO_CLUSTER), ("regular mode", pasted.text(pasted.REGULAR))):
            with self.subTest(text=label):
                read = debug_info.parse(text)
                checks = engine.assess(Snapshot(debug_info=read), ALL_RULES)
                got = web.check_debug_info(read)["coverage"]
                self.assertEqual(got["total"], len(ALL_RULES))
                self.assertEqual(got["total"], 24)  # the number the page and the documents name
                self.assertEqual(got["decided"], sum(check.outcome in ("finding", "passed", "not_applicable") for check in checks))
                self.assertEqual(got["waiting"], {source: waiting_for(checks, source) for source in SOURCE_IDS})
                self.assertEqual(got["unstated"], sum(check.reason in ("not_stated", "value_unreadable") for check in checks))
                self.assertEqual(got["failed"], dict.fromkeys(SOURCE_IDS, 0))  # nothing was asked for, so nothing failed
                self.assertEqual((got["disagree"], got["errors"]), (0, 0))
                # Every check is in exactly one of the groups; a check that waits is counted under each source it names.
                waits = sum(check.reason == "source_missing" for check in checks)
                self.assertEqual(got["decided"] + waits + got["unstated"], got["total"])
                self.assertGreaterEqual(sum(got["waiting"].values()), waits)
                self.assertGreater(got["decided"], 0)
                self.assertGreater(waits, 0)

    def test_it_is_the_result_of_a_run_given_nothing_else(self):
        read = debug_info.parse(pasted.FULL)
        run = web.run_check({"debug_info_text": read})
        self.assertEqual(web.check_debug_info(read)["coverage"], run["coverage"])
        self.assertEqual(run["coverage"], web.coverage(run["profile"]["check"]["checks"]))

    def test_a_check_that_names_several_sources_is_counted_under_each(self):
        entries = [
            {"outcome": "not_assessed", "reason": "source_missing", "needs": ["env", "kubernetes"]},
            {"outcome": "not_assessed", "reason": "source_missing", "needs": ["env"]},
            {"outcome": "not_assessed", "reason": "source_failed", "needs": ["redis"]},
            {"outcome": "not_assessed", "reason": "not_stated", "needs": []},
            {"outcome": "not_assessed", "reason": "value_unreadable", "needs": []},
            {"outcome": "not_assessed", "reason": "sources_disagree", "needs": []},
            {"outcome": "not_assessed", "reason": "rule_error", "needs": []},
            {"outcome": "finding"}, {"outcome": "passed"}, {"outcome": "not_applicable"},
        ]  # fmt: skip
        got = web.coverage(entries)
        self.assertEqual((got["total"], got["decided"], got["unstated"], got["disagree"], got["errors"]), (10, 3, 2, 1, 1))
        self.assertEqual(got["waiting"], {**dict.fromkeys(SOURCE_IDS, 0), "env": 2, "kubernetes": 1})
        self.assertEqual(got["failed"], {**dict.fromkeys(SOURCE_IDS, 0), "redis": 1})

    def test_a_source_given_in_the_run_no_longer_has_checks_waiting_for_it(self):
        read = debug_info.parse(pasted.FULL)
        alone = web.check_debug_info(read)["coverage"]
        self.assertGreater(alone["waiting"]["env"], 0)
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=Snapshot(env={"EXECUTIONS_MODE": "queue"}, debug_info=read)):
            run = web.run_check({"env_text": "EXECUTIONS_MODE=queue\n", "debug_info_text": read})["coverage"]
        self.assertEqual(run["waiting"]["env"], 0)
        self.assertGreater(run["decided"], alone["decided"])

    def test_a_source_asked_for_and_not_collected_is_counted_as_failed_not_as_waiting(self):
        read = debug_info.parse(pasted.FULL)
        alone = web.check_debug_info(read)["coverage"]
        self.assertGreater(alone["waiting"]["redis"], 0)
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=Snapshot(debug_info=read)):
            with contextlib.redirect_stderr(io.StringIO()):
                run = web.run_check({"redis_url": "redis://cache.example:6379", "debug_info_text": read})["coverage"]
        self.assertGreater(run["failed"]["redis"], 0)
        self.assertEqual(run["decided"], alone["decided"])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.log = io.StringIO()  # everything the server writes to stderr
        redirect = contextlib.redirect_stderr(self.log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.server = web.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, body, headers=None):
        """(status, JSON, raw text) of a POST; `body` may be bytes or a JSON-able object."""
        headers = dict(headers or {})
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode()
        return response.status, json.loads(raw), raw

    # --- the requirement ------------------------------------------------------------

    def test_a_run_without_the_debug_information_answers_with_the_report_and_no_profile(self):
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=Snapshot(env={"EXECUTIONS_MODE": "queue"})):
            status, result, _ = self.post("/api/run", {"env_text": "EXECUTIONS_MODE=queue\n"})
        self.assertEqual(status, 200)
        self.assertEqual((result["profile"], result["coverage"]), (None, None))
        self.assertIn("# n8n health check report", result["markdown"])
        self.assertIsInstance(result["findings"], list)

    def test_a_run_with_it_answers_with_the_profile_and_its_numbers(self):
        status, result, raw = self.post("/api/run", {"debug_info_text": pasted.FULL})
        self.assertEqual(status, 200)
        self.assertIs(profile.validate(result["profile"]), result["profile"])
        self.assertEqual(result["coverage"], web.coverage(result["profile"]["check"]["checks"]))
        fact = result["profile"]["facts"][0]
        self.assertEqual((fact["id"], fact["value"], fact["provenance"]), ("n8n.version", "3.3.0", "measured"))
        self.assertNotIn(pasted.MARK, raw)

    # --- POST /api/debug-info -------------------------------------------------------

    def test_an_accepted_text_says_what_was_read_and_collects_nothing(self):
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=AssertionError("must not collect")):
            status, result, raw = self.post("/api/debug-info", {"debug_info_text": pasted.FULL})
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"read", "notes", "coverage"})
        self.assertEqual(
            result["read"],
            [
                {"label": "n8n version", "value": "3.3.0"},
                {"label": "Execution mode", "value": "queue (single main)"},
                {"label": "Instances", "value": "3 (1 main, 2 worker)"},
                {"label": "Generated on", "value": "2026-11-19"},
            ],
        )
        self.assertEqual(result["coverage"], web.check_debug_info(debug_info.parse(pasted.FULL))["coverage"])
        # The ids of the text were read and discarded: nothing of them is answered or logged.
        self.assertNotIn(pasted.MARK, raw)
        self.assertNotIn(pasted.MARK, self.log.getvalue())
        self.assertIn('"POST /api/debug-info" 200', self.log.getvalue())

    def test_what_was_read_in_other_shapes(self):
        rows = lambda text: {row["label"]: row["value"] for row in web.check_debug_info(debug_info.parse(text))["read"]}  # noqa: E731
        self.assertEqual(rows(pasted.text(pasted.REGULAR))["Execution mode"], "regular (single main)")
        self.assertEqual(rows(pasted.text(pasted.MULTI_MAIN))["Execution mode"], "queue (multi-main)")
        self.assertEqual(rows(pasted.text(("executionMode: scaling (single-main)", "executionMode: scaling")))["Execution mode"], "queue")
        self.assertEqual(rows(pasted.text(("- executionMode: scaling (single-main)\n", "")))["Execution mode"], "not stated")
        self.assertEqual(rows(pasted.NO_CLUSTER)["Instances"], "not stated")
        self.assertEqual(rows(pasted.text((pasted.STAMP, "")))["Generated on"], "date unknown")

    def test_the_notes_are_the_sentences_the_report_gives(self):
        for label, text, said in (
            ("fresh, with a cluster section", pasted.FULL, []),
            ("an older n8n without a cluster section", pasted.NO_CLUSTER, ["cannot be told from it: " + debug_info.NO_CLUSTER]),
            ("more than 30 days old", pasted.text(("2026-11-19T", "2025-01-02T")), ["days ago, the instance may have changed."]),
            ("without a date", pasted.text((pasted.STAMP, "")), ["It may be old, and the instance may have changed."]),
        ):
            with self.subTest(text=label):
                today = mock.Mock(wraps=web.datetime)
                today.now.return_value = web.datetime(2026, 11, 25, tzinfo=web.timezone.utc)
                with mock.patch.object(web, "datetime", today):
                    status, result, raw = self.post("/api/debug-info", {"debug_info_text": text})
                self.assertEqual(status, 200)  # accepted in every case
                self.assertEqual(len(result["notes"]), len(said))
                for note, sentence in zip(result["notes"], said):
                    self.assertIn(sentence, note)
                # The same sentences, word for word, as in the Summary of the report of that text.
                summary = report.build_report(Snapshot(debug_info=debug_info.parse(text)), [], today=web.datetime(2026, 11, 25).date())["summary"]
                for note in result["notes"]:
                    self.assertIn(note, summary)
                self.assertNotIn(pasted.MARK, raw)
        self.assertEqual(report.DEBUG_INFO_STALE_DAYS, 30)

    def test_a_refused_text_is_a_400_next_to_its_field_with_the_parsers_sentence(self):
        cases = {
            debug_info.NOT_DEBUG_INFO: f"hello {pasted.MARK}, the instance is slow\nN8N_HOST={pasted.MARK}.example\n",
            debug_info.SEVERAL: pasted.FULL + "\n\n" + pasted.FULL,
            debug_info.TOO_LARGE: pasted.FULL + "\n" + f"{pasted.MARK} " * (debug_info.MAX_BYTES // 7),
        }
        for sentence, text in cases.items():
            with self.subTest(sentence=sentence):
                status, result, raw = self.post("/api/debug-info", {"debug_info_text": text})
                self.assertEqual((status, result), (400, {"error": sentence, "field": "debug_info_text"}))
                self.assertNotIn(pasted.MARK, raw)
        for body in ({}, {"debug_info_text": ""}, {"debug_info_text": "  \n"}, {"debug_info_text": None}, {"debug_info_text": 7}):
            with self.subTest(body=body):
                status, result, _ = self.post("/api/debug-info", body)
                self.assertEqual((status, result.get("field")), (400, "debug_info_text"))
        self.assertNotIn(pasted.MARK, self.log.getvalue())

    def test_it_takes_the_text_and_no_other_field(self):
        mark = "zqleak"
        for extra in ({"url": f"http://{mark}.internal:5678"}, {"api_key": mark}, {mark: "1"}):
            status, result, raw = self.post("/api/debug-info", {"debug_info_text": pasted.FULL, **extra})
            self.assertEqual(status, 400)
            self.assertTrue(result["error"].startswith("unknown field: "))
            self.assertNotIn("internal", raw)
        for body in (b"{", b"[]", b'"text"'):
            self.assertEqual(self.post("/api/debug-info", body, {"Content-Type": "application/json"})[0], 400)
        self.assertNotIn(mark, self.log.getvalue())

    def test_it_passes_the_same_checks_as_a_run(self):
        body = json.dumps({"debug_info_text": pasted.FULL}).encode()
        json_type = {"Content-Type": "application/json"}
        for headers, expected in (
            ({**json_type, "Host": "evil.example"}, 403),  # DNS rebinding
            ({**json_type, "Origin": "http://evil.example"}, 403),  # a cross-site request
            ({"Content-Type": "text/plain"}, 415),
            ({}, 415),
            (json_type, 200),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.post("/api/debug-info", body, headers)[0], expected)
        large = json.dumps({"debug_info_text": "x" * (web.MAX_BODY_BYTES + 1)}).encode()
        self.assertEqual(self.post("/api/debug-info", large, json_type)[0], 413)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/debug-info")
        response = connection.getresponse()
        self.assertEqual((response.status, json.loads(response.read())), (405, {"error": "use POST for /api/debug-info"}))

    def test_nothing_is_written_to_disk_and_it_does_not_wait_for_a_run(self):
        self.assertTrue(self.server.run_lock.acquire(blocking=False))  # a health check is running
        self.addCleanup(self.server.run_lock.release)
        with mock.patch("tempfile.TemporaryDirectory", side_effect=AssertionError("a file was asked for")):
            with mock.patch("os.open", side_effect=AssertionError("a file was opened")):
                status, _, _ = self.post("/api/debug-info", {"debug_info_text": pasted.FULL})
        self.assertEqual(status, 200)

    def test_a_failure_says_only_its_type(self):
        with mock.patch.object(web, "check_debug_info", side_effect=RuntimeError(f"boom {pasted.MARK}")):
            status, result, raw = self.post("/api/debug-info", {"debug_info_text": pasted.FULL})
        self.assertEqual((status, result), (500, {"error": "the debug information could not be read (RuntimeError)"}))
        self.assertNotIn(pasted.MARK, raw + self.log.getvalue())


if __name__ == "__main__":
    unittest.main()
