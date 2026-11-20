"""Debug information as a source: the parser, what never leaks, precedence, rules, profile, report, CLI.

Every text here is synthetic. The ids are made up, and each identifier and free text
carries MARK so that a leak anywhere is found by one search.
"""

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import textwrap
import unittest
from datetime import date
from unittest import mock

import validate_0_2_0
from n8n_healthcheck import cli, debug_info, engine, profile, report
from n8n_healthcheck.models import Snapshot
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.rules import maintainability as maint
from n8n_healthcheck.rules import performance as perf
from n8n_healthcheck.rules import reliability as rel
from n8n_healthcheck.rules import scalability as scal
from n8n_healthcheck.rules import security as sec
from n8n_healthcheck.rules._helpers import stated, with_debug_info

MARK = "zzleak"
TODAY = date(2026, 11, 20)

CORE = f"""## core

- n8nVersion: 3.3.0
- platform: docker (self-hosted)
- nodeJsVersion: 22.16.0
- nodeEnv: {MARK}-env
- database: postgres
- executionMode: scaling (single-main)
- concurrency: -1
- license: enterprise (production)
- consumerId: {MARK}-consumer-0000

"""
STORAGE = """## storage

- success: all
- error: all
- progress: false
- manual: true
- binaryMode: filesystem

"""
PRUNING = """## pruning

- enabled: true
- maxAge: 336 hours
- maxCount: 10000 executions

"""
CLIENT = f"""## client

- userAgent: {MARK}-agent/5.0 (x11; linux: x86_64) like {MARK}
- isTouchDevice: false

"""
SECURITY = """## security

- blockFileAccessToN8nFiles: false
- secureCookie: false

"""
CLUSTER = f"""## cluster

- instanceCount: 3
- versions: 3.3.0
- instances:
  - instanceKey: 00000000-0000-4000-8000-{MARK}0001, hostId: main-{MARK}aaaa, instanceType: main, instanceRole: leader, version: 3.3.0
  - instanceKey: 00000000-0000-4000-8000-{MARK}0002, hostId: worker-{MARK}bbbb, instanceType: worker, instanceRole: unset, version: 3.3.0
  - instanceKey: 00000000-0000-4000-8000-{MARK}0003, hostId: worker-{MARK}cccc, instanceType: worker, instanceRole: unset, version: 3.3.0
- checks:
  - check: hostid-clash, status: succeeded, warnings: {MARK}-warning-code
  - check: lifecycle, status: succeeded, warnings: -
  - check: split-brain, status: succeeded, warnings: -
  - check: version-mismatch, status: succeeded, warnings: -

"""
STAMP = "Generated at: 2026-11-19T16:50:44.057Z"
# The order n8n writes the sections in.
FULL = "# Debug info\n\n" + CORE + STORAGE + PRUNING + CLIENT + SECURITY + CLUSTER + STAMP
NO_CLUSTER = "# Debug info\n\n" + CORE + STORAGE + PRUNING + CLIENT + STAMP

PARSED = {
    "generated_on": "2026-11-19",
    "n8n_version": "3.3.0",
    "node_version": "22.16.0",
    "platform": "docker_self_hosted",
    "database": "postgresdb",
    "execution_mode": "queue",
    "multi_main": False,
    "concurrency_limit": "unlimited",
    "license": "enterprise_production",
    "save_success": "all",
    "save_error": "all",
    "save_progress": False,
    "save_manual": True,
    "binary_mode": "filesystem",
    "pruning": {"enabled": True, "max_age_hours": 336, "max_count": 10000},
    "insecure": {"secure_cookie": True, "file_access_blocked": True},
    "cluster": {
        "instances": {"main": 1, "worker": 2, "webhook": 0, "engine": 0},
        "versions": ["3.3.0"],
        "checks_failed": [],
    },
}


def parse(text):
    return debug_info.parse(text, today=TODAY)


def text(*changes, base=FULL):
    """`base` with (old, new) replacements applied; each must match exactly once."""
    for old, new in changes:
        assert base.count(old) == 1, old
        base = base.replace(old, new)
    return base


def snapshot(*changes, base=FULL, **sections):
    return Snapshot(debug_info=parse(text(*changes, base=base)), **sections)


REGULAR = ("executionMode: scaling (single-main)", "executionMode: regular")
MULTI_MAIN = ("executionMode: scaling (single-main)", "executionMode: scaling (multi-main)")
SQLITE = ("database: postgres", "database: sqlite")
PRUNING_OFF = (PRUNING, "## pruning\n\n- enabled: false\n\n")
SECURE = (SECURITY, "")
ONE_WORKER = (f"  - instanceKey: 00000000-0000-4000-8000-{MARK}0003, hostId: worker-{MARK}cccc, instanceType: worker, instanceRole: unset, version: 3.3.0\n", "")
TWO_VERSIONS = (f"hostId: worker-{MARK}bbbb, instanceType: worker, instanceRole: unset, version: 3.3.0", f"hostId: worker-{MARK}bbbb, instanceType: worker, instanceRole: unset, version: 3.2.1")
FAILED_CHECK = ("check: split-brain, status: succeeded, warnings: -", f"check: split-brain, status: failed, warnings: {MARK}-one; {MARK}-two")
SANDBOX = ("license: enterprise (production)", "license: enterprise (sandbox)")


def container(env=None, command=None, image="n8nio/n8n:2.40.1"):
    return {
        "name": "n8n", "image": image, "command": command or [], "env": env or {},
        "requests": {}, "limits": {}, "liveness_probe": True, "readiness_probe": True, "env_complete": True,
    }  # fmt: skip


def kube(*deployments):
    return {"deployments": [{"name": n, "replicas": r, "ready_replicas": r, "containers": [c]} for n, r, c in deployments]}


def facts_of(snap, **kwargs):
    return {fact["id"]: fact for fact in profile.build_profile(snap, [], **kwargs)["facts"]}


