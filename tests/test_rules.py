"""Rule tests: every rule fires, stays quiet, and returns None when its data is missing."""

import unittest

from n8n_healthcheck.models import AREAS, SEVERITIES, Finding, Snapshot
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.rules import maintainability as maint
from n8n_healthcheck.rules import performance as perf
from n8n_healthcheck.rules import reliability as rel
from n8n_healthcheck.rules import scalability as scal
from n8n_healthcheck.rules import security as sec
from n8n_healthcheck.rules._helpers import concurrency_flag, image_tag, image_version, n8n_role

QUEUE = {"EXECUTIONS_MODE": "queue", "DB_TYPE": "postgresdb"}


def container(image="n8nio/n8n:2.40.1", command=None, **overrides):
    base = {
        "name": "n8n",
        "image": image,
        "command": command or [],
        "env": {},
        "requests": {"cpu": "200m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
        "liveness_probe": True,
        "readiness_probe": True,
        "env_complete": True,
    }
    return {**base, **overrides}


def kube(*deployments):
    return {
        "deployments": [
            {"name": name, "replicas": replicas, "ready_replicas": replicas, "containers": [c]}
            for name, replicas, c in deployments
        ]
    }


def healthy_kube():
    return kube(
        ("n8n-main", 1, container()),
        ("n8n-worker", 2, container(command=["worker"])),
        ("n8n-webhook", 2, container(command=["n8n", "webhook"])),
    )


# One entry per rule: (rule, expected id, snapshot that triggers, snapshot that does not).
# A Snapshot() with every section None is checked against every rule separately.
CASES = [
    (
        scal.regular_mode_unbounded_concurrency,
        "SCAL-001",
        Snapshot(env={"DB_TYPE": "postgresdb"}),
        Snapshot(env={"DB_TYPE": "postgresdb", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}),
    ),
    (
        scal.single_worker,
        "SCAL-002",
        Snapshot(kubernetes=kube(("n8n-worker", 1, container(command=["worker"])))),
        Snapshot(kubernetes=healthy_kube()),
    ),
    (
        scal.manual_executions_on_main,
        "SCAL-003",
        Snapshot(env=dict(QUEUE)),
        Snapshot(env={**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"}),
    ),
    (
        scal.low_worker_concurrency,
        "SCAL-004",
        Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"}),
        Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}),
    ),
    (
        scal.postgres_connections_near_limit,
        "SCAL-005",
        Snapshot(postgres={"connections": 92, "max_connections": 100}),
        Snapshot(postgres={"connections": 20, "max_connections": 100}),
    ),
    (
        rel.sqlite_in_queue_mode,
        "REL-001",
        Snapshot(env={"EXECUTIONS_MODE": "queue"}),
        Snapshot(env=dict(QUEUE)),
    ),
    (
        rel.redis_eviction_policy,
        "REL-002",
        Snapshot(redis={"maxmemory_policy": "allkeys-lru", "maxmemory_bytes": 1 << 30}),
        Snapshot(redis={"maxmemory_policy": "noeviction", "maxmemory_bytes": 1 << 30}),
    ),
    (
        rel.redis_memory_near_limit,
        "REL-003",
        Snapshot(
            redis={
                "used_memory_bytes": 950,
                "maxmemory_bytes": 1000,
                "maxmemory_policy": "noeviction",
            }
        ),
        Snapshot(redis={"used_memory_bytes": 100, "maxmemory_bytes": 1000}),
    ),
    (
        rel.queue_backlog,
        "REL-004",
        Snapshot(redis={"waiting_jobs": 500, "active_jobs": 20}),
        Snapshot(redis={"waiting_jobs": 3, "active_jobs": 20}),
    ),
    (
        rel.readiness_failing,
        "REL-005",
        Snapshot(health={"healthz": True, "readiness": False}),
        Snapshot(health={"healthz": True, "readiness": True}),
    ),
    (
        rel.single_main,
        "REL-006",
        Snapshot(env=dict(QUEUE)),
        Snapshot(env={**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": "true"}),
    ),
    (
        rel.missing_probes,
        "REL-007",
        Snapshot(kubernetes=kube(("n8n-main", 1, container(readiness_probe=False)))),
        Snapshot(kubernetes=healthy_kube()),
    ),
    (
        rel.missing_resources,
        "REL-008",
        Snapshot(kubernetes=kube(("n8n-main", 1, container(requests={}, limits={})))),
        Snapshot(kubernetes=healthy_kube()),
    ),
    (
        perf.pruning_disabled,
        "PERF-001",
        Snapshot(env={"EXECUTIONS_DATA_PRUNE": "false"}),
        Snapshot(env={}),
    ),
    (
        perf.saving_execution_progress,
        "PERF-002",
        Snapshot(env={"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"}),
        Snapshot(env={}),
    ),
    (
        perf.binary_data_in_memory,
        "PERF-003",
        Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "default"}),
        Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "filesystem"}),
    ),
    (
        maint.metrics_disabled,
        "MAINT-001",
        Snapshot(env={}),
        Snapshot(env={"N8N_METRICS": "true"}),
    ),
    (
        maint.mutable_image_tag,
        "MAINT-002",
        Snapshot(kubernetes=kube(("n8n-main", 1, container(image="n8nio/n8n:latest")))),
        Snapshot(kubernetes=healthy_kube()),
    ),
    (
        maint.version_skew,
        "MAINT-003",
        Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image="n8nio/n8n:2.40.1")),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.38.0", command=["worker"])),
            )
        ),
        Snapshot(kubernetes=healthy_kube()),
    ),
    (
        sec.insecure_cookie,
        "SEC-001",
        Snapshot(env={"N8N_SECURE_COOKIE": "false"}),
        Snapshot(env={}),
    ),
    (
        sec.task_runners_not_external,
        "SEC-002",
        Snapshot(env={"N8N_RUNNERS_ENABLED": "true"}),
        Snapshot(env={"N8N_RUNNERS_MODE": "external"}),
    ),
]


