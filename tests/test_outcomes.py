"""Outcomes: every rule states one of four on every run, and never "passed" on what it did not read."""

import json
import unittest
from dataclasses import asdict

import test_debug_info as pasted  # the synthetic debug information
from n8n_healthcheck import engine
from n8n_healthcheck.models import (
    BASES,
    NOT_ASSESSED_REASONS,
    OUTCOMES,
    SOURCE_IDS,
    Finding,
    Outcome,
    Snapshot,
)
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.rules._helpers import NOT_APPLICABLE, PASSED, VALUE_FROM
from test_rules import QUEUE, container, healthy_kube, kube

RULES = {rule.info.rule_id: rule for rule in ALL_RULES}

FINDING = ("finding",)
READ = ("passed", "read")
DEFAULT = ("passed", "default")
NOT_APPLICABLE_ = ("not_applicable",)
NOT_STATED = ("not_assessed", "not_stated", ())
UNREADABLE = ("not_assessed", "value_unreadable", ())


def missing(*needs):
    return ("not_assessed", "source_missing", needs)


ENV_OR_KUBE = missing("env", "kubernetes")
ENV_KUBE_OR_TEXT = missing("env", "kubernetes", "debug_info")

REGULAR = {"EXECUTIONS_MODE": "regular"}
PRODUCTION = {"DB_TYPE": "postgresdb"}  # regular mode on Postgres: not a stock install
SECRET = VALUE_FROM
REDIS = {"maxmemory_policy": "noeviction", "maxmemory_bytes": 1000, "used_memory_bytes": 100, "waiting_jobs": 0, "active_jobs": 1}
TEXT = pasted.snapshot  # a debug information: queue mode on Postgres, 2 workers, one version, both security settings flagged


def main(env, **overrides):
    """Kubernetes with one main container."""
    return kube(("n8n-main", 1, container(env=env, **overrides)))


def with_workers(main_env, *worker_envs, **overrides):
    workers = [(f"worker-{i}", 1, container(**{"command": ["worker"], "env": env, **overrides})) for i, env in enumerate(worker_envs)]
    return kube(("n8n-main", 1, container(env=main_env)), *workers)


def text_without(*keys, **sections):
    """A debug information that does not state `keys`."""
    snapshot = TEXT(pasted.SECURE, **sections)
    for key in keys:
        snapshot.debug_info[key] = None
    return snapshot