class ParserTest(unittest.TestCase):
    def test_the_full_text(self):
        self.assertEqual(parse(FULL), PARSED)

    def test_without_a_cluster_section(self):
        self.assertEqual(parse(NO_CLUSTER), {**PARSED, "cluster": None, "insecure": {"secure_cookie": False, "file_access_blocked": False}})

    def test_security_is_true_only_for_what_the_text_flags(self):
        self.assertEqual(parse(text(SECURE))["insecure"], {"secure_cookie": False, "file_access_blocked": False})
        one = parse(text(("- blockFileAccessToN8nFiles: false\n", "")))
        self.assertEqual(one["insecure"], {"secure_cookie": True, "file_access_blocked": False})
        # `true` is never written by n8n here; were it there, it flags nothing.
        other = parse(text(("- secureCookie: false", "- secureCookie: true")))
        self.assertEqual(other["insecure"], {"secure_cookie": False, "file_access_blocked": True})

    def test_execution_modes(self):
        regular = parse(text(REGULAR))
        self.assertEqual((regular["execution_mode"], regular["multi_main"]), ("regular", False))
        multi = parse(text(MULTI_MAIN))
        self.assertEqual((multi["execution_mode"], multi["multi_main"]), ("queue", True))

    def test_pruning_off(self):
        self.assertEqual(parse(text(PRUNING_OFF))["pruning"], {"enabled": False, "max_age_hours": None, "max_count": None})

    def test_known_values_are_stored_as_the_tools_own_words(self):
        for old, new, key, expected in (
            ("platform: docker (self-hosted)", "platform: docker (cloud)", "platform", "docker_cloud"),
            ("platform: docker (self-hosted)", "platform: npm", "platform", "npm"),
            ("database: postgres", "database: mysqldb", "database", "mysqldb"),
            ("database: postgres", "database: mariadb", "database", "mariadb"),
            ("concurrency: -1", "concurrency: 20", "concurrency_limit", 20),
            ("license: enterprise (production)", "license: community", "license", "community"),
            ("binaryMode: filesystem", "binaryMode: memory", "binary_mode", "default"),
            ("binaryMode: filesystem", "binaryMode: s3", "binary_mode", "s3"),
            ("success: all", "success: none", "save_success", "none"),
            ("nodeJsVersion: 22.16.0", "nodeJsVersion: v22.16.0", "node_version", "22.16.0"),
            ("n8nVersion: 3.3.0", "n8nVersion: 3.3.0-beta.1", "n8n_version", "3.3.0"),
        ):
            with self.subTest(new=new):
                self.assertEqual(parse(text((old, new)))[key], expected)

    def test_the_bug_report_variant_has_one_more_hash_on_every_heading(self):
        variant = "\n".join("#" + line if line.startswith("#") else line for line in FULL.splitlines())
        self.assertIn("### core", variant)
        self.assertEqual(parse(variant), PARSED)

    def test_a_text_that_went_through_a_ticket_or_a_chat_still_reads(self):
        lines = FULL.splitlines()
        mangled = {
            "windows line ends": "\r\n".join(lines) + "\r\n",
            "old mac line ends": "\r".join(lines),
            "blank lines removed": "\n".join(line for line in lines if line.strip()),
            "blank lines added": "\n\n\n".join(lines),
            "headings lost their #": "\n".join(line.lstrip("# ") for line in lines),
            "headings gained a #": "\n".join("##" + line if line.startswith("#") else line for line in lines),
            "headings in bold": "\n".join(f"**{line.lstrip('# ')}**" if line.startswith("#") else line for line in lines),
            "bullets as *": "\n".join(line.replace("- ", "* ", 1) if line.lstrip().startswith("- ") else line for line in lines),
            "bullets as •": "\n".join(line.replace("- ", "• ", 1) if line.lstrip().startswith("- ") else line for line in lines),
            "bullets lost": "\n".join(line.replace("- ", "", 1) if line.lstrip().startswith("- ") else line for line in lines),
            "indentation lost": "\n".join(line.strip() for line in lines),
            "leading spaces": "\n".join("    " + line for line in lines),
            "tabs": "\n".join("\t" + line for line in lines),
            "quoted": "\n".join("> " + line for line in lines),
            "quoted twice": "\n".join(">> " + line for line in lines),
            "quoted, no space": "\n".join(">" + line for line in lines),
            "code fence": "```\n" + FULL + "\n```",
            "code fence with a language": "```markdown\n" + FULL + "\n```\n",
            "tilde fence": "~~~\n" + FULL + "\n~~~",
            "non-breaking spaces": FULL.replace(" ", " "),
            "byte order mark": "﻿" + FULL,
            "text around it": "Hi, here it is:\n\n" + FULL + "\n\nThanks!",
            "everything at once": "> ```\r\n" + "\r\n".join(">   " + line.strip().lstrip("# ").replace("- ", "• ", 1) for line in lines if line.strip()) + "\r\n> ```",
        }
        for name, mangled_text in mangled.items():
            with self.subTest(mangling=name):
                self.assertEqual(parse(mangled_text), PARSED)

    def test_unknown_keys_and_sections_are_ignored(self):
        with_more = text(
            ("- consumerId:", f"- futureKey: {MARK}\n- executionMode: regular\n- consumerId:"),  # the first of a key wins
            ("## storage", f"## future{MARK}\n\n- enabled: false\n- n8nVersion: 9.9.9\n- progress: true\n\n## storage"),
            ("- instanceCount: 3", f"- instanceCount: 3\n- leader: {MARK}"),
        )
        self.assertEqual(parse(with_more), PARSED)

    def test_an_unknown_section_ends_the_one_before_it_even_without_its_hash(self):
        # Its items must not be read as pruning's.
        cut = text(("- maxAge: 336 hours\n", f"future{MARK}\n- maxAge: 5 hours\n"))
        self.assertEqual(parse(cut)["pruning"], {"enabled": True, "max_age_hours": None, "max_count": None})

    def test_a_value_that_does_not_fit_is_dropped_and_only_that_one(self):
        for old, new, key in (
            ("platform: docker (self-hosted)", f"platform: {MARK}", "platform"),
            ("nodeJsVersion: 22.16.0", f"nodeJsVersion: {MARK}", "node_version"),
            ("nodeJsVersion: 22.16.0", "nodeJsVersion: 22.16", "node_version"),
            ("database: postgres", "database: postgres://db.example", "database"),
            ("concurrency: -1", "concurrency: 0", "concurrency_limit"),
            ("concurrency: -1", "concurrency: -2", "concurrency_limit"),
            ("concurrency: -1", "concurrency: 1_000", "concurrency_limit"),
            ("concurrency: -1", "concurrency: 2.5", "concurrency_limit"),
            ("license: enterprise (production)", f"license: {MARK}", "license"),
            ("success: all", "success: sometimes", "save_success"),
            ("progress: false", "progress: no", "save_progress"),
            ("binaryMode: filesystem", "binaryMode: /var/lib/n8n", "binary_mode"),
        ):
            with self.subTest(new=new):
                got = parse(text((old, new)))
                self.assertIsNone(got[key])
                self.assertEqual({**got, key: PARSED[key]}, PARSED)
        self.assertIsNone(parse(text(("enabled: true", "enabled: maybe")))["pruning"])
        self.assertIsNone(parse(text(("maxAge: 336 hours", "maxAge: 336")))["pruning"]["max_age_hours"])
        self.assertIsNone(parse(text(("maxCount: 10000 executions", "maxCount: many executions")))["pruning"]["max_count"])
        mode = parse(text(("executionMode: scaling (single-main)", "executionMode: scaling (two-main)")))
        self.assertEqual((mode["execution_mode"], mode["multi_main"]), (None, None))

    def test_cluster_instances_are_counted_by_type_and_nothing_else_is_kept(self):
        more = text(
            (
                "- checks:",
                f"  - instanceKey: {MARK}, hostId: {MARK}, instanceType: webhook, instanceRole: unset, version: 3.3.0\n"
                f"  - instanceKey: {MARK}, hostId: {MARK}, instanceType: engine, instanceRole: unset, version: 3.2.1\n"
                f"  - instanceKey: {MARK}, hostId: {MARK}, instanceType: {MARK}, instanceRole: unset, version: {MARK}\n"
                "- checks:",
            ),
            ("check: lifecycle, status: succeeded", "check: lifecycle, status: failed"),
            ("check: version-mismatch, status: succeeded", f"check: {MARK}-new-check, status: failed"),
            ("check: hostid-clash, status: succeeded", f"check: hostid-clash, status: {MARK}"),
        )
        self.assertEqual(
            parse(more)["cluster"],
            {
                "instances": {"main": 1, "worker": 2, "webhook": 1, "engine": 1},
                "versions": ["3.2.1", "3.3.0"],
                "checks_failed": ["lifecycle", "other"],
            },
        )
        # A section with no check listed cannot say that none failed.
        self.assertIsNone(parse(text((CLUSTER, "## cluster\n\n- instanceCount: 0\n\n")))["cluster"]["checks_failed"])
        self.assertEqual(parse(text((CLUSTER, "## cluster\n\n- checks:\n\n")))["cluster"]["checks_failed"], [])
        # Versions are sorted as versions, not as text.
        self.assertEqual(parse(text(("- versions: 3.3.0", "- versions: 3.10.0, 3.9.1, 3.3.0")))["cluster"]["versions"], ["3.3.0", "3.9.1", "3.10.0"])

    def test_generated_at(self):
        for stamp, expected in (
            (STAMP, "2026-11-19"),
            ("Generated at: 2026-11-20T00:00:01.000Z", "2026-11-20"),  # the day of the run
            ("generated at: 2026-01-02", "2026-01-02"),
            ("", None),
            (f"Generated at: {MARK}", None),
            ("Generated at: 2026-13-45T00:00:00.000Z", None),
            ("Generated at: 2026-11-21T00:00:00.000Z", None),  # in the future
            ("Generated at: 20261119", None),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(parse(text((STAMP, stamp)))["generated_on"], expected)
        self.assertEqual(debug_info.label({"generated_on": "2026-11-19"}), "debug information generated on 2026-11-19")
        self.assertEqual(debug_info.label({"generated_on": None}), "debug information, date unknown")

    def test_what_is_not_a_debug_information_is_refused_without_echoing_it(self):
        refused = {
            "empty": "",
            "prose": f"hello {MARK}, the instance is slow",
            "an env file": f"EXECUTIONS_MODE=queue\nN8N_HOST={MARK}.example\n",
            "no core section": text((CORE, "")),
            "no version": text(("- n8nVersion: 3.3.0\n", "")),
            "an unreadable version": text(("n8nVersion: 3.3.0", f"n8nVersion: {MARK}")),
            "the version in another section": text(("- n8nVersion: 3.3.0\n", ""), ("- success: all", "- n8nVersion: 3.3.0\n- success: all")),
            "core without its heading": FULL.replace("## core\n", ""),
            "json": json.dumps({"core": {"n8nVersion": "3.3.0", "note": MARK}}),
            "not text": None,
        }
        for name, refused_text in refused.items():
            with self.subTest(text=name):
                with self.assertRaises(debug_info.DebugInfoError) as caught:
                    parse(refused_text)
                self.assertEqual(str(caught.exception), debug_info.NOT_DEBUG_INFO)
                self.assertNotIn(MARK, str(caught.exception))

    def test_size_limit(self):
        padding = f"- userAgent: {MARK}" + "x" * debug_info.MAX_BYTES
        with self.assertRaises(debug_info.DebugInfoError) as caught:
            parse(FULL + "\n" + padding)
        self.assertEqual(str(caught.exception), debug_info.TOO_LARGE)
        self.assertIn("64 KB", debug_info.TOO_LARGE)
        fits = FULL + "\n" + "x" * (debug_info.MAX_BYTES - len(FULL.encode()) - 1)
        self.assertEqual(len(fits.encode()), debug_info.MAX_BYTES)
        self.assertEqual(parse(fits), PARSED)
        # The limit is in bytes, not characters.
        with self.assertRaises(debug_info.DebugInfoError):
            parse(FULL + "\n" + "é" * (debug_info.MAX_BYTES // 2))

    def test_nothing_but_known_values_numbers_and_versions_survives(self):
        self.assertNotIn(MARK, json.dumps(parse(FULL)))
        self.assertNotIn(MARK, json.dumps(parse(text(FAILED_CHECK, TWO_VERSIONS))))
        self.assertNotIn("0000", json.dumps(parse(FULL)["cluster"]))  # no part of an id


# Everything a text can carry that must never come out: each holds MARK.
LEAKY = text(
    FAILED_CHECK,
    ("check: lifecycle, status: succeeded", f"check: {MARK}-check, status: failed"),
    ("- consumerId:", f"- {MARK}Key: {MARK}-value\n- consumerId:"),
    ("## storage", f"## {MARK}-section\n\n- token: {MARK}-token\n\n## storage"),
    ("n8nVersion: 3.3.0", f"n8nVersion: 3.3.0-{MARK}"),
    ("instanceRole: leader", f"instanceRole: {MARK}"),
    (STAMP, f"{STAMP}\n{MARK} said: thanks"),
)


class CliRun(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, content):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(content)
        return self.path(name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def run_cli(self, argv, stdin=None):
        """(exit code, stdout, stderr) of main() with the real collectors and rules."""
        out, err = io.StringIO(), io.StringIO()
        patches = [contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)]
        if stdin is not None:
            patches.append(mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin))))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                code = cli.main(argv)
            except SystemExit as exit_:  # argparse
                code = exit_.code
        return code, out.getvalue(), err.getvalue()