class EveryRule(unittest.TestCase):
    def test_every_registered_rule_has_a_case(self):
        self.assertEqual([case[0] for case in CASES], ALL_RULES)

    def test_triggers(self):
        for rule, rule_id, triggering, _quiet in CASES:
            with self.subTest(rule=rule_id):
                finding = rule(triggering)
                self.assertIsInstance(finding, Finding)
                self.assertEqual(finding.rule_id, rule_id)
                self.assertIn(finding.area, AREAS)
                self.assertIn(finding.severity, SEVERITIES)
                for field in ("title", "evidence", "impact", "recommendation"):
                    self.assertTrue(getattr(finding, field).strip(), field)

    def test_does_not_trigger(self):
        for rule, rule_id, _triggering, quiet in CASES:
            with self.subTest(rule=rule_id):
                self.assertIsNone(rule(quiet))

    def test_not_collected_is_never_a_finding(self):
        for rule, rule_id, _triggering, _quiet in CASES:
            with self.subTest(rule=rule_id):
                self.assertIsNone(rule(Snapshot()))

    def test_rule_ids_are_unique_and_match_their_area(self):
        prefixes = {
            "SCAL": "scalability",
            "REL": "reliability",
            "PERF": "performance",
            "MAINT": "maintainability",
            "SEC": "security",
        }
        ids = [rule_id for _rule, rule_id, _t, _q in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for rule, rule_id, triggering, _quiet in CASES:
            with self.subTest(rule=rule_id):
                self.assertRegex(rule_id, r"^[A-Z]+-\d{3}$")
                self.assertEqual(rule(triggering).area, prefixes[rule_id.split("-")[0]])


class Evidence(unittest.TestCase):
    """Evidence must quote what was observed, not a generic sentence."""

    def test_quotes_observed_values(self):
        expectations = {
            "SCAL-002": "n8n-worker (replicas: 1)",
            "SCAL-004": "N8N_CONCURRENCY_PRODUCTION_LIMIT=2 (EXECUTIONS_MODE=queue)",
            "SCAL-005": "92 of 100",
            "REL-001": "DB_TYPE is not set",
            "REL-002": "allkeys-lru",
            "REL-003": "950 of 1000",
            "REL-004": "500 jobs waiting and 20 active",
            "REL-007": "n8n-main/n8n (no readiness probe)",
            "REL-008": "requests.cpu, requests.memory, limits.memory",
            "MAINT-002": "n8nio/n8n:latest",
            "MAINT-003": "2.38.0: n8n-worker (worker)",
            "SEC-001": "N8N_SECURE_COOKIE=false",
        }
        findings = {rule_id: rule(triggering) for rule, rule_id, triggering, _q in CASES}
        for rule_id, fragment in expectations.items():
            with self.subTest(rule=rule_id):
                self.assertIn(fragment, findings[rule_id].evidence)


class EdgeCases(unittest.TestCase):
    def test_regular_mode_on_sqlite_is_not_flagged_as_unbounded(self):
        self.assertIsNone(scal.regular_mode_unbounded_concurrency(Snapshot(env={})))

    def test_queue_mode_is_not_flagged_as_unbounded_regular_mode(self):
        self.assertIsNone(scal.regular_mode_unbounded_concurrency(Snapshot(env=dict(QUEUE))))

    def test_zero_workers_is_left_to_the_backlog_rule(self):
        snapshot = Snapshot(kubernetes=kube(("n8n-worker", 0, container(command=["worker"]))))
        self.assertIsNone(scal.single_worker(snapshot))

    def test_no_worker_deployment_in_namespace_is_not_a_finding(self):
        snapshot = Snapshot(kubernetes=kube(("n8n-main", 1, container())))
        self.assertIsNone(scal.single_worker(snapshot))

    def test_queue_only_rules_ignore_regular_mode(self):
        regular = Snapshot(env={"N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"})
        for rule in (
            scal.manual_executions_on_main,
            scal.low_worker_concurrency,
            rel.sqlite_in_queue_mode,
            rel.single_main,
        ):
            with self.subTest(rule=rule.__name__):
                self.assertIsNone(rule(regular))

    def test_concurrency_disabled_is_not_low(self):
        env = {**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "-1"}
        self.assertIsNone(scal.low_worker_concurrency(Snapshot(env=env)))

    def test_db_type_from_file_is_unknown(self):
        env = {"EXECUTIONS_MODE": "queue", "DB_TYPE_FILE": "/run/secrets/db_type"}
        self.assertIsNone(rel.sqlite_in_queue_mode(Snapshot(env=env)))

    def test_eviction_policy_is_inert_without_maxmemory(self):
        redis = {"maxmemory_policy": "allkeys-lru", "maxmemory_bytes": 0, "used_memory_bytes": 9}
        self.assertIsNone(rel.redis_eviction_policy(Snapshot(redis=redis)))
        self.assertIsNone(rel.redis_memory_near_limit(Snapshot(redis=redis)))

    def test_unconsumed_queue_is_critical(self):
        finding = rel.queue_backlog(Snapshot(redis={"waiting_jobs": 25, "active_jobs": 0}))
        self.assertEqual(finding.severity, "critical")

    def test_busy_backlog_is_a_warning(self):
        finding = rel.queue_backlog(Snapshot(redis={"waiting_jobs": 500, "active_jobs": 20}))
        self.assertEqual(finding.severity, "warning")

    def test_job_between_enqueue_and_pickup_is_not_a_stall(self):
        self.assertIsNone(rel.queue_backlog(Snapshot(redis={"waiting_jobs": 2, "active_jobs": 0})))

    def test_readiness_not_probed_is_not_a_failure(self):
        self.assertIsNone(rel.readiness_failing(Snapshot(health={"healthz": True})))

    def test_cpu_limit_is_not_required(self):
        snapshot = Snapshot(kubernetes=kube(("n8n-main", 1, container())))
        self.assertNotIn("cpu", container()["limits"])
        self.assertIsNone(rel.missing_resources(snapshot))

    def test_non_n8n_containers_are_ignored(self):
        bare = {"requests": {}, "limits": {}, "liveness_probe": False, "readiness_probe": False}
        snapshot = Snapshot(
            kubernetes=kube(
                ("redis", 1, container(image="redis", **bare)),
                ("runners", 1, container(image="n8nio/runners:latest", **bare)),
            )
        )
        for rule in (rel.missing_probes, rel.missing_resources, maint.mutable_image_tag):
            with self.subTest(rule=rule.__name__):
                self.assertIsNone(rule(snapshot))

    def test_pruning_evidence_includes_database_size_when_collected(self):
        snapshot = Snapshot(
            env={"EXECUTIONS_DATA_PRUNE": "false"},
            postgres={
                "execution_count": 1200000,
                "execution_table_bytes": 5 << 30,
                "oldest_execution_age_days": 410.5,
            },
        )
        evidence = perf.pruning_disabled(snapshot).evidence
        self.assertIn("1200000 executions", evidence)
        self.assertIn("410.5 days", evidence)

    def test_unset_binary_data_mode_is_not_flagged(self):
        self.assertIsNone(perf.binary_data_in_memory(Snapshot(env={})))
        self.assertIsNone(perf.binary_data_in_memory(Snapshot(env=dict(QUEUE))))

    def test_binary_data_fix_depends_on_execution_mode(self):
        memory = {"N8N_DEFAULT_BINARY_DATA_MODE": "default"}
        regular = perf.binary_data_in_memory(Snapshot(env=memory))
        queue = perf.binary_data_in_memory(Snapshot(env={**QUEUE, **memory}))
        self.assertIn("N8N_DEFAULT_BINARY_DATA_MODE=filesystem", regular.recommendation)
        self.assertIn("N8N_DEFAULT_BINARY_DATA_MODE=database", queue.recommendation)

    def test_scraped_metrics_override_a_stale_env_file(self):
        snapshot = Snapshot(env={}, metrics={"n8n_version_info": 1.0})
        self.assertIsNone(maint.metrics_disabled(snapshot))

    def test_untagged_image_is_mutable(self):
        snapshot = Snapshot(kubernetes=kube(("n8n-main", 1, container(image="n8nio/n8n"))))
        self.assertIsNotNone(maint.mutable_image_tag(snapshot))

    def test_digest_pinned_image_is_not_mutable(self):
        image = "docker.n8n.io/n8nio/n8n@sha256:" + "a" * 64
        snapshot = Snapshot(kubernetes=kube(("n8n-main", 1, container(image=image))))
        self.assertIsNone(maint.mutable_image_tag(snapshot))

    def test_mutable_tags_are_not_reported_as_version_skew(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image="n8nio/n8n:latest")),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.40.1", command=["worker"])),
            )
        )
        self.assertIsNone(maint.version_skew(snapshot))

    def test_secure_cookie_default_is_fine(self):
        self.assertIsNone(sec.insecure_cookie(Snapshot(env={"N8N_SECURE_COOKIE": "true"})))

    def test_internal_runners_are_flagged(self):
        env = {"N8N_RUNNERS_MODE": "internal"}
        self.assertIsNotNone(sec.task_runners_not_external(Snapshot(env=env)))


