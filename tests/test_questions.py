"""Profile questions: closed answers become declared facts, and a profile gives a question sheet."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

import n8n_healthcheck
import test_profile as base  # helpers and the seeded snapshot; imported as a module so its tests run once
import validate_0_2_0
from n8n_healthcheck import cli, engine, profile, questions
from n8n_healthcheck.models import Snapshot
from n8n_healthcheck.rules import ALL_RULES

# The operator group as the design lists it: (fact id, kind, values), in order.
OPERATOR = [
    ("hosting.model", "choice", "public_cloud private_cloud on_premises hybrid"),
    ("hosting.cloud_provider", "choice", "aws azure gcp other none"),
    ("orchestrator", "choice", "kubernetes ecs docker_compose docker_swarm nomad virtual_machine other"),
    ("install.method", "choice", "helm_chart kubernetes_manifests compose_file task_definition npm other"),
    ("n8n.image_modified", "yes_no", "yes no"),
    ("proxy.kind", "choice", "nginx traefik haproxy envoy cloud_load_balancer other none"),
    ("proxy.tls_termination", "choice", "at_proxy at_n8n none"),
    ("proxy.sticky_sessions", "yes_no", "yes no"),
    ("database.service", "choice", "managed_service self_managed"),
    ("database.high_availability", "yes_no", "yes no"),
    ("database.backups", "choice", "point_in_time scheduled none"),
    ("queue.redis_topology", "choice", "single sentinel cluster managed_service"),
    ("secrets.source", "choice", "environment orchestrator_secrets external_store files"),
    ("worker.autoscaling", "yes_no", "yes no"),
    ("observability.tools", "multi", "prometheus grafana datadog opentelemetry cloudwatch splunk elastic other none"),
]
NEW_IDS = [fact_id for fact_id, _, _ in OPERATOR if fact_id != "orchestrator"]

# One valid answer to every operator question.
EVERY_ANSWER = {
    fact_id: values.split()[:2] if kind == "multi" else values.split()[0] for fact_id, kind, values in OPERATOR
}


def facts_of(built):
    return {f["id"]: f for f in built["facts"]}


def answered(answers, snapshot=None, **kwargs):
    """(profile, dropped ids) of a run with these answers."""
    return profile.apply_answers(base.build(snapshot, **kwargs), answers)


def written_by_0_2_0(built):
    """The same profile as tool version 0.2.0 wrote it: without the ids added since."""
    return {**built, "tool_version": "0.2.0", "facts": [f for f in built["facts"] if f["id"] not in NEW_IDS]}


class CatalogueTest(unittest.TestCase):
    def test_the_operator_group_is_the_one_of_the_design(self):
        got = [(q["id"], q["kind"], " ".join(value for value, _ in q["values"])) for q in questions.OPERATOR]
        self.assertEqual(got, OPERATOR)
        self.assertLessEqual(len(questions.OPERATOR), 15)  # more than 15 is a failure of the design

    def test_every_value_has_a_label_and_nothing_is_free_text(self):
        for question in questions.OPERATOR:
            self.assertRegex(question["id"], r"^[a-z0-9_.]+$")
            self.assertTrue(question["question"].endswith("?"))
            for value, label in question["values"]:
                self.assertRegex(value, r"^[a-z0-9_]+$")
                self.assertTrue(label.strip())
        self.assertEqual(set(questions.OPERATOR_BY_ID), {q["id"] for q in questions.OPERATOR})

    def test_the_conversation_group(self):
        self.assertEqual(len(questions.CONVERSATION), 10)
        for question, why in questions.CONVERSATION:
            self.assertTrue(question.endswith("?"))
            self.assertTrue(why.endswith("."))

    def test_every_catalogue_id_has_a_sentence(self):
        for fact_id in profile.FACT_IDS:
            self.assertTrue(questions.sentence(fact_id).endswith("?"), fact_id)
        # One place per sentence: an operator question is not repeated among the measured ones.
        self.assertEqual(set(questions.MEASURED), set(profile.FACT_IDS) - set(questions.OPERATOR_BY_ID))

    def test_the_new_ids_join_the_catalogue_but_not_the_frozen_list(self):
        self.assertEqual(len(NEW_IDS), 14)
        self.assertEqual(list(profile.FACT_IDS[32:]), NEW_IDS)
        self.assertEqual(set(profile.FACT_IDS[:32]), profile.FORMAT_1_FACT_IDS)
        self.assertFalse(set(NEW_IDS) & profile.FORMAT_1_FACT_IDS)
        self.assertEqual(profile.FORMAT_1_FACT_IDS, validate_0_2_0.FORMAT_1_FACT_IDS)

    def test_version(self):
        self.assertEqual(n8n_healthcheck.__version__, "0.3.0")
        self.assertEqual(profile.FORMAT_VERSION, 1)
        self.assertEqual(base.build()["tool_version"], "0.3.0")

    def test_every_operator_question_is_documented(self):
        with open(base.DOCS, encoding="utf-8") as handle:
            added = handle.read().split("### Added in tool version 0.3.0")[1].split("\nThese ids")[0]
        rows = dict(re.findall(r"^\| `([a-z0-9_.]+)` \| (.*) \|$", added, re.MULTILINE))
        self.assertEqual(list(rows), [q["id"] for q in questions.OPERATOR])
        for question in questions.OPERATOR:
            row = rows[question["id"]]
            self.assertIn(question["question"], row)
            if question["kind"] == "yes_no":
                self.assertIn("boolean", row)
            else:
                self.assertEqual(re.findall(r"`([a-z0-9_]+)`", row.replace("`true`", "")), [v for v, _ in question["values"]])

    def test_unanswered_they_are_unknown(self):
        for snapshot in (Snapshot(), base.leaky_snapshot()):
            facts = facts_of(base.build(snapshot))
            for fact_id in NEW_IDS:
                self.assertEqual(
                    facts[fact_id],
                    {"id": fact_id, "value": None, "provenance": "unknown", "source": None, "detail": "not answered"},
                )


class AnswersTest(unittest.TestCase):
    def test_accepted_answers_become_declared_facts(self):
        before = base.build()
        text = json.dumps(before)
        built, dropped = profile.apply_answers(
            before, {"hosting.model": "hybrid", "worker.autoscaling": "yes", "proxy.sticky_sessions": "no"}
        )
        self.assertEqual(dropped, [])
        self.assertEqual(json.dumps(before), text)  # a copy: the original is not modified
        facts = facts_of(built)
        for fact_id, value in (("hosting.model", "hybrid"), ("worker.autoscaling", True), ("proxy.sticky_sessions", False)):
            self.assertEqual(
                facts[fact_id], {"id": fact_id, "value": value, "provenance": "declared", "source": None, "detail": ""}
            )
        # Nothing else moved.
        untouched = [f for f in built["facts"] if f["provenance"] != "declared"]
        self.assertEqual(untouched, [f for f in before["facts"] if f["id"] not in ("hosting.model", "worker.autoscaling", "proxy.sticky_sessions")])
        self.assertEqual({key: built[key] for key in built if key != "facts"}, {key: before[key] for key in before if key != "facts"})

    def test_every_value_of_every_question_is_accepted(self):
        for question in questions.OPERATOR:
            for value, _label in question["values"]:
                answer = [value] if question["kind"] == "multi" else value
                with self.subTest(id=question["id"], value=value):
                    built, dropped = answered({question["id"]: answer})
                    self.assertEqual(dropped, [])
                    self.assertEqual(facts_of(built)[question["id"]]["provenance"], "declared")

    def test_refusals(self):
        for answers, message in (
            ({"hosting.region": "eu"}, "no question the tool asks has the id 'hosting.region'"),
            ({"n8n.version": "2.40.1"}, "no question the tool asks has the id"),  # a fact, but only ever measured
            ({questions.CONVERSATION[0][0]: "SAML"}, "no question the tool asks has the id"),  # a conversation question
            ({"hosting.model": "my-datacentre.example"}, "the answer to hosting.model must be one of: public_cloud, private_cloud, on_premises, hybrid"),
            ({"hosting.model": ""}, "must be one of"),
            ({"hosting.model": "Public_Cloud"}, "must be one of"),
            ({"hosting.model": ["hybrid"]}, "must be one of"),
            ({"hosting.model": None}, "must be one of"),
            ({"worker.autoscaling": "true"}, "the answer to worker.autoscaling must be one of: yes, no"),
            ({"worker.autoscaling": True}, "must be one of"),
            ({"observability.tools": []}, "the answer to observability.tools must be one or more of: prometheus, grafana"),
            ({"observability.tools": "prometheus"}, "must be one or more of"),
            ({"observability.tools": ["prometheus", "zabbix"]}, "must be one or more of"),
            ({"observability.tools": ["prometheus", "prometheus"]}, "must be one or more of"),
            ({"observability.tools": [["prometheus"]]}, "must be one or more of"),
            ({"observability.tools": ["none", "grafana"]}, 'cannot be "none" together with another value'),
            ({"hosting.model": "hybrid", "proxy.kind": "apache"}, "the answer to proxy.kind"),  # one bad answer refuses all
        ):
            with self.subTest(answers=answers):
                with self.assertRaises(profile.ProfileError) as raised:
                    answered(answers)
                self.assertIn(message, str(raised.exception))
        # A refusal never repeats the value that was given.
        with self.assertRaises(profile.ProfileError) as raised:
            answered({"hosting.model": "my-datacentre.example"})
        self.assertNotIn("datacentre", str(raised.exception))

    def test_measured_wins(self):
        snapshot = Snapshot(kubernetes=base.kube(("m", 1, base.container())))
        measured = facts_of(base.build(snapshot))["orchestrator"]
        self.assertEqual(measured["provenance"], "measured")
        for value in ("ecs", "kubernetes"):  # contradicting it or agreeing with it: dropped either way
            built, dropped = answered({"orchestrator": value, "hosting.model": "hybrid"}, snapshot)
            self.assertEqual(dropped, ["orchestrator"])
            self.assertEqual(facts_of(built)["orchestrator"], measured)
            self.assertEqual(facts_of(built)["hosting.model"]["provenance"], "declared")
        # Not measured: the same answer is taken.
        built, dropped = answered({"orchestrator": "ecs"}, Snapshot(env={}))
        self.assertEqual(dropped, [])
        self.assertEqual(facts_of(built)["orchestrator"], {"id": "orchestrator", "value": "ecs", "provenance": "declared", "source": None, "detail": ""})

    def test_only_an_unknown_fact_takes_an_answer(self):
        first, _ = answered({"hosting.model": "hybrid"})
        again, dropped = profile.apply_answers(first, {"hosting.model": "on_premises"})
        self.assertEqual((dropped, again), (["hosting.model"], first))
        withheld = profile.apply_withholding(base.build(), ["hosting.model"])
        again, dropped = profile.apply_answers(withheld, {"hosting.model": "hybrid"})
        self.assertEqual((dropped, again), (["hosting.model"], withheld))

    def test_a_profile_without_the_fact_is_refused(self):
        with self.assertRaises(profile.ProfileError):
            profile.apply_answers(written_by_0_2_0(base.build()), {"hosting.model": "hybrid"})

    def test_multi_is_an_object_of_the_chosen_values_in_catalogue_order(self):
        built, _ = answered({"observability.tools": ["datadog", "other", "prometheus"]})
        value = facts_of(built)["observability.tools"]["value"]
        self.assertEqual(list(value.items()), [("prometheus", True), ("datadog", True), ("other", True)])
        self.assertIn('"value": {\n        "prometheus": true,\n        "datadog": true,\n        "other": true\n      }', profile.to_json(built))
        built, _ = answered({"observability.tools": ["none"]})
        self.assertEqual(facts_of(built)["observability.tools"]["value"], {"none": True})
        self.assertIn("| observability.tools | none: yes | declared |  |  |", profile.render_markdown(built))

    def test_a_declared_fact_can_be_withheld(self):
        built, _ = answered({"hosting.model": "hybrid", "database.backups": "none"})
        built = profile.apply_withholding(built, ["hosting.model"])
        facts = facts_of(built)
        self.assertEqual(facts["hosting.model"], {"id": "hosting.model", "value": None, "provenance": "withheld", "source": None, "detail": ""})
        self.assertEqual(facts["database.backups"]["value"], "none")
        self.assertNotIn("hybrid", profile.to_json(built))

    def test_a_declared_value_is_shown_as_declared(self):
        built, _ = answered({"hosting.model": "hybrid", "worker.autoscaling": "no"})
        markdown = profile.render_markdown(profile.validate(built))
        self.assertIn("| hosting.model | hybrid | declared |  |  |", markdown)
        self.assertIn("| worker.autoscaling | no | declared |  |  |", markdown)
        self.assertIn("| database.backups |  | unknown |  | not answered |", markdown)


class CompatibilityTest(unittest.TestCase):
    def test_a_file_with_every_question_answered_opens_in_0_2_0(self):
        for snapshot in (Snapshot(), base.leaky_snapshot()):
            built, _ = answered(EVERY_ANSWER, snapshot)
            self.assertEqual({facts_of(built)[fact_id]["provenance"] for fact_id in NEW_IDS}, {"declared"})
            for candidate in (built, profile.apply_withholding(built, NEW_IDS), base.build(snapshot)):
                data = json.loads(profile.to_json(candidate))
                self.assertIs(validate_0_2_0.validate(data), data)
                self.assertEqual(profile.validate(data), data)

    def test_the_copy_of_the_0_2_0_rules_still_refuses(self):
        # ... so the test above does not pass because the copy accepts anything.
        built, _ = answered(EVERY_ANSWER)
        as_list = json.loads(profile.to_json(built))
        next(f for f in as_list["facts"] if f["id"] == "observability.tools")["value"] = ["prometheus", "grafana"]
        newer = {**built, "format_version": 2}
        incomplete = {**built, "facts": [f for f in built["facts"] if f["id"] != "n8n.version"]}
        for bad in (as_list, newer, incomplete):
            with self.assertRaises(validate_0_2_0.ProfileError):
                validate_0_2_0.validate(bad)

    def test_a_0_2_0_file_still_opens_and_gets_a_sheet(self):
        old = json.loads(profile.to_json(written_by_0_2_0(base.build(Snapshot(env={"EXECUTIONS_MODE": "queue"})))))
        self.assertEqual(len(old["facts"]), 32)
        self.assertEqual(profile.validate(old), old)
        self.assertIn("| executions.mode | queue | measured | env |  |", profile.render_markdown(old))
        sheet = profile.render_questions(old)
        operator_part = sheet.split("## 2. ")[1].split("## 3. ")[0]
        for question in questions.OPERATOR:  # all fifteen are open questions
            self.assertIn(questions.line(question), operator_part)


class ZeroLeakTest(unittest.TestCase):
    """The zero-leak test of test_profile.py, with every question answered."""

    def assert_clean(self, text):
        self.assertNotIn(base.MARK, text.lower())
        for ip in base.SEEDED_IPS + ("471147",):
            self.assertNotIn(ip, text)
        for fragment in ("://", "@", "\\", "<redacted>", "<valueFrom>"):
            self.assertNotIn(fragment, text)
        self.assertIsNone(re.search(r"/(?!100\b)", text.replace("n/a", "")), "a slash outside a score")

    def test_nothing_seeded_reaches_a_profile_with_every_question_answered(self):
        for with_env in (True, False):
            snapshot = base.leaky_snapshot()
            if not with_env:
                snapshot.env = None
            findings = engine.run(snapshot, ALL_RULES)
            plain = profile.build_profile(snapshot, findings, failed=("redis",))
            built, dropped = profile.apply_answers(plain, EVERY_ANSWER)
            self.assertEqual(dropped, ["orchestrator"])  # Kubernetes was read
            for text in (profile.to_json(built), profile.render_markdown(built), profile.render_questions(built), profile.render_questions(plain)):
                self.assert_clean(text)
            # The only new text in the file is values of the catalogue.
            before, after = facts_of(plain), facts_of(built)
            for fact_id in profile.FACT_IDS:
                if fact_id not in NEW_IDS:
                    self.assertEqual(after[fact_id], before[fact_id])
                    continue
                question = questions.OPERATOR_BY_ID[fact_id]
                value = after[fact_id]["value"]
                allowed = [v for v, _ in question["values"]]
                if question["kind"] == "yes_no":
                    self.assertIs(value, True)
                elif question["kind"] == "multi":
                    self.assertEqual(value, {v: True for v in allowed[:2]})
                else:
                    self.assertEqual(value, allowed[0])
                self.assertEqual((after[fact_id]["source"], after[fact_id]["detail"]), (None, ""))
            self.assertEqual({k: built[k] for k in built if k != "facts"}, {k: plain[k] for k in plain if k != "facts"})

    def test_the_catalogue_itself_is_clean(self):
        # Every sentence, label and reason the sheet or the list can print.
        self.assert_clean(questions.render_list())
        self.assert_clean(" ".join(questions.MEASURED.values()))
        self.assert_clean(profile.render_questions(base.build()))


class SheetTest(unittest.TestCase):
    def sheet_of(self, built):
        sheet = profile.render_questions(profile.validate(built))
        head, rest = sheet.split("## 1. Could be measured\n")
        measurable, rest = rest.split("## 2. Ask whoever runs the platform\n")
        operator, conversation = rest.split("## 3. Ask in conversation\n")
        return sheet, head, measurable, operator, conversation

    def test_the_three_parts(self):
        snapshot = Snapshot(env={"EXECUTIONS_MODE": "queue"}, redis={"version": "7.2.4"})
        built, _ = answered({"hosting.model": "hybrid"}, snapshot, reason="incident", reference="ticket 42", failed=("postgres",))
        built = profile.apply_withholding(built, ["worker.replicas", "proxy.kind"])
        text = json.dumps(built)
        sheet, head, measurable, operator, conversation = self.sheet_of(built)
        self.assertEqual(json.dumps(built), text)  # the sheet changes nothing in the profile
        self.assertTrue(sheet.endswith("\n") and not sheet.endswith("\n\n"))
        self.assertEqual(
            head.splitlines()[:5],
            [
                "# n8n environment profile: questions still open",
                "",
                f"- **Taken:** {built['created_at']}",
                "- **Reason:** incident",
                "- **Reference:** ticket 42",
            ],
        )
        # 1: what a probe can fill, with the profile's own detail.
        self.assertIn("- **Which Postgres version runs?** (`database.version`): Postgres could not be collected\n", measurable)
        self.assertIn("- **How many main replicas run?** (`main.replicas`): Kubernetes was not provided\n", measurable)
        self.assertNotIn("executions.mode", sheet)  # measured
        self.assertNotIn("queue.redis_version", sheet)  # measured
        # 2: operator questions still unknown, with their values; orchestrator here, once.
        self.assertEqual(sheet.count("`orchestrator`"), 1)
        self.assertIn(
            "- **What runs the n8n containers or processes?** (`orchestrator`): one of `kubernetes` (Kubernetes), `ecs` (Amazon ECS), ",
            operator,
        )
        self.assertIn("- **Do workers scale automatically?** (`worker.autoscaling`): `yes` or `no`\n", operator)
        self.assertIn("(`observability.tools`): one or more of `prometheus` (Prometheus), ", operator)
        self.assertNotIn("not answered", sheet)
        self.assertNotIn("hosting.model", sheet)  # answered
        self.assertEqual(operator.count("\n- **"), 15 - 2)  # one answered, one withheld
        # 3: the whole conversation group, always.
        for question, why in questions.CONVERSATION:
            self.assertIn(f"- **{question}** {why}\n", conversation)
        # Withheld facts: one line at the end, not questions.
        self.assertTrue(sheet.endswith("\nWithheld by the sender, not questions to press on: worker.replicas, proxy.kind.\n"))
        self.assertEqual(sheet.count("worker.replicas"), 1)
        self.assertEqual(sheet.count("proxy.kind"), 1)

    def test_a_profile_with_nothing_unknown(self):
        built, _ = answered(EVERY_ANSWER)
        for fact in built["facts"]:
            if fact["provenance"] == "unknown":
                fact.update(value=1, provenance="measured", source="env", detail="")
        sheet, _, measurable, operator, conversation = self.sheet_of(built)
        self.assertEqual(measurable.strip().splitlines()[-1], "Nothing is open here.")
        self.assertEqual(operator.strip().splitlines()[-1], "Nothing is open here.")
        self.assertNotIn("- **", measurable + operator)
        self.assertEqual(conversation.count("- **"), 10)
        self.assertNotIn("Withheld", sheet)

    def test_nothing_collected_lists_everything(self):
        _, _, measurable, operator, _ = self.sheet_of(base.build())
        self.assertEqual(measurable.count("\n- **"), 31)
        self.assertEqual(operator.count("\n- **"), 15)

    def test_text_from_a_file_cannot_become_markup_or_reach_the_terminal(self):
        hostile = "x\x1b[2J<b>`|[link](http)"
        data = base.build(reference="ref <i>")
        facts_of(data)["database.version"]["detail"] = hostile
        data["facts"].append({"id": hostile, "value": None, "provenance": "withheld", "source": None, "detail": ""})
        data["facts"].append({"id": "later.fact", "value": None, "provenance": "unknown", "source": None, "detail": hostile})
        sheet = profile.render_questions(profile.validate(data))
        self.assertNotIn("\x1b", sheet)
        self.assertIsNone(re.search(r"(?<!\\)[<|\[]", sheet))
        self.assertIn(r"- **Reference:** ref \<i\>", sheet)
        self.assertIn(r"(`database.version`): x \[2J\<b\>\`\|\[link\](http)", sheet)
        self.assertNotIn("later.fact", sheet)  # an id this version has no question for is not asked

    def test_a_repeated_id_is_read_once_as_its_first_entry(self):
        data = base.build()
        before = profile.render_questions(data)
        data["facts"].append({"id": "hosting.model", "value": "hybrid", "provenance": "declared", "source": None, "detail": ""})
        self.assertEqual(profile.render_questions(data), before)
        self.assertIn("(`hosting.model`)", before)


SOURCES = ["--url", "https://n8n.example.com", "--env-file", "n8n.env"]
COLLECTED = Snapshot(health={"healthz": True, "readiness": True, "metrics_disabled": True}, env={"EXECUTIONS_MODE": "queue"})


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "n8n-profile.json")

    def run_cli(self, argv, snapshot=COLLECTED, rules=()):
        """(exit code, stdout, stderr). `snapshot=None`: collecting anything fails the test."""
        out, err = io.StringIO(), io.StringIO()
        collect = {"return_value": snapshot} if snapshot else {"side_effect": AssertionError("must not collect")}
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", list(rules)):
            with mock.patch("n8n_healthcheck.collectors.collect", **collect):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = cli.main(argv)
                    except SystemExit as exit_:  # a usage error
                        code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def saved_facts(self):
        with open(self.path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(validate_0_2_0.validate(saved), saved)
        return facts_of(saved)

    def save(self, *answers):
        argv = SOURCES + ["--save-profile", self.path]
        for answer in answers:
            argv += ["--answer", answer]
        return argv

    def test_list_questions(self):
        code, out, err = self.run_cli(["--list-questions"], snapshot=None)
        self.assertEqual((code, err, out), (0, "", questions.render_list()))
        for question in questions.OPERATOR:
            self.assertIn(f"(`{question['id']}`)", out)
        self.assertEqual(out.count("\n- **"), 15)
        self.assertNotIn(questions.CONVERSATION[0][0], out)
        for extra in (["--url", "https://n8n.example.com"], ["-o", "list.md"], ["--answer", "hosting.model=hybrid"], ["--show-profile", "p.json"]):
            code, out, err = self.run_cli(["--list-questions"] + extra, snapshot=None)
            self.assertEqual((code, out), (3, ""))
            self.assertIn("takes no other flag", err)

    def test_answers_are_saved_as_declared_facts(self):
        plain_code, plain_out, _ = self.run_cli(SOURCES, rules=[base.warning_rule])
        argv = self.save("hosting.model=public_cloud", "observability.tools=grafana, prometheus", "worker.autoscaling=no", " proxy.kind = none ")
        code, out, err = self.run_cli(argv, rules=[base.warning_rule])
        self.assertEqual((code, out, err), (plain_code, plain_out, ""))  # report and exit code unchanged
        facts = self.saved_facts()
        self.assertEqual(facts["hosting.model"], {"id": "hosting.model", "value": "public_cloud", "provenance": "declared", "source": None, "detail": ""})
        self.assertEqual(list(facts["observability.tools"]["value"].items()), [("prometheus", True), ("grafana", True)])
        self.assertIs(facts["worker.autoscaling"]["value"], False)
        self.assertEqual(facts["proxy.kind"]["value"], "none")
        self.assertEqual(facts["database.backups"]["detail"], "not answered")
        self.assertEqual(facts["executions.mode"]["provenance"], "measured")

    def test_an_answer_for_a_measured_fact_is_dropped_with_one_line(self):
        snapshot = Snapshot(kubernetes=base.kube(("m", 1, base.container())))
        argv = ["--kube-namespace", "n8n", "--save-profile", self.path, "--answer", "orchestrator=ecs", "--answer", "hosting.model=hybrid"]
        plain_code = self.run_cli(argv[:2], snapshot, [base.warning_rule])[0]
        code, _, err = self.run_cli(argv, snapshot, [base.warning_rule])
        self.assertEqual(code, plain_code)  # not an error
        self.assertEqual(err, "note: the answer to orchestrator was dropped: the tool measured it\n")
        facts = self.saved_facts()
        self.assertEqual((facts["orchestrator"]["value"], facts["orchestrator"]["provenance"]), ("kubernetes", "measured"))
        self.assertEqual(facts["hosting.model"]["provenance"], "declared")
        # Without Kubernetes the same answer is taken, silently.
        code, _, err = self.run_cli(self.save("orchestrator=ecs"))
        self.assertEqual(err, "")
        self.assertEqual(self.saved_facts()["orchestrator"]["value"], "ecs")

    def test_a_declared_fact_can_be_withheld(self):
        code, _, err = self.run_cli(self.save("hosting.model=hybrid") + ["--withhold", "hosting.model", "--withhold", "database.backups"])
        self.assertEqual((code, err), (0, ""))
        facts = self.saved_facts()
        self.assertEqual(facts["hosting.model"], {"id": "hosting.model", "value": None, "provenance": "withheld", "source": None, "detail": ""})
        self.assertEqual(facts["database.backups"]["provenance"], "withheld")

    def test_usage_errors_exit_3_before_anything_is_collected(self):
        for argv, message in (
            (self.save("hosting.region=eu"), "--answer: no question the tool asks has the id 'hosting.region' (see --list-questions)"),
            (self.save("n8n.version=2.40.1"), "no question the tool asks has the id 'n8n.version'"),
            (self.save(questions.CONVERSATION[1][0] + "=yes"), "no question the tool asks has the id"),
            (self.save("hosting.model=moon"), "--answer: the answer to hosting.model must be one of: public_cloud, private_cloud, on_premises, hybrid"),
            (self.save("hosting.model=public_cloud,hybrid"), "the answer to hosting.model must be one of"),
            (self.save("hosting.model="), "the answer to hosting.model must be one of"),
            (self.save("worker.autoscaling=true"), "the answer to worker.autoscaling must be one of: yes, no"),
            (self.save("observability.tools=prometheus,zabbix"), "the answer to observability.tools must be one or more of"),
            (self.save("observability.tools="), "must be one or more of"),
            (self.save("observability.tools=none,grafana"), 'cannot be "none" together with another value'),
            (self.save("hosting.model=hybrid", "hosting.model=hybrid"), "--answer: 'hosting.model' is answered twice"),
            (self.save("hosting.model=hybrid", "hosting.model=on_premises"), "is answered twice"),
            (self.save("hosting.model"), "--answer: 'hosting.model' is not ID=VALUE"),
            (SOURCES + ["--answer", "hosting.model=hybrid"], "--answer needs --save-profile"),
            (["--snapshot", "s.json", "--save-profile", self.path, "--answer", "hosting.model=hybrid"], "cannot be combined with --snapshot"),
        ):
            with self.subTest(argv=argv[len(SOURCES) :]):
                report_path = os.path.join(self.dir, "report.md")
                code, out, err = self.run_cli(argv + ["-o", report_path], snapshot=None)
                self.assertEqual((code, out), (3, ""))
                self.assertIn(message, err)
                self.assertFalse(os.path.exists(self.path))
                self.assertFalse(os.path.exists(report_path))

    def test_show_questions(self):
        self.run_cli(self.save("hosting.model=hybrid") + ["--reference", "ticket 42", "--withhold", "proxy.kind"], rules=[base.warning_rule])
        with open(self.path, encoding="utf-8") as fh:
            expected = profile.render_questions(json.load(fh))
        code, out, err = self.run_cli(["--show-questions", self.path], snapshot=None)
        self.assertEqual((code, err, out), (0, "", expected))  # 0 although the run it records had a warning
        self.assertTrue(out.startswith("# n8n environment profile: questions still open\n"))
        self.assertIn("- **Reference:** ticket 42", out)
        self.assertIn("## 3. Ask in conversation", out)
        self.assertTrue(out.endswith("Withheld by the sender, not questions to press on: proxy.kind.\n"))
        output = os.path.join(self.dir, "sheet.md")
        code, out, _ = self.run_cli(["--show-questions", self.path, "-o", output], snapshot=None)
        self.assertEqual((code, out), (0, ""))
        with open(output, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), expected)

    def test_show_questions_of_a_0_2_0_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(profile.to_json(written_by_0_2_0(base.build())))
        code, out, err = self.run_cli(["--show-questions", self.path], snapshot=None)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.split("## 2. ")[1].split("## 3. ")[0].count("\n- **"), 15)

    def test_show_questions_takes_no_other_flag(self):
        self.run_cli(self.save())
        for extra in (["--url", "https://n8n.example.com"], ["--show-profile", self.path], ["--save-profile", "p.json"],
                      ["--answer", "hosting.model=hybrid"], ["--list-questions"], ["--withhold", "check"]):  # fmt: skip
            with self.subTest(extra=extra):
                code, out, err = self.run_cli(["--show-questions", self.path] + extra, snapshot=None)
                self.assertEqual((code, out), (3, ""))
                self.assertIn("takes no other flag except -o", err)

    def test_show_questions_of_a_bad_file_exits_3_without_a_traceback(self):
        code, out, err = self.run_cli(["--show-questions", os.path.join(self.dir, "missing.json")], snapshot=None)
        self.assertEqual((code, out), (3, ""))
        self.assertIn("cannot read profile", err)
        good = base.build()
        hostile = json.loads(json.dumps(good))
        hostile["facts"][2].update(id="x\x1b[2J<b>pwned", provenance="guessed")
        for content, message in (
            ("not json", "not a valid profile"),
            (json.dumps({"format": "other"}), "not a valid profile"),
            (json.dumps({**good, "format_version": 9}), "newer than this tool reads"),
            (json.dumps({**good, "facts": good["facts"][1:]}), "a fact of the catalogue is missing"),
            (json.dumps(hostile), "not a valid profile: provenance of fact 3"),
            ("[" * 200_000, "not a valid profile"),
            ("null", "not a valid profile"),
        ):
            with self.subTest(content=content[:30]):
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                code, out, err = self.run_cli(["--show-questions", self.path], snapshot=None)
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(len(err.splitlines()), 1, err[:300])
                self.assertTrue(err.startswith("error: cannot show profile"))
                self.assertIn(message, err)
                self.assertNotIn("pwned", err)

    def test_show_questions_survives_a_renderer_that_fails(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(profile.to_json(base.build()))
        with mock.patch("n8n_healthcheck.profile.render_questions", side_effect=KeyError("detail")):
            code, out, err = self.run_cli(["--show-questions", self.path], snapshot=None)
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(len(err.splitlines()), 1)

    def test_abbreviations(self):
        # What a prefix meant before these flags, it still means.
        self.assertEqual(cli._parse_args(["--sh", "p.json"]).show_profile, "p.json")
        self.assertEqual(cli._parse_args(["--show", "p.json"]).show_profile, "p.json")
        self.assertEqual(cli._parse_args(["--show-", "p.json"]).show_profile, "p.json")
        args = cli._parse_args(["--url", "https://n8n.example.com", "--save", "s.json", "--re", "redis://cache.example:6379"])
        self.assertEqual((args.save_snapshot, args.redis_url, args.save_profile), ("s.json", "redis://cache.example:6379", None))
        # The new flags can be shortened where nothing older starts the same way.
        self.assertEqual(cli._parse_args(["--show-q", "p.json"]).show_questions, "p.json")
        self.assertTrue(cli._parse_args(["--l"]).list_questions)
        args = cli._parse_args(SOURCES + ["--save-p", "p.json", "--a", "hosting.model=hybrid", "--ans", "proxy.kind=none"])
        self.assertEqual(args.answer, {"hosting.model": "hybrid", "proxy.kind": "none"})
        for argv in (["--s", "x"], ["--k", "x"]):  # what was ambiguous before still is
            code, _, err = self.run_cli(argv, snapshot=None)
            self.assertEqual(code, 3)
            self.assertIn("ambiguous option", err)

    def test_help_lists_the_question_flags(self):
        code, out, _ = self.run_cli(["--help"], snapshot=None)
        self.assertEqual(code, 0)
        for flag in ("--answer", "--list-questions", "--show-questions"):
            self.assertIn(flag, out)
        with open(os.path.join(os.path.dirname(base.DOCS), "..", "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        for flag in ("--answer", "--list-questions", "--show-questions"):
            self.assertIn(flag, readme)


if __name__ == "__main__":
    unittest.main()