class ZeroLeakTest(CliRun):
    def test_no_marker_in_any_output_of_a_run(self):
        self.assertGreaterEqual(LEAKY.count(MARK), 20)
        argv = ["--debug-info", self.write("debug.txt", LEAKY), "--save-snapshot", self.path("snapshot.json")]
        argv += ["--save-profile", self.path("profile.json"), "-o", self.path("report.md")]
        code, out, err = self.run_cli(argv)
        self.assertEqual((code, out, err), (1, "", ""))
        outputs = {name: self.read(name) for name in ("snapshot.json", "profile.json", "report.md")}
        outputs["show-profile"] = self.run_cli(["--show-profile", self.path("profile.json")])[1]
        outputs["show-questions"] = self.run_cli(["--show-questions", self.path("profile.json")])[1]
        outputs["report from the snapshot"] = self.run_cli(["--snapshot", self.path("snapshot.json")])[1]
        for name, output in outputs.items():
            with self.subTest(output=name):
                self.assertTrue(output.strip())
                self.assertNotIn(MARK, output)
        # The run did read the text: both failed checks are there, the unknown one unnamed.
        self.assertIn("2 failed cluster checks: split-brain, 1 other check.", outputs["report.md"])
        self.assertEqual(json.loads(outputs["snapshot.json"])["debug_info"]["cluster"]["checks_failed"], ["other", "split-brain"])

    def test_no_marker_in_what_the_library_returns(self):
        snap = Snapshot(debug_info=parse(LEAKY))
        findings = engine.run(snap, ALL_RULES)
        built = profile.build_profile(snap, findings)
        data = report.build_report(snap, findings, today=TODAY)
        for output in (repr(snap), repr(findings), profile.to_json(built), repr(data), report.render_markdown(snap, findings, today=TODAY)):
            self.assertNotIn(MARK, output)

    def test_no_marker_in_an_error_message(self):
        refused = {
            "not a debug information": LEAKY.replace("## core", f"## {MARK}"),
            "too large": LEAKY + "\n" + MARK * debug_info.MAX_BYTES,
        }
        for name, content in refused.items():
            with self.subTest(text=name):
                code, out, err = self.run_cli(["--debug-info", self.write("refused.txt", content)])
                self.assertEqual((code, out), (3, ""))
                self.assertTrue(err.startswith("error: --debug-info: "))
                self.assertNotIn(MARK, err)
                code, out, err = self.run_cli(["--debug-info", "-"], stdin=content.encode())
                self.assertEqual((code, out), (3, ""))
                self.assertNotIn(MARK, err)


class PrecedenceTest(unittest.TestCase):
    def test_a_probe_that_states_the_value_wins_for_the_fact_and_for_the_rule(self):
        # The env file says regular and pruning on; the text says queue (on SQLite) and pruning off.
        env = {"EXECUTIONS_MODE": "regular", "EXECUTIONS_DATA_PRUNE": "true", "DB_TYPE": "postgresdb"}
        snap = snapshot(SQLITE, PRUNING_OFF, env=env)
        facts = facts_of(snap)
        for fact_id, value in (("executions.mode", "regular"), ("pruning.enabled", True), ("database.engine", "postgresdb")):
            self.assertEqual((facts[fact_id]["value"], facts[fact_id]["source"]), (value, "env"), fact_id)
        for rule in (rel.sqlite_in_queue_mode, rel.single_main, perf.pruning_disabled):
            self.assertIsNone(rule(snap), rule.__name__)

    def test_a_variable_that_is_not_set_gives_way_and_the_rule_and_the_fact_agree(self):
        # Without the text the tool would assume regular mode on SQLite with pruning unknown.
        without = Snapshot(env={})
        self.assertEqual(facts_of(without)["executions.mode"]["value"], "regular")
        self.assertIsNone(rel.sqlite_in_queue_mode(without))
        snap = snapshot(SQLITE, PRUNING_OFF, env={})
        facts = facts_of(snap)
        for fact_id, value in (("executions.mode", "queue"), ("database.engine", "sqlite"), ("pruning.enabled", False), ("multi_main.enabled", False)):
            self.assertEqual((facts[fact_id]["value"], facts[fact_id]["source"]), (value, "debug_info"), fact_id)
            self.assertEqual(facts[fact_id]["detail"], "debug information generated on 2026-11-19")
        for rule, names in (
            (rel.sqlite_in_queue_mode, "EXECUTIONS_MODE, DB_TYPE"),
            (rel.single_main, "EXECUTIONS_MODE, N8N_MULTI_MAIN_SETUP_ENABLED"),
            (perf.pruning_disabled, "EXECUTIONS_DATA_PRUNE"),
        ):
            finding = rule(snap)
            self.assertIsNotNone(finding, rule.__name__)
            self.assertIn(f" From the debug information generated on 2026-11-19: {names}.", finding.evidence)
        self.assertIn("EXECUTIONS_MODE=queue; DB_TYPE=sqlite (default: sqlite).", rel.sqlite_in_queue_mode(snap).evidence)

    def test_value_by_value_not_source_by_source(self):
        # The env file states the mode only: the database still comes from the text.
        snap = snapshot(SQLITE, env={"EXECUTIONS_MODE": "queue"})
        facts = facts_of(snap)
        self.assertEqual(facts["executions.mode"]["source"], "env")
        self.assertEqual(facts["database.engine"]["source"], "debug_info")
        self.assertIn("From the debug information generated on 2026-11-19: DB_TYPE.", rel.sqlite_in_queue_mode(snap).evidence)

    def test_a_finding_resting_on_two_sources_names_both(self):
        main = container(env={"EXECUTIONS_MODE": "queue"})
        snap = snapshot(SQLITE, kubernetes=kube(("n8n-main", 1, main)))
        evidence = rel.sqlite_in_queue_mode(snap).evidence
        self.assertIn("Environment read from Deployment n8n-main, container n8n.", evidence)
        self.assertIn("From the debug information generated on 2026-11-19: DB_TYPE.", evidence)
        facts = facts_of(snap)
        self.assertEqual((facts["executions.mode"]["source"], facts["database.engine"]["source"]), ("kubernetes", "debug_info"))

    def test_set_but_not_readable_is_stated(self):
        cases = {
            "a Secret": Snapshot(kubernetes=kube(("n8n-main", 1, container(env={"EXECUTIONS_DATA_PRUNE": "<valueFrom>"})))),
            "redacted": Snapshot(env={"EXECUTIONS_DATA_PRUNE": "<redacted>"}),
            "through a file": Snapshot(env={"EXECUTIONS_DATA_PRUNE_FILE": "/run/secrets/prune"}),
            "two mains, one sets it": Snapshot(
                kubernetes=kube(("a", 1, container(env={"EXECUTIONS_DATA_PRUNE": "true"})), ("b", 1, container()))
            ),
        }
        for name, snap in cases.items():
            with self.subTest(case=name):
                snap.debug_info = parse(text(PRUNING_OFF))
                self.assertTrue(stated(snap, "EXECUTIONS_DATA_PRUNE"))
                self.assertEqual(facts_of(snap)["pruning.enabled"]["provenance"], "unknown")
                self.assertIsNone(perf.pruning_disabled(snap))

    def test_part_of_the_environment_not_readable_is_not_stated(self):
        main = {**container(), "env_complete": False}
        snap = snapshot(PRUNING_OFF, kubernetes=kube(("n8n-main", 1, main)))
        self.assertEqual(facts_of(snap)["pruning.enabled"]["source"], "debug_info")
        self.assertIsNotNone(perf.pruning_disabled(snap))

    def test_a_measured_version_and_measured_replicas_win(self):
        workers = container(command=["worker"])
        snap = snapshot(n8n_version="2.40.1", kubernetes=kube(("n8n-main", 1, container()), ("n8n-worker", 5, workers)))
        facts = facts_of(snap)
        self.assertEqual((facts["n8n.version"]["value"], facts["n8n.version"]["source"]), ("2.40.1", "metrics"))
        self.assertEqual((facts["worker.replicas"]["value"], facts["worker.replicas"]["source"]), (5, "kubernetes"))
        # Kubernetes shows no webhook container, and the text lists none either.
        self.assertEqual(facts["webhook.replicas"]["provenance"], "unknown")

    def test_with_debug_info_alone_a_missing_value_is_unknown_never_a_default(self):
        env = with_debug_info(snapshot(("- database: postgres\n", "")), "EXECUTIONS_MODE", "DB_TYPE")
        self.assertEqual(dict(env), {"EXECUTIONS_MODE": "queue"})
        self.assertFalse(env.complete)
        self.assertIsNone(rel.sqlite_in_queue_mode(snapshot(("- database: postgres\n", ""))))
        self.assertIsNone(with_debug_info(Snapshot(), "EXECUTIONS_MODE"))
        self.assertIsNone(with_debug_info(snapshot(), "N8N_RUNNERS_ENABLED"))  # nothing the text states


