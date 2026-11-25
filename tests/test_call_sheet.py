"""check.checks and the call sheet: fixed texts only, nothing leaks, one check withheld, the same sheet on the page."""

import contextlib
import copy
import io
import json
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import test_debug_info as pasted
import test_outcomes as outcomes
import test_profile as base  # the seeded snapshot and its markers
import test_web_profile as page
from n8n_healthcheck import cli, engine, profile
from n8n_healthcheck.models import AREAS, BASES, NOT_ASSESSED_REASONS, SEVERITIES, SOURCE_IDS, Snapshot
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.web import server as web
from test_rules import QUEUE, container, healthy_kube, kube

DOCS = os.path.join(os.path.dirname(__file__), "..", "docs")
TODAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 9, 30, tzinfo=timezone.utc)

# Every sentence a rule can write to a profile: the closed list.
LITERALS = set()
for _rule in ALL_RULES:
    LITERALS |= {_rule.info.subject, _rule.info.passed}
    for _variant in _rule.info.variants:
        LITERALS |= {_variant.title, _variant.impact, _variant.remedy}

# Queue mode on Postgres, with findings, passed checks, checks that lack a source and one that does not apply.
# The env file describes every process, and the containers of Kubernetes are set as it says:
# a container that says otherwise, or leaves a variable to its default, would be a second
# source that disagrees (see test_outcomes).
RICH_ENV = {**QUEUE, "N8N_SECURE_COOKIE": "false", "N8N_RUNNERS_MODE": "external", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}
RICH = Snapshot(
    health={"healthz": True, "readiness": True},
    env=dict(RICH_ENV),
    redis={"version": "7.2.4", "maxmemory_policy": "allkeys-lru", "maxmemory_bytes": 1000, "used_memory_bytes": 100, "waiting_jobs": 0, "active_jobs": 1},
    kubernetes=kube(
        ("n8n-main", 1, container(env=dict(RICH_ENV))),
        ("n8n-worker", 2, container(command=["worker"], env=dict(RICH_ENV))),
        ("n8n-webhook", 2, container(command=["n8n", "webhook"], env=dict(RICH_ENV))),
    ),
)


def built(snapshot=RICH, failed=("postgres",), **kwargs):
    return profile.build_profile(snapshot, [], failed, now=NOW, checks=engine.assess(snapshot, ALL_RULES), **kwargs)


def entries(built_profile):
    return {entry["rule_id"]: entry for entry in built_profile["check"]["checks"]}


def snapshots():
    """Many different instances: every case of the outcome tests, and the seeded ones."""
    found = [snapshot for cases in outcomes.CASES.values() for _, snapshot, _ in cases]
    without_env = base.leaky_snapshot()
    without_env.env = None
    return found + [RICH, base.leaky_snapshot(), without_env, pasted.snapshot(kubernetes=base.leaky_snapshot().kubernetes)]


# --- the format --------------------------------------------------------------------

KEYS = {
    "finding": {"rule_id", "area", "outcome", "severity", "text", "impact", "remedy", "rests_on"},
    "passed": {"rule_id", "area", "outcome", "basis", "text", "rests_on"},
    "not_applicable": {"rule_id", "area", "outcome", "text", "rests_on"},
    "not_assessed": {"rule_id", "area", "outcome", "text", "reason", "needs", "rests_on"},
}