# rule id -> [(what the snapshot shows, snapshot, expected outcome)]
CASES = {
    "SCAL-001": [
        ("nothing given", Snapshot(), ENV_KUBE_OR_TEXT),
        ("no limit on Postgres", Snapshot(env=dict(PRODUCTION)), FINDING),
        ("a limit of 0 in queue mode", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "0"}), FINDING),
        ("a limit", Snapshot(env={**PRODUCTION, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}), READ),
        ("queue mode", Snapshot(env=dict(QUEUE)), NOT_APPLICABLE_),
        ("a stock install", Snapshot(env={}), NOT_APPLICABLE_),
        ("a stock install with a limit", Snapshot(env={"N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}), NOT_APPLICABLE_),
        ("the mode in a Secret", Snapshot(kubernetes=main({"EXECUTIONS_MODE": SECRET, **PRODUCTION})), UNREADABLE),
        ("the mode in a file", Snapshot(env={**PRODUCTION, "EXECUTIONS_MODE_FILE": "x", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}), UNREADABLE),
        ("the database type in a Secret", Snapshot(kubernetes=main({"DB_TYPE": SECRET})), UNREADABLE),
        ("the limit in a Secret", Snapshot(kubernetes=main({**PRODUCTION, "N8N_CONCURRENCY_PRODUCTION_LIMIT": SECRET})), UNREADABLE),
        ("an environment read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("Kubernetes without a main container", Snapshot(kubernetes=kube()), NOT_STATED),
        ("mains that disagree", Snapshot(kubernetes=kube(("a", 1, container(env=QUEUE)), ("b", 1, container(env={})))), NOT_STATED),
        ("a text that does not state the mode", text_without("execution_mode"), NOT_STATED),
        ("a text in regular mode without a limit", TEXT(pasted.REGULAR), FINDING),
    ],
    "SCAL-002": [
        ("nothing given", Snapshot(), missing("kubernetes", "debug_info")),
        ("an env file in queue mode", Snapshot(env=dict(QUEUE)), missing("kubernetes", "debug_info")),
        ("an env file in regular mode", Snapshot(env=dict(REGULAR)), NOT_APPLICABLE_),
        ("one worker replica", Snapshot(kubernetes=kube(("w", 1, container(command=["worker"])))), FINDING),
        ("two worker replicas", Snapshot(kubernetes=healthy_kube()), READ),
        ("workers scaled to zero", Snapshot(kubernetes=kube(("w", 0, container(command=["worker"])))), NOT_STATED),
        ("no worker in Kubernetes, queue mode", Snapshot(kubernetes=main(QUEUE)), NOT_STATED),
        ("no worker in Kubernetes, regular mode", Snapshot(kubernetes=main({})), NOT_APPLICABLE_),
        ("a text with two workers", TEXT(), READ),
        ("a text with one worker", TEXT(pasted.ONE_WORKER), FINDING),
        ("a text without a cluster section", TEXT(base=pasted.NO_CLUSTER), NOT_STATED),
        ("a text with two workers that does not state the mode", text_without("execution_mode"), NOT_STATED),
        ("a text with two workers, the mode in a Secret", TEXT(kubernetes=main({"EXECUTIONS_MODE": SECRET})), UNREADABLE),
        ("a text in regular mode", TEXT(pasted.REGULAR), NOT_APPLICABLE_),
    ],
    "SCAL-003": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("a text alone", TEXT(), ENV_OR_KUBE),
        ("queue mode", Snapshot(env=dict(QUEUE)), FINDING),
        ("offloading on", Snapshot(env={**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"}), READ),
        ("regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("offloading in a Secret", Snapshot(kubernetes=main({**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": SECRET})), UNREADABLE),
        ("the mode read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("Kubernetes without a main container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "SCAL-004": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("an env file with a low limit", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"}), FINDING),
        ("an env file with a limit of 10", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}), READ),
        ("an env file in queue mode without a limit", Snapshot(env=dict(QUEUE)), missing("kubernetes")),
        ("an env file in regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("an env file with a limit that is no number", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "many"}, kubernetes=healthy_kube()), NOT_STATED),
        ("a worker with a low flag", Snapshot(kubernetes=with_workers(QUEUE, {}, command=["worker", "--concurrency=2"])), FINDING),
        ("workers with a flag of 10", Snapshot(kubernetes=with_workers(QUEUE, {}, {}, command=["worker", "--concurrency=10"])), READ),
        ("workers that set neither", Snapshot(kubernetes=with_workers(QUEUE, {}, {})), DEFAULT),
        ("a worker limit in a Secret", Snapshot(kubernetes=with_workers(QUEUE, {"N8N_CONCURRENCY_PRODUCTION_LIMIT": SECRET})), UNREADABLE),
        ("a worker read in part", Snapshot(kubernetes=with_workers(QUEUE, {}, env_complete=False)), UNREADABLE),
        ("a flag that is not a number", Snapshot(kubernetes=with_workers(QUEUE, {}, command=["worker", "--concurrency=$N"])), NOT_STATED),
        ("no worker, queue mode", Snapshot(kubernetes=main(QUEUE)), NOT_STATED),
        ("no worker, regular mode", Snapshot(kubernetes=main({})), NOT_APPLICABLE_),
    ],
    "SCAL-005": [
        ("nothing given", Snapshot(), missing("postgres")),
        ("95 of 100 in use", Snapshot(postgres={"connections": 95, "max_connections": 100}), FINDING),
        ("10 of 100 in use", Snapshot(postgres={"connections": 10, "max_connections": 100}), READ),
        ("no count", Snapshot(postgres={"max_connections": 100}), NOT_STATED),
    ],
    "REL-001": [
        ("nothing given", Snapshot(), ENV_KUBE_OR_TEXT),
        ("queue mode, type not set", Snapshot(env={"EXECUTIONS_MODE": "queue"}), FINDING),
        ("queue mode on Postgres", Snapshot(env=dict(QUEUE)), READ),
        ("regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("the type in a file", Snapshot(env={"EXECUTIONS_MODE": "queue", "DB_TYPE_FILE": "x"}), UNREADABLE),
        ("the type in a Secret", Snapshot(kubernetes=main({"EXECUTIONS_MODE": "queue", "DB_TYPE": SECRET})), UNREADABLE),
        ("a type n8n does not know", Snapshot(env={"EXECUTIONS_MODE": "queue", "DB_TYPE": "oracle"}), NOT_STATED),
        ("a text on Postgres", TEXT(), READ),
        ("a text on SQLite", TEXT(pasted.SQLITE), FINDING),
        ("a text that does not state the database", text_without("database"), NOT_STATED),
    ],
    "REL-002": [
        ("nothing given", Snapshot(), missing("redis")),
        ("no Redis, regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("no Redis, queue mode", Snapshot(env=dict(QUEUE)), missing("redis")),
        ("a policy that evicts", Snapshot(redis={**REDIS, "maxmemory_policy": "allkeys-lru"}), FINDING),
        ("noeviction", Snapshot(redis=dict(REDIS)), READ),
        ("no memory limit", Snapshot(redis={**REDIS, "maxmemory_policy": "allkeys-lru", "maxmemory_bytes": 0}), READ),
        ("no policy reported", Snapshot(redis={"maxmemory_bytes": 1000}), NOT_STATED),
        ("no limit reported", Snapshot(redis={"maxmemory_policy": "allkeys-lru"}), NOT_STATED),
    ],
    "REL-003": [
        ("nothing given", Snapshot(), missing("redis")),
        ("no Redis, regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("90% in use, noeviction", Snapshot(redis={**REDIS, "used_memory_bytes": 900}), FINDING),
        ("90% in use, a policy that evicts", Snapshot(redis={**REDIS, "used_memory_bytes": 900, "maxmemory_policy": "allkeys-lru"}), FINDING),
        ("10% in use", Snapshot(redis=dict(REDIS)), READ),
        ("no memory limit", Snapshot(redis={**REDIS, "maxmemory_bytes": 0}), NOT_APPLICABLE_),
        ("no memory reported", Snapshot(redis={"maxmemory_bytes": 1000}), NOT_STATED),
    ],
    "REL-004": [
        ("nothing given", Snapshot(), missing("redis")),
        ("no Redis, regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("50 waiting, none active", Snapshot(redis={**REDIS, "waiting_jobs": 50, "active_jobs": 0}), FINDING),
        ("500 waiting", Snapshot(redis={**REDIS, "waiting_jobs": 500, "active_jobs": 10}), FINDING),
        ("a queue that moves", Snapshot(redis=dict(REDIS)), READ),
        ("no queue key", Snapshot(redis={"waiting_jobs": None, "active_jobs": None}), NOT_STATED),
    ],
    "REL-005": [
        ("nothing given", Snapshot(), missing("url")),
        ("not ready", Snapshot(health={"healthz": True, "readiness": False}), FINDING),
        ("ready", Snapshot(health={"healthz": True, "readiness": True}), READ),
        ("no answer", Snapshot(health={"healthz": True}), NOT_STATED),
    ],
    "REL-006": [
        ("nothing given", Snapshot(), ENV_KUBE_OR_TEXT),
        ("queue mode", Snapshot(env=dict(QUEUE)), FINDING),
        ("multi-main", Snapshot(env={**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": "true"}), READ),
        ("regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("multi-main in a Secret", Snapshot(kubernetes=main({**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": SECRET})), UNREADABLE),
        ("a text with multi-main", TEXT(pasted.MULTI_MAIN), READ),
        ("a text that does not state the mode", text_without("execution_mode"), NOT_STATED),
    ],
    "REL-007": [
        ("nothing given", Snapshot(), missing("kubernetes")),
        ("a container without probes", Snapshot(kubernetes=main({}, liveness_probe=False)), FINDING),
        ("probes everywhere", Snapshot(kubernetes=healthy_kube()), READ),
        ("no n8n container", Snapshot(kubernetes=kube(("redis", 1, container(image="redis:7")))), NOT_STATED),
    ],
    "REL-008": [
        ("nothing given", Snapshot(), missing("kubernetes")),
        ("a container without a memory limit", Snapshot(kubernetes=main({}, limits={})), FINDING),
        ("resources everywhere", Snapshot(kubernetes=healthy_kube()), READ),
        ("no n8n container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "REL-009": [
        ("nothing given", Snapshot(), missing("debug_info")),
        ("a failed check", TEXT(pasted.FAILED_CHECK), FINDING),
        ("every check succeeded", TEXT(), READ),
        ("no cluster section", TEXT(base=pasted.NO_CLUSTER), NOT_STATED),
    ],
    "PERF-001": [
        ("nothing given", Snapshot(), ENV_KUBE_OR_TEXT),
        ("pruning off", Snapshot(env={"EXECUTIONS_DATA_PRUNE": "false"}), FINDING),
        ("pruning on", Snapshot(env={"EXECUTIONS_DATA_PRUNE": "true"}), READ),
        ("not set", Snapshot(env={}), DEFAULT),
        ("in a file", Snapshot(env={"EXECUTIONS_DATA_PRUNE_FILE": "x"}), UNREADABLE),
        ("in a Secret", Snapshot(kubernetes=main({"EXECUTIONS_DATA_PRUNE": SECRET})), UNREADABLE),
        ("read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("not a boolean", Snapshot(env={"EXECUTIONS_DATA_PRUNE": "sometimes"}), NOT_STATED),
        ("a text with pruning on", TEXT(), READ),
        ("a text with pruning off", TEXT(pasted.PRUNING_OFF), FINDING),
        ("a text that does not state it", text_without("pruning"), NOT_STATED),
    ],
    "PERF-002": [
        ("nothing given", Snapshot(), ENV_KUBE_OR_TEXT),
        ("saved in the env file", Snapshot(env={"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"}), FINDING),
        ("not saved in the env file", Snapshot(env={"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false"}), READ),
        ("not set in the env file", Snapshot(env=dict(QUEUE)), DEFAULT),
        ("saved on a worker", Snapshot(kubernetes=with_workers(QUEUE, {}, {"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"})), FINDING),
        ("not set on any worker", Snapshot(kubernetes=with_workers(QUEUE, {}, {})), DEFAULT),
        ("set to false on every worker", Snapshot(kubernetes=with_workers(QUEUE, {"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false"})), READ),
        ("in a Secret on a worker", Snapshot(kubernetes=with_workers(QUEUE, {}, {"EXECUTIONS_DATA_SAVE_ON_PROGRESS": SECRET})), UNREADABLE),
        ("queue mode with no worker", Snapshot(kubernetes=main(QUEUE)), NOT_STATED),
        ("the mode in a Secret", Snapshot(kubernetes=with_workers({"EXECUTIONS_MODE": SECRET}, {})), UNREADABLE),
        ("regular mode in Kubernetes", Snapshot(kubernetes=main({})), DEFAULT),
        ("a text in queue mode", TEXT(), NOT_STATED),  # the text describes main, the workers are judged
        ("a text in regular mode", TEXT(pasted.REGULAR), READ),
    ],
    "PERF-003": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("a text alone", TEXT(), ENV_OR_KUBE),
        ("kept in memory", Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "default"}), FINDING),
        ("kept in memory, queue mode", Snapshot(env={**QUEUE, "N8N_DEFAULT_BINARY_DATA_MODE": "default"}), FINDING),
        ("kept in memory, mode in a Secret", Snapshot(kubernetes=main({"EXECUTIONS_MODE": SECRET, "N8N_DEFAULT_BINARY_DATA_MODE": "default"})), FINDING),
        ("on the filesystem", Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "filesystem"}), READ),
        ("not set", Snapshot(env={}), NOT_STATED),  # what n8n then uses depends on its version
        ("a word n8n does not know", Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "tape"}), NOT_STATED),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_DEFAULT_BINARY_DATA_MODE": SECRET})), UNREADABLE),
    ],
    "MAINT-001": [
        ("nothing given", Snapshot(), missing("env", "kubernetes", "url")),
        ("a URL whose metrics are off", Snapshot(health={"healthz": True, "metrics_disabled": True}), ENV_OR_KUBE),
        ("metrics scraped, nothing else", Snapshot(metrics={"n8n_version_info": 1}), READ),
        ("not set", Snapshot(env={}), FINDING),
        ("not set, queue mode", Snapshot(env=dict(QUEUE)), FINDING),
        ("set", Snapshot(env={"N8N_METRICS": "true"}), READ),
        ("not set but scraped", Snapshot(env={}, metrics={"n8n_version_info": 1}), READ),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_METRICS": SECRET})), UNREADABLE),
        ("Kubernetes without a main container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "MAINT-002": [
        ("nothing given", Snapshot(), missing("kubernetes")),
        ("latest", Snapshot(kubernetes=main({}, image="n8nio/n8n:latest")), FINDING),
        ("a version", Snapshot(kubernetes=healthy_kube()), READ),
        ("no n8n container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "MAINT-003": [
        ("nothing given", Snapshot(), missing("kubernetes", "debug_info")),
        ("two versions", Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(image="n8nio/n8n:2.39.0", command=["worker"])))), FINDING),
        ("one version everywhere", Snapshot(kubernetes=healthy_kube()), READ),
        ("one image pinned by digest", Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(image="n8nio/n8n@sha256:abc", command=["worker"])))), NOT_STATED),
        ("no tag names a version", Snapshot(kubernetes=main({}, image="n8nio/n8n:latest")), NOT_STATED),
        ("a text with one version", TEXT(), READ),
        ("a text with two versions", TEXT(pasted.TWO_VERSIONS), FINDING),
        ("a text without a cluster section", TEXT(base=pasted.NO_CLUSTER), NOT_STATED),
    ],
    "MAINT-004": [
        ("nothing given", Snapshot(), missing("debug_info")),
        ("a sandbox license", TEXT(pasted.SANDBOX), FINDING),
        ("a production license", TEXT(), READ),
        ("no license stated", text_without("license"), NOT_STATED),
    ],
    "SEC-001": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("set to false", Snapshot(env={"N8N_SECURE_COOKIE": "false"}), FINDING),
        ("set to true", Snapshot(env={"N8N_SECURE_COOKIE": "true"}), READ),
        ("not set", Snapshot(env={}), DEFAULT),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_SECURE_COOKIE": SECRET})), UNREADABLE),
        ("read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("a text that flags it", TEXT(), FINDING),
        # The text reports it only when it is insecure: its silence is never a pass.
        ("a text that does not flag it", TEXT(pasted.SECURE), NOT_STATED),
    ],
    "SEC-002": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("not set", Snapshot(env={}), FINDING),
        ("external", Snapshot(env={"N8N_RUNNERS_MODE": "external"}), READ),
        ("external on main, internal on a worker", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {})), FINDING),
        ("external on main and on every worker", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {"N8N_RUNNERS_MODE": "external"})), READ),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_RUNNERS_MODE": SECRET})), UNREADABLE),
        ("the mode in a Secret", Snapshot(kubernetes=main({"EXECUTIONS_MODE": SECRET, "N8N_RUNNERS_MODE": "external"})), UNREADABLE),
        ("offloading in a Secret, workers external", Snapshot(kubernetes=with_workers({**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": SECRET}, {"N8N_RUNNERS_MODE": "external"})), UNREADABLE),
        ("main offloads and no worker is found", Snapshot(kubernetes=main({**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"})), NOT_STATED),
    ],
    "SEC-003": [
        ("nothing given", Snapshot(), missing("debug_info")),
        ("an env file that says false, no text", Snapshot(env={"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "false"}), missing("debug_info")),
        ("an env file that says true, no text", Snapshot(env={"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true"}), READ),
        ("not set, no text", Snapshot(env={}), missing("debug_info")),  # never on the default
        ("a text that flags it", TEXT(), FINDING),
        ("a text that does not flag it", TEXT(pasted.SECURE), NOT_STATED),
        ("a text that flags it, a probe that says true", TEXT(env={"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true"}), READ),
        ("a text that flags it, the variable in a Secret", TEXT(kubernetes=main({"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": SECRET})), UNREADABLE),
    ],
}


def stated(check):
    """A Check as the tuples above write an outcome."""
    if check.outcome == "passed":
        return (check.outcome, check.basis)
    if check.outcome == "not_assessed":
        return (check.outcome, check.reason, tuple(check.needs))
    return (check.outcome,)


class RuleOutcomeTest(unittest.TestCase):
    def test_every_rule_has_cases(self):
        self.assertEqual(list(CASES), [rule.info.rule_id for rule in ALL_RULES])

    def test_each_outcome_of_each_rule(self):
        for rule_id, cases in CASES.items():
            for label, snapshot, expected in cases:
                with self.subTest(rule=rule_id, case=label):
                    (check,) = engine.assess(snapshot, [RULES[rule_id]])
                    self.assertEqual(stated(check), expected)
                    # The rule called alone keeps its old contract: a Finding, or None.
                    alone = RULES[rule_id](snapshot)
                    self.assertEqual(alone, check.finding)
                    self.assertEqual(alone is not None, expected == FINDING)

    def test_the_cases_cover_what_every_rule_can_state(self):
        for rule_id, cases in CASES.items():
            seen = {expected[0] for _, _, expected in cases}
            with self.subTest(rule=rule_id):
                # Every rule can find, pass and be left without its source.
                self.assertLessEqual({"finding", "passed", "not_assessed"}, seen)
                self.assertIn("source_missing", {expected[1] for _, _, expected in cases if expected[0] == "not_assessed"})
        variants = {rule_id: set() for rule_id in CASES}
        for rule_id, cases in CASES.items():
            for _, snapshot, expected in cases:
                if expected == FINDING:
                    found = RULES[rule_id](snapshot)
                    variants[rule_id].add((found.title, found.impact, found.recommendation))
        for rule in ALL_RULES:
            with self.subTest(rule=rule.info.rule_id):
                declared = {(v.title, v.impact, v.remedy) for v in rule.info.variants}
                self.assertEqual(variants[rule.info.rule_id], declared)  # each variant is raised by a case

    def test_nothing_collected_passes_nothing(self):
        outcomes = [stated(check) for check in engine.assess(Snapshot(), ALL_RULES)]
        self.assertEqual({outcome[:2] for outcome in outcomes}, {("not_assessed", "source_missing")})
        for outcome in outcomes:
            self.assertTrue(outcome[2] and set(outcome[2]) <= set(SOURCE_IDS))

    def test_a_default_is_a_pass_only_in_an_environment_read_in_full(self):
        # The same rules, the same variables not set: read in full, then in part.
        full = {rule.info.rule_id: stated(check) for rule, check in zip(ALL_RULES, engine.assess(Snapshot(kubernetes=main({})), ALL_RULES))}
        part = engine.assess(Snapshot(kubernetes=main({}, env_complete=False)), ALL_RULES)
        by_default = [rule_id for rule_id, outcome in full.items() if outcome == DEFAULT]
        self.assertEqual(by_default, ["PERF-001", "PERF-002", "SEC-001"])
        for rule, check in zip(ALL_RULES, part):
            if rule.info.rule_id in by_default:
                self.assertEqual(stated(check), UNREADABLE, rule.info.rule_id)
            self.assertNotEqual(check.basis, "default", rule.info.rule_id)


class DeclarationTest(unittest.TestCase):
    def test_every_rule_declares_its_fixed_texts(self):
        ids = [rule.info.rule_id for rule in ALL_RULES]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        for rule in ALL_RULES:
            info = rule.info
            with self.subTest(rule=info.rule_id):
                self.assertTrue(info.subject and info.passed.endswith("."))
                self.assertFalse(info.subject.endswith("."))
                self.assertTrue(info.variants)
                self.assertEqual(len(set(info.variants)), len(info.variants))
                for variant in info.variants:
                    self.assertTrue(variant.title and variant.impact and variant.remedy)
                self.assertLessEqual(set(info.scopes), {"instance", "main", "worker"})
                self.assertIs(rule.assess.__name__, rule.__name__)

    def test_what_a_rule_rests_on_exists_in_the_profile(self):
        from n8n_healthcheck import n8n_settings, profile

        for rule in ALL_RULES:
            for fact_id in rule.info.facts:
                self.assertIn(fact_id, profile.FACT_IDS, rule.info.rule_id)
            for name in rule.info.settings:
                self.assertIn(name, n8n_settings.KNOWN, rule.info.rule_id)


class AssessTest(unittest.TestCase):
    def test_one_check_per_rule_in_rule_order_and_run_gives_the_same_findings(self):
        for snapshot in (Snapshot(), Snapshot(env=dict(QUEUE), kubernetes=healthy_kube()), pasted.snapshot()):
            checks = engine.assess(snapshot, ALL_RULES)
            self.assertEqual([check.info for check in checks], [rule.info for rule in ALL_RULES])
            for check in checks:
                self.assertIn(check.outcome, OUTCOMES)
                self.assertEqual(check.outcome == "finding", isinstance(check.finding, Finding))
                self.assertEqual(check.outcome == "passed", check.basis in BASES)
                self.assertEqual(check.outcome == "not_assessed", check.reason in NOT_ASSESSED_REASONS)
            self.assertEqual(engine.findings_of(checks), engine.run(snapshot, ALL_RULES))

    def test_a_rule_that_raises_is_not_assessed_and_keeps_its_place(self):
        def broken(snapshot):
            raise KeyError("version")

        broken.info = RULES["REL-005"].info
        checks = engine.assess(Snapshot(), [RULES["SCAL-005"], broken, RULES["MAINT-004"]])
        self.assertEqual([check.info.rule_id for check in checks], ["SCAL-005", "REL-005", "MAINT-004"])
        self.assertEqual((checks[1].outcome, checks[1].reason, checks[1].needs), ("not_assessed", "rule_error", ()))
        self.assertEqual(checks[1].finding.rule_id, engine.SKIPPED_RULE_ID)
        self.assertEqual([f.rule_id for f in engine.findings_of(checks)], [engine.SKIPPED_RULE_ID])

    def test_a_declared_rule_must_state_an_outcome(self):
        def silent(snapshot):
            return None

        silent.info = RULES["REL-005"].info
        bad = [
            silent,
            lambda snapshot: Outcome("passed"),  # without a basis
            lambda snapshot: Outcome("passed", basis="guess"),
            lambda snapshot: Outcome("not_assessed"),  # without a reason
            lambda snapshot: Outcome("not_assessed", reason="rule_error"),  # the engine's own
            lambda snapshot: Outcome("not_assessed", reason="source_missing", needs=("a customer",)),
            lambda snapshot: Outcome("not_applicable", basis="read"),
            lambda snapshot: Outcome("finding"),
            lambda snapshot: Outcome("withheld"),
        ]
        for number, rule in enumerate(bad):
            with self.subTest(rule=number):
                (check,) = engine.assess(Snapshot(), [rule])
                self.assertEqual((check.outcome, check.reason), ("not_assessed", "rule_error"))
        for good in (PASSED, NOT_APPLICABLE):
            (check,) = engine.assess(Snapshot(), [lambda snapshot: good])
            self.assertEqual((check.outcome, check.finding), (good.outcome, None))

    def test_a_snapshot_read_back_from_a_file_gives_the_same_outcomes(self):
        for snapshot in (pasted.snapshot(env=dict(QUEUE), kubernetes=healthy_kube(), redis=dict(REDIS)), Snapshot(env={})):
            again = Snapshot(**json.loads(json.dumps(asdict(snapshot))))
            self.assertEqual(
                [stated(check) for check in engine.assess(again, ALL_RULES)],
                [stated(check) for check in engine.assess(snapshot, ALL_RULES)],
            )


if __name__ == "__main__":
    unittest.main()