# (rule, id, a text from which it fires, texts from which it stays silent)
POSTGRES_REGULAR = (REGULAR,)
RULE_CASES = [
    (rel.sqlite_in_queue_mode, "REL-001", (SQLITE,), [(), (SQLITE, REGULAR), (SQLITE, ("- executionMode: scaling (single-main)\n", ""))]),
    (rel.single_main, "REL-006", (), [(MULTI_MAIN,), (REGULAR,), (("- executionMode: scaling (single-main)\n", ""),)]),
    (scal.regular_mode_unbounded_concurrency, "SCAL-001", POSTGRES_REGULAR, [
        (),  # queue mode
        (REGULAR, ("concurrency: -1", "concurrency: 20")),
        (REGULAR, SQLITE),  # the production gate
        (REGULAR, ("- concurrency: -1\n", "")),
        (REGULAR, ("- database: postgres\n", "")),
    ]),
    (scal.single_worker, "SCAL-002", (ONE_WORKER,), [(), (ONE_WORKER, REGULAR), (ONE_WORKER, ("- executionMode: scaling (single-main)\n", "")), ((CLUSTER, ""),)]),
    (perf.pruning_disabled, "PERF-001", (PRUNING_OFF,), [(), ((PRUNING, ""),)]),
    (perf.saving_execution_progress, "PERF-002", (REGULAR, ("progress: false", "progress: true")), [
        (REGULAR,),
        (("progress: false", "progress: true"),),  # queue mode: the workers are judged, not the main
        (("progress: false", "progress: true"), ("- executionMode: scaling (single-main)\n", "")),
    ]),
    (sec.insecure_cookie, "SEC-001", (), [(SECURE,), (("- secureCookie: false\n", ""),)]),
    (maint.version_skew, "MAINT-003", (TWO_VERSIONS,), [(), ((CLUSTER, ""),)]),
    (rel.cluster_check_failed, "REL-009", (FAILED_CHECK,), [(), ((CLUSTER, ""),)]),
    (sec.file_access_not_blocked, "SEC-003", (), [(SECURE,), (("- blockFileAccessToN8nFiles: false\n", ""),)]),
    (maint.sandbox_license, "MAINT-004", (SANDBOX,), [(), (("license: enterprise (production)", "license: community"),)]),
]


class RulesTest(unittest.TestCase):
    def test_each_rule_fires_from_the_text_alone_and_names_it(self):
        for rule, rule_id, changes, _ in RULE_CASES:
            with self.subTest(rule=rule_id):
                finding = rule(snapshot(*changes))
                self.assertIsNotNone(finding)
                self.assertEqual(finding.rule_id, rule_id)
                self.assertIn("debug information generated on 2026-11-19", finding.evidence)
                self.assertNotIn(MARK, finding.evidence)
                undated = rule(snapshot(*changes, (STAMP, "")))
                self.assertIn("debug information of unknown date", undated.evidence)

    def test_each_rule_stays_silent_when_the_text_does_not_state_its_condition(self):
        for rule, rule_id, _, silent in RULE_CASES:
            for changes in silent:
                with self.subTest(rule=rule_id, changes=[new for _, new in changes]):
                    self.assertIsNone(rule(snapshot(*changes)))
            with self.subTest(rule=rule_id, changes="no debug information"):
                self.assertIsNone(rule(Snapshot()))

    def test_the_new_rules(self):
        self.assertEqual(
            [(r.__name__, r(snapshot(FAILED_CHECK, SANDBOX)).severity) for r in (rel.cluster_check_failed, sec.file_access_not_blocked, maint.sandbox_license)],
            [("cluster_check_failed", "warning"), ("file_access_not_blocked", "warning"), ("sandbox_license", "info")],
        )
        self.assertEqual(rel.cluster_check_failed(snapshot(FAILED_CHECK)).title, "A cluster check failed")
        self.assertEqual(sec.file_access_not_blocked(snapshot()).title, "Access to n8n's own files is not blocked")
        self.assertEqual(maint.sandbox_license(snapshot(SANDBOX)).title, "Enterprise sandbox license")
        self.assertIn("only if this instance serves production", maint.sandbox_license(snapshot(SANDBOX)).impact)
        self.assertIn("1 failed cluster check: split-brain.", rel.cluster_check_failed(snapshot(FAILED_CHECK)).evidence)
        # A check that succeeded with warnings is not a failed check.
        self.assertIsNone(rel.cluster_check_failed(snapshot()))

    def test_sec_003_gives_way_to_a_probe_and_never_fires_without_the_text(self):
        name = "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES"
        self.assertIsNone(sec.file_access_not_blocked(snapshot(env={name: "true"})))
        self.assertIsNotNone(sec.file_access_not_blocked(snapshot(env={name: "false"})))
        self.assertIsNone(sec.file_access_not_blocked(Snapshot(env={name: "false"})))

    def test_perf_003_does_not_read_the_text(self):
        snap = snapshot(REGULAR, ("binaryMode: filesystem", "binaryMode: memory"))
        self.assertEqual(snap.debug_info["binary_mode"], "default")
        self.assertIsNone(perf.binary_data_in_memory(snap))

    def test_the_other_rules_are_unchanged(self):
        reading = {case[1] for case in RULE_CASES}
        for changes in ((), (REGULAR,), (SQLITE, PRUNING_OFF, ONE_WORKER, TWO_VERSIONS, FAILED_CHECK, SANDBOX)):
            fired = {finding.rule_id for finding in engine.run(snapshot(*changes), ALL_RULES)}
            self.assertLessEqual(fired, reading)
        # The same rule set with and without the text, when a probe states everything it could add.
        env = {"EXECUTIONS_MODE": "queue", "DB_TYPE": "postgresdb", "N8N_MULTI_MAIN_SETUP_ENABLED": "true",
               "EXECUTIONS_DATA_PRUNE": "true", "N8N_SECURE_COOKIE": "true", "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true",
               "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20", "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false"}  # fmt: skip
        workers = kube(("n8n-main", 1, container()), ("n8n-worker", 3, container(command=["worker"])))
        probes = Snapshot(env=env, kubernetes=workers)
        both = snapshot(SQLITE, PRUNING_OFF, ONE_WORKER, TWO_VERSIONS, env=env, kubernetes=workers)
        self.assertEqual(engine.run(both, ALL_RULES), engine.run(probes, ALL_RULES))

    def test_kubernetes_wins_for_workers_and_versions(self):
        two_workers = kube(("n8n-main", 1, container()), ("n8n-worker", 2, container(command=["worker"])))
        self.assertIsNone(scal.single_worker(snapshot(ONE_WORKER, kubernetes=two_workers)))
        self.assertIsNone(maint.version_skew(snapshot(TWO_VERSIONS, kubernetes=two_workers)))
        # Kubernetes was read and shows no worker: the instances n8n listed are used.
        only_main = kube(("n8n-main", 1, container(env={"EXECUTIONS_MODE": "queue"})))
        finding = scal.single_worker(snapshot(ONE_WORKER, kubernetes=only_main))
        self.assertIn("lists 1 worker instance; EXECUTIONS_MODE=queue. Environment read from Deployment n8n-main", finding.evidence)