class ChecksTest(unittest.TestCase):
    def test_one_entry_per_rule_in_rule_order_with_the_keys_of_its_outcome(self):
        seen = set()
        for snapshot in snapshots():
            checks = built(snapshot)["check"]["checks"]
            self.assertEqual([entry["rule_id"] for entry in checks], [rule.info.rule_id for rule in ALL_RULES])
            for entry, rule in zip(checks, ALL_RULES):
                self.assertEqual(set(entry), KEYS[entry["outcome"]], entry["rule_id"])
                self.assertEqual(entry["area"], rule.info.area)
                seen.add(entry["outcome"])
        self.assertEqual(seen, set(KEYS))

    def test_the_example_instance(self):
        got = entries(built())
        self.assertEqual(
            got["SEC-001"],
            {
                "rule_id": "SEC-001", "area": "security", "outcome": "finding", "severity": "warning",
                "text": "Session cookie is not restricted to HTTPS",
                "impact": ALL_RULES[-3].info.variants[0].impact, "remedy": ALL_RULES[-3].info.variants[0].remedy,
                # The env file, and the main container it was compared with.
                "rests_on": ["setting:instance:N8N_SECURE_COOKIE", "setting:main:N8N_SECURE_COOKIE"],
            },
        )  # fmt: skip
        self.assertEqual(
            got["REL-001"],
            {
                "rule_id": "REL-001", "area": "reliability", "outcome": "passed", "basis": "read",
                "text": "Queue mode runs on a database that every process can share, not on SQLite.",
                "rests_on": ["executions.mode", "database.engine", "setting:instance:EXECUTIONS_MODE", "setting:main:EXECUTIONS_MODE", "setting:instance:DB_TYPE", "setting:main:DB_TYPE"],
            },
        )  # fmt: skip
        self.assertEqual(got["PERF-001"]["basis"], "default")
        self.assertEqual(
            got["SCAL-001"],
            {"rule_id": "SCAL-001", "area": "scalability", "outcome": "not_applicable",
             # What made it not apply: the mode, and what the rule read on the way.
             "rests_on": [
                 "executions.mode", "database.engine", "setting:instance:EXECUTIONS_MODE", "setting:main:EXECUTIONS_MODE", "setting:instance:DB_TYPE",
                 "setting:main:DB_TYPE", "setting:instance:N8N_CONCURRENCY_PRODUCTION_LIMIT", "setting:main:N8N_CONCURRENCY_PRODUCTION_LIMIT",
             ],
             "text": "Limit on concurrent production executions in regular mode"},
        )  # fmt: skip
        self.assertEqual(
            got["MAINT-004"],
            {"rule_id": "MAINT-004", "area": "maintainability", "outcome": "not_assessed", "text": "Kind of license",
             "reason": "source_missing", "needs": ["debug_info"], "rests_on": []},
        )  # fmt: skip

    def test_checks_and_findings_are_one_list(self):
        for snapshot in snapshots():
            check = built(snapshot)["check"]
            from_checks = [
                {"rule_id": e["rule_id"], "severity": e["severity"], "area": e["area"], "title": e["text"]}
                for e in check["checks"]
                if e["outcome"] == "finding"
            ]
            from_checks.sort(key=lambda finding: SEVERITIES.index(finding["severity"]))
            self.assertEqual(check["findings"], from_checks)

    def test_findings_scores_and_the_rest_of_the_file_are_what_they_were_without_checks(self):
        for snapshot in snapshots():
            findings = engine.run(snapshot, ALL_RULES)
            old = profile.build_profile(snapshot, findings, ("postgres",), now=NOW)
            new = built(snapshot)
            self.assertNotIn("checks", old["check"])
            del new["check"]["checks"]
            self.assertEqual(new, old)

    def test_a_source_that_was_asked_for_and_failed(self):
        snapshot = Snapshot(env=dict(QUEUE))
        got = entries(built(snapshot, failed=("redis", "health", "metrics")))
        self.assertEqual((got["REL-002"]["reason"], got["REL-002"]["needs"]), ("source_failed", ["redis"]))
        self.assertEqual((got["REL-005"]["reason"], got["REL-005"]["needs"]), ("source_failed", ["url"]))
        # Only the sources that failed are named, and one that was not asked for stays missing.
        self.assertEqual((got["SCAL-005"]["reason"], got["SCAL-005"]["needs"]), ("source_missing", ["postgres"]))
        got = entries(built(Snapshot(health={"healthz": True, "readiness": True}), failed=("kubernetes",)))
        self.assertEqual((got["SCAL-003"]["reason"], got["SCAL-003"]["needs"]), ("source_failed", ["kubernetes"]))
        self.assertEqual((got["MAINT-004"]["reason"], got["MAINT-004"]["needs"]), ("source_missing", ["debug_info"]))

    def test_a_rule_that_raised_is_not_assessed_and_says_nothing_of_the_error(self):
        def broken(snapshot):
            raise KeyError(f"{base.MARK}-secret")

        broken.info = ALL_RULES[0].info
        checks = engine.assess(RICH, [broken, *ALL_RULES[1:]])
        made = profile.build_profile(RICH, [], (), checks=checks)
        self.assertEqual(
            made["check"]["checks"][0],
            {"rule_id": "SCAL-001", "area": "scalability", "outcome": "not_assessed", "reason": "rule_error", "needs": [],
             "text": ALL_RULES[0].info.subject, "rests_on": []},
        )  # fmt: skip
        self.assertTrue(made["check"]["partial"])
        text = profile.to_json(made) + profile.render_markdown(made) + profile.render_sheet(made)
        for hidden in (base.MARK, "KeyError", engine.SKIPPED_RULE_ID, "broken"):
            self.assertNotIn(hidden, text)
        self.assertIn("The rule failed to run, which is a bug in n8n-healthcheck:** SCAL-001", profile.render_sheet(made))

    def test_a_callable_that_is_not_a_declared_rule_has_no_entry(self):
        checks = engine.assess(Snapshot(), [lambda snapshot: None, ALL_RULES[0]])
        self.assertEqual([e["rule_id"] for e in profile.build_profile(Snapshot(), [], (), checks=checks)["check"]["checks"]], ["SCAL-001"])

    def test_rests_on_names_entries_of_the_file_in_the_scope_that_was_read(self):
        for snapshot in snapshots():
            made = built(snapshot)
            # A fact is named only when the file holds a value for it.
            ids = {fact["id"] for fact in made["facts"] if fact["provenance"] != "unknown"} | {profile.setting_id(s) for s in made["settings"]}
            for entry in made["check"]["checks"]:
                self.assertLessEqual(set(entry["rests_on"]), ids, entry["rule_id"])
                self.assertEqual(len(set(entry["rests_on"])), len(entry["rests_on"]), entry["rule_id"])
                # A check that lacks every source it reads rests on nothing: no entry of the file is about it.
                if entry.get("reason") == "source_missing" and snapshot.env is None and snapshot.kubernetes is None and snapshot.debug_info is None:
                    self.assertEqual(entry["rests_on"], [])
            # Every id is one `--withhold` takes.
            profile.apply_withholding(made, {item for entry in made["check"]["checks"] for item in entry["rests_on"]})
        in_kubernetes = Snapshot(
            kubernetes=kube(
                ("main", 1, container(env={**QUEUE, "N8N_RUNNERS_MODE": "external"})),
                ("worker", 2, container(command=["worker"], env={"N8N_RUNNERS_MODE": "external"})),
            )
        )
        got = entries(built(in_kubernetes))
        self.assertEqual(got["SEC-002"]["outcome"], "passed")
        self.assertEqual(
            got["SEC-002"]["rests_on"],
            # The version: external passes without N8N_RUNNERS_ENABLED only on n8n 2.x or later. DB_TYPE: the severity of the finding.
            ["executions.mode", "n8n.version", "setting:main:N8N_RUNNERS_MODE", "setting:worker:N8N_RUNNERS_MODE", "setting:main:EXECUTIONS_MODE", "setting:main:DB_TYPE"],
        )
        # A rule that judges the webhook containers too names no fact about main and worker only.
        self.assertEqual((got["REL-007"]["outcome"], got["REL-007"]["rests_on"]), ("passed", []))

    def test_a_withheld_setting_of_a_container_names_the_checks_that_tell_something_about_it(self):
        # The three inputs of the second review: an env file, and a container it is compared with.
        limit = "N8N_CONCURRENCY_PRODUCTION_LIMIT"
        for env, containers, withheld, named in (
            (
                {"EXECUTIONS_MODE": "queue", "DB_TYPE": "postgresdb", "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"},
                [("main", 1, container(env={"EXECUTIONS_MODE": "regular"}))],
                "setting:main:EXECUTIONS_MODE",
                {"SCAL-001", "SCAL-003", "REL-001", "PERF-002"},
            ),
            (
                {"EXECUTIONS_MODE": "queue", limit: "10"},
                [("worker", 1, container(command=["worker"], env={limit: "2"}))],
                f"setting:worker:{limit}",
                {"SCAL-004"},
            ),
            ({"DB_TYPE": "postgresdb"}, [("main", 1, container(env={"N8N_SECURE_COOKIE": "false"}))], "setting:main:N8N_SECURE_COOKIE", {"SEC-001"}),
        ):
            with self.subTest(withheld=withheld):
                made = built(Snapshot(env=env, kubernetes=kube(*containers)), failed=(), withhold=(withheld,))
                self.assertLessEqual(named, set(profile.rested_on_withheld(made).get(withheld, ())))
                for rule_id in named:
                    self.assertEqual(entries(made)[rule_id]["reason"], "sources_disagree", rule_id)
                self.assertIn(f"- `{withheld}`: ", profile.render_markdown(made))
        # A pass by default that the setting of a container bears on: it says the container agrees.
        made = built(Snapshot(env={"DB_TYPE": "postgresdb"}, kubernetes=kube(("main", 1, container(env={"DB_TYPE": "postgresdb", "N8N_SECURE_COOKIE": "true"})))), failed=())
        self.assertEqual((entries(made)["SEC-001"]["basis"], entries(made)["SEC-001"]["rests_on"]), ("default", ["setting:main:N8N_SECURE_COOKIE"]))
        # Without an env file nothing changes: the roles the rule judges, and main for the mode.
        alone = entries(built(Snapshot(kubernetes=kube(("main", 1, container(env=dict(QUEUE))), ("worker", 2, container(command=["worker"], env={limit: "10"})))), failed=()))
        self.assertEqual(alone["SCAL-004"]["rests_on"], ["worker.concurrency", "executions.mode", "setting:main:EXECUTIONS_MODE", f"setting:worker:{limit}"])

    def test_a_check_that_lacks_a_source_names_what_it_read_on_the_way(self):
        # Redis was not given: on an instance in regular mode the check would not apply, so the entry says the mode is not regular.
        got = entries(built(Snapshot(env=dict(QUEUE)), failed=()))
        self.assertEqual((got["REL-002"]["reason"], got["REL-002"]["rests_on"]), ("source_missing", ["executions.mode", "setting:instance:EXECUTIONS_MODE"]))
        # The worker flag is in Kubernetes only: the mode and the limit of the env file were read first.
        self.assertEqual((got["SCAL-004"]["reason"], got["SCAL-004"]["rests_on"]), ("source_missing", ["executions.mode", "setting:instance:EXECUTIONS_MODE"]))
        self.assertEqual(profile.rested_on_withheld(built(Snapshot(env=dict(QUEUE)), failed=(), withhold=("setting:instance:EXECUTIONS_MODE",)))["setting:instance:EXECUTIONS_MODE"][:3], ["SCAL-001", "SCAL-002", "SCAL-003"])
        # Nothing given: nothing read, nothing named.
        self.assertEqual({tuple(entry["rests_on"]) for entry in built(Snapshot(), failed=())["check"]["checks"]}, {()})

    def test_rests_on_names_a_fact_only_when_the_rule_read_the_source_the_fact_reads(self):
        # Three facts are filled by a debug information only; the rules that rest on them read the environment too.
        from_env = entries(built(Snapshot(env={"DB_TYPE": "postgresdb", "N8N_SECURE_COOKIE": "false", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}), failed=()))
        self.assertEqual((from_env["SEC-001"]["outcome"], from_env["SEC-001"]["rests_on"]), ("finding", ["setting:instance:N8N_SECURE_COOKIE"]))
        self.assertEqual(from_env["SCAL-001"]["outcome"], "passed")
        self.assertEqual(
            from_env["SCAL-001"]["rests_on"],
            ["executions.mode", "database.engine", "setting:instance:DB_TYPE", "setting:instance:N8N_CONCURRENCY_PRODUCTION_LIMIT"],
        )
        sheet = profile.render_sheet(built(Snapshot(env={"N8N_SECURE_COOKIE": "false"}), failed=()), TODAY)
        self.assertIn("Rests on: `setting:instance:N8N_SECURE_COOKIE` = false\n", sheet)
        self.assertNotIn("`security.secure_cookie`", sheet.split("## What does not")[1].split("## What to improve")[0])
        # From the text alone, the same rules rest on those facts.
        from_text = entries(built(pasted.snapshot(pasted.REGULAR), failed=()))
        self.assertEqual((from_text["SEC-001"]["outcome"], from_text["SEC-001"]["rests_on"]), ("finding", ["security.secure_cookie"]))
        self.assertEqual((from_text["SEC-003"]["outcome"], from_text["SEC-003"]["rests_on"]), ("finding", ["security.file_access_blocked"]))
        self.assertEqual(
            (from_text["SCAL-001"]["outcome"], from_text["SCAL-001"]["rests_on"]),
            ("finding", ["executions.mode", "database.engine", "main.concurrency_limit"]),
        )

    def test_a_withheld_mode_names_the_checks_that_do_not_apply_because_of_it(self):
        regular = Snapshot(env={"EXECUTIONS_MODE": "regular", "DB_TYPE": "postgresdb"})
        made = built(regular, failed=(), withhold=("executions.mode", "setting:instance:EXECUTIONS_MODE"))
        left_out = [entry["rule_id"] for entry in made["check"]["checks"] if entry["outcome"] == "not_applicable"]
        self.assertEqual(left_out, ["SCAL-002", "SCAL-003", "SCAL-004", "REL-001", "REL-002", "REL-003", "REL-004", "REL-006"])
        rested = profile.rested_on_withheld(made)
        for item in ("executions.mode", "setting:instance:EXECUTIONS_MODE"):
            # Together these checks tell that the mode is regular: each one is named.
            self.assertLessEqual(set(left_out), set(rested[item]), item)
        self.assertLessEqual({"SCAL-001", "PERF-002", "MAINT-001", "SEC-002"}, set(rested["setting:instance:EXECUTIONS_MODE"]))  # as before
        shown = profile.render_markdown(made)
        self.assertIn("- `executions.mode`: " + ", ".join(rested["executions.mode"]) + "\n", shown)
        for rule_id in left_out:
            self.assertIn(rule_id, shown.split("still rests on:")[1])
        # With Redis given, a Redis rule that does not apply for its own reason did not read the mode.
        no_limit = entries(built(Snapshot(env={}, redis={"maxmemory_policy": "noeviction", "maxmemory_bytes": 0, "used_memory_bytes": 1}), failed=()))
        self.assertEqual((no_limit["REL-003"]["outcome"], no_limit["REL-003"]["rests_on"]), ("not_applicable", ["queue.maxmemory_bytes", "queue.maxmemory_policy"]))
        self.assertEqual((no_limit["REL-002"]["outcome"], no_limit["REL-002"]["rests_on"]), ("passed", ["queue.maxmemory_policy", "queue.maxmemory_bytes"]))
        # A check that could not decide after it read a value rests on what it read.
        disabled = entries(built(Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "false"}), failed=()))
        self.assertEqual((disabled["SEC-002"]["outcome"], disabled["SEC-002"]["reason"]), ("not_assessed", "not_stated"))
        self.assertEqual(disabled["SEC-002"]["rests_on"], ["executions.mode", "setting:instance:N8N_RUNNERS_MODE", "setting:instance:N8N_RUNNERS_ENABLED"])
        self.assertEqual(entries(built(Snapshot(), failed=()))["SEC-002"]["rests_on"], [])