class ReviewFixes(unittest.TestCase):
    def test_thresholds_fire_at_the_boundary_and_not_below(self):
        def pg(used):
            return Snapshot(postgres={"connections": used, "max_connections": 100})

        def mem(used):
            return Snapshot(redis={"used_memory_bytes": used, "maxmemory_bytes": 100})

        def jobs(waiting, active):
            return Snapshot(redis={"waiting_jobs": waiting, "active_jobs": active})

        self.assertIsNone(scal.postgres_connections_near_limit(pg(79)))
        self.assertIsNotNone(scal.postgres_connections_near_limit(pg(80)))
        self.assertIsNone(rel.redis_memory_near_limit(mem(79)))
        self.assertIsNotNone(rel.redis_memory_near_limit(mem(80)))
        self.assertIsNone(rel.queue_backlog(jobs(9, 0)))
        self.assertEqual(rel.queue_backlog(jobs(10, 0)).severity, "critical")
        self.assertIsNone(rel.queue_backlog(jobs(99, 5)))
        self.assertEqual(rel.queue_backlog(jobs(100, 5)).severity, "warning")
        self.assertIsNotNone(scal.low_worker_concurrency(
            Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "4"})))
        self.assertIsNone(scal.low_worker_concurrency(
            Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "5"})))

    def test_digest_next_to_tag_is_not_version_skew(self):
        digest = "n8nio/n8n@sha256:" + "a" * 64
        snapshot = Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image=digest)),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.40.1", command=["worker"])),
            )
        )
        self.assertIsNone(maint.version_skew(snapshot))

    def test_build_suffix_is_the_same_version(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image="n8nio/n8n:2.40.1-build7")),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.40.1", command=["worker"])),
            )
        )
        self.assertIsNone(maint.version_skew(snapshot))

    def test_custom_tag_names_no_version(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image="acme/n8n:prod")),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.40.1", command=["worker"])),
            )
        )
        self.assertIsNone(maint.version_skew(snapshot))

    def test_version_skew_reports_the_leading_version(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("n8n-main", 1, container(image="n8nio/n8n:2.40.1-build7")),
                ("n8n-worker", 2, container(image="n8nio/n8n:2.38.0", command=["worker"])),
            )
        )
        evidence = maint.version_skew(snapshot).evidence
        self.assertIn("2.40.1: n8n-main (main)", evidence)
        self.assertNotIn("build7", evidence)

    def test_queue_metrics_advice_is_left_out_on_multi_main(self):
        single = maint.metrics_disabled(Snapshot(env=dict(QUEUE)))
        multi = maint.metrics_disabled(
            Snapshot(env={**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": "true"})
        )
        regular = maint.metrics_disabled(Snapshot(env={}))
        self.assertIn("N8N_METRICS_INCLUDE_QUEUE_METRICS", single.recommendation)
        self.assertIn("multi-main", single.recommendation)
        self.assertNotIn("N8N_METRICS_INCLUDE_QUEUE_METRICS", multi.recommendation)
        self.assertNotIn("N8N_METRICS_INCLUDE_QUEUE_METRICS", regular.recommendation)

    def test_backlog_advice_carries_the_multi_main_caveat(self):
        finding = rel.queue_backlog(Snapshot(redis={"waiting_jobs": 500, "active_jobs": 20}))
        self.assertIn("multi-main", finding.recommendation)

    def test_task_runner_severity_follows_how_production_like_the_install_is(self):
        stock = sec.task_runners_not_external(Snapshot(env={}))
        postgres = sec.task_runners_not_external(Snapshot(env={"DB_TYPE": "postgresdb"}))
        queue = sec.task_runners_not_external(Snapshot(env={"EXECUTIONS_MODE": "queue"}))
        self.assertEqual(stock.severity, "info")
        self.assertEqual(postgres.severity, "warning")
        self.assertEqual(queue.severity, "warning")
        self.assertIn("deprecated from n8n 3.0", stock.recommendation)

    def test_unbounded_regular_mode_is_info(self):
        finding = scal.regular_mode_unbounded_concurrency(Snapshot(env={"DB_TYPE": "postgresdb"}))
        self.assertEqual(finding.severity, "info")

    def test_concurrency_limit_of_zero_is_reported_as_invalid(self):
        for env in ({"N8N_CONCURRENCY_PRODUCTION_LIMIT": "0"},
                    {"DB_TYPE": "postgresdb", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "0"}):
            with self.subTest(env=env):
                finding = scal.regular_mode_unbounded_concurrency(Snapshot(env=env))
                self.assertEqual(finding.severity, "warning")
                self.assertIn("N8N_CONCURRENCY_PRODUCTION_LIMIT=0", finding.evidence)
                self.assertIn("InvalidConcurrencyLimitError", finding.evidence)
                self.assertNotIn("no limit", finding.evidence)

    def test_worker_concurrency_flag_is_read_from_the_command(self):
        def workers(*command, env=None):
            return Snapshot(
                kubernetes=kube(("n8n-worker", 3, container(command=list(command), env=env or {})))
            )

        low = scal.low_worker_concurrency(workers("worker", "--concurrency=2"))
        self.assertIn("n8n-worker/n8n: --concurrency=2", low.evidence)
        self.assertIsNotNone(scal.low_worker_concurrency(workers("worker", "--concurrency", "3")))
        self.assertIsNotNone(
            scal.low_worker_concurrency(workers("/bin/sh", "-c", "n8n worker --concurrency=1"))
        )
        self.assertIsNone(scal.low_worker_concurrency(workers("worker", "--concurrency=5")))
        self.assertIsNone(scal.low_worker_concurrency(workers("worker")))
        # a main container's flags are not worker concurrency
        self.assertIsNone(scal.low_worker_concurrency(workers("start", "--concurrency=2")))

    def test_env_limit_overrides_the_concurrency_flag(self):
        flag_low = kube(("n8n-worker", 3, container(command=["worker", "--concurrency=2"])))
        overridden = Snapshot(
            env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=flag_low
        )
        self.assertIsNone(scal.low_worker_concurrency(overridden))
        own_env = kube(
            (
                "n8n-worker",
                3,
                container(
                    command=["worker", "--concurrency=20"],
                    env={"N8N_CONCURRENCY_PRODUCTION_LIMIT": "3"},
                ),
            )
        )
        finding = scal.low_worker_concurrency(Snapshot(kubernetes=own_env))
        self.assertIn("N8N_CONCURRENCY_PRODUCTION_LIMIT=3", finding.evidence)
        disabled = Snapshot(
            env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "-1"}, kubernetes=flag_low
        )
        self.assertIsNotNone(scal.low_worker_concurrency(disabled))

    def test_shell_form_command_is_a_worker(self):
        shell = container(command=["/bin/sh", "-c", "n8n worker"])
        self.assertEqual(n8n_role(shell), "worker")
        self.assertIsNotNone(scal.single_worker(Snapshot(kubernetes=kube(("w", 1, shell)))))

    def test_sqlite_queue_mode_is_described_as_unsupported(self):
        finding = rel.sqlite_in_queue_mode(Snapshot(env={"EXECUTIONS_MODE": "queue"}))
        self.assertIn("unsupported", finding.impact)

    def test_redacted_secrets_do_not_change_any_result(self):
        secrets = {
            "N8N_ENCRYPTION_KEY": "<redacted>",
            "DB_POSTGRESDB_PASSWORD": "<redacted>",
            "QUEUE_BULL_REDIS_PASSWORD": "<redacted>",
            "N8N_RUNNERS_AUTH_TOKEN": "<redacted>",
        }
        for base in ({}, dict(QUEUE), {"DB_TYPE": "postgresdb"}):
            for rule in ALL_RULES:
                with self.subTest(rule=rule.__name__, base=base):
                    plain = rule(Snapshot(env=dict(base)))
                    redacted = rule(Snapshot(env={**base, **secrets}))
                    self.assertEqual(plain, redacted)

    def test_values_outside_the_contract_are_tolerated_by_the_shared_helpers(self):
        self.assertIsNotNone(
            scal.postgres_connections_near_limit(
                Snapshot(postgres={"connections": "92", "max_connections": "100"})
            )
        )
        self.assertIsNone(
            rel.queue_backlog(Snapshot(redis={"waiting_jobs": "n/a", "active_jobs": None}))
        )
        odd = {
            "deployments": [
                {"name": "a", "replicas": None, "containers": None},
                {"name": "b", "replicas": None, "containers": [{"name": "c", "image": None}]},
                {"name": "w", "replicas": None, "containers": [container(command=["worker"])]},
            ]
        }
        for rule in (scal.single_worker, maint.mutable_image_tag, maint.version_skew):
            with self.subTest(rule=rule.__name__):
                self.assertIsNone(rule(Snapshot(kubernetes=odd)))
        self.assertIsNone(scal.single_worker(Snapshot(kubernetes={"deployments": None})))


def from_kube(env, complete=True, worker_env=None):
    """A Kubernetes-only snapshot whose main container carries `env`."""
    return Snapshot(
        kubernetes=kube(
            ("n8n-main", 1, container(env=env, env_complete=complete)),
            ("n8n-worker", 2, container(command=["worker"], env=worker_env or {})),
        )
    )


class KubernetesEnv(unittest.TestCase):
    """The env-based rules read the main container when there is no env file."""

    PG = {"DB_TYPE": "postgresdb"}
    SOURCE = " Environment read from Deployment n8n-main, container n8n."
    ENV_RULES = [
        case
        for case in CASES
        if case[2].env is not None and case[0] is not scal.low_worker_concurrency
    ]

    def test_same_findings_as_the_env_file(self):
        self.assertEqual(len(self.ENV_RULES), 10)
        for rule, rule_id, triggering, quiet in self.ENV_RULES:
            with self.subTest(rule=rule_id):
                expected = rule(triggering)
                finding = rule(from_kube(triggering.env))
                self.assertEqual(finding.evidence, expected.evidence + self.SOURCE)
                expected.evidence = finding.evidence
                self.assertEqual(finding, expected)
                self.assertIsNone(rule(from_kube(quiet.env)))

    def test_env_file_wins_over_the_container(self):
        bad = {**QUEUE, "EXECUTIONS_DATA_PRUNE": "false"}
        good = {**QUEUE, "EXECUTIONS_DATA_PRUNE": "true"}
        self.assertIsNone(
            perf.pruning_disabled(Snapshot(env=good, kubernetes=from_kube(bad).kubernetes))
        )
        finding = perf.pruning_disabled(Snapshot(env=bad, kubernetes=from_kube(good).kubernetes))
        self.assertNotIn("Deployment", finding.evidence)

    def test_unreadable_value_is_never_a_finding(self):
        for opaque in ("<valueFrom>", "<redacted>"):
            for rule, rule_id, triggering, quiet in self.ENV_RULES:
                # Every variable that separates the triggering case from the quiet one.
                differing = {
                    name
                    for name in {*triggering.env, *quiet.env}
                    if triggering.env.get(name) != quiet.env.get(name)
                }
                # Quoted in the SEC-002 evidence, decides nothing.
                differing.discard("N8N_RUNNERS_ENABLED")
                for name in differing:
                    with self.subTest(rule=rule_id, variable=name, value=opaque):
                        self.assertIsNone(rule(from_kube({**triggering.env, name: opaque})))
                        self.assertIsNone(rule(Snapshot(env={**triggering.env, name: opaque})))

    def test_unreadable_execution_mode_is_not_read_as_regular(self):
        env = {"EXECUTIONS_MODE": "<valueFrom>", "DB_TYPE": "postgresdb"}
        self.assertIsNone(scal.regular_mode_unbounded_concurrency(from_kube(env)))
        zero = {**env, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "0"}
        self.assertIsNone(scal.regular_mode_unbounded_concurrency(from_kube(zero)))
        incomplete = from_kube({"DB_TYPE": "postgresdb"}, complete=False)
        self.assertIsNone(scal.regular_mode_unbounded_concurrency(incomplete))

    def test_incomplete_env_proves_nothing_from_an_absent_variable(self):
        # Each of these fires on a complete environment because a variable is absent.
        absent = [
            (scal.regular_mode_unbounded_concurrency, {**self.PG, "EXECUTIONS_MODE": "regular"}),
            (scal.manual_executions_on_main, dict(QUEUE)),
            (rel.sqlite_in_queue_mode, {"EXECUTIONS_MODE": "queue"}),
            (rel.single_main, dict(QUEUE)),
            (maint.metrics_disabled, dict(QUEUE)),
            (sec.task_runners_not_external, dict(QUEUE)),
        ]
        for rule, env in absent:
            with self.subTest(rule=rule.__name__):
                self.assertIsNotNone(rule(from_kube(env)))
                self.assertIsNone(rule(from_kube(env, complete=False)))
        # A container without the field (snapshot from an older version) is incomplete too.
        old = from_kube(dict(QUEUE))
        del old.kubernetes["deployments"][0]["containers"][0]["env_complete"]
        self.assertIsNone(maint.metrics_disabled(old))

    def test_incomplete_env_still_reports_an_explicit_bad_value(self):
        explicit = [
            (perf.pruning_disabled, {"EXECUTIONS_DATA_PRUNE": "false"}),
            (perf.saving_execution_progress, {"EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"}),
            (perf.binary_data_in_memory, {"N8N_DEFAULT_BINARY_DATA_MODE": "default"}),
            (sec.insecure_cookie, {"N8N_SECURE_COOKIE": "false"}),
            (maint.metrics_disabled, {"N8N_METRICS": "false"}),
            (sec.task_runners_not_external, {"N8N_RUNNERS_MODE": "internal"}),
            (rel.sqlite_in_queue_mode, {"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite"}),
            (rel.single_main, {**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": "false"}),
            (
                scal.manual_executions_on_main,
                {**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "false"},
            ),
            (
                scal.regular_mode_unbounded_concurrency,
                {**self.PG, "EXECUTIONS_MODE": "regular", "N8N_CONCURRENCY_PRODUCTION_LIMIT": "-1"},
            ),
        ]
        for rule, env in explicit:
            with self.subTest(rule=rule.__name__):
                finding = rule(from_kube(env, complete=False))
                self.assertIn("Part of it could not be read", finding.evidence)
                self.assertNotIn("is not set", finding.evidence)

    def test_binary_data_fix_is_neutral_when_the_mode_is_unreadable(self):
        env = {"N8N_DEFAULT_BINARY_DATA_MODE": "default"}
        opaque = {**env, "EXECUTIONS_MODE": "<valueFrom>"}
        for snapshot in (from_kube(env, complete=False), from_kube(opaque)):
            fix = perf.binary_data_in_memory(snapshot).recommendation
            self.assertIn("regular mode", fix)
            self.assertIn("queue mode", fix)

    def test_metrics_queue_advice_needs_a_readable_multi_main_setting(self):
        env = {**QUEUE, "N8N_MULTI_MAIN_SETUP_ENABLED": "<valueFrom>"}
        fix = maint.metrics_disabled(from_kube(env)).recommendation
        self.assertNotIn("INCLUDE_QUEUE_METRICS", fix)

    def test_no_main_container_is_never_a_finding(self):
        bad = {**QUEUE, "EXECUTIONS_DATA_PRUNE": "false", "N8N_SECURE_COOKIE": "false"}
        workers_only = Snapshot(
            kubernetes=kube(
                ("n8n-worker", 2, container(command=["worker"], env=bad)),
                ("n8n-webhook", 2, container(command=["webhook"], env=bad)),
                ("redis", 1, container(image="redis:7", env=bad)),
            )
        )
        for rule, rule_id, _t, _q in self.ENV_RULES:
            with self.subTest(rule=rule_id):
                self.assertIsNone(rule(workers_only))

    def test_main_containers_that_disagree_are_not_guessed_between(self):
        def two(first, second):
            mains = kube(("a", 1, container(env=first)), ("b", 1, container(env=second)))
            return Snapshot(kubernetes=mains)

        bad = {"N8N_SECURE_COOKIE": "false"}
        self.assertIsNone(sec.insecure_cookie(two(bad, {"N8N_SECURE_COOKIE": "true"})))
        self.assertIsNotNone(sec.insecure_cookie(two(bad, dict(bad))))

    def test_main_env_does_not_stand_in_for_the_workers(self):
        low = {**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"}
        self.assertIsNone(scal.low_worker_concurrency(from_kube(low)))
        self.assertIsNotNone(scal.low_worker_concurrency(from_kube(dict(QUEUE), worker_env=low)))

    def test_worker_flag_is_not_trusted_when_its_limit_is_unreadable(self):
        def worker(**overrides):
            low = container(command=["worker", "--concurrency=2"], **overrides)
            return Snapshot(kubernetes=kube(("w", 2, low)))

        self.assertIsNotNone(scal.low_worker_concurrency(worker()))
        self.assertIsNone(scal.low_worker_concurrency(worker(env_complete=False)))
        opaque = worker(env={"N8N_CONCURRENCY_PRODUCTION_LIMIT": "<valueFrom>"})
        self.assertIsNone(scal.low_worker_concurrency(opaque))
        explicit = worker(env={"N8N_CONCURRENCY_PRODUCTION_LIMIT": "3"}, env_complete=False)
        self.assertIsNotNone(scal.low_worker_concurrency(explicit))


class Helpers(unittest.TestCase):
    def test_image_tag(self):
        self.assertEqual(image_tag("n8nio/n8n:2.40.1"), "2.40.1")
        self.assertEqual(image_tag("registry.local:5000/n8nio/n8n:2.40.1"), "2.40.1")
        self.assertIsNone(image_tag("registry.local:5000/n8nio/n8n"))
        self.assertTrue(image_tag("n8nio/n8n@sha256:abc").startswith("sha256:"))

    def test_n8n_role(self):
        self.assertEqual(n8n_role(container()), "main")
        self.assertEqual(n8n_role(container(command=["n8n", "worker", "--concurrency=5"])), "worker")
        self.assertEqual(n8n_role(container(command=["webhook"])), "webhook")
        self.assertEqual(n8n_role(container(image="ghcr.io/acme/custom-n8n:1.2.3")), "main")
        self.assertIsNone(n8n_role(container(image="n8nio/runners:2.40.1")))
        self.assertIsNone(n8n_role(container(image="redis:7")))

    def test_image_version(self):
        self.assertEqual(image_version("n8nio/n8n:2.40.1"), "2.40.1")
        self.assertEqual(image_version("n8nio/n8n:v2.40.1-build7"), "2.40.1")
        self.assertIsNone(image_version("n8nio/n8n:latest"))
        self.assertIsNone(image_version("n8nio/n8n"))
        self.assertIsNone(image_version("n8nio/n8n@sha256:abc"))
        self.assertIsNone(image_version(None))

    def test_concurrency_flag(self):
        self.assertEqual(concurrency_flag(container(command=["worker", "--concurrency=7"])), 7)
        self.assertEqual(concurrency_flag(container(command=["worker", "--concurrency", "7"])), 7)
        self.assertIsNone(concurrency_flag(container(command=["worker"])))
        self.assertIsNone(concurrency_flag(container(command=["worker", "--concurrency=x"])))


if __name__ == "__main__":
    unittest.main()