NEW_FACTS = {
    "n8n.node_version": "22.16.0",
    "n8n.platform": "docker_self_hosted",
    "license.type": "enterprise_production",
    "main.concurrency_limit": "unlimited",
    "executions.save_success": "all",
    "executions.save_error": "all",
    "executions.save_progress": False,
    "executions.save_manual": True,
    "pruning.max_count": 10000,
    "cluster.versions_aligned": True,
    "cluster.checks_failed": 0,
    "security.secure_cookie": False,
    "security.file_access_blocked": False,
}
EXISTING_FACTS = {
    "n8n.version": "3.3.0",
    "executions.mode": "queue",
    "multi_main.enabled": False,
    "database.engine": "postgresdb",
    "pruning.enabled": True,
    "pruning.max_age_hours": 336,
    "binary_data.mode": "filesystem",
    "main.replicas": 1,
    "worker.replicas": 2,
}


class ProfileTest(unittest.TestCase):
    def test_the_thirteen_new_ids_come_after_the_existing_ones(self):
        self.assertEqual(list(profile.FACT_IDS[-13:]), list(NEW_FACTS))
        self.assertEqual(len(profile.FACT_IDS), 32 + 14 + 13)
        self.assertFalse(set(NEW_FACTS) & profile.FORMAT_1_FACT_IDS)
        self.assertEqual(len(profile.FORMAT_1_FACT_IDS), 32)

    def test_the_text_alone_fills_the_nine_existing_and_the_thirteen_new_facts(self):
        built = profile.build_profile(snapshot(), [])
        facts = {fact["id"]: fact for fact in built["facts"]}
        measured = {fact_id: fact["value"] for fact_id, fact in facts.items() if fact["provenance"] == "measured"}
        self.assertEqual(measured, {**EXISTING_FACTS, **NEW_FACTS})
        for fact_id in measured:
            self.assertEqual(facts[fact_id]["source"], "debug_info")
            self.assertTrue(facts[fact_id]["detail"].startswith("debug information generated on 2026-11-19"), fact_id)
        self.assertEqual(facts["worker.replicas"]["detail"], "debug information generated on 2026-11-19; counted from the instances n8n listed")
        self.assertEqual(built["sources"], {**dict.fromkeys(report.SECTIONS, "not_provided"), "debug_info": "collected"})
        self.assertEqual(list(built["sources"])[:6], list(report.SECTIONS))
        self.assertEqual((built["tool_version"], built["format_version"]), ("0.4.0", 1))

    def test_a_date_that_is_not_known_is_said_in_the_detail(self):
        facts = facts_of(snapshot((STAMP, "")))
        self.assertEqual(facts["n8n.version"]["detail"], "debug information, date unknown")
        self.assertEqual(facts["main.replicas"]["detail"], "debug information, date unknown; counted from the instances n8n listed")

    def test_a_type_with_no_instance_listed_is_unknown_never_zero(self):
        facts = facts_of(snapshot())
        self.assertEqual((facts["webhook.replicas"]["provenance"], facts["webhook.replicas"]["value"]), ("unknown", None))
        self.assertEqual(facts["webhook.replicas"]["detail"], "the debug information lists no webhook instance")
        # engine instances are in none of the three counts
        with_engine = snapshot(("instanceType: worker, instanceRole: unset, version: 3.3.0\n- checks", "instanceType: engine, instanceRole: unset, version: 3.3.0\n- checks"))
        self.assertEqual(with_engine.debug_info["cluster"]["instances"], {"main": 1, "worker": 1, "webhook": 0, "engine": 1})
        self.assertEqual([facts_of(with_engine)[f"{role}.replicas"]["value"] for role in profile.ROLES], [1, 1, None])

    def test_without_a_cluster_section(self):
        facts = facts_of(snapshot(base=NO_CLUSTER))
        detail = "this debug information has no cluster section (older n8n, or the instance registry was not loaded)"
        for fact_id in ("main.replicas", "worker.replicas", "webhook.replicas", "cluster.versions_aligned", "cluster.checks_failed"):
            self.assertEqual((facts[fact_id]["provenance"], facts[fact_id]["detail"]), ("unknown", detail), fact_id)
        self.assertEqual(facts["executions.mode"]["value"], "queue")

    def test_worker_concurrency_is_never_filled_from_it(self):
        self.assertEqual(facts_of(snapshot(("concurrency: -1", "concurrency: 20")))["worker.concurrency"]["provenance"], "unknown")
        self.assertEqual(facts_of(snapshot(("concurrency: -1", "concurrency: 20")))["main.concurrency_limit"]["value"], 20)

    def test_the_security_facts_are_only_ever_false(self):
        facts = facts_of(snapshot(SECURE))
        for fact_id in ("security.secure_cookie", "security.file_access_blocked"):
            self.assertEqual((facts[fact_id]["provenance"], facts[fact_id]["value"]), ("unknown", None))
            self.assertEqual(facts[fact_id]["detail"], "n8n reports this only when it is insecure")

    def test_the_cluster_facts(self):
        facts = facts_of(snapshot(TWO_VERSIONS, FAILED_CHECK, ("check: lifecycle, status: succeeded", f"check: {MARK}, status: failed")))
        self.assertIs(facts["cluster.versions_aligned"]["value"], False)
        self.assertEqual(facts["cluster.checks_failed"]["value"], 2)
        # A succeeded check with warnings does not count: FULL has one.
        self.assertEqual(facts_of(snapshot())["cluster.checks_failed"]["value"], 0)

    def test_without_a_debug_information_the_new_facts_say_so(self):
        for snap in (Snapshot(), Snapshot(env={"EXECUTIONS_MODE": "queue"})):
            facts = facts_of(snap)
            for fact_id in NEW_FACTS:
                self.assertEqual(facts[fact_id], {"id": fact_id, "value": None, "provenance": "unknown", "source": None, "detail": "The debug information was not provided"})

    def test_a_value_the_text_does_not_state_is_unknown(self):
        facts = facts_of(snapshot(("- license: enterprise (production)\n", ""), ("- manual: true\n", ""), PRUNING_OFF))
        for fact_id in ("license.type", "executions.save_manual", "pruning.max_count", "pruning.max_age_hours"):
            self.assertEqual(facts[fact_id]["provenance"], "unknown", fact_id)
        self.assertEqual(facts["license.type"]["detail"], "the debug information does not state it")

    def test_a_new_fact_whose_variable_a_probe_states_is_not_filled_from_the_text(self):
        env = {"N8N_CONCURRENCY_PRODUCTION_LIMIT": "5", "N8N_SECURE_COOKIE": "true", "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"}
        facts = facts_of(snapshot(env=env))
        for fact_id in ("main.concurrency_limit", "security.secure_cookie", "executions.save_progress"):
            self.assertEqual((facts[fact_id]["provenance"], facts[fact_id]["value"]), ("unknown", None), fact_id)
            self.assertIn("another source of this run states this setting", facts[fact_id]["detail"])
        self.assertEqual(facts["executions.save_manual"]["source"], "debug_info")  # not stated: still filled

    def test_a_file_with_the_new_source_and_ids_is_accepted_by_0_2_0_and_by_this_version(self):
        for snap in (snapshot(), snapshot(base=NO_CLUSTER), snapshot(SECURE, env={"DB_TYPE": "sqlite"}), Snapshot()):
            findings = engine.run(snap, ALL_RULES)
            data = json.loads(profile.to_json(profile.build_profile(snap, findings)))
            self.assertIs(validate_0_2_0.validate(data), data)
            self.assertEqual(profile.validate(data), data)
            self.assertIn("| debug_info |", profile.render_markdown(data))
            self.assertNotIn("-1", json.dumps([fact["value"] for fact in data["facts"]]))  # numbers are never negative

    def test_files_written_by_0_2_0_and_0_3_0_still_open(self):
        built = profile.build_profile(Snapshot(env={"EXECUTIONS_MODE": "queue"}), [])
        six = {name: state for name, state in built["sources"].items() if name in report.SECTIONS}
        older = {
            "0.2.0": {**built, "tool_version": "0.2.0", "sources": six, "facts": built["facts"][:32]},
            "0.3.0": {**built, "tool_version": "0.3.0", "sources": six, "facts": built["facts"][:46]},
        }
        for version, data in older.items():
            with self.subTest(version=version):
                data = json.loads(profile.to_json(data))
                self.assertEqual(len(data["sources"]), 6)
                self.assertEqual(profile.validate(data), data)
                self.assertNotIn("debug_info", profile.render_markdown(data))
                sheet = profile.render_questions(data)  # the ids it never held are open questions
                for fact_id in NEW_FACTS:
                    self.assertIn(f"(`{fact_id}`)", sheet)

    def test_the_sheet_lists_the_new_facts_with_a_sentence_and_the_reason(self):
        sheet = profile.render_questions(profile.build_profile(snapshot(SECURE, (CLUSTER, "")), []))
        self.assertIn("- **Is the session cookie restricted to HTTPS?** (`security.secure_cookie`): n8n reports this only when it is insecure", sheet)
        self.assertIn("- **Do all n8n instances run the same version?** (`cluster.versions_aligned`): this debug information has no cluster section", sheet)
        self.assertNotIn("`n8n.node_version`", sheet)  # measured: not open


def summary(snap, findings=(), **kwargs):
    return report.build_report(snap, list(findings), today=TODAY, **kwargs)["summary"]