class ValidateTest(unittest.TestCase):
    def test_a_profile_with_checks_round_trips(self):
        made = built()
        again = json.loads(profile.to_json(made))
        self.assertEqual(profile.validate(again), made)

    def test_a_profile_written_before_checks_existed_still_opens(self):
        older = built()
        del older["check"]["checks"]
        older["tool_version"] = "0.5.0"
        self.assertIs(profile.validate(older), older)
        self.assertNotIn("### Checks", profile.render_markdown(older))

    def test_checks_must_be_a_list_of_objects_with_the_four_strings(self):
        entry = {"rule_id": "X-1", "area": "security", "outcome": "passed", "text": "A sentence."}
        bad = [None, {}, "x", 1, [None], [[]], ["x"]]
        bad += [[{key: value for key, value in entry.items() if key != missing}] for missing in entry]
        bad += [[{**entry, key: wrong}] for key in entry for wrong in (None, 1, True, [], {})]
        for checks in bad:
            made = built()
            made["check"]["checks"] = checks
            with self.subTest(checks=checks):
                with self.assertRaises(profile.ProfileError) as raised:
                    profile.validate(made)
                self.assertEqual(str(raised.exception), 'not a valid profile: "check.checks"')

    def test_what_a_later_version_may_add_is_accepted_and_shown_as_not_known(self):
        made = built()
        made["check"]["checks"] += [
            {"rule_id": "NEW-001", "area": "upgrade", "outcome": "will_break", "text": "A later outcome", "since": "2.30"},
            {"rule_id": "NEW-002", "area": "upgrade", "outcome": "not_assessed", "reason": "too_new", "text": "A later reason"},
            # Keys of the wrong type are read as absent, never a crash.
            {"rule_id": "ODD-001", "area": "security", "outcome": "finding", "text": "Odd", "severity": 3, "impact": None, "remedy": ["x"], "rests_on": "worker.replicas"},
            {"rule_id": "ODD-002", "area": "security", "outcome": "not_assessed", "text": "Odd too", "reason": "source_missing", "needs": [1, "postgres", "a moon"], "rests_on": [None, 2]},
            {"rule_id": "ODD-003", "area": 5 * "x", "outcome": "passed", "text": "Odd three", "basis": 7},
        ]
        profile.validate(made)
        shown, sheet = profile.render_markdown(made), profile.render_sheet(made, TODAY)
        for text in (shown, sheet):
            self.assertIn("NEW-001", text)
            self.assertNotIn("will_break", text)
            self.assertNotIn("too_new", text)
        self.assertIn("- **NEW-001** (upgrade): not known to this version of n8n-healthcheck. A later outcome", shown)
        self.assertIn("- **Not known to this version of n8n-healthcheck:** NEW-001 A later outcome; NEW-002 A later reason", sheet)
        self.assertIn("- **With Postgres or a moon:** ODD-002 Odd too", sheet)
        self.assertIn("**ODD-001** (, security): Odd", sheet)
        self.assertIn("- ODD-003 Odd three\n", sheet)


# --- withholding one check ---------------------------------------------------------


class WithholdCheckTest(unittest.TestCase):
    def test_a_withheld_check_keeps_its_rule_id_and_its_finding_leaves_the_findings(self):
        made = built()
        blanked = profile.apply_withholding(made, ["check:SEC-001", "check:REL-001"])
        got = entries(blanked)
        self.assertEqual(got["SEC-001"], {"rule_id": "SEC-001", "area": "security", "outcome": "withheld", "text": ""})
        self.assertEqual(got["REL-001"], {"rule_id": "REL-001", "area": "reliability", "outcome": "withheld", "text": ""})
        self.assertIn("SEC-001", [f["rule_id"] for f in made["check"]["findings"]])
        self.assertEqual(
            blanked["check"]["findings"], [f for f in made["check"]["findings"] if f["rule_id"] != "SEC-001"]
        )
        # The score of each of their areas goes with them; the other areas keep theirs.
        for row, before in zip(blanked["check"]["scores"], made["check"]["scores"]):
            if row["area"] in ("security", "reliability"):
                self.assertEqual(row, {"area": row["area"], "score": None, "partial": before["partial"], "withheld": True})
                self.assertIsInstance(before["score"], int)
            else:
                self.assertEqual(row, before)
        # Nothing else moves: the other entries and the rest of the file.
        for rule_id, entry in entries(made).items():
            if rule_id not in ("SEC-001", "REL-001"):
                self.assertEqual(got[rule_id], entry)
        self.assertEqual({k: v for k, v in blanked.items() if k != "check"}, {k: v for k, v in made.items() if k != "check"})
        self.assertNotIn(outcomes.RULES["SEC-001"].info.variants[0].impact, profile.to_json(blanked))
        self.assertIs(profile.validate(blanked), blanked)
        # Withholding it again changes nothing, and the original was not touched.
        self.assertEqual(profile.apply_withholding(blanked, ["check:SEC-001"]), blanked)
        self.assertEqual(entries(made)["SEC-001"]["outcome"], "finding")

    def test_ids_that_match_no_check_are_refused(self):
        made = built()
        for ids in (["check:NOPE-1"], ["check:"], ["check:sec-001"], ["checks"], ["check:SEC-001", "check:NOPE-1"]):
            with self.subTest(ids=ids), self.assertRaises(profile.ProfileError):
                profile.apply_withholding(made, ids)
        whole = profile.apply_withholding(made, ["check"])
        with self.assertRaises(profile.ProfileError):
            profile.apply_withholding(whole, ["check:SEC-001"])
        without = profile.build_profile(RICH, engine.run(RICH, ALL_RULES))
        with self.assertRaises(profile.ProfileError):
            profile.apply_withholding(without, ["check:SEC-001"])

    def test_the_whole_outcome_withheld_removes_every_check(self):
        for ids in (["check"], ["check", "check:SEC-001"]):
            self.assertEqual(profile.apply_withholding(built(), ids)["check"], {"withheld": True})

    def test_nothing_is_withheld_automatically_and_the_review_names_what_rests_on_a_withheld_entry(self):
        blanked = built(withhold=("executions.mode", "setting:instance:DB_TYPE", "main.replicas"))
        self.assertNotIn("withheld", [entry["outcome"] for entry in blanked["check"]["checks"]])
        rested = profile.rested_on_withheld(blanked)
        self.assertEqual(list(rested), ["executions.mode", "setting:instance:DB_TYPE"])  # nothing rests on main.replicas
        # SCAL-001 does not apply in queue mode and read it; for SEC-002 it decides the severity of a finding.
        self.assertEqual(rested["setting:instance:DB_TYPE"], ["SCAL-001", "REL-001", "SEC-002"])
        self.assertIn("REL-001", rested["executions.mode"])
        shown = profile.render_markdown(blanked)
        self.assertIn("Withheld entries that a check in the file still rests on:", shown)
        self.assertIn("- `setting:instance:DB_TYPE`: SCAL-001, REL-001, SEC-002\n", shown)
        self.assertIn("`--withhold check:RULE-ID`", shown)
        # Once those checks are withheld too, nothing rests on the entry any more.
        gone = profile.apply_withholding(blanked, [f"check:{rule_id}" for rules in rested.values() for rule_id in rules])
        self.assertEqual(profile.rested_on_withheld(gone), {})
        self.assertNotIn("still rests on", profile.render_markdown(gone))


def restated(made, rule_id, outcome, severity=None):
    """A copy of a profile in which one rule stated something else, with the findings and the scores that follow from it."""
    made = copy.deepcopy(made)
    info = outcomes.RULES[rule_id].info
    check = made["check"]
    position = [entry["rule_id"] for entry in check["checks"]].index(rule_id)
    entry = {"rule_id": rule_id, "area": info.area, "outcome": outcome}
    if outcome == "finding":
        variant = info.variants[0]
        entry.update(severity=severity, text=variant.title, impact=variant.impact, remedy=variant.remedy, rests_on=[])
    elif outcome == "passed":
        entry.update(basis="read", text=info.passed, rests_on=[])
    else:
        entry.update(text=info.subject, rests_on=[])
        if outcome == "not_assessed":
            entry.update(reason="not_stated", needs=[])
    check["checks"][position] = entry
    found = [e for e in check["checks"] if e["outcome"] == "finding"]
    found.sort(key=lambda e: SEVERITIES.index(e["severity"]))
    check["findings"] = [{"rule_id": e["rule_id"], "severity": e["severity"], "area": e["area"], "title": e["text"]} for e in found]
    for row in check["scores"]:
        row["score"] = max(0, 100 - sum(engine.PENALTY[e["severity"]] for e in found if e["area"] == row["area"]))
    return profile.validate(made)