class ReportTest(unittest.TestCase):
    FULL_RUN = Snapshot(health={"healthz": True, "readiness": True}, metrics={}, env={}, postgres={}, redis={}, kubernetes={"deployments": []}, n8n_version="2.40.1")

    def test_not_given_it_is_not_mentioned(self):
        for snap in (Snapshot(), Snapshot(env={}), self.FULL_RUN):
            self.assertNotIn("ebug information", report.render_markdown(snap, [], today=TODAY))
        self.assertIn("Every data source was collected and every check ran.", report.render_markdown(self.FULL_RUN, []))

    def test_given_next_to_every_probe_the_count_of_sources_stays_six(self):
        with_text = dataclasses.replace(self.FULL_RUN, debug_info=parse(FULL))
        markdown = report.render_markdown(with_text, [], today=TODAY)
        self.assertTrue(markdown.startswith("# n8n health check report — n8n 2.40.1\n"))  # what a probe measured
        self.assertIn("Debug information generated on 2026-11-19 was read.", markdown)
        self.assertIn("Every data source was collected and every check ran.", markdown)
        self.assertNotIn("partial", markdown)
        self.assertNotIn("rests on the debug information only", markdown)
        missing_one = dataclasses.replace(with_text, redis=None)
        self.assertIn("only 5 of 6 data sources were assessed", summary(missing_one))

    def test_given_alone_the_run_is_assessed_and_partial(self):
        snap = snapshot(SQLITE)
        findings = engine.run(snap, ALL_RULES)
        data = report.build_report(snap, findings, today=TODAY)
        self.assertEqual(data["n8n_version"], "3.3.0")
        self.assertTrue(data["partial"])
        self.assertTrue(all(row["score"] is not None and row["partial"] for row in data["scores"]))
        self.assertEqual({row["area"]: row["score"] for row in data["scores"]}["reliability"], 100 - 40 - 5)
        self.assertNotIn("could not run", data["summary"])
        self.assertIn("only 0 of 6 data sources were assessed", data["summary"])
        self.assertTrue(data["summary"].endswith(
            "Debug information generated on 2026-11-19 was read. "
            "The check rests on the debug information only: none of the six data sources was given."
        ))
        markdown = report.render_markdown(snap, findings, today=TODAY)
        self.assertIn("| Reliability | 55/100 (partial) | 2 |", markdown)
        self.assertNotIn("n/a", markdown)
        self.assertEqual(len(data["not_assessed"]), 6)
        # Without the text, the same empty run has no score at all.
        self.assertTrue(all(row["score"] is None for row in report.build_report(Snapshot(), [])["scores"]))

    def test_given_next_to_probes_that_failed(self):
        self.assertIn("none of the six data sources was collected.", summary(snapshot(), failed=("env",)))

    def test_the_three_warnings(self):
        old = snapshot((STAMP, "Generated at: 2026-10-01T08:00:00.000Z"))
        self.assertIn("Debug information generated on 2026-10-01 was read. It was generated 50 days ago, the instance may have changed.", summary(old))
        thirty = snapshot((STAMP, "Generated at: 2026-10-21T08:00:00.000Z"))
        self.assertNotIn("days ago", summary(thirty))
        self.assertIn("generated 31 days ago", summary(snapshot((STAMP, "Generated at: 2026-10-20T08:00:00.000Z"))))
        undated = summary(snapshot((STAMP, "")))
        self.assertIn("Debug information was read, date unknown.", undated)
        self.assertNotIn("generated on", undated)
        no_cluster = summary(snapshot(base=NO_CLUSTER))
        self.assertIn("The number of replicas and whether every process runs the same version cannot be told from it: this debug information has no cluster section (older n8n, or the instance registry was not loaded).", no_cluster)
        self.assertNotIn("cluster section", summary(snapshot()))

    def test_the_web_api_still_gets_its_could_not_run_summary(self):
        self.assertTrue(summary(Snapshot(), failed=("health",), could_not_run=()).startswith("This health check could not run"))


class CliTest(CliRun):
    def test_the_flag_alone(self):
        code, out, err = self.run_cli(["--debug-info", self.write("debug.txt", text(SQLITE))])
        self.assertEqual((code, err), (2, ""))  # REL-001 is critical
        self.assertTrue(out.startswith("# n8n health check report — n8n 3.3.0\n"))
        self.assertIn("#### REL-001 — Queue mode on SQLite", out)
        self.assertIn("The check rests on the debug information only", out)
        self.assertIn("/100 (partial)", out)
        self.assertEqual(self.run_cli(["--debug-info", self.write("clean.txt", text(SECURE, MULTI_MAIN))])[0], 0)

    def test_from_standard_input(self):
        code, out, err = self.run_cli(["--debug-info", "-"], stdin=("﻿" + text(SQLITE)).encode("utf-8"))
        self.assertEqual((code, err), (2, ""))
        self.assertIn("#### REL-001 — Queue mode on SQLite", out)
        # Bytes that are not UTF-8 around the text do not stop it from being read.
        code, out, _ = self.run_cli(["--debug-info", "-"], stdin=b"\x80\x81 junk\n" + text(SQLITE).encode("utf-8"))
        self.assertEqual(code, 2)

    def test_next_to_another_source_the_probe_wins(self):
        env = self.write("n8n.env", "EXECUTIONS_MODE=regular\nDB_TYPE=postgresdb\nN8N_CONCURRENCY_PRODUCTION_LIMIT=20\nN8N_SECURE_COOKIE=true\nN8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=true\n")
        argv = ["--env-file", env, "--debug-info", self.write("debug.txt", text(SQLITE)), "--save-profile", self.path("profile.json")]
        code, out, err = self.run_cli(argv)
        self.assertEqual((code, err), (1, ""))  # the warnings the env file gives by itself
        self.assertEqual(out.count("\n#### "), self.run_cli(["--env-file", env])[1].count("\n#### "))
        self.assertNotIn("REL-001", out)
        self.assertIn("only 1 of 6 data sources were assessed", out)
        self.assertIn("Debug information generated on 2026-11-19 was read.", out)
        self.assertNotIn("rests on the debug information only", out)
        facts = {fact["id"]: fact for fact in json.loads(self.read("profile.json"))["facts"]}
        self.assertEqual((facts["executions.mode"]["value"], facts["executions.mode"]["source"]), ("regular", "env"))
        self.assertEqual((facts["n8n.version"]["value"], facts["n8n.version"]["source"]), ("3.3.0", "debug_info"))

    def test_next_to_a_probe_that_failed_the_run_is_not_nothing_collected(self):
        argv = ["--env-file", self.path("missing.env"), "--debug-info", self.write("debug.txt", text(SECURE, MULTI_MAIN))]
        code, out, err = self.run_cli(argv)
        self.assertEqual(code, 0)
        self.assertIn("(`--env-file`) — requested but could not be collected", out)
        self.assertIn("none of the six data sources was collected.", out)
        self.assertNotIn("none of the requested sources", err)
        # Without the text the same run exits 3.
        code, out, err = self.run_cli(["--env-file", self.path("missing.env")])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("none of the requested sources could be collected", err)

    def test_each_refusal_is_a_usage_error_before_anything_is_collected(self):
        cases = {
            "a file that is not there": ([self.path("missing.txt")], "error: cannot read the debug information: No such file or directory\n"),
            "a directory": ([self.dir], "error: cannot read the debug information: Is a directory\n"),
            "not a debug information": ([self.write("env.txt", "EXECUTIONS_MODE=queue\n")], f"error: --debug-info: {debug_info.NOT_DEBUG_INFO}\n"),
            "an empty file": ([self.write("empty.txt", "")], f"error: --debug-info: {debug_info.NOT_DEBUG_INFO}\n"),
            "too large": ([self.write("large.txt", FULL + "\n" + "x" * debug_info.MAX_BYTES)], f"error: --debug-info: {debug_info.TOO_LARGE}\n"),
        }
        for name, (values, message) in cases.items():
            with self.subTest(case=name):
                with mock.patch("n8n_healthcheck.collectors.collect") as collect:
                    code, out, err = self.run_cli(["--url", "https://n8n.example.com", "--debug-info", *values])
                self.assertEqual((code, out, err), (3, "", message))
                collect.assert_not_called()
        code, _, err = self.run_cli(["--debug-info", " "])
        self.assertEqual(code, 3)
        self.assertIn("--debug-info must not be empty", err)
        code, _, err = self.run_cli(["--debug-info", self.write("debug.txt", FULL), "--snapshot", self.path("any.json")])
        self.assertEqual(code, 3)
        self.assertIn("--debug-info cannot be combined with --snapshot", err)
        code, _, err = self.run_cli([])
        self.assertEqual(code, 3)
        self.assertIn("nothing to inspect", err)

    def test_the_snapshot_round_trip_holds_the_section_and_no_identifier(self):
        argv = ["--debug-info", self.write("debug.txt", text(SQLITE)), "--save-snapshot", self.path("snapshot.json")]
        code, out, _ = self.run_cli(argv)
        saved = json.loads(self.read("snapshot.json"))
        self.assertEqual(saved["debug_info"], parse(text(SQLITE)))
        self.assertNotIn(MARK, self.read("snapshot.json"))
        again = self.run_cli(["--snapshot", self.path("snapshot.json")])
        self.assertEqual(again, (code, out, ""))

    def test_a_snapshot_without_the_field_still_loads_and_one_without_the_text_does_not_write_it(self):
        self.write("old.json", json.dumps({"env": {"EXECUTIONS_MODE": "queue"}, "n8n_version": "2.40.1"}))
        code, out, _ = self.run_cli(["--snapshot", self.path("old.json")])
        self.assertEqual(code, 2)
        self.assertNotIn("ebug information", out)
        argv = ["--env-file", self.write("n8n.env", "EXECUTIONS_MODE=queue\n"), "--save-snapshot", self.path("new.json")]
        self.run_cli(argv)
        self.assertNotIn("debug_info", json.loads(self.read("new.json")))  # as tool version 0.3.0 wrote it

    def test_existing_abbreviations_keep_their_meaning(self):
        argv = ["--env", self.write("n8n.env", "EXECUTIONS_MODE=queue\n"), "--save", self.path("snapshot.json"), "--debug", self.write("debug.txt", FULL)]
        self.assertNotEqual(self.run_cli(argv)[0], 3)
        self.assertEqual(json.loads(self.read("snapshot.json"))["env"], {"EXECUTIONS_MODE": "queue"})
        self.assertIn("--debug-info FILE", self.run_cli(["--help"])[1])