class WithheldScoreTest(unittest.TestCase):
    """Decided by the owner after the build: a check withheld on its own takes the score of its area with it."""

    STATES = [("finding", "critical"), ("finding", "warning"), ("finding", "info"), ("passed", None), ("not_assessed", None), ("not_applicable", None)]

    def variants(self, rule_id="SEC-001"):
        return [restated(built(), rule_id, outcome, severity) for outcome, severity in self.STATES]

    def test_the_variants_really_differ_before_the_check_is_withheld(self):
        files = self.variants()
        scores = [next(row["score"] for row in file["check"]["scores"] if row["area"] == "security") for file in files]
        self.assertEqual(scores, [60, 85, 95, 100, 100, 100])
        self.assertEqual(len({json.dumps(file["check"], sort_keys=True) for file in files}), 6)
        self.assertEqual(len({profile.render_sheet(file, TODAY) for file in files}), 6)

    def test_nothing_in_the_file_or_on_the_sheet_tells_what_a_withheld_check_had_stated(self):
        blanked = [profile.apply_withholding(file, ["check:SEC-001"]) for file in self.variants()]
        for file in blanked[1:]:
            self.assertEqual(file, blanked[0])
        for render in (profile.to_json, profile.render_markdown, lambda file: profile.render_sheet(file, TODAY), profile.render_questions):
            self.assertEqual(len({render(file) for file in blanked}), 1)
        # Not kept, and not computed again without the finding: either would differ between the variants.
        row = next(row for row in blanked[0]["check"]["scores"] if row["area"] == "security")
        self.assertEqual(row, {"area": "security", "score": None, "partial": True, "withheld": True})
        info = outcomes.RULES["SEC-001"].info
        text = profile.to_json(blanked[0]) + profile.render_markdown(blanked[0]) + profile.render_sheet(blanked[0], TODAY)
        for telling in (info.subject, info.passed, info.variants[0].title, info.variants[0].impact, info.variants[0].remedy):
            self.assertNotIn(telling, text)

    def test_two_instances_that_differ_only_in_what_was_withheld_give_the_same_file(self):
        def file(cookie):
            snapshot = Snapshot(env={**QUEUE, "N8N_SECURE_COOKIE": cookie}, health={"healthz": True, "readiness": True})
            made = profile.build_profile(snapshot, [], (), now=NOW, checks=engine.assess(snapshot, ALL_RULES))
            return made, profile.apply_withholding(made, ["check:SEC-001", "setting:instance:N8N_SECURE_COOKIE"])

        (insecure, hidden_insecure), (secure, hidden_secure) = file("false"), file("true")
        self.assertEqual((entries(insecure)["SEC-001"]["outcome"], entries(secure)["SEC-001"]["outcome"]), ("finding", "passed"))
        self.assertNotEqual(insecure["check"]["scores"], secure["check"]["scores"])
        self.assertEqual(hidden_insecure, hidden_secure)
        self.assertEqual(profile.render_sheet(hidden_insecure, TODAY), profile.render_sheet(hidden_secure, TODAY))

    def test_only_the_area_of_the_check_and_only_when_one_check_is_withheld(self):
        made = built()
        self.assertTrue(all("withheld" not in row for row in made["check"]["scores"]))
        self.assertTrue(all("withheld" not in row for row in profile.apply_withholding(made, ["worker.replicas", "setting:instance:DB_TYPE"])["check"]["scores"]))
        for rule in ALL_RULES:
            blanked = profile.apply_withholding(made, [f"check:{rule.info.rule_id}"])
            for row, before in zip(blanked["check"]["scores"], made["check"]["scores"]):
                expected = {**before, "score": None, "withheld": True} if row["area"] == rule.info.area else before
                self.assertEqual(row, expected, rule.info.rule_id)
            self.assertIs(profile.validate(blanked), blanked)

    def test_a_withheld_score_is_shown_as_withheld_and_missing_data_as_missing(self):
        blanked = profile.apply_withholding(built(), ["check:SEC-001"])
        shown = profile.render_markdown(blanked)
        self.assertIn("| security | withheld |\n", shown)
        self.assertIn("| reliability | 80/100 (partial) |\n", shown)
        self.assertNotIn("n/a", shown)
        sheet = profile.render_sheet(blanked, TODAY)
        self.assertIn("\n\nChecks withheld by the operator: 1. The score of its area is withheld with a check: security.\n", sheet)
        # A score that is null because nothing was read is not a withheld one.
        nothing = profile.render_markdown(built(Snapshot(), failed=()))
        self.assertIn("| security | n/a |\n", nothing)
        self.assertNotIn("| withheld |", nothing)
        self.assertNotIn("is withheld with a check", profile.render_sheet(built(Snapshot(), failed=()), TODAY))
        # The mark alone, next to a score, hides nothing and is not shown as withheld.
        odd = built()
        odd["check"]["scores"][0]["withheld"] = True
        self.assertNotIn("| withheld |", profile.render_markdown(profile.validate(odd)))

    @unittest.skipUnless(page.NODE, page.NO_NODE)
    def test_the_page_withholds_and_shows_the_same(self):
        files = self.variants()
        for raw in page.PATHS:
            got = page.run_page_code(
                "input.files.map((text) => { const saved = profileText(withholdEntries(parseExact(text).profile, new Set(['check:SEC-001']), 'baseline', ''));"
                " return [saved, callSheetMarkdown(parseExact(saved), input.catalogue, new Date(input.today))]; })",
                raw=raw, files=[page.response_text(file) for file in files], catalogue=page.CATALOGUE, today="2026-11-24T12:00:00Z",
            )  # fmt: skip
            expected = profile.apply_withholding(files[0], ["check:SEC-001"])
            for saved, sheet in got:
                self.assertEqual(json.loads(saved), expected)
                self.assertEqual(sheet, profile.render_sheet(expected, TODAY))
            self.assertEqual(len({saved for saved, _ in got}), 1)
        shown = page.run_page(
            """async (page, input) => {
              const opened = await page.open(input.file);
              const rows = page.walk(page.$("profile-body")).filter((node) => node.tagName === "tr").map((node) => node.textContent);
              return { shown: opened.shown, scores: rows.filter((row) => input.areas.some((area) => row.startsWith(area))) };
            }""",
            file=profile.to_json(expected), areas=list(AREAS),
        )
        self.assertIn("securitywithheld", shown["scores"])
        self.assertIn("reliability80/100 (partial)", shown["scores"])
        self.assertNotIn("securityn/a", shown["scores"])
        self.assertIn("The score of its area is withheld with a check: security.", shown["shown"])
        for telling in (outcomes.RULES["SEC-001"].info.variants[0].title, outcomes.RULES["SEC-001"].info.passed):
            self.assertNotIn(telling, shown["shown"])


# --- fixed texts -------------------------------------------------------------------


class FixedTextTest(unittest.TestCase):
    def test_every_text_in_checks_is_a_literal_of_the_rules_whatever_the_instance(self):
        vocabulary = (
            {rule.info.rule_id for rule in ALL_RULES} | set(AREAS) | set(SEVERITIES) | set(BASES)
            | set(NOT_ASSESSED_REASONS) | set(SOURCE_IDS) | {"finding", "passed", "not_applicable", "not_assessed"}
        )  # fmt: skip
        seen = set()
        for snapshot in snapshots():
            made = built(snapshot)
            ids = {fact["id"] for fact in made["facts"]} | {profile.setting_id(s) for s in made["settings"]}
            for entry in made["check"]["checks"]:
                for key in ("text", "impact", "remedy"):
                    if key in entry:
                        self.assertIn(entry[key], LITERALS, (entry["rule_id"], key))
                        seen.add(entry[key])
                # ... and nothing else in an entry is free text either.
                for key, value in entry.items():
                    for item in value if isinstance(value, list) else [value]:
                        self.assertIn(item, LITERALS | vocabulary | ids, (entry["rule_id"], key))
        self.assertEqual(seen, LITERALS)  # every sentence of every rule was written at least once

    def test_no_literal_can_hold_what_a_profile_forbids(self):
        for text in LITERALS:
            with self.subTest(text=text[:40]):
                self.assertTrue(text.isascii() and text == " ".join(text.split()))
                for forbidden in ("://", "@", "\\", "\n", "{", "}", "%s", base.MARK):
                    self.assertNotIn(forbidden, text)
                self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text))  # no IP address

    def test_rules_md_is_in_step_with_the_code(self):
        with open(os.path.join(DOCS, "rules.md"), encoding="utf-8") as handle:
            document = handle.read()
        self.assertEqual(re.findall(r"^### ([A-Z]+-\d+)$", document, re.M), [rule.info.rule_id for rule in ALL_RULES])
        for rule in ALL_RULES:
            info = rule.info
            section = document.split(f"### {info.rule_id}\n")[1].split("\n### ")[0].split("\n## ")[0]
            with self.subTest(rule=info.rule_id):
                self.assertIn(f"| [{info.rule_id}](#{info.rule_id.lower()}) | {info.area} | {info.subject} |", document)
                self.assertIn(f"could not decide): {info.subject}\n", section)
                self.assertIn(f"- **Passed text:** {info.passed}\n", section)
                self.assertEqual(
                    [(title, impact, remedy) for title, impact, remedy in re.findall(r"- Title: (.*)\n- Impact: (.*)\n- Remedy: (.*)\n", section)],
                    [(v.title, v.impact, v.remedy) for v in info.variants],
                )
                for item in info.facts + info.settings:
                    self.assertIn(f"`{item}`", section.split("- **Rests on:**")[1].split("\n")[0])
        for word in ("finding", "passed", "not_applicable", "not_assessed", *NOT_ASSESSED_REASONS, *SOURCE_IDS):
            self.assertIn(f"`{word}`", document)

    def test_the_format_document_describes_checks(self):
        with open(os.path.join(DOCS, "profile-format.md"), encoding="utf-8") as handle:
            document = handle.read()
        for word in ("check.checks", "`rests_on`", "`basis`", "`needs`", "`check:<RULE_ID>`", "--show-sheet", *profile.CHECK_OUTCOMES, *NOT_ASSESSED_REASONS, *SOURCE_IDS):
            self.assertIn(word, document)
        self.assertEqual(set(profile.REASON_LABELS), set(NOT_ASSESSED_REASONS))
        self.assertEqual(set(profile.NEED_LABELS), set(SOURCE_IDS))
        self.assertEqual(set(profile.OUTCOME_LABELS), set(profile.CHECK_OUTCOMES))
        # The JSON examples of the section are entries a reader accepts.
        section = document.split("### The outcome of every rule")[1].split("| Key |")[0]
        examples = [json.loads(block) for block in section.split("```json")[1].split("```")[0].strip().split("\n\n")]
        self.assertEqual([example["outcome"] for example in examples], ["passed", "finding", "not_assessed", "not_applicable", "withheld"])
        made = built()
        made["check"]["checks"] = examples
        profile.validate(made)
        for example in examples[:1] + examples[2:4]:
            self.assertIn(example["text"], LITERALS)


# --- zero leak ---------------------------------------------------------------------
# With the same markers as tests/test_profile.py. Do not weaken it: add places, never remove them.


def without_literals(text):
    """`text` with every fixed sentence of the rules taken out, as written and as a Markdown cell holds it."""
    for literal in sorted(LITERALS, key=len, reverse=True):
        text = text.replace(profile._cell(literal), "").replace(literal, "")
    return text


def seeded():
    """(label, snapshot, failed): instances whose every identifier and secret holds a marker."""
    leaky = base.leaky_snapshot()
    without_env = base.leaky_snapshot()
    without_env.env = None
    with_text = pasted.snapshot(kubernetes=base.leaky_snapshot().kubernetes)  # its ids and its user agent are seeded too
    return [("everything", leaky, ("redis",)), ("Kubernetes alone", without_env, ("env", "postgres")), ("a debug information", with_text, ())]


class ZeroLeakTest(unittest.TestCase):
    MARKS = (base.MARK, pasted.MARK)

    def assert_clean(self, text, where):
        for mark in self.MARKS:
            self.assertNotIn(mark, text.lower(), where)
        for seeded_value in base.SEEDED_IPS + ("471147",):
            self.assertNotIn(seeded_value, text, where)
        # Once the fixed sentences are taken out, what is left obeys the rules of the rest of the
        # profile: no URL, e-mail or path can be written at all.
        rest = without_literals(text)
        for fragment in ("://", "@", "\\", "<redacted>", "<valueFrom>"):
            self.assertNotIn(fragment, rest, where)
        self.assertIsNone(re.search(r"/(?!100\b)", rest.replace("n/a", "")), f"a slash outside a score: {where}")

    def test_the_seeded_instances_really_hold_the_markers_and_raise_findings(self):
        for label, snapshot, _failed in seeded():
            evidence = " ".join(f.evidence for f in engine.run(snapshot, ALL_RULES))
            self.assertTrue(any(mark in evidence.lower() for mark in self.MARKS), label)
            self.assertTrue(any(mark in json.dumps(snapshot.kubernetes).lower() for mark in self.MARKS), label)

    def test_nothing_seeded_reaches_the_checks_or_the_review(self):
        for label, snapshot, failed in seeded():
            made = profile.build_profile(snapshot, [], failed, checks=engine.assess(snapshot, ALL_RULES))
            self.assert_clean(json.dumps(made["check"]["checks"], ensure_ascii=False), label)
            self.assert_clean(profile.to_json(made), label)
            self.assert_clean(profile.render_markdown(made), label)
            # ... and the checks are not clean merely because they are empty.
            found = [entry for entry in made["check"]["checks"] if entry["outcome"] == "finding"]
            self.assertGreater(len(found), 3, label)
            self.assertTrue(all(entry["impact"] and entry["remedy"] for entry in found))
            self.assertTrue(any(entry["rests_on"] for entry in found))
            self.assertIn("passed", {entry["outcome"] for entry in made["check"]["checks"]})

    def test_nothing_seeded_reaches_the_call_sheet(self):
        for label, snapshot, failed in seeded():
            made = profile.build_profile(snapshot, [], failed, checks=engine.assess(snapshot, ALL_RULES))
            for variant, file in (("as built", made), ("a fact withheld", profile.apply_withholding(made, ["main.replicas", "check:REL-008"]))):
                sheet = profile.render_sheet(file, TODAY)
                for mark in self.MARKS:
                    self.assertNotIn(mark, sheet.lower(), (label, variant))
                # The checks' part of the sheet, under the same rules as the file. (The questions
                # that follow are the question sheet, which tests/test_questions.py holds to its own.)
                self.assert_clean(sheet.split("\n## Questions still open")[0], (label, variant))
                self.assertIn("## What does not\n\n1. **", sheet)
                self.assertIn("   - Impact: ", sheet)

    def test_a_value_reaches_the_sheet_only_through_an_entry_the_operator_can_withhold(self):
        made = built()
        sheet = profile.render_sheet(made, TODAY)
        self.assertIn("`setting:instance:N8N_SECURE_COOKIE` = false", sheet)
        self.assertIn("`queue.maxmemory_policy` = allkeys-lru", sheet)
        self.assertIn("- **Execution mode:** queue (measured)", sheet)
        # The same variable on the main container is an entry of its own, which the check names as well.
        self.assertIn("`setting:main:N8N_SECURE_COOKIE` = false", sheet)
        blanked = profile.apply_withholding(
            made, ["setting:instance:N8N_SECURE_COOKIE", "setting:main:N8N_SECURE_COOKIE", "queue.maxmemory_policy", "executions.mode", "worker.replicas"]
        )
        sheet = profile.render_sheet(blanked, TODAY)
        self.assertIn("`setting:instance:N8N_SECURE_COOKIE` withheld", sheet)
        self.assertIn("`setting:main:N8N_SECURE_COOKIE` withheld", sheet)
        self.assertIn("`queue.maxmemory_policy` withheld", sheet)
        self.assertIn("- **Execution mode:** withheld", sheet)
        self.assertIn("- **Worker replicas:** withheld", sheet)
        self.assertNotIn("allkeys-lru", sheet)
        self.assertNotIn("= false", sheet)

    @unittest.skipUnless(page.NODE, page.NO_NODE)
    def test_nothing_seeded_reaches_the_page(self):
        for label, snapshot, failed in seeded():
            made = profile.build_profile(snapshot, [], failed, checks=engine.assess(snapshot, ALL_RULES))
            got = page.run_page(
                """async (page, input) => {
                  const opened = await page.open(input.file);
                  page.$("call-copy").click();
                  await page.settle();
                  const made = page.walk(page.$("profile-body"));
                  return { view: opened.view, shown: opened.shown, copied: page.copied[0],
                           attributes: made.flatMap((node) => Object.values(node.attributes)).join(" ") };
                }""",
                file=profile.to_json(made),
            )
            self.assertEqual(got["view"], "profile")
            for text in (got["shown"], got["copied"], got["attributes"]):
                for mark in self.MARKS:
                    self.assertNotIn(mark, text.lower(), label)
            self.assert_clean(got["copied"].split("\n## Questions still open")[0], label)
            self.assertIn("Call sheet", got["shown"])
            self.assertIn("What does not (", got["shown"])


# --- the call sheet ----------------------------------------------------------------

HEADINGS = ["Run", "The environment in brief", "What works well", "What does not", "What to improve", "Not assessed", "Does not apply", "Questions still open"]


def headings(sheet):
    return re.findall(r"^## (.*)$", sheet, re.M)


def part(sheet, heading):
    return sheet.split(f"\n## {heading}\n")[1].split("\n## ")[0].strip()


def with_facts(made, **values):
    """A copy of a profile with the given facts measured ("usage_users" for usage.users); None withholds one."""
    made = copy.deepcopy(made)
    wanted = {name.replace("__", "."): value for name, value in values.items()}
    for fact in made["facts"]:
        if fact["id"] in wanted:
            value = wanted[fact["id"]]
            fact.update(value=value, provenance="withheld" if value is None else "measured", source=None if value is None else "api")
    return made


def sheet_files():
    """(label, profile): every shape of the sheet, compared between the command line and the page."""
    made = built()
    hostile_text = "a|b <b>bold</b> [link](x) `code` back\\slash *star* # <script>alert(1)</script> \ttab\nline\x00nul  ​ wide \U0001f600"
    hostile = copy.deepcopy(made)
    hostile["created_at"] = hostile["tool_version"] = hostile_text
    hostile["run"]["reference"] = "a|b <b>bold</b> `code` [link](x)"
    hostile["sources"][hostile_text] = "failed"
    hostile["facts"][0].update(value=hostile_text, provenance="declared")
    hostile["settings"].append({"scope": hostile_text, "name": hostile_text, "state": "value", "value": hostile_text})
    hostile["settings"].append({"scope": "instance", "name": "N8N_HOST", "state": "set", "value": None})
    for entry in hostile["check"]["checks"][:12]:
        for key in ("rule_id", "area", "text", "impact", "remedy", "severity"):
            if key in entry:
                entry[key] = hostile_text + entry[key]
        if "needs" in entry:
            entry["needs"] = [hostile_text, "postgres", "redis", "url"]
        entry["rests_on"] = [hostile_text, f"setting:{hostile_text}:{hostile_text}", "worker.replicas", "database.version", "setting:instance:DB_TYPE", "setting:instance:N8N_HOST", "nowhere.at.all"]
    later = copy.deepcopy(made)
    later["check"]["checks"] += [
        {"rule_id": "NEW-001", "area": "upgrade", "outcome": "will_break", "text": "A later outcome"},
        {"rule_id": "NEW-002", "area": "upgrade", "outcome": "not_assessed", "reason": "too_new", "text": "A later reason"},
        {"rule_id": "ODD-001", "area": "security", "outcome": "finding", "text": "Odd", "severity": 3, "impact": None, "remedy": ["x"], "rests_on": "worker.replicas"},
        {"rule_id": "ODD-002", "area": "security", "outcome": "not_assessed", "text": "Odd too", "reason": "source_missing", "needs": [1, "postgres", "a moon"]},
        {"rule_id": "ODD-003", "area": "constructor", "outcome": "passed", "text": "Odd three", "basis": 7},
        {"rule_id": "ODD-004", "area": "__proto__", "outcome": "toString", "text": "Odd four", "reason": "constructor"},
        {"rule_id": "ODD-005", "area": "x", "outcome": "not_assessed", "text": "No needs", "reason": "source_missing"},
        {"rule_id": "ODD-006", "area": "x", "outcome": "not_assessed", "text": "Failed", "reason": "source_failed", "needs": ["redis"]},
    ]
    older = copy.deepcopy(made)
    del older["check"]["checks"]
    older["tool_version"] = "0.5.0"
    older["created_at"] = "2026-09-01T08:00:00Z"
    oldest = copy.deepcopy(older)
    oldest["tool_version"] = "0.2.0"
    oldest["facts"] = [fact for fact in oldest["facts"] if fact["id"] in profile.FORMAT_1_FACT_IDS]
    oldest["check"].update(findings=[], partial=False)
    usage = {"usage__executions_per_day": 1200.5, "usage__failure_rate_percent": 2.5, "usage__active_workflows": 34, "usage__users": 7}
    nothing = built(Snapshot(), failed=())
    clean = copy.deepcopy(made)
    clean["check"].update(findings=[], partial=False, checks=[e for e in made["check"]["checks"] if e["outcome"] == "passed"])
    return [
        ("a run with findings", made),
        ("one check and some facts withheld", profile.apply_withholding(made, ["check:SEC-001", "executions.mode", "worker.replicas", "setting:instance:DB_TYPE"])),
        ("the check outcome withheld", profile.apply_withholding(made, ["check"])),
        ("written by 0.5.0, 84 days old", older),
        ("written by 0.2.0, no finding", oldest),
        ("nothing collected", nothing),
        ("sources that disagree", built(pasted.snapshot(env={"N8N_SECURE_COOKIE": "true"}), failed=())),
        ("only passed checks", clean),
        ("no check at all", {**made, "check": {**made["check"], "checks": [], "findings": []}}),
        ("usage numbers, an estimate", with_facts(made, usage__estimate=True, **usage)),
        ("usage numbers, exact", with_facts(made, usage__estimate=False, **usage)),
        ("usage numbers, the estimate withheld", with_facts(made, usage__estimate=None, **usage)),
        ("a debug information alone", profile.build_profile(pasted.snapshot(), [], (), now=NOW, checks=engine.assess(pasted.snapshot(), ALL_RULES))),
        ("what a later version may add", later),
        ("Markdown and HTML in its text", hostile),
        ("a date that is not one", {**made, "created_at": "2026-02-30T00:00:00Z"}),
        ("a date in the future", {**made, "created_at": "2031-01-01T00:00:00Z"}),
    ]