# --- the review of af078e3: one test per fix ---------------------------------------

ON_2_40_1 = kube(("n8n-main", 1, container()), ("n8n-worker", 2, container(command=["n8n", "worker"])))
# n8n 1.x: no client or cluster section, `scaling` without the number of mains, `mysql`.
ONE_X = text(
    ("n8nVersion: 3.3.0", "n8nVersion: 1.90.0"),
    ("executionMode: scaling (single-main)", "executionMode: scaling"),
    ("database: postgres", "database: mysql"),
    (f"- nodeEnv: {MARK}-env\n", ""),
    (CLUSTER, ""),
)


def wrapped(source, width, prefix=""):
    """`source` as a mail client sends it: every line longer than `width` broken at a space."""
    lines = []
    for line in source.splitlines():
        parts = textwrap.wrap(line, width, break_long_words=False, break_on_hyphens=False) or [""]
        lines += [prefix + part for part in parts]
    return "\n".join(lines)


class ReviewTest(CliRun):
    def test_1_the_title_follows_the_precedence_of_the_fact(self):
        custom = kube(("n8n-main", 1, container(image="registry.example/n8n-custom:9.9.9")))
        cases = {
            "Kubernetes names the version": (snapshot(kubernetes=ON_2_40_1), None, ("2.40.1", "kubernetes")),
            "metrics name it": (snapshot(kubernetes=ON_2_40_1, n8n_version="2.41.0"), "2.41.0", ("2.41.0", "metrics")),
            "an image that names no version": (snapshot(kubernetes=custom), "3.3.0", ("3.3.0", "debug_info")),
            "the text alone": (snapshot(), "3.3.0", ("3.3.0", "debug_info")),
        }
        for name, (snap, title, fact) in cases.items():
            with self.subTest(case=name):
                found = facts_of(snap)["n8n.version"]
                self.assertEqual((found["value"], found["source"]), fact)
                self.assertEqual(report.build_report(snap, [], today=TODAY)["n8n_version"], title)
        # As on main without the text: no version in the title, never the text's.
        markdown = report.render_markdown(snapshot(kubernetes=ON_2_40_1), [], today=TODAY)
        self.assertEqual(markdown.splitlines()[0], "# n8n health check report")
        self.assertEqual(report.render_markdown(Snapshot(kubernetes=ON_2_40_1), []).splitlines()[0], "# n8n health check report")

    def test_2_versions_aligned_gives_way_where_maint_003_does(self):
        skewed = kube(("n8n-main", 1, container()), ("n8n-worker", 2, container(command=["n8n", "worker"], image="n8nio/n8n:2.39.0")))
        unnamed = kube(("n8n-main", 1, container(image="n8nio/n8n:latest")))
        detail = "another source of this run states the versions, so the debug information was not used for it"
        for name, snap, fires, fact in (
            ("Kubernetes on one version, the text lists two", snapshot(TWO_VERSIONS, kubernetes=ON_2_40_1), False, ("unknown", None, detail)),
            ("Kubernetes on two versions, the text lists one", snapshot(kubernetes=skewed), True, ("unknown", None, detail)),
            ("Kubernetes names no version, the text lists two", snapshot(TWO_VERSIONS, kubernetes=unnamed), True, ("measured", False, "debug information generated on 2026-11-19")),
            ("Kubernetes names no version, the text lists one", snapshot(kubernetes=unnamed), False, ("measured", True, "debug information generated on 2026-11-19")),
            ("the text alone lists two", snapshot(TWO_VERSIONS), True, ("measured", False, "debug information generated on 2026-11-19")),
        ):
            with self.subTest(case=name):
                found = facts_of(snap)["cluster.versions_aligned"]
                self.assertEqual((found["provenance"], found["value"], found["detail"]), fact)
                self.assertEqual(maint.version_skew(snap) is not None, fires)
                # The rule and the fact never say opposite things.
                self.assertFalse(fires and found["value"] is True)
                self.assertFalse(not fires and found["value"] is False)
        # Without the text the fact still says that the text is what is missing.
        self.assertEqual(facts_of(Snapshot(kubernetes=ON_2_40_1))["cluster.versions_aligned"]["detail"], "The debug information was not provided")

    def test_2_checks_failed_and_rel_009_read_the_same_thing(self):
        # No probe states a cluster check, so there is nothing for either to give way to.
        for changes in ((), (FAILED_CHECK,), ((CLUSTER, ""),), ((CLUSTER, "## cluster\n\n- instanceCount: 0\n\n"),)):
            for sections in ({}, {"kubernetes": ON_2_40_1, "env": {"EXECUTIONS_MODE": "queue"}}):
                snap = snapshot(*changes, **sections)
                count = facts_of(snap)["cluster.checks_failed"]["value"]
                self.assertEqual(rel.cluster_check_failed(snap) is not None, bool(count))

    def test_3_a_text_that_holds_more_than_one_debug_information_is_refused(self):
        older = text(("n8nVersion: 3.3.0", "n8nVersion: 1.99.0"), REGULAR, (STAMP, "Generated at: 2025-01-05T08:00:00.000Z"))
        several = {
            "an old one above a new one": older + "\n\n" + FULL,
            "the same text twice": FULL + "\n" + FULL,
            "quoted once, then again": "> " + FULL.replace("\n", "\n> ") + "\n\n" + FULL,
            "a second title": FULL + "\n\n# Debug info\n",
            "a section that comes twice": FULL + "\n\n## storage\n\n- progress: true\n",
            "the first section again, without its #": FULL + "\ncore\n- n8nVersion: 9.9.9\n",
            "a second date": FULL + "\nGenerated at: 2026-11-18T00:00:00.000Z",
            "the same date twice": FULL + "\n" + STAMP,
        }
        for name, pasted in several.items():
            with self.subTest(text=name):
                with self.assertRaises(debug_info.DebugInfoError) as caught:
                    parse(pasted)
                self.assertEqual(str(caught.exception), debug_info.SEVERAL)
        self.assertNotIn(MARK, debug_info.SEVERAL)
        self.assertEqual(len({debug_info.SEVERAL, debug_info.NOT_DEBUG_INFO, debug_info.TOO_LARGE}), 3)
        with mock.patch("n8n_healthcheck.collectors.collect") as collect:
            code, out, err = self.run_cli(["--debug-info", self.write("twice.txt", FULL + "\n" + FULL)])
        self.assertEqual((code, out, err), (3, "", f"error: --debug-info: {debug_info.SEVERAL}\n"))
        collect.assert_not_called()

    def test_4_the_values_n8n_1_x_writes(self):
        got = parse(ONE_X)
        self.assertEqual((got["execution_mode"], got["multi_main"], got["database"]), ("queue", None, "mysqldb"))
        self.assertEqual({**got, "multi_main": False, "database": "postgresdb", "n8n_version": "3.3.0"}, {**PARSED, "cluster": None})
        snap = Snapshot(debug_info=got)
        facts = facts_of(snap)
        self.assertEqual((facts["executions.mode"]["value"], facts["executions.mode"]["source"]), ("queue", "debug_info"))
        self.assertEqual((facts["database.engine"]["value"], facts["database.engine"]["source"]), ("mysqldb", "debug_info"))
        self.assertEqual(facts["multi_main.enabled"]["provenance"], "unknown")
        self.assertIsNone(rel.single_main(snap))  # the text does not say how many mains
        # Queue mode is known, so the rules that need only that still fire.
        on_sqlite = Snapshot(debug_info=parse(ONE_X.replace("database: mysql", "database: sqlite")))
        self.assertEqual(rel.sqlite_in_queue_mode(on_sqlite).rule_id, "REL-001")
        # Next to an env file that does not set the mode, the text's queue mode is used.
        self.assertEqual(facts_of(Snapshot(debug_info=got, env={}))["executions.mode"]["value"], "queue")

    def test_5_a_utf_16_file_is_read(self):
        expected = self.run_cli(["--debug-info", self.write("utf8.txt", text(SQLITE))])
        self.assertEqual(expected[0], 2)
        for name, encoded in (
            ("little endian", b"\xff\xfe" + text(SQLITE).encode("utf-16-le")),
            ("big endian", b"\xfe\xff" + text(SQLITE).encode("utf-16-be")),
            ("windows line ends", b"\xff\xfe" + text(SQLITE).replace("\n", "\r\n").encode("utf-16-le")),
        ):
            with self.subTest(encoding=name):
                with open(self.path("utf16.txt"), "wb") as handle:
                    handle.write(encoded)
                self.assertEqual(self.run_cli(["--debug-info", self.path("utf16.txt")]), expected)
                self.assertEqual(self.run_cli(["--debug-info", "-"], stdin=encoded), expected)
        # The size limit is on the text, not on the two bytes a character takes in the file.
        large = b"\xff\xfe" + (FULL + "\n" + "x" * debug_info.MAX_BYTES).encode("utf-16-le")
        with open(self.path("large16.txt"), "wb") as handle:
            handle.write(large)
        self.assertEqual(self.run_cli(["--debug-info", self.path("large16.txt")]), (3, "", f"error: --debug-info: {debug_info.TOO_LARGE}\n"))
        fits = b"\xff\xfe" + (FULL + "\n" + "x" * (debug_info.MAX_BYTES - len(FULL.encode()) - 1)).encode("utf-16-le")
        with open(self.path("fits16.txt"), "wb") as handle:
            handle.write(fits)
        self.assertNotEqual(self.run_cli(["--debug-info", self.path("fits16.txt")])[0], 3)

    def test_6_a_list_item_a_mail_client_broke_is_joined_again(self):
        source = text(FAILED_CHECK, TWO_VERSIONS)
        expected = parse(source)
        self.assertEqual(expected["cluster"]["instances"], {"main": 1, "worker": 2, "webhook": 0, "engine": 0})
        broken = 0
        for width in range(40, 161):  # no other line of the text is 40 characters long
            for prefix in ("", "> "):
                with self.subTest(width=width, prefix=prefix):
                    mail = wrapped(source, width, prefix)
                    broken += mail.count("\n") > source.count("\n")
                    self.assertEqual(parse(mail), expected)
        self.assertGreater(broken, 150)
        # The reviewer's case: the break falls between `instanceType:` and its value.
        at_the_value = source.replace("instanceType: worker", "instanceType:\nworker")
        self.assertEqual(parse(at_the_value), expected)
        self.assertEqual(parse(at_the_value.replace("- ", "* ")), expected)
        self.assertEqual(parse(source.replace("status: failed, warnings:", "status:\nfailed, warnings:")), expected)

    def test_6_a_line_without_a_colon_still_ends_a_section_where_it_cannot_be_a_continuation(self):
        # Not after an instance or a check: nothing that long is read there, so it is a heading.
        cut = text(("- instanceCount: 3\n", f"- instanceCount: 3\nfuture{MARK}\n"))
        self.assertEqual(parse(cut)["cluster"]["instances"], dict.fromkeys(debug_info.INSTANCE_TYPES, 0))
        # After a blank line, with a bullet or with a "#": it starts something of its own.
        for start in (f"\nfuture{MARK}\n", f"## future{MARK}\n"):
            cut = text(("- checks:\n", f"{start}- instanceKey: {MARK}, hostId: {MARK}, instanceType: worker, instanceRole: unset, version: 3.3.0\n- checks:\n"))
            self.assertEqual(parse(cut)["cluster"]["instances"]["worker"], 2, start)
        # A new item is never taken for the rest of the one before it, with or without bullets.
        self.assertEqual(parse("\n".join(line.lstrip("- ") for line in FULL.splitlines())), PARSED)

    def test_7_only_known_values_come_out_of_a_section_whatever_it_holds(self):
        for changes in ((), (SQLITE, PRUNING_OFF), (FAILED_CHECK, TWO_VERSIONS, SANDBOX), (SECURE, (STAMP, "")), ((CLUSTER, ""),)):
            parsed = parse(text(*changes))
            self.assertEqual(debug_info.checked(parsed), parsed)  # what parse() writes is left as it is
        self.assertEqual(debug_info.checked(parse(ONE_X)), parse(ONE_X))
        hostile = {
            "generated_on": f"{MARK}-day", "n8n_version": f"{MARK}V", "node_version": "22.16", "platform": "<b>",
            "database": f"postgres://{MARK}", "execution_mode": MARK, "multi_main": "yes", "concurrency_limit": -7,
            "license": f"{MARK}L", "save_success": MARK, "save_error": ["all"], "save_progress": 1, "save_manual": MARK,
            "binary_mode": f"/var/{MARK}", "pruning": {"enabled": True, "max_age_hours": -5, "max_count": f"{MARK}"},
            "insecure": {"secure_cookie": MARK, "file_access_blocked": 1},
            "cluster": {"instances": {"main": -3, "worker": MARK, MARK: 9}, "versions": [MARK, "3.3.0", 7, f"3.3.0-{MARK}"], "checks_failed": [MARK, "split-brain", 5]},
            MARK: MARK,
        }  # fmt: skip
        snap = Snapshot(debug_info=hostile)
        findings = engine.run(snap, ALL_RULES)
        built = profile.build_profile(snap, findings)
        outputs = (profile.to_json(built), repr(findings), report.render_markdown(snap, findings, today=TODAY), profile.render_questions(built))
        for output in outputs:
            self.assertNotIn(MARK, output)
            self.assertNotIn("<b>", output)
        facts = {fact["id"]: fact for fact in built["facts"]}
        measured = {fact_id: fact["value"] for fact_id, fact in facts.items() if fact["provenance"] == "measured"}
        self.assertEqual(measured, {"pruning.enabled": True, "cluster.versions_aligned": True, "cluster.checks_failed": 3})
        self.assertEqual(profile.validate(json.loads(profile.to_json(built))), built)
        self.assertEqual([f.rule_id for f in findings], ["REL-009"])
        self.assertIn("3 failed cluster checks: split-brain, 2 other checks.", findings[0].evidence)
        self.assertIn("debug information of unknown date", findings[0].evidence)
        # A value of the wrong type that equals a good one is not let through either.
        self.assertIsNone(debug_info.checked({"concurrency_limit": True})["concurrency_limit"])
        self.assertIsNone(debug_info.checked({"pruning": {"enabled": 1}})["pruning"])
        for not_a_section in ("text", ["list"], 7):
            self.assertIsNone(debug_info.checked(not_a_section))
            self.assertEqual(profile.build_profile(Snapshot(debug_info=not_a_section), [])["sources"]["debug_info"], "not_provided")

    def test_8_a_finding_resting_on_an_env_file_and_on_the_text_names_both(self):
        date_ = "debug information generated on 2026-11-19"
        both = rel.sqlite_in_queue_mode(snapshot(SQLITE, env={"EXECUTIONS_MODE": "queue"})).evidence
        self.assertTrue(both.endswith(f" From the env file: EXECUTIONS_MODE. From the {date_}: DB_TYPE."), both)
        # Where the text gives no variable but the finding rests on it all the same.
        workers = scal.single_worker(snapshot(ONE_WORKER, env={"EXECUTIONS_MODE": "queue"})).evidence
        self.assertEqual(workers, f"The {date_} lists 1 worker instance; EXECUTIONS_MODE=queue. From the env file: EXECUTIONS_MODE.")
        files = sec.file_access_not_blocked(snapshot(env={"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "false"})).evidence
        self.assertTrue(files.endswith("N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=false (default: true). From the env file: N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES."), files)
        # Every value from the text: the env file is not named for nothing.
        only_text = rel.sqlite_in_queue_mode(snapshot(SQLITE, env={})).evidence
        self.assertNotIn("env file", only_text)
        self.assertIn(f"From the {date_}: EXECUTIONS_MODE, DB_TYPE.", only_text)
        # A finding that does not rest on the text reads as it does without one.
        env = {"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite", "EXECUTIONS_DATA_PRUNE": "false"}
        for rule in (rel.sqlite_in_queue_mode, perf.pruning_disabled):
            self.assertEqual(rule(snapshot(env=env)), rule(Snapshot(env=env)))
        # Kubernetes was already named.
        main = kube(("n8n-main", 1, container(env={"EXECUTIONS_MODE": "queue"})))
        self.assertIn("Environment read from Deployment n8n-main, container n8n. From the", rel.sqlite_in_queue_mode(snapshot(SQLITE, kubernetes=main)).evidence)


if __name__ == "__main__":
    unittest.main()