class SheetTest(unittest.TestCase):
    def test_the_sections_in_order(self):
        sheet = profile.render_sheet(built(), TODAY)
        self.assertTrue(sheet.startswith("# n8n call sheet\n\n## Run\n\n- **Taken:** 2026-11-24T09:30:00Z\n"))
        self.assertEqual(headings(sheet), HEADINGS)
        self.assertIn("- **Sources:** health collected; metrics not provided; env collected; postgres failed; redis collected; kubernetes collected; debug_info not provided; api not provided", sheet)
        self.assertNotIn("days ago", sheet)
        self.assertEqual(
            part(sheet, "The environment in brief"),
            "- **n8n version:** 2.40.1 (measured)\n- **Execution mode:** queue (measured)\n- **Main replicas:** 1 (measured)\n"
            "- **Worker replicas:** 2 (measured)\n- **Database type:** postgresdb (measured)\n- **Executions per day:** unknown\n"
            "- **Failure rate:** unknown\n- **Active workflows:** unknown\n- **Users:** unknown",
        )
        self.assertTrue(sheet.endswith("\n"))
        self.assertFalse(sheet.endswith("\n\n"))

    def test_what_works_well_is_the_passed_checks_by_area(self):
        made = built()
        works = part(profile.render_sheet(made, TODAY), "What works well")
        passed = [entry for entry in made["check"]["checks"] if entry["outcome"] == "passed"]
        self.assertEqual(re.findall(r"^- ([A-Z]+-\d+) ", works, re.M), [entry["rule_id"] for entry in passed])
        self.assertEqual(re.findall(r"^### (.*)$", works, re.M), [area.capitalize() for area in dict.fromkeys(e["area"] for e in passed)])
        self.assertIn(f"- SCAL-002 {outcomes.RULES['SCAL-002'].info.passed}\n", works)
        # A pass that rests on a default says so.
        self.assertIn("- PERF-001 Execution data pruning is enabled. (by n8n's default, not set explicitly)", works)
        self.assertEqual(works.count("by n8n's default"), sum(entry["basis"] == "default" for entry in passed))
        # Nothing that was not positively verified is under this heading.
        for entry in made["check"]["checks"]:
            if entry["outcome"] != "passed":
                self.assertNotIn(entry["rule_id"], works)

    def test_what_does_not_and_what_to_improve_are_the_findings_most_severe_first(self):
        made = built(Snapshot(env={"EXECUTIONS_MODE": "queue", "N8N_SECURE_COOKIE": "false"}, health={"healthz": True, "readiness": False}), failed=())
        sheet = profile.render_sheet(made, TODAY)
        order = [finding["rule_id"] for finding in made["check"]["findings"]]
        self.assertEqual([finding["severity"] for finding in made["check"]["findings"]][:2], ["critical", "critical"])
        self.assertEqual(re.findall(r"^\d+\. \*\*([A-Z]+-\d+)\*\* \(", part(sheet, "What does not"), re.M), order)
        self.assertEqual(re.findall(r"^\d+\. \*\*([A-Z]+-\d+), ", part(sheet, "What to improve"), re.M), order)
        info = outcomes.RULES["REL-001"].info
        self.assertIn(
            f"1. **REL-001** (critical, reliability): Queue mode on SQLite\n   - Impact: {info.variants[0].impact}\n"
            "   - Rests on: `executions.mode` = queue; `database.engine` = sqlite; `setting:instance:EXECUTIONS_MODE` = queue\n",
            sheet,
        )
        self.assertIn(f"1. **REL-001, Queue mode on SQLite:** {info.variants[0].remedy}\n", sheet)
        # What the rule read is in no part of the profile: the sheet says where it is.
        self.assertIn("   - Rests on values that are not in the profile: they are in the report the operator keeps.\n", part(sheet, "What does not") + "\n")

    def test_not_assessed_is_grouped_by_what_would_have_to_be_given(self):
        sheet = profile.render_sheet(built(), TODAY)
        self.assertEqual(
            part(sheet, "Not assessed"),
            "- **With the debug information:** REL-009 Cluster checks that n8n runs itself; MAINT-004 Kind of license; "
            "SEC-003 Access to n8n's own files from the nodes\n"
            "- **Asked for and not collected: Postgres:** SCAL-005 Postgres connections in use\n"
            "- **A source was read and does not say:** PERF-003 Where binary data is kept",
        )
        self.assertEqual(part(sheet, "Does not apply"), "SCAL-001 Limit on concurrent production executions in regular mode")
        nothing = profile.render_sheet(built(Snapshot(), failed=()), TODAY)
        groups = part(nothing, "Not assessed").splitlines()
        self.assertIn("- **With Kubernetes or the debug information:** SCAL-002 Number of queue-mode workers; MAINT-003 n8n version of every process", groups)
        self.assertTrue(groups[0].startswith("- **With any one of the env file, Kubernetes or the debug information:** SCAL-001 "))
        self.assertIn("- **With the n8n URL:** REL-005 Readiness of the instance", groups)
        self.assertEqual(sum(line.count(";") + 1 for line in groups), 24)
        self.assertEqual(part(nothing, "What works well"), "Checks that read the values they need and found the good condition.\n\nNo check passed.")
        unreadable = built(Snapshot(kubernetes=outcomes.main({"EXECUTIONS_DATA_PRUNE": outcomes.SECRET})), failed=())
        self.assertIn("- **The value could not be read (a Secret, a file, a redacted value or an incomplete environment):** PERF-001 Pruning of execution data", profile.render_sheet(unreadable, TODAY))

    def test_edge_cases(self):
        files = dict(sheet_files())
        withheld = profile.render_sheet(files["the check outcome withheld"], TODAY)
        self.assertEqual(headings(withheld), ["Run", "The environment in brief", "Check outcome", "Questions still open"])
        self.assertEqual(part(withheld, "Check outcome"), "The operator withheld the check outcome: what passed, the findings and what was not assessed are not in the file.")

        older = profile.render_sheet(files["written by 0.5.0, 84 days old"], TODAY)
        self.assertEqual(headings(older), ["Run", "The environment in brief", "What does not", "Questions still open"])
        self.assertIn("\n\nThis profile was taken 84 days ago: the environment may have changed since.\n", older)
        findings = files["written by 0.5.0, 84 days old"]["check"]["findings"]
        self.assertEqual(
            part(older, "What does not"),
            "\n".join(f"{n}. **{f['rule_id']}** ({f['severity']}, {f['area']}): {f['title']}" for n, f in enumerate(findings, 1))
            + "\n\nThis file was written by a version of n8n-healthcheck that does not record what passed, the impact and "
            "the remedy of a finding, what was not assessed or what does not apply.",
        )
        self.assertNotIn("Impact", older)
        oldest = profile.render_sheet(files["written by 0.2.0, no finding"], TODAY)
        self.assertIn("## What does not\n\nNo finding. Every source was collected and every rule ran.\n\nThis file was written by a version", oldest)
        self.assertIn("- **Users:** unknown\n", oldest)  # a fact the file does not hold

        one = profile.render_sheet(files["one check and some facts withheld"], TODAY)
        self.assertEqual(headings(one), HEADINGS)
        self.assertTrue(part(one, "Does not apply").endswith("\n\nChecks withheld by the operator: 1. The score of its area is withheld with a check: security."))
        self.assertNotIn("SEC-001", one.split("## Questions still open")[0])
        self.assertNotIn("Session cookie", one)

        clean = profile.render_sheet(files["only passed checks"], TODAY)
        self.assertEqual(part(clean, "What does not"), "No finding. Every source was collected and every rule ran.")
        self.assertEqual(part(clean, "What to improve"), "Nothing to improve was found.")
        self.assertEqual(part(clean, "Not assessed"), "Every check that applies was assessed.")
        self.assertEqual(part(clean, "Does not apply"), "No check was left out as not applying.")
        nothing = profile.render_sheet(files["nothing collected"], TODAY)
        self.assertEqual(part(nothing, "What does not"), "No finding. The check was partial: not every source was collected or not every rule ran.")

    def test_the_age_of_the_profile(self):
        made = built()
        for days, said in ((0, False), (30, False), (31, True), (400, True), (-3, False)):
            made["created_at"] = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sheet = profile.render_sheet(made, TODAY)
            self.assertEqual(f"This profile was taken {days} days ago" in sheet, said, days)
        for not_a_date in ("", "yesterday", "2026-02-30T00:00:00Z", "0000-01-01", "２０２６-01-01"):
            made["created_at"] = not_a_date
            self.assertNotIn("days ago", profile.render_sheet(made, TODAY))
        # Without a day given, today's.
        made["created_at"] = "2020-01-01T00:00:00Z"
        self.assertIn("days ago", profile.render_sheet(made))

    def test_usage_numbers_say_when_they_are_an_estimate(self):
        files = dict(sheet_files())
        brief = lambda label: part(profile.render_sheet(files[label], TODAY), "The environment in brief")
        self.assertIn("- **Executions per day:** about 1200.5 (measured)\n- **Failure rate:** about 2.5% (measured)\n- **Active workflows:** 34 (measured)\n- **Users:** 7 (measured)", brief("usage numbers, an estimate"))
        self.assertIn("- **Executions per day:** 1200.5 (measured)\n- **Failure rate:** 2.5% (measured)\n", brief("usage numbers, exact"))
        self.assertIn("- **Executions per day:** 1200.5 (measured, may be an estimate)\n- **Failure rate:** 2.5% (measured, may be an estimate)\n- **Active workflows:** 34 (measured)", brief("usage numbers, the estimate withheld"))

    def test_text_of_a_file_cannot_become_markup(self):
        sheet = profile.render_sheet(dict(sheet_files())["Markdown and HTML in its text"], TODAY)
        self.assertEqual(headings(sheet), HEADINGS)
        for raw in ("<b>", "<script>", "[link]", "`code`", "a|b", "\x00", " ", "​", "\ttab"):
            self.assertNotIn(raw, sheet)
        self.assertIn("a\\|b \\<b\\>bold\\<\\/b\\>".replace("\\/", "/"), sheet)
        self.assertNotIn("\n\n\n", sheet)

    def test_the_sheet_changes_nothing(self):
        for label, file in sheet_files():
            before = copy.deepcopy(file)
            profile.render_sheet(file, TODAY)
            self.assertEqual(file, before, label)

    def test_the_questions_part_is_the_question_sheet(self):
        for label, file in sheet_files():
            questions_sheet = profile.render_questions(file).split("\n## ", 1)[1]
            in_the_call_sheet = profile.render_sheet(file, TODAY).split("\n## Questions still open\n\n### ", 1)[1]
            self.assertEqual(in_the_call_sheet, questions_sheet.replace("\n## ", "\n### "), label)


# --- command line ------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def run_cli(self, *argv, snapshot=RICH):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as exit_:
                    code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def saved(self, *argv):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "profile.json")
            code, out, err = self.run_cli("--url", "https://n8n.example.com", "--save-profile", path, *argv)
            file = profile.load_profile(path) if os.path.exists(path) else None
        return code, out, err, file

    def test_a_saved_profile_holds_the_checks_and_the_report_is_unchanged(self):
        code, out, err, file = self.saved()
        self.assertEqual(code, 1)
        self.assertEqual([entry["rule_id"] for entry in file["check"]["checks"]], [rule.info.rule_id for rule in ALL_RULES])
        self.assertEqual(file["tool_version"], "0.6.0")
        self.assertEqual(file["format_version"], 1)
        from n8n_healthcheck import report

        self.assertEqual(out, report.render_markdown(RICH, engine.run(RICH, ALL_RULES), failed=("metrics",)))
        self.assertEqual(err, "")

    def test_withhold_one_check(self):
        code, _out, err, file = self.saved("--withhold", "check:SEC-001", "--withhold", "worker.replicas")
        self.assertEqual(code, 1)  # the exit code is the one of the health check
        self.assertEqual(entries(file)["SEC-001"], {"rule_id": "SEC-001", "area": "security", "outcome": "withheld", "text": ""})
        self.assertNotIn("SEC-001", [finding["rule_id"] for finding in file["check"]["findings"]])
        # Nothing was withheld for the operator: they are told which checks rest on what they withheld.
        self.assertEqual(entries(file)["SCAL-002"]["outcome"], "passed")
        self.assertEqual(
            err,
            "note: worker.replicas is withheld, and these checks in the profile rest on it: SCAL-002. "
            "To leave one out too: --withhold check:<RULE_ID>\n",
        )
        code, _out, err, file = self.saved("--withhold", "check:SCAL-002", "--withhold", "worker.replicas")
        self.assertEqual(err, "")
        code, _out, err, file = self.saved("--withhold", "check", "--withhold", "worker.replicas")
        self.assertEqual((err, file["check"]), ("", {"withheld": True}))

    def test_a_check_id_that_does_not_exist_exits_3_and_writes_nothing(self):
        for item in ("check:NOPE-1", "check:", "checks:SEC-001"):
            code, out, err, file = self.saved("--withhold", item)
            self.assertEqual((code, out, file), (3, "", None), item)
            self.assertIn("--withhold", err)

    def test_show_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            path, output = os.path.join(folder, "profile.json"), os.path.join(folder, "sheet.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(profile.to_json(built()))
            code, out, err = self.run_cli("--show-sheet", path)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(out, profile.render_sheet(built()))
            self.assertEqual(self.run_cli("--show-sheet", path, "-o", output), (0, "", ""))
            with open(output, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), out)
            # Like --show-profile and --show-questions: it takes only -o, and a bad file is exit 3.
            for extra in (("--url", "https://n8n.example.com"), ("--show-profile", path), ("--withhold", "check"), ("--reason", "other")):
                code, out, err = self.run_cli("--show-sheet", path, *extra)
                self.assertEqual((code, out), (3, ""), extra)
                self.assertIn("takes no other flag except -o", err)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"format": "something else"}')
            code, out, err = self.run_cli("--show-sheet", path)
            self.assertEqual((code, out), (3, ""))
            self.assertIn("cannot show profile", err)
            code, out, err = self.run_cli("--show-sheet", os.path.join(folder, "missing.json"))
            self.assertEqual((code, out), (3, ""))

    def test_the_sheet_never_ends_in_a_traceback(self):
        made = built()
        made["check"]["checks"][0]["rests_on"] = [{"a": 1}, ["b"], 3, None]
        made["check"]["checks"][1].update(needs={"a": 1}, severity=[], impact=3, remedy={}, basis=[], reason={})
        made["facts"][0]["value"] = {"a": {"b": 1}}
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "profile.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(made))
            for flag in ("--show-sheet", "--show-profile"):
                code, out, err = self.run_cli(flag, path)
                self.assertEqual((code, err), (0, ""), flag)
                self.assertNotIn("Traceback", out)

    def test_flags_that_worked_before_still_work(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "profile.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(profile.to_json(built()))
            self.assertEqual(self.run_cli("--show-p", path)[1], profile.render_markdown(built()))
            self.assertEqual(self.run_cli("--show-q", path)[1], profile.render_questions(built()))
            self.assertEqual(self.run_cli("--sh", path)[1], profile.render_markdown(built()))  # as before the questions and the sheet
            self.assertEqual(self.run_cli("--show-s", path)[1], profile.render_sheet(built()))
        code, out, _err = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--show-sheet FILE", out)
        self.assertIn("check:<RULE_ID>", out)


# --- the page ----------------------------------------------------------------------


def run_response(snapshot=RICH):
    """The text POST /api/run answers with, from the real server code and the real rules."""
    with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot):
        return json.dumps(cli.json_safe(web.run_check({"url": "https://n8n.example.com"})), allow_nan=False)


class PageStyleTest(unittest.TestCase):
    def test_a_long_text_without_spaces_wraps_in_the_findings_of_the_sheet(self):
        # The impact and the remedy of a finding are paragraphs inside these list items.
        css = (web.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        for selector in (r"\.pfindings li", r"\.sheet li"):
            (rule,) = re.findall(rf"^{selector} \{{([^}}]*)\}}", css, re.M)
            self.assertIn("overflow-wrap: anywhere", rule, selector)
        # Every ordered list of the call sheet is one of the two.
        script = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r'el\("ol", \{ class: "([a-z]+)" \}', script)), {"pfindings", "sheet"})
        self.assertEqual(len(re.findall(r'el\("ol",', script)), len(re.findall(r'el\("ol", \{ class: "(?:pfindings|sheet)" \}', script)))


@unittest.skipUnless(page.NODE, page.NO_NODE)
class PageCodeTest(unittest.TestCase):
    def test_the_sheet_is_the_one_show_sheet_prints(self):
        files = sheet_files()
        for raw in page.PATHS:
            got = page.run_page_code(
                "input.files.map((text) => callSheetMarkdown(parseExact(text), input.catalogue, new Date(input.today)))",
                raw=raw, files=[json.dumps(file, ensure_ascii=False) for _, file in files], catalogue=page.CATALOGUE, today="2026-11-24T23:59:00Z",
            )  # fmt: skip
            for (label, file), sheet in zip(files, got):
                with self.subTest(rawJSON=raw, file=label):
                    self.assertEqual(sheet, profile.render_sheet(profile.validate(json.loads(json.dumps(file))), TODAY))
        # The comparison covers every shape of the sheet.
        printed = "".join(profile.render_sheet(file, TODAY) for _, file in files)
        for text in (
            "days ago", "about 1200.5", "may be an estimate", "by n8n's default", "No check passed.", "Rests on values that are not in the profile",
            "is not in the file", "is set (value not written)", "` withheld", "` unknown", "No finding. The check was partial", "No finding. Every source",
            "Nothing to improve was found.", "Every check that applies was assessed.", "No check was left out", "Checks withheld by the operator: 1.",
            "The operator withheld the check outcome", "does not record what passed", "**With any one of", "**Asked for and not collected: Redis",
            "**A source was read and does not say", "**Not known to this version", "**With a source that states it:** ODD-005",
            "**Two sources given in the run disagree on what the check reads:** SCAL-001",
        ):  # fmt: skip
            self.assertIn(text, printed)

    def test_the_age_is_counted_in_days_of_the_calendar_in_utc(self):
        cases = [("2026-10-24T23:59:59Z", "2026-11-24T00:00:01Z"), ("2026-10-24T00:00:00Z", "2026-11-23T23:59:59Z"), ("2024-02-29T12:00:00Z", "2026-11-24T12:00:00Z"),
                 ("0099-01-01T00:00:00Z", "2026-11-24T12:00:00Z"), ("2026-11-25T00:00:00Z", "2026-11-24T12:00:00Z"), ("2026-13-01", "2026-11-24T12:00:00Z"),
                 ("2026-1-1T00:00:00Z", "2026-11-24T12:00:00Z"), ("0000-01-01", "2026-11-24T12:00:00Z"), ("", "2026-11-24T12:00:00Z")]  # fmt: skip
        got = page.run_page_code("input.cases.map(([taken, now]) => ageDays(taken, new Date(now)))", cases=cases)
        expected = [profile._age_days(taken, date.fromisoformat(now[:10])) for taken, now in cases]
        self.assertEqual(got, expected)
        self.assertEqual(expected[:2], [31, 30])
        self.assertEqual(expected[5:], [None, None, None, None])

    def test_one_check_is_withheld_as_apply_withholding_does(self):
        made = built()
        cases = [["check:SEC-001"], ["check:SEC-001", "check:REL-001", "worker.replicas"], ["check", "check:SEC-001"], ["check:NOPE-1"], []]
        for raw in page.PATHS:
            got = page.run_page_code(
                "input.cases.map((ids) => profileText(withholdEntries(parseExact(input.response).profile, new Set(ids), 'baseline', '')))",
                raw=raw, cases=cases, response=page.response_text(made),
            )  # fmt: skip
            for ids, text in zip(cases, got):
                known = [item for item in ids if item != "check:NOPE-1"]  # the page offers no box for a check that is not there
                with self.subTest(ids=ids, rawJSON=raw):
                    self.assertEqual(json.loads(text), profile.apply_withholding(made, known))
                    if raw:
                        self.assertEqual(text, profile.to_json(profile.apply_withholding(made, known)))
        # A profile without checks is left as it is.
        older = copy.deepcopy(made)
        del older["check"]["checks"]
        got = page.run_page_code(
            "profileText(withholdEntries(parseExact(input.response).profile, new Set(['check:SEC-001']), 'baseline', ''))", response=page.response_text(older)
        )
        self.assertEqual(got, profile.to_json(older))

    def test_the_page_opens_exactly_the_files_validate_accepts(self):
        entry = {"rule_id": "X-1", "area": "security", "outcome": "passed", "text": "A sentence."}
        values = [None, {}, "x", 1, True, [], [None], [[]], ["x"], [entry], [{**entry, "more": {"a": [1]}}], [{**entry, "outcome": "later"}]]
        values += [[{key: value for key, value in entry.items() if key != missing}] for missing in entry]
        values += [[{**entry, key: wrong}] for key in entry for wrong in (None, 1, True, [], {})]
        files, expected = [], []
        for checks in values:
            made = built()
            made["check"]["checks"] = checks
            files.append(json.dumps(made))
            try:
                profile.validate(made)
                expected.append(None)
            except profile.ProfileError:
                expected.append("This file is not a valid environment profile: “check.checks”. Nothing of it is shown.")
        withheld = profile.apply_withholding(built(), ["check"])
        withheld["check"]["checks"] = "ignored, as the rest of a withheld check is"
        files.append(json.dumps(withheld))
        expected.append(None)
        profile.validate(withheld)
        got = page.run_page_code("input.files.map((text) => profileProblem(parseExact(text)))", files=files)
        self.assertEqual(got, expected)
        self.assertEqual(expected.count(None), 5)

    def test_the_entries_are_read_as_check_entries_reads_them(self):
        for label, file in sheet_files():
            got = page.run_page_code("checkEntries(parseExact(input.file))", file=json.dumps(file))
            expected = profile.check_entries(file)
            if expected is not None:
                expected = [
                    {**{k: v for k, v in entry.items() if k not in ("by_default", "rests_on")}, "byDefault": entry["by_default"], "restsOn": entry["rests_on"]}
                    for entry in expected
                ]
            self.assertEqual(got, expected, label)
            rested = page.run_page_code("Object.fromEntries(restedOnWithheld(parseExact(input.file)))", file=json.dumps(file))
            self.assertEqual(rested, profile.rested_on_withheld(file), label)

    def test_the_fixed_text_of_the_page_is_the_one_profile_py_prints(self):
        for name, expected in (("OUTCOME_LABELS", profile.OUTCOME_LABELS), ("REASON_LABELS", profile.REASON_LABELS), ("NEED_LABELS", profile.NEED_LABELS)):
            self.assertEqual(page.run_page_code(name), expected)
        self.assertEqual(page.run_page_code("[BY_DEFAULT, NOT_KNOWN, SHEET_STALE_DAYS, BRIEF, ESTIMATED, SEVERITY_ORDER]"),
                         [profile.BY_DEFAULT, profile.NOT_KNOWN, profile.SHEET_STALE_DAYS, [list(pair) for pair in profile.BRIEF], list(profile.ESTIMATED), list(SEVERITIES)])  # fmt: skip
        # The page holds no sentence of a rule: every one comes from the file.
        for literal in LITERALS:
            self.assertNotIn(literal, page.APP_JS + page.INDEX_HTML)


@unittest.skipUnless(page.NODE, page.NO_NODE)
class PageBehaviourTest(unittest.TestCase):
    def test_the_copied_call_sheet_is_the_one_show_sheet_prints(self):
        files = [(label, file) for label, file in sheet_files() if "date" not in label]
        scenario = """async (page, input) => {
          const seen = [];
          for (const text of input.files) {
            const opened = await page.open(text);
            const status = page.walk(page.$("profile-body")).filter((node) => node.getAttribute("role") === "status")[0];
            page.$("call-copy").click();
            await page.settle();
            const box = page.$("call-text");
            seen.push({ view: opened.view, shown: opened.shown, text: page.copied[page.copied.length - 1], status: status.textContent,
                        boxShown: !box.hidden, box: box.value, headings: page.walk(page.$("profile-body")).filter((node) => ["h3", "h4"].includes(node.tagName)).map((node) => node.textContent),
                        ...Object.fromEntries(["h3", "h4", "h5"].map((tag) => [tag, page.walk(page.$("profile-body")).filter((node) => node.tagName === tag).map((node) => node.textContent)])) });
            page.$("profile-close").click();
          }
          return seen;
        }"""
        for raw in page.PATHS:
            got = page.run_page(scenario, raw=raw, files=[json.dumps(file, ensure_ascii=False) for _, file in files])
            for (label, file), seen in zip(files, got):
                with self.subTest(rawJSON=raw, file=label):
                    file = profile.validate(json.loads(json.dumps(file)))
                    self.assertEqual(seen["view"], "profile")
                    self.assertEqual(seen["text"], profile.render_sheet(file))
                    self.assertEqual(seen["status"], "Call sheet copied to the clipboard as Markdown.")
                    self.assertFalse(seen["boxShown"])
                    # The page opens on the sheet, before the profile itself.
                    self.assertEqual(seen["headings"][:2], ["Call sheet", "The environment in brief"])
                    self.assertLess(seen["headings"].index("Call sheet"), seen["headings"].index("Run details"))
                    # "Checks (n)" is one level under "Check outcome", an h3 here: no level is skipped.
                    self.assertEqual("Check outcome" in seen["h3"] and file["check"].get("checks") is not None, any(title.startswith("Checks (") for title in seen["h4"]))
                    self.assertEqual([title for title in seen["h5"] if title.startswith("Checks (")], [])
                    listed = profile.check_entries(file)
                    for entry in listed or []:
                        if entry["known"] and entry["outcome"] != "withheld":
                            self.assertIn(entry["text"], seen["shown"])
        # The browser refuses the clipboard: the same text is offered in a box.
        got = page.run_page(scenario, files=[json.dumps(files[0][1])], no_clipboard=True)[0]
        self.assertEqual((got.get("text"), got["boxShown"]), (None, True))
        self.assertEqual(got["box"], profile.render_sheet(files[0][1]))
        self.assertIn("did not allow copying", got["status"])
        # Without the questions of the server the sheet is still shown; only the copy is not offered.
        got = page.run_page("async (page, input) => (await page.open(input.file))", file=json.dumps(files[0][1]), no_questions=True)
        self.assertIn("What works well (", got["shown"])
        self.assertIn("so the sheet cannot be copied", got["shown"])

    def test_what_the_sheet_shows_on_the_page(self):
        files = dict(sheet_files())
        scenario = "async (page, input) => { const shown = {}; for (const [label, text] of Object.entries(input.files)) { shown[label] = (await page.open(text)).shown; page.$('profile-close').click(); } return shown; }"
        wanted = ("a run with findings", "the check outcome withheld", "written by 0.5.0, 84 days old", "one check and some facts withheld", "only passed checks", "usage numbers, an estimate")
        got = page.run_page(scenario, files={label: json.dumps(files[label]) for label in wanted})
        shown = got["a run with findings"]
        made = files["a run with findings"]
        count = lambda outcome: sum(entry["outcome"] == outcome for entry in made["check"]["checks"])
        for heading in (f"What works well ({count('passed')})", f"What does not ({count('finding')})", f"What to improve ({count('finding')})", f"Not assessed ({count('not_assessed')})", f"Does not apply ({count('not_applicable')})", f"Checks ({len(made['check']['checks'])})"):
            self.assertIn(heading, shown)
        info = outcomes.RULES["SEC-001"].info.variants[0]
        self.assertIn(f"Impact: {info.impact}", shown)
        self.assertIn(info.remedy, shown)
        self.assertIn("Rests on: setting:instance:N8N_SECURE_COOKIE = false", shown)
        self.assertIn("By n8n's default, not set explicitly.", shown)
        self.assertIn("With the debug information", shown)
        self.assertIn("The operator withheld the check outcome", got["the check outcome withheld"])
        self.assertNotIn("What works well", got["the check outcome withheld"])
        self.assertIn("This profile was taken", got["written by 0.5.0, 84 days old"])
        self.assertIn("does not record what passed", got["written by 0.5.0, 84 days old"])
        self.assertNotIn("Checks (", got["written by 0.5.0, 84 days old"])
        one = got["one check and some facts withheld"]
        self.assertIn("Checks withheld by the operator: 1.", one)
        self.assertIn("Checks in the file that rest on this entry: SCAL-002", one)
        self.assertNotIn("Session cookie", one)
        self.assertIn("No finding. Every source was collected and every rule ran.", got["only passed checks"])
        self.assertIn("about 1200.5", got["usage numbers, an estimate"])

    def test_the_share_step_shows_every_check_and_withholds_one(self):
        expected_base = json.loads(run_response())["profile"]
        got = page.run_page(
            """async (page, input) => {
              await page.run(input.response);
              const before = { shown: page.$("share-body").textContent, boxes: Object.keys(page.boxes()), summary: page.$("share-summary").textContent };
              page.toggle(page.boxes()["check:SEC-001"]);
              page.toggle(page.boxes()["worker.replicas"]);
              const one = { shown: page.$("share-body").textContent, summary: page.$("share-summary").textContent, file: await page.download(),
                            labels: page.walk(page.$("share-body")).filter((node) => node.tagName === "label").map((node) => node.textContent) };
              page.toggle(page.boxes()["check:SCAL-002"]);
              const two = { shown: page.$("share-body").textContent, file: await page.download() };
              page.toggle(page.boxes()["check"]);
              const whole = { boxes: Object.keys(page.boxes()), file: await page.download() };
              page.toggle(page.boxes()["check"]);
              const back = { boxes: Object.keys(page.boxes()), file: await page.download() };
              return { before, one, two, whole, back };
            }""",
            response=run_response(),
        )
        checks = expected_base["check"]["checks"]
        self.assertEqual(len(checks), 24)
        # Every entry is listed with every text it holds, before anything is saved.
        for entry in checks:
            for key in ("rule_id", "text", "impact", "remedy"):
                if key in entry:
                    self.assertIn(entry[key], got["before"]["shown"], (entry["rule_id"], key))
            for item in entry["rests_on"]:
                self.assertIn(item, got["before"]["shown"])
        self.assertLessEqual({f"check:{entry['rule_id']}" for entry in checks} | {"check"}, set(got["before"]["boxes"]))
        self.assertNotIn("withheld)", got["before"]["summary"])

        with_times = lambda made: {**made, "created_at": expected_base["created_at"]}
        expected = profile.apply_withholding(expected_base, ["check:SEC-001", "worker.replicas"])
        self.assertEqual(json.loads(got["one"]["file"]), with_times(expected))
        self.assertIn("(1 of 24 checks withheld)", got["one"]["summary"])
        self.assertIn("Withhold the check SEC-001", got["one"]["labels"])
        self.assertNotIn(outcomes.RULES["SEC-001"].info.variants[0].remedy, got["one"]["shown"])
        # The withheld fact names the check that still rests on it; nothing was withheld for the operator.
        self.assertIn("Still in the file, resting on this entry: SCAL-002.", got["one"]["shown"])
        self.assertEqual(json.loads(got["two"]["file"]), with_times(profile.apply_withholding(expected, ["check:SCAL-002"])))
        self.assertNotIn("resting on this entry", got["two"]["shown"])
        # The whole outcome withheld: no check is left to tick, and unticking it brings the choices back.
        self.assertEqual(json.loads(got["whole"]["file"])["check"], {"withheld": True})
        self.assertFalse([box for box in got["whole"]["boxes"] if box.startswith("check:")])
        self.assertEqual(json.loads(got["back"]["file"]), json.loads(got["two"]["file"]))


if __name__ == "__main__":
    unittest.main()
