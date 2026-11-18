"""Environment profile: nothing leaks, every fact is right or unknown, the file round-trips."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from unittest import mock

import n8n_healthcheck
from n8n_healthcheck import cli, engine, n8n_settings, profile
from n8n_healthcheck.collectors import envfile, kubernetes
from n8n_healthcheck.models import REDACTED, Finding, Snapshot
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.rules import scalability as scal

DOCS = os.path.join(os.path.dirname(__file__), "..", "docs", "profile-format.md")
LIMIT = "N8N_CONCURRENCY_PRODUCTION_LIMIT"


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


def main_kube(*envs, **overrides):
    """One main Deployment per given environment."""
    return kube(*((f"main-{i}", 1, container(env=env, **overrides)) for i, env in enumerate(envs)))


def worker(env=None, command=("worker",), **overrides):
    return container(command=list(command), env=env or {}, **overrides)


def build(snapshot=None, findings=(), **kwargs):
    return profile.build_profile(snapshot or Snapshot(), list(findings), **kwargs)


def fact(snapshot, fact_id, failed=()):
    return {f["id"]: f for f in build(snapshot, failed=failed)["facts"]}[fact_id]


def settings(snapshot):
    return {profile.setting_id(s): (s["state"], s["value"]) for s in build(snapshot)["settings"]}


# --- zero leak ---------------------------------------------------------------------
# This test is the point of the profile. Do not weaken it: add places, never remove them.

MARK = "zqleak"  # part of every seeded host, URL, path, e-mail, name and secret
SEEDED_IPS = ("10.11.12.13", "192.168.77.5")

LEAKY_ENV_FILE = f"""
EXECUTIONS_MODE=queue
DB_TYPE=postgresdb
EXECUTIONS_DATA_PRUNE=true
N8N_ENCRYPTION_KEY={MARK}EncryptionSecret
DB_POSTGRESDB_PASSWORD={MARK}DbSecret
N8N_RUNNERS_AUTH_TOKEN={MARK}TokenSecret
N8N_HOST={MARK}-host
N8N_EDITOR_BASE_URL=https://editor.{MARK}.example/
WEBHOOK_URL=https://hooks.{MARK}.example/
DB_POSTGRESDB_HOST=10.11.12.13
DB_POSTGRESDB_DATABASE={MARK}_prod
DB_POSTGRESDB_SCHEMA={MARK}_schema
DB_POSTGRESDB_USER={MARK}_user
DB_TABLE_PREFIX={MARK}_
QUEUE_BULL_REDIS_HOST=redis.{MARK}.internal
QUEUE_BULL_PREFIX={MARK}queue
N8N_SMTP_SENDER=ops@{MARK}.example
N8N_USER_FOLDER=/srv/{MARK}/n8n
N8N_LOG_FILE_LOCATION=/var/log/{MARK}.log
N8N_EXTERNAL_STORAGE_S3_BUCKET_NAME={MARK}-bucket
NODE_OPTIONS=--require /opt/{MARK}/hook.js
NODE_FUNCTION_ALLOW_EXTERNAL={MARK}.internal.lib
N8N_INNOCENT_ONE=https://{MARK}.example/x
N8N_INNOCENT_TWO=db.{MARK}.internal
N8N_INNOCENT_THREE=192.168.77.5
N8N_INNOCENT_FOUR=admin@{MARK}.example
N8N_INNOCENT_FIVE=/data/{MARK}
N8N_INNOCENT_SIX=redis://:{MARK}pw@cache:6379
N8N_INNOCENT_SEVEN={MARK} with spaces
N8N_INNOCENT_EIGHT={MARK}
EXECUTIONS_DATA_MAX_AGE={MARK}.hours
N8N_RUNNERS_MODE=https://{MARK}.example
QUEUE_BULL_REDIS_CLUSTER_NODES={MARK}-redis-0,{MARK}-redis-1
N8N_CUSTOM_EXTENSIONS={MARK}-nodes
NODE_FUNCTION_ALLOW_EXTERNAL={MARK}-internal-sdk,lodash
N8N_RUNNERS_EXTERNAL_ALLOW={MARK}lib
N8N_LICENSE_TENANT_ID=471147
N8N_SMTP_AUTH={MARK}hunter2
DB_POSTGRESDB_SSL_CA=MIIB{MARK}base64
N8N_{MARK.upper()}CORP_SSO_BRIDGE=enabled
N8N_PORT={MARK}
N8N_METRICS={MARK}
N8N_LOG_LEVEL={MARK}
EXECUTIONS_MODE_FILE=/run/secrets/{MARK}
{MARK.upper()}CORP_FEATURE=1
N8N_{MARK}.example=1
n8n_{MARK}_lower=1
HTTP_PROXY=http://proxy.{MARK}.example:3128
"""

LEAKY_CONFIGMAPS = {
    f"{MARK}-config": {
        "EXECUTIONS_MODE": "queue",
        "N8N_EDITOR_BASE_URL": f"https://cm.{MARK}.example/",
        "DB_POSTGRESDB_HOST": "10.11.12.13",
        "N8N_INNOCENT_CM": f"/mnt/{MARK}",
        f"{MARK.upper()}_CUSTOM": "1",
    }
}


def leaky_deployment(name, args, resources, image=f"registry.{MARK}.example:5000/{MARK}team/n8n:2.40.1"):
    return {
        "metadata": {"name": f"{MARK}-{name}", "namespace": f"{MARK}-namespace"},
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": f"{MARK}-container",
                            "image": image,
                            "command": ["n8n"],
                            "args": args + [f"--config=/etc/{MARK}/n8n.json"],
                            "envFrom": [{"configMapRef": {"name": f"{MARK}-config"}}],
                            "env": [
                                {"name": "N8N_HOST", "value": f"n8n.{MARK}.example"},
                                {"name": "N8N_INNOCENT_KUBE", "value": f"svc.{MARK}.cluster.local"},
                                {"name": "N8N_ENCRYPTION_KEY", "value": f"{MARK}KubeSecret"},
                                {"name": "N8N_INNOCENT_REF", "value": f"$({MARK.upper()}_OTHER)"},
                                {
                                    "name": "DB_POSTGRESDB_PASSWORD",
                                    "valueFrom": {"secretKeyRef": {"name": f"{MARK}-secret", "key": "password"}},
                                },
                                {"name": f"{MARK.upper()}_SERVICE_NAME", "value": "1"},
                            ],
                            "resources": resources,
                            "livenessProbe": {"httpGet": {"path": f"/{MARK}/healthz"}},
                        }
                    ]
                }
            },
        },
    }


def leaky_snapshot():
    # Each role leaks through a different part of its resources, so that one bad value
    # making a whole fact unknown cannot hide another.
    deployments = {
        "items": [
            # a resource name that is a domain, next to quantities that are fine
            leaky_deployment("main", [], {"requests": {"cpu": "250m", f"{MARK}.example/gpu": "1"}, "limits": {"memory": "1Gi"}}),
            # a quantity that is an address
            leaky_deployment("worker", ["worker"], {"requests": {"cpu": "10.11.12.13"}}),
            # a quantity that is free text
            leaky_deployment("webhook", ["webhook"], {"limits": {"memory": f"{MARK}Gi"}}),
            # left out of the main role, and said so in the details without its name
            leaky_deployment("exporter", [], {}, image=f"{MARK}/n8n-{MARK}-exporter:1.0.0"),
        ]
    }
    return Snapshot(
        health={"healthz": True, "readiness": True, "metrics_disabled": False},
        metrics={f'n8n_version_info{{version="v2.40.1",instance="{MARK}.example:5678"}}': 1.0},
        env=envfile.parse_env(LEAKY_ENV_FILE),
        postgres={
            "version": f"16.4 (built on {MARK}-host)",
            "execution_count": 1200,
            "execution_table_bytes": 5_000_000,
            "oldest_execution_age_days": 3.5,
            "connections": 4,
            "max_connections": 100,
        },
        redis={
            "version": f"7.2.4-{MARK}",
            "waiting_jobs": 0,
            "active_jobs": 1,
            "failed_jobs": 2,
            "used_memory_bytes": 1000,
            "maxmemory_bytes": 0,
            "maxmemory_policy": f"{MARK}-policy",
        },
        kubernetes=kubernetes.parse_deployments(json.dumps(deployments), LEAKY_CONFIGMAPS),
        n8n_version=f"2.40.1-{MARK}",
    )


class ZeroLeakTest(unittest.TestCase):
    def assert_clean(self, snapshot):
        findings = engine.run(snapshot, ALL_RULES)
        built = profile.build_profile(snapshot, findings, failed=("redis",))
        for text in (profile.to_json(built), profile.render_markdown(built)):
            self.assertNotIn(MARK, text.lower())
            for ip in SEEDED_IPS + ("471147",):  # ... and the seeded tenant id
                self.assertNotIn(ip, text)
            # No URL, e-mail or path can be written at all: these never appear in a profile.
            for fragment in ("://", "@", "\\", "<redacted>", "<valueFrom>"):
                self.assertNotIn(fragment, text)
            self.assertIsNone(re.search(r"/(?!100\b)", text.replace("n/a", "")), "a slash outside a score")
        return built

    def test_seeded_snapshot_really_holds_the_markers(self):
        # Otherwise the test below would pass for the wrong reason.
        snapshot = leaky_snapshot()
        for section in ("metrics", "env", "postgres", "redis", "kubernetes", "n8n_version"):
            self.assertIn(MARK, json.dumps(asdict(snapshot)[section]).lower(), section)
        evidence = " ".join(f.evidence for f in engine.run(snapshot, ALL_RULES))
        self.assertIn(MARK, evidence.lower())

    def test_nothing_seeded_reaches_the_profile(self):
        built = self.assert_clean(leaky_snapshot())
        # ... and the profile is not clean merely because it is empty.
        facts = {f["id"]: f for f in built["facts"]}
        self.assertEqual(facts["executions.mode"]["provenance"], "unknown")  # EXECUTIONS_MODE_FILE is there
        self.assertEqual(facts["database.engine"]["value"], "postgresdb")
        self.assertEqual(
            facts["resources.main"]["value"],
            {"requests": {"cpu": "250m", "memory": None}, "limits": {"cpu": None, "memory": "1Gi"}},
        )
        # The note about what was left out is in the text the assertions above went through.
        self.assertEqual(facts["resources.main"]["detail"], "1 other container with n8n in its image name was left out")
        self.assertEqual(facts["main.replicas"]["value"], 2)
        self.assertEqual(facts["database.version"]["value"], "16.4")
        self.assertEqual(facts["n8n.version"]["value"], "2.40.1")
        self.assertEqual(facts["n8n.image_official"]["value"], False)
        self.assertEqual(facts["worker.replicas"]["value"], 2)
        states = {profile.setting_id(s): s["state"] for s in built["settings"]}
        self.assertEqual(states["setting:instance:EXECUTIONS_MODE"], "value")
        for name in ("N8N_HOST", "QUEUE_BULL_REDIS_CLUSTER_NODES", "N8N_LICENSE_TENANT_ID", "N8N_PORT", "N8N_METRICS"):
            self.assertEqual(states[f"setting:instance:{name}"], "set")
        self.assertGreater(built["settings_not_listed"], 0)
        self.assertTrue(built["check"]["findings"])

    def test_nothing_seeded_reaches_the_profile_from_kubernetes_alone(self):
        snapshot = leaky_snapshot()
        snapshot.env = None
        built = self.assert_clean(snapshot)
        self.assertIn("setting:main:EXECUTIONS_MODE", {profile.setting_id(s) for s in built["settings"]})

    def test_details_are_fixed_text(self):
        # A detail never quotes what was observed: the same sentences for any input.
        clean = Snapshot(env={}, kubernetes=kube(("a", 1, container())), postgres={}, redis={})
        details = lambda snapshot: {f["detail"] for f in build(snapshot)["facts"] if f["provenance"] == "unknown"}
        known = details(clean) | details(Snapshot()) | details(Snapshot(kubernetes=kube()))
        leaky = leaky_snapshot()
        without_env = leaky_snapshot()
        without_env.env = None
        for snapshot in (leaky, without_env):
            for detail in details(snapshot):
                self.assertNotIn(MARK, detail.lower())
                self.assertRegex(detail, r"^[A-Za-z0-9 ,:\-]+$")
        self.assertTrue(known)


# --- facts -------------------------------------------------------------------------

# (fact id, variable, raw value, expected value, value when provably unset or None)
ENV_FACTS = [
    ("executions.mode", "EXECUTIONS_MODE", " Queue ", "queue", "regular"),
    ("multi_main.enabled", "N8N_MULTI_MAIN_SETUP_ENABLED", "true", True, False),
    ("task_runners.enabled", "N8N_RUNNERS_ENABLED", "false", False, None),
    ("task_runners.mode", "N8N_RUNNERS_MODE", "external", "external", None),
    ("binary_data.mode", "N8N_DEFAULT_BINARY_DATA_MODE", "filesystem", "filesystem", None),
    ("pruning.enabled", "EXECUTIONS_DATA_PRUNE", "1", True, None),
    ("pruning.max_age_hours", "EXECUTIONS_DATA_MAX_AGE", "336", 336, None),
    ("database.engine", "DB_TYPE", "postgresdb", "postgresdb", "sqlite"),
    ("database.pool_size", "DB_POSTGRESDB_POOL_SIZE", "4", 4, None),
]

# (fact id, section, key, raw value, expected value)
SECTION_FACTS = [
    ("database.version", "postgres", "version", "16.4 (Debian 16.4-1.pgdg120+1)", "16.4"),
    ("database.max_connections", "postgres", "max_connections", 100, 100),
    ("database.execution_count", "postgres", "execution_count", 1200, 1200),
    ("database.execution_table_bytes", "postgres", "execution_table_bytes", 5_000_000, 5_000_000),
    ("database.oldest_execution_age_days", "postgres", "oldest_execution_age_days", 3.456, 3.46),
    ("queue.redis_version", "redis", "version", "7.2.4", "7.2.4"),
    ("queue.maxmemory_policy", "redis", "maxmemory_policy", "noeviction", "noeviction"),
    ("queue.maxmemory_bytes", "redis", "maxmemory_bytes", 0, 0),
    ("queue.waiting_jobs", "redis", "waiting_jobs", 7, 7),
    ("queue.active_jobs", "redis", "active_jobs", 2, 2),
    ("queue.failed_jobs", "redis", "failed_jobs", 0, 0),
]

KUBE_FACTS = [
    "main.replicas", "worker.replicas", "webhook.replicas", "worker.concurrency",
    "resources.main", "resources.worker", "probes.main", "probes.worker",
    "n8n.image_official", "orchestrator",
]  # fmt: skip


class CatalogueTest(unittest.TestCase):
    def test_every_table_in_this_file_covers_the_catalogue(self):
        covered = {row[0] for row in ENV_FACTS + SECTION_FACTS} | set(KUBE_FACTS)
        self.assertEqual(covered | {"n8n.version", "metrics.enabled"}, set(profile.FACT_IDS))

    def test_full_catalogue_in_order_whatever_was_collected(self):
        for snapshot in (Snapshot(), Snapshot(env={}), leaky_snapshot()):
            facts = build(snapshot)["facts"]
            self.assertEqual(tuple(f["id"] for f in facts), profile.FACT_IDS)
            for f in facts:
                self.assertEqual(set(f), {"id", "value", "provenance", "source", "detail"})
                self.assertIn(f["provenance"], ("measured", "unknown"))  # never declared
                if f["provenance"] == "unknown":
                    self.assertEqual((f["value"], f["source"]), (None, None))
                    self.assertTrue(f["detail"])
                else:
                    self.assertIsNotNone(f["value"])
                    self.assertIn(f["source"], build(snapshot)["sources"])

    def test_nothing_collected_means_every_fact_is_unknown(self):
        for f in build(Snapshot())["facts"]:
            self.assertEqual(f["provenance"], "unknown", f["id"])

    def test_every_fact_id_is_documented(self):
        with open(DOCS, encoding="utf-8") as handle:
            catalogue = handle.read().split("### Catalogue, version 1")[1].split("\nValue shapes:")[0]
        documented = set(re.findall(r"^\| `([a-z0-9_.]+)` \|", catalogue, re.MULTILINE))
        self.assertEqual(documented, set(profile.FACT_IDS))


class EnvFactTest(unittest.TestCase):
    def each(self):
        for row in ENV_FACTS:
            with self.subTest(fact=row[0]):
                yield row

    def test_measured_from_the_env_file(self):
        for fact_id, name, raw, expected, _ in self.each():
            found = fact(Snapshot(env={name: raw}), fact_id)
            self.assertEqual((found["value"], found["provenance"], found["source"]), (expected, "measured", "env"))
            self.assertIs(type(found["value"]), type(expected))

    def test_measured_from_the_main_containers(self):
        for fact_id, name, raw, expected, _ in self.each():
            # Two main Deployments that differ in another variable still agree on this one.
            snapshot = Snapshot(kubernetes=main_kube({name: raw, "N8N_PORT": "5678"}, {name: raw, "N8N_PORT": "5679"}))
            found = fact(snapshot, fact_id)
            self.assertEqual((found["value"], found["provenance"], found["source"]), (expected, "measured", "kubernetes"))

    def test_env_file_wins_over_kubernetes(self):
        for fact_id, name, raw, expected, _ in self.each():
            snapshot = Snapshot(env={name: raw}, kubernetes=main_kube({name: "something-else"}))
            self.assertEqual(fact(snapshot, fact_id)["value"], expected)

    def test_unset_gives_the_default_only_where_it_is_stable(self):
        for fact_id, _name, _raw, _expected, default in self.each():
            for snapshot, source in ((Snapshot(env={}), "env"), (Snapshot(kubernetes=main_kube({})), "kubernetes")):
                found = fact(snapshot, fact_id)
                if default is None:
                    self.assertEqual(found["provenance"], "unknown")
                    self.assertEqual(found["detail"], "not set, the n8n default for this version applies")
                else:
                    self.assertEqual((found["value"], found["source"]), (default, source))
                    self.assertEqual(found["detail"], "n8n default, variable not set")
        stable = {row[0] for row in ENV_FACTS if row[4] is not None}
        self.assertEqual(stable, {"executions.mode", "database.engine", "multi_main.enabled"})

    def test_unknown_when_the_source_is_missing(self):
        for fact_id, *_ in self.each():
            self.assertEqual(fact(Snapshot(), fact_id)["detail"], "The n8n environment was not provided")
            self.assertEqual(
                fact(Snapshot(), fact_id, failed=("env",))["detail"], "The n8n environment could not be collected"
            )
            # Kubernetes without a main container says nothing about the main process.
            only_worker = Snapshot(kubernetes=kube(("w", 1, worker())))
            self.assertEqual(fact(only_worker, fact_id)["provenance"], "unknown")

    def test_unknown_when_the_environment_is_incomplete(self):
        for fact_id, *_ in self.each():
            snapshot = Snapshot(kubernetes=main_kube({}, env_complete=False))
            found = fact(snapshot, fact_id)
            self.assertEqual((found["provenance"], found["value"]), ("unknown", None))
            self.assertEqual(found["detail"], "part of the environment could not be read")

    def test_unknown_when_the_value_cannot_be_read(self):
        for fact_id, name, *_ in self.each():
            for snapshot in (Snapshot(env={name: REDACTED}), Snapshot(kubernetes=main_kube({name: "<valueFrom>"}))):
                found = fact(snapshot, fact_id)
                self.assertEqual((found["provenance"], found["value"]), ("unknown", None))

    def test_unknown_when_the_main_containers_disagree(self):
        for fact_id, name, raw, *_ in self.each():
            for other in ({}, {name: "0"}):  # one does not set it, or sets something else
                found = fact(Snapshot(kubernetes=main_kube({name: raw}, other)), fact_id)
                self.assertEqual(found["provenance"], "unknown")
                self.assertEqual(found["detail"], "the main containers disagree on this setting")

    def test_unknown_when_the_value_has_the_wrong_shape(self):
        for fact_id, name, *_ in self.each():
            for raw in ("", "n8n.internal.example", "-5", "maybe", "12.5"):
                found = fact(Snapshot(env={name: raw}), fact_id)
                self.assertEqual((found["provenance"], found["value"]), ("unknown", None), raw)


class DefaultsTest(unittest.TestCase):
    """A default is a fact only when the variable is provably not set."""

    DEFAULTS = [row for row in ENV_FACTS if row[4] is not None]

    def test_a_file_variant_makes_the_fact_unknown(self):
        for fact_id, name, raw, *_ in ENV_FACTS:
            for env in ({name + "_FILE": "/run/secrets/x"}, {name: raw, name + "_FILE": "/run/secrets/x"}):
                for snapshot in (Snapshot(env=env), Snapshot(kubernetes=main_kube(env))):
                    with self.subTest(fact=fact_id, env=sorted(env)):
                        found = fact(snapshot, fact_id)
                        self.assertEqual((found["value"], found["provenance"]), (None, "unknown"))
                        self.assertEqual(found["detail"], "set through a file, which this tool does not read")

    def test_no_default_for_an_image_that_is_not_the_official_one(self):
        custom = "registry.example/platform/n8n:7.3.1"
        for fact_id, _name, _raw, _expected, default in self.DEFAULTS:
            with self.subTest(fact=fact_id):
                for snapshot in (
                    Snapshot(kubernetes=main_kube({}, image=custom)),
                    Snapshot(env={}, kubernetes=main_kube({}, image=custom)),
                    Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(image=custom)))),
                ):
                    found = fact(snapshot, fact_id)
                    self.assertEqual((found["value"], found["provenance"]), (None, "unknown"))
                    self.assertIn("the image is not the official one", found["detail"])
                # A value that is set is still read from a custom image.
                self.assertEqual(fact(Snapshot(env={}), fact_id)["value"], default)

    def test_a_set_value_is_read_whatever_the_image(self):
        snapshot = Snapshot(kubernetes=main_kube({"EXECUTIONS_MODE": "queue"}, image="example/custom-n8n:1"))
        self.assertEqual(fact(snapshot, "executions.mode")["value"], "queue")


class SectionFactTest(unittest.TestCase):
    def test_measured_missing_failed_and_wrong_shape(self):
        for fact_id, section, key, raw, expected in SECTION_FACTS:
            with self.subTest(fact=fact_id):
                found = fact(Snapshot(**{section: {key: raw}}), fact_id)
                self.assertEqual((found["value"], found["provenance"], found["source"]), (expected, "measured", section))
                label = "Postgres" if section == "postgres" else "Redis"
                self.assertEqual(fact(Snapshot(), fact_id)["detail"], f"{label} was not provided")
                self.assertEqual(fact(Snapshot(), fact_id, (section,))["detail"], f"{label} could not be collected")
                for bad in (None, "", "db.internal.example", -1, float("nan"), "allkeys-custom", "192.168.77.5", "7.2.4.1"):
                    found = fact(Snapshot(**{section: {key: bad}}), fact_id)
                    self.assertEqual((found["provenance"], found["value"]), ("unknown", None), bad)

    def test_an_empty_execution_table_has_no_oldest_execution(self):
        # The Postgres collector stores 0.0 for an empty table.
        fact_id = "database.oldest_execution_age_days"
        empty = fact(Snapshot(postgres={"execution_count": 0, "oldest_execution_age_days": 0.0}), fact_id)
        self.assertEqual((empty["value"], empty["provenance"], empty["source"]), (None, "unknown", None))
        self.assertEqual(empty["detail"], "there are no stored executions")
        # ... and 0 without a count proves nothing either.
        self.assertEqual(fact(Snapshot(postgres={"oldest_execution_age_days": 0.0}), fact_id)["provenance"], "unknown")
        # An execution that stopped a moment ago is a real 0.
        fresh = fact(Snapshot(postgres={"execution_count": 3, "oldest_execution_age_days": 0.0}), fact_id)
        self.assertEqual((fresh["value"], fresh["provenance"]), (0, "measured"))
        aged = fact(Snapshot(postgres={"execution_count": 3, "oldest_execution_age_days": 12.5}), fact_id)
        self.assertEqual(aged["value"], 12.5)
        self.assertEqual(fact(Snapshot(postgres={"execution_count": 0}), "database.execution_count")["value"], 0)

    def test_counts_are_whole_numbers(self):
        self.assertEqual(fact(Snapshot(redis={"waiting_jobs": 2.5}), "queue.waiting_jobs")["provenance"], "unknown")
        # The queue key was not found in Redis: the collector stores None.
        self.assertEqual(fact(Snapshot(redis={"waiting_jobs": None}), "queue.waiting_jobs")["provenance"], "unknown")


class KubernetesFactTest(unittest.TestCase):
    def test_unknown_without_kubernetes(self):
        for fact_id in KUBE_FACTS:
            with self.subTest(fact=fact_id):
                self.assertEqual(fact(Snapshot(), fact_id)["detail"], "Kubernetes was not provided")
                self.assertEqual(
                    fact(Snapshot(), fact_id, failed=("kubernetes",))["detail"], "Kubernetes could not be collected"
                )

    def test_orchestrator(self):
        self.assertEqual(fact(Snapshot(kubernetes=kube()), "orchestrator")["value"], "kubernetes")

    def test_replicas_are_summed_per_role(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("main", 1, container()),
                ("worker-a", 2, worker()),
                ("worker-b", 3, worker(command=["sh", "-c", "n8n worker --concurrency=5"])),
                ("redis", 4, container(image="redis:7")),
            )
        )
        self.assertEqual(fact(snapshot, "main.replicas")["value"], 1)
        self.assertEqual(fact(snapshot, "worker.replicas")["value"], 5)
        # A role with no container is unknown, never 0.
        webhook = fact(snapshot, "webhook.replicas")
        self.assertEqual((webhook["value"], webhook["provenance"]), (None, "unknown"))

    def test_scaled_to_zero_is_a_measured_zero(self):
        snapshot = Snapshot(kubernetes=kube(("worker", 0, worker())))
        found = fact(snapshot, "worker.replicas")
        self.assertEqual((found["value"], found["provenance"]), (0, "measured"))

    def test_a_deployment_with_two_containers_of_a_role_counts_once(self):
        snapshot = Snapshot(kubernetes=kube(("main", 2, container())))
        snapshot.kubernetes["deployments"][0]["containers"].append(container())
        self.assertEqual(fact(snapshot, "main.replicas")["value"], 2)

    EXPORTER = "example/n8n-metrics-exporter:1.0.0"
    MCP = "example/n8n-mcp:0.4.0"

    def beside_unrelated(self, env=None):
        """Two n8n replicas next to an exporter and another server whose names contain "n8n"."""
        return Snapshot(
            kubernetes=kube(
                ("n8n", 2, container(env=env or {})),
                ("exporter", 1, container(image=self.EXPORTER, env={"EXECUTIONS_MODE": "regular", "N8N_PORT": "9"})),
                ("mcp", 3, container(image=self.MCP, requests={}, limits={}, liveness_probe=False)),
            )
        )

    def test_unrelated_images_are_left_out_of_the_main_role_never_counted(self):
        snapshot = self.beside_unrelated({"EXECUTIONS_MODE": "queue", "N8N_PORT": "5678"})
        for fact_id, expected in (
            ("main.replicas", 2),  # not 6
            ("probes.main", {"liveness": True, "readiness": True}),
            ("resources.main", {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"cpu": None, "memory": "1Gi"}}),
            ("n8n.version", "2.40.1"),
            ("n8n.image_official", True),
            ("executions.mode", "queue"),
            ("database.engine", "sqlite"),
        ):
            found = fact(snapshot, fact_id)
            self.assertEqual((found["value"], found["provenance"]), (expected, "measured"), fact_id)
        self.assertEqual(
            settings(snapshot),
            {"setting:main:EXECUTIONS_MODE": ("value", "queue"), "setting:main:N8N_PORT": ("value", "5678")},
        )

    def test_the_reader_is_told_how_many_containers_were_left_out(self):
        # A main built under another name cannot be told from an exporter: say that
        # something was left out, with the count only.
        one = "1 other container with n8n in its image name was left out"
        two = "2 other containers with n8n in their image name were left out"
        snapshot = self.beside_unrelated({"EXECUTIONS_MODE": "queue"})
        for fact_id, detail in (
            ("main.replicas", two),
            ("resources.main", two),
            ("probes.main", two),
            ("n8n.image_official", two),
            ("n8n.version", "from the image tag; " + two),
            ("executions.mode", two),
            ("database.engine", "n8n default, variable not set; " + two),
        ):
            found = fact(snapshot, fact_id)
            self.assertEqual((found["provenance"], found["detail"]), ("measured", detail), fact_id)
        text = profile.to_json(build(snapshot)) + profile.render_markdown(build(snapshot))
        for hidden in ("exporter", "mcp", "example"):
            self.assertNotIn(hidden, text)
        single = Snapshot(kubernetes=kube(("n8n", 1, container()), ("x", 5, container(image=self.EXPORTER)), ("w", 2, worker())))
        self.assertEqual(fact(single, "main.replicas")["detail"], one)
        # Not on facts that do not come from the main containers, nor when nothing was left out.
        for fact_id in ("worker.replicas", "worker.concurrency", "resources.worker", "probes.worker", "orchestrator"):
            self.assertNotIn("left out", fact(single, fact_id)["detail"], fact_id)
        with_file = Snapshot(env={"EXECUTIONS_MODE": "queue"}, kubernetes=single.kubernetes)
        self.assertEqual(fact(with_file, "executions.mode")["detail"], "")  # read from the env file
        self.assertEqual(fact(with_file, "main.replicas")["detail"], one)
        clean = Snapshot(kubernetes=kube(("n8n", 1, container()), ("w", 2, worker())))
        for found in build(clean)["facts"]:
            self.assertNotIn("left out", found["detail"])

    def test_a_role_with_no_image_called_n8n_stays_unknown(self):
        # A build of n8n under another name cannot be told from an unrelated image.
        for image in (self.MCP, "example/n8n-custom:7.3.1"):
            alone = Snapshot(kubernetes=kube(("x", 3, container(image=image))))
            for fact_id in ("main.replicas", "resources.main", "probes.main", "n8n.version"):
                found = fact(alone, fact_id)
                self.assertEqual((found["value"], found["provenance"]), (None, "unknown"), fact_id)
            self.assertEqual(
                fact(alone, "main.replicas")["detail"], "a container of this role runs an image that may not be n8n itself"
            )

    def test_a_worker_or_webhook_under_another_name_makes_its_role_unknown(self):
        # It carries the role's own command, so it may well be n8n: neither counted nor left out.
        snapshot = Snapshot(
            kubernetes=kube(
                ("main", 1, container()),
                ("w1", 2, worker()),
                ("w2", 1, worker(image="example/n8n-worker:1")),
                ("h1", 1, container(command=["webhook"])),
                ("h2", 1, container(command=["webhook"], image="example/n8n-hooks:1")),
            )
        )
        for fact_id in ("worker.replicas", "resources.worker", "probes.worker", "worker.concurrency", "webhook.replicas"):
            found = fact(snapshot, fact_id)
            self.assertEqual((found["value"], found["provenance"]), (None, "unknown"), fact_id)
        self.assertEqual(fact(snapshot, "main.replicas")["value"], 1)

    def test_a_copy_of_n8n_in_another_registry_is_still_n8n(self):
        mirror = Snapshot(kubernetes=kube(("a", 2, container()), ("b", 1, container(image="registry.example:5000/mirror/n8n:2.40.1"))))
        self.assertEqual(fact(mirror, "main.replicas")["value"], 3)

    def test_replicas_unknown_when_not_stated(self):
        snapshot = Snapshot(kubernetes=kube(("main", None, container())))
        self.assertEqual(fact(snapshot, "main.replicas")["provenance"], "unknown")

    def test_resources(self):
        snapshot = Snapshot(kubernetes=kube(("main", 1, container()), ("w", 1, worker(limits={"cpu": 2, "memory": "1.5Gi"}))))
        self.assertEqual(
            fact(snapshot, "resources.main")["value"],
            {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"cpu": None, "memory": "1Gi"}},
        )
        self.assertEqual(fact(snapshot, "resources.worker")["value"]["limits"], {"cpu": "2", "memory": "1.5Gi"})

    def test_resources_unknown_on_disagreement_or_an_odd_quantity(self):
        differ = Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(limits={"memory": "2Gi"}))))
        self.assertEqual(fact(differ, "resources.main")["provenance"], "unknown")
        for quantity in ("10.0.0.1", "db.internal", "1 Gi", "", "/dev/shm", "1Gi;"):
            odd = Snapshot(kubernetes=kube(("a", 1, container(requests={"cpu": quantity}))))
            self.assertEqual(fact(odd, "resources.main")["provenance"], "unknown", quantity)

    def test_resources_only_carry_cpu_and_memory(self):
        snapshot = Snapshot(kubernetes=kube(("a", 1, container(requests={"vendor.example/gpu": "1", "cpu": "1"}))))
        self.assertEqual(fact(snapshot, "resources.main")["value"]["requests"], {"cpu": "1", "memory": None})

    def test_probes(self):
        snapshot = Snapshot(kubernetes=kube(("main", 1, container(readiness_probe=False))))
        self.assertEqual(fact(snapshot, "probes.main")["value"], {"liveness": True, "readiness": False})
        differ = Snapshot(kubernetes=kube(("a", 1, worker()), ("b", 1, worker(liveness_probe=False))))
        self.assertEqual(fact(differ, "probes.worker")["provenance"], "unknown")
        missing = Snapshot(kubernetes=kube(("a", 1, container(liveness_probe=None))))
        self.assertEqual(fact(missing, "probes.main")["provenance"], "unknown")

    def test_image_official(self):
        for image, expected in (
            ("n8nio/n8n:2.40.1", True),
            ("docker.n8n.io/n8nio/n8n", True),
            ("docker.io/n8nio/n8n@sha256:" + "a" * 64, True),
            ("registry.example:5000/n8nio/n8n:2.40.1", False),
            ("example/custom-n8n:1", False),
        ):
            with self.subTest(image=image):
                found = fact(Snapshot(kubernetes=kube(("main", 1, container(image=image)))), "n8n.image_official")
                self.assertEqual((found["value"], found["provenance"]), (expected, "measured"))
        mixed = Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, worker(image="example/custom-n8n:1"))))
        self.assertEqual(fact(mixed, "n8n.image_official")["provenance"], "unknown")
        none = Snapshot(kubernetes=kube(("redis", 1, container(image="redis:7"))))
        self.assertEqual(fact(none, "n8n.image_official")["provenance"], "unknown")


class VersionAndMetricsTest(unittest.TestCase):
    def test_version_reported_by_n8n_wins(self):
        snapshot = Snapshot(n8n_version="2.47.0", kubernetes=kube(("main", 1, container())))
        found = fact(snapshot, "n8n.version")
        self.assertEqual((found["value"], found["source"]), ("2.47.0", "metrics"))
        self.assertEqual(fact(Snapshot(n8n_version="2.47.0-exp.1+host"), "n8n.version")["value"], "2.47.0")

    def test_version_from_the_image_tag_when_every_main_agrees(self):
        same = Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container()), ("w", 1, worker(image="n8nio/n8n:1.0.0"))))
        found = fact(same, "n8n.version")
        self.assertEqual((found["value"], found["source"], found["detail"]), ("2.40.1", "kubernetes", "from the image tag"))
        for images in (("n8nio/n8n:2.40.1", "n8nio/n8n:2.41.0"), ("n8nio/n8n:latest",), ("n8nio/n8n@sha256:abc",)):
            snapshot = Snapshot(kubernetes=kube(*((f"m{i}", 1, container(image=image)) for i, image in enumerate(images))))
            self.assertEqual(fact(snapshot, "n8n.version")["provenance"], "unknown", images)
        self.assertEqual(fact(Snapshot(kubernetes=kube(("w", 1, worker()))), "n8n.version")["provenance"], "unknown")

    def test_no_version_from_the_tag_of_an_image_that_is_not_official(self):
        for images in (("registry.example/platform/n8n-custom:7.3.1",), ("n8nio/n8n:2.40.1", "example/n8n:2.40.1")):
            snapshot = Snapshot(kubernetes=kube(*((f"m{i}", 1, container(image=image)) for i, image in enumerate(images))))
            found = fact(snapshot, "n8n.version")
            self.assertEqual((found["value"], found["provenance"]), (None, "unknown"), images)
        # ... but what n8n reports about itself is taken whatever the image.
        custom = Snapshot(n8n_version="2.40.1", kubernetes=kube(("m", 1, container(image="example/n8n-custom:7.3.1"))))
        self.assertEqual(fact(custom, "n8n.version")["value"], "2.40.1")

    def test_a_version_is_not_the_start_of_a_longer_dotted_number(self):
        for raw in ("192.168.77.5", "2.47.0.1", "10.0.0.1:5678", "2.47."):
            self.assertEqual(fact(Snapshot(n8n_version=raw), "n8n.version")["provenance"], "unknown", raw)
            tagged = Snapshot(kubernetes=kube(("m", 1, container(image=f"n8nio/n8n:{raw.split(':')[0]}"))))
            self.assertEqual(fact(tagged, "n8n.version")["provenance"], "unknown", raw)
        for raw, expected in (("2.47.0", "2.47.0"), ("16.4 (Debian 16.4-1)", "16.4"), ("7.2.4-rc1", "7.2.4"), ("2.47", "2.47")):
            self.assertEqual(fact(Snapshot(n8n_version=raw), "n8n.version")["value"], expected)
        self.assertEqual(fact(Snapshot(n8n_version="unknown"), "n8n.version")["provenance"], "unknown")

    def test_metrics_enabled(self):
        self.assertEqual(fact(Snapshot(metrics={}), "metrics.enabled")["value"], True)
        disabled = fact(Snapshot(health={"metrics_disabled": True}), "metrics.enabled")
        self.assertEqual((disabled["value"], disabled["provenance"], disabled["source"]), (False, "measured", "health"))
        for snapshot in (Snapshot(), Snapshot(health={"healthz": True}), Snapshot(health={"metrics_disabled": False})):
            self.assertEqual(fact(snapshot, "metrics.enabled")["provenance"], "unknown")
        failed = fact(Snapshot(health={"healthz": True}), "metrics.enabled", failed=("metrics",))
        self.assertEqual(failed["detail"], "The metrics endpoint could not be collected")


class WorkerConcurrencyTest(unittest.TestCase):
    def value(self, snapshot):
        found = fact(snapshot, "worker.concurrency")
        return found["value"], found["provenance"], found["source"]

    def test_an_env_file_alone_is_unknown(self):
        snapshot = Snapshot(env={"EXECUTIONS_MODE": "queue", LIMIT: "20"})
        self.assertEqual(self.value(snapshot), (None, "unknown", None))

    def test_no_worker_container_is_unknown(self):
        self.assertEqual(self.value(Snapshot(kubernetes=main_kube({LIMIT: "20"})))[1], "unknown")

    def test_precedence(self):
        for env, command, expected in (
            ({LIMIT: "8"}, ["worker", "--concurrency=3"], 8),  # the limit overrides the flag
            ({LIMIT: "-1"}, ["worker", "--concurrency=3"], 3),  # -1 disables the limit
            ({}, ["worker", "--concurrency", "4"], 4),
            ({}, ["worker"], 10),  # the worker default
            ({LIMIT: "-1"}, ["worker"], 10),
        ):
            with self.subTest(env=env, command=command):
                snapshot = Snapshot(kubernetes=kube(("w", 2, worker(env, command))))
                self.assertEqual(self.value(snapshot), (expected, "measured", "kubernetes"))

    def test_env_file_limit_in_queue_mode_comes_first(self):
        workers = kube(("w", 2, worker({LIMIT: "8"}, ["worker", "--concurrency=3"])))
        queue = Snapshot(env={"EXECUTIONS_MODE": "queue", LIMIT: "6"}, kubernetes=workers)
        self.assertEqual(self.value(queue), (6, "measured", "env"))
        # Not queue mode in the file, or no usable limit there: the workers are read one by one.
        for env in ({LIMIT: "6"}, {"EXECUTIONS_MODE": "queue"}, {"EXECUTIONS_MODE": "queue", LIMIT: "-1"}):
            self.assertEqual(self.value(Snapshot(env=env, kubernetes=workers)), (8, "measured", "kubernetes"), env)

    def test_unknown_when_it_cannot_be_told(self):
        for workers in (
            [worker({LIMIT: "<valueFrom>"}, ["worker", "--concurrency=3"])],  # may override the flag
            [worker({}, ["worker", "--concurrency=3"], env_complete=False)],  # may be set in a Secret
            [worker({LIMIT: "0"})],  # n8n rejects 0
            [worker({LIMIT: "many"})],
            [worker({}, ["worker", "--concurrency=3"]), worker({}, ["worker", "--concurrency=5"])],  # disagree
        ):
            snapshot = Snapshot(kubernetes=kube(*((f"w{i}", 1, w) for i, w in enumerate(workers))))
            self.assertEqual(self.value(snapshot), (None, "unknown", None))

    def test_a_flag_that_is_not_a_plain_number_is_unknown(self):
        for command in (
            ["sh", "-c", "n8n worker --concurrency=$WORKER_CONC"],
            ["worker", "--concurrency", "$(CONC)"],
            ["worker", "--concurrency="],
            ["worker", "--concurrency"],
        ):
            with self.subTest(command=command):
                found = fact(Snapshot(kubernetes=kube(("w", 1, worker({}, command)))), "worker.concurrency")
                self.assertEqual((found["value"], found["provenance"]), (None, "unknown"))
                self.assertEqual(found["detail"], "the --concurrency flag on a worker is not a plain number")
        # The limit variable overrides the flag, so then the flag does not matter.
        limited = Snapshot(kubernetes=kube(("w", 1, worker({LIMIT: "7"}, ["worker", "--concurrency=$X"]))))
        self.assertEqual(self.value(limited)[0], 7)

    def test_the_default_is_stated_only_when_nothing_can_have_set_it(self):
        official = fact(Snapshot(kubernetes=kube(("w", 1, worker()))), "worker.concurrency")
        self.assertEqual((official["value"], official["provenance"]), (10, "measured"))
        file_variant = {LIMIT + "_FILE": "/run/limit"}
        for label, snapshot in (
            ("limit given through a file on the worker", Snapshot(kubernetes=kube(("w", 1, worker(file_variant))))),
            ("... even with a flag", Snapshot(kubernetes=kube(("w", 1, worker(file_variant, ["worker", "--concurrency=3"]))))),
            ("limit given through a file in the env file", Snapshot(env={"EXECUTIONS_MODE": "queue", **file_variant}, kubernetes=kube(("w", 1, worker())))),
            ("... whatever the mode in the file", Snapshot(env=file_variant, kubernetes=kube(("w", 1, worker())))),
            ("image called n8n in another registry", Snapshot(kubernetes=kube(("w", 1, worker(image="registry.example/n8n:2.40.1"))))),
            ("image under another name", Snapshot(kubernetes=kube(("w", 1, worker(image="example/n8n-worker:1"))))),
            ("... even with a flag", Snapshot(kubernetes=kube(("w", 1, worker({}, ["worker", "--concurrency=3"], image="example/n8n-worker:1"))))),
        ):
            with self.subTest(case=label):
                self.assertEqual(self.value(snapshot), (None, "unknown", None))
        # A number that is written down is read whatever the registry.
        mirror = Snapshot(kubernetes=kube(("w", 1, worker({}, ["worker", "--concurrency=3"], image="registry.example/n8n:2.40.1"))))
        self.assertEqual(self.value(mirror)[0], 3)

    def test_numbers_are_plain_ascii_digits(self):
        for raw in ("\u0664", "1_0", "\uff11\uff10", "+5", "5 0"):
            with self.subTest(raw=raw):
                self.assertEqual(self.value(Snapshot(kubernetes=kube(("w", 1, worker({LIMIT: raw})))))[1], "unknown")
                self.assertEqual(fact(Snapshot(env={"DB_POSTGRESDB_POOL_SIZE": raw}), "database.pool_size")["provenance"], "unknown")
                self.assertIsNone(profile.setting_value("DB_POSTGRESDB_POOL_SIZE", raw))
                self.assertEqual(
                    settings(Snapshot(env={"DB_POSTGRESDB_POOL_SIZE": raw})),
                    {"setting:instance:DB_POSTGRESDB_POOL_SIZE": ("set", None)},
                )
        self.assertEqual(fact(Snapshot(n8n_version="\u0662.\u0664\u0667.\u0660"), "n8n.version")["provenance"], "unknown")
        self.assertEqual(fact(Snapshot(postgres={"version": "\u0661\u0666.\u0664"}), "database.version")["provenance"], "unknown")

    def test_workers_that_reach_the_same_number_differently_agree(self):
        snapshot = Snapshot(kubernetes=kube(("a", 1, worker({LIMIT: "5"})), ("b", 1, worker({}, ["worker", "--concurrency=5"]))))
        self.assertEqual(self.value(snapshot)[0], 5)

    def test_same_number_as_the_scalability_rule_quotes(self):
        # SCAL-004 reads the same precedence; whenever it fires, the fact carries its number.
        for env, workers in (
            (None, kube(("w", 1, worker({LIMIT: "2"}, ["worker", "--concurrency=9"])))),
            (None, kube(("w", 1, worker({}, ["worker", "--concurrency=3"])))),
            (None, kube(("w", 1, worker({LIMIT: "-1"}, ["worker", "--concurrency=4"])))),
            ({"EXECUTIONS_MODE": "queue", LIMIT: "2"}, kube(("w", 1, worker({}, ["worker", "--concurrency=9"])))),
        ):
            snapshot = Snapshot(env=env, kubernetes=workers)
            finding = scal.low_worker_concurrency(snapshot)
            quoted = int(re.search(r"(?:LIMIT|concurrency)=(\d+)", finding.evidence).group(1))
            self.assertEqual(self.value(snapshot)[0], quoted)


# --- sources, settings, check ------------------------------------------------------


class SourcesTest(unittest.TestCase):
    def test_one_state_per_section(self):
        snapshot = Snapshot(health={"metrics_disabled": True}, postgres={}, kubernetes=main_kube({"EXECUTIONS_MODE": "queue"}))
        self.assertEqual(
            build(snapshot, failed=("redis",))["sources"],
            {
                "health": "collected",
                "metrics": "disabled",
                # Read from Kubernetes, yet the env source itself was not provided.
                "env": "not_provided",
                "postgres": "collected",
                "redis": "failed",
                "kubernetes": "collected",
            },
        )
        self.assertEqual(build(Snapshot(health={}), failed=("metrics",))["sources"]["metrics"], "failed")
        self.assertEqual(build(Snapshot(metrics={}))["sources"]["metrics"], "collected")


class SettingsTest(unittest.TestCase):
    def test_only_documented_n8n_variables_are_listed(self):
        env = {
            "N8N_PORT": "5678",
            "EXECUTIONS_MODE": "queue",
            "QUEUE_HEALTH_CHECK_ACTIVE": "true",
            "DB_TYPE": "postgresdb",
            "WEBHOOK_URL": "https://n8n.example.com/",
            "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true",
            "NODE_FUNCTION_ALLOW_BUILTIN": "crypto",
            "NODE_ENV": "production",
            "NODE_OPTIONS": "--max-old-space-size=4096",
            "GENERIC_TIMEZONE": "Europe/Rome",
            "N8N_ENCRYPTION_KEY_FILE": "/run/secrets/key",
            # Not listed, not even by name:
            "ACME_FEATURE_FLAG": "1",
            "PATH": "/usr/bin",
            "NODE_EXTRA_CA_CERTS": "/certs/ca.pem",
            "n8n_port": "1",
            "N8N_odd.name": "1",
            "N8N": "1",
            "N8N_ACMECORP_SSO_BRIDGE": "enabled",  # looks like n8n's, names a company
            "N8N_SMTP_AUTH": "hunter2hunter2",  # not a variable n8n documents
            "ACME_SECRET_FILE": "/run/secrets/acme",
        }
        built = build(Snapshot(env=env))
        self.assertEqual([s["name"] for s in built["settings"]], sorted(list(env)[:11]))
        self.assertEqual(built["settings_not_listed"], 9)
        self.assertEqual({s["scope"] for s in built["settings"]}, {"instance"})
        text = profile.to_json(built) + profile.render_markdown(built)
        for hidden in ("ACME", "n8n_port", "odd.name", "SMTP_AUTH", "hunter2", "NODE_EXTRA"):
            self.assertNotIn(hidden, text)

    def test_value_written_only_for_a_boolean_a_number_or_a_fixed_word(self):
        for name, value, written in (
            ("EXECUTIONS_DATA_PRUNE", "true", "true"),
            ("N8N_METRICS", " FALSE ", "false"),
            ("N8N_PORT", "5678", "5678"),
            ("N8N_CONCURRENCY_PRODUCTION_LIMIT", "-1", "-1"),
            ("N8N_OTEL_TRACES_SAMPLE_RATE", "0.25", "0.25"),
            ("EXECUTIONS_MODE", "queue", "queue"),
            ("N8N_LOG_LEVEL", "debug", "debug"),
            ("N8N_WORKFLOW_CALLER_POLICY_DEFAULT_OPTION", "workflowsFromSameOwner", "workflowsFromSameOwner"),
            ("NODE_ENV", "production", "production"),
        ):
            with self.subTest(name=name):
                self.assertEqual(settings(Snapshot(env={name: value})), {f"setting:instance:{name}": ("value", written)})

    def test_a_value_that_does_not_fit_its_type_is_only_set(self):
        for name, values in (
            ("N8N_METRICS", ("yes", "1", "enabled", "", "acme", "true false")),
            ("N8N_PORT", ("acme", "10.0.0.1", "1e3", "1,000", "5678/tcp", "", "9" * 16, "0.1234567", "--1", "redis:6379")),
            ("EXECUTIONS_MODE", ("Queue", "acme", "", "queue,regular", "queue.internal")),
            ("N8N_LOG_OUTPUT", ("console,file", "/var/log/n8n.log")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    self.assertIsNone(profile.setting_value(name, value))
                    self.assertEqual(settings(Snapshot(env={name: value})), {f"setting:instance:{name}": ("set", None)})

    def test_other_documented_variables_are_never_written_whatever_they_hold(self):
        # Hosts without dots, a relative path, package names, a tenant id, a short secret.
        for name, value in (
            ("QUEUE_BULL_REDIS_CLUSTER_NODES", "acme-redis-0,acme-redis-1"),
            ("N8N_CUSTOM_EXTENSIONS", "acme-nodes"),
            ("NODE_FUNCTION_ALLOW_EXTERNAL", "acme-internal-sdk,lodash"),
            ("N8N_LICENSE_TENANT_ID", "4711"),
            ("DB_POSTGRESDB_SSL_CA", "MIIBacme"),
            ("N8N_HOST", "n8n"),
            ("DB_POSTGRESDB_DATABASE", "acme"),
            ("DB_POSTGRESDB_USER", "acme"),
            ("DB_TABLE_PREFIX", "acme"),
            ("QUEUE_BULL_PREFIX", "acme"),
            ("N8N_ENDPOINT_WEBHOOK", "acme"),
            ("N8N_DEFAULT_LOCALE", "en"),
            ("GENERIC_TIMEZONE", "UTC"),
            ("NODES_EXCLUDE", "[]"),
            ("N8N_EMAIL_MODE", "smtp"),
            ("N8N_ENCRYPTION_KEY_FILE", "true"),
        ):
            with self.subTest(name=name):
                self.assertIsNone(profile.setting_value(name, value))
                self.assertEqual(settings(Snapshot(env={name: value})), {f"setting:instance:{name}": ("set", None)})

    def test_an_unreadable_value_is_refused_by_its_own_check(self):
        # Explicit, not left to the type tests: this fails if the check is removed.
        for value in (REDACTED, "<valueFrom>", None, 5, True):
            self.assertFalse(profile.value_readable(value), value)
        self.assertTrue(profile.value_readable("true"))
        for name in ("N8N_METRICS", "N8N_PORT", "EXECUTIONS_MODE"):
            for value in (REDACTED, "<valueFrom>"):
                self.assertIsNone(profile.setting_value(name, value))
        # ... and setting_value() really asks it: no marker fits a type today, so only
        # this shows that the call is still there.
        self.assertEqual(profile.setting_value("N8N_METRICS", "true"), "true")
        with mock.patch("n8n_healthcheck.profile.value_readable", return_value=False) as asked:
            self.assertIsNone(profile.setting_value("N8N_METRICS", "true"))
            self.assertIsNone(profile.setting_value("N8N_PORT", "5678"))
            self.assertIsNone(profile.setting_value("EXECUTIONS_MODE", "queue"))
        self.assertEqual(asked.call_count, 3)

    def test_the_known_list_is_well_formed(self):
        groups = (n8n_settings.BOOLEANS, n8n_settings.NUMBERS, frozenset(n8n_settings.ENUMS), n8n_settings.SET_ONLY)
        self.assertEqual(sum(len(group) for group in groups), len(n8n_settings.KNOWN))  # no name in two groups
        for name in n8n_settings.KNOWN:
            self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")
        for name, words in n8n_settings.ENUMS.items():
            for word in words:
                self.assertRegex(word, r"^[A-Za-z0-9_]{1,40}$", name)
        # Numbers and words that identify a customer, or free text, are not in a value group.
        for name in ("N8N_LICENSE_TENANT_ID", "QUEUE_BULL_REDIS_CLUSTER_NODES", "NODE_FUNCTION_ALLOW_EXTERNAL",
                     "N8N_CUSTOM_EXTENSIONS", "N8N_HOST", "WEBHOOK_URL", "NODE_OPTIONS", "DB_POSTGRESDB_SSL_CA"):  # fmt: skip
            self.assertIn(name, n8n_settings.SET_ONLY)
        # The facts read these with the same words as the settings list.
        for name in ("EXECUTIONS_MODE", "DB_TYPE", "N8N_RUNNERS_MODE", "N8N_DEFAULT_BINARY_DATA_MODE"):
            self.assertIn(name, n8n_settings.ENUMS)

    def test_kubernetes_scopes_are_the_roles(self):
        snapshot = Snapshot(
            kubernetes=kube(
                ("main", 1, container(env={"EXECUTIONS_MODE": "queue", "ACME": "1"})),
                ("worker-a", 1, worker({"EXECUTIONS_MODE": "queue", "N8N_LOG_LEVEL": "debug", "N8N_PORT": "1"})),
                ("worker-b", 1, worker({"EXECUTIONS_MODE": "queue", "N8N_LOG_LEVEL": "info"})),
                ("redis", 1, container(image="redis:7", env={"N8N_PORT": "1"})),
            )
        )
        self.assertEqual(
            settings(snapshot),
            {
                "setting:main:EXECUTIONS_MODE": ("value", "queue"),
                "setting:worker:EXECUTIONS_MODE": ("value", "queue"),
                "setting:worker:N8N_LOG_LEVEL": ("set", None),  # the workers disagree
                "setting:worker:N8N_PORT": ("set", None),  # one worker does not have it
            },
        )
        self.assertEqual(build(snapshot)["settings_not_listed"], 1)

    def test_both_sources_are_listed(self):
        snapshot = Snapshot(env={"DB_TYPE": "postgresdb"}, kubernetes=main_kube({"DB_TYPE": "sqlite"}))
        self.assertEqual(
            settings(snapshot),
            {"setting:instance:DB_TYPE": ("value", "postgresdb"), "setting:main:DB_TYPE": ("value", "sqlite")},
        )

    def test_unreadable_values_are_only_set(self):
        snapshot = Snapshot(kubernetes=main_kube({"N8N_LOG_LEVEL": "<valueFrom>", "N8N_RUNNERS_MODE": REDACTED}))
        self.assertEqual(set(settings(snapshot).values()), {("set", None)})

    def test_no_environment_no_settings(self):
        built = build(Snapshot(postgres={}))
        self.assertEqual((built["settings"], built["settings_not_listed"]), ([], 0))


def finding(rule_id="REL-002", severity="warning", area="reliability"):
    return Finding(
        rule_id=rule_id,
        area=area,
        severity=severity,
        title="A fixed title",
        evidence="DB_POSTGRESDB_HOST=db.internal.example",
        impact="impact text",
        recommendation="recommendation text",
    )


class CheckAndRunTest(unittest.TestCase):
    def test_check_keeps_the_outcome_without_the_evidence(self):
        skipped = finding(engine.SKIPPED_RULE_ID, "info", "maintainability")
        built = build(Snapshot(env={}), [finding(), finding("SEC-001", "critical", "security"), skipped])
        check = built["check"]
        self.assertTrue(check["partial"])
        self.assertEqual(
            check["findings"],
            [
                {"rule_id": "SEC-001", "severity": "critical", "area": "security", "title": "A fixed title"},
                {"rule_id": "REL-002", "severity": "warning", "area": "reliability", "title": "A fixed title"},
            ],
        )
        self.assertIn({"area": "reliability", "score": 85, "partial": True}, check["scores"])
        text = profile.to_json(built)
        for hidden in ("db.internal.example", "impact text", "recommendation text", engine.SKIPPED_RULE_ID):
            self.assertNotIn(hidden, text)

    def test_header_fields(self):
        now = datetime(2026, 11, 18, 10, 22, 5, tzinfo=timezone.utc)
        built = build(Snapshot(), reason="pre_upgrade", reference="  upgrade to 2.50, change 1234 ", now=now)
        self.assertEqual(
            list(built),
            ["format", "format_version", "tool_version", "created_at", "run", "sources", "facts", "settings", "settings_not_listed", "check"],
        )
        self.assertEqual((built["format"], built["format_version"]), ("n8n-healthcheck-profile", 1))
        self.assertEqual(built["tool_version"], n8n_healthcheck.__version__)
        self.assertEqual(built["created_at"], "2026-11-18T10:22:05Z")
        self.assertEqual(built["run"], {"reason": "pre_upgrade", "reference": "upgrade to 2.50, change 1234"})
        self.assertEqual(build(Snapshot())["run"], {"reason": "baseline", "reference": ""})
        self.assertRegex(build(Snapshot())["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_reason_and_reference_are_checked(self):
        for kwargs in ({"reason": "because"}, {"reference": "x" * 201}, {"reference": "two\nlines"}, {"reference": "a\x00b"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(profile.ProfileError):
                build(Snapshot(), **kwargs)
        self.assertEqual(len(build(Snapshot(), reference="x" * 200)["run"]["reference"]), 200)

    def test_the_file_is_strict_json(self):
        snapshot = Snapshot(postgres={"oldest_execution_age_days": float("inf")}, metrics={"a": float("nan")})

        def reject(constant):
            raise AssertionError(f"{constant} is not valid JSON")

        text = profile.to_json(build(snapshot, reference="caf\u00e9"))
        self.assertIn("caf\u00e9", text)
        self.assertEqual(json.loads(text, parse_constant=reject)["run"]["reference"], "caf\u00e9")
        self.assertTrue(text.startswith('{\n  "format"'))


class WithholdingTest(unittest.TestCase):
    SNAPSHOT = Snapshot(env={"EXECUTIONS_MODE": "queue", "N8N_HOST": "n8n"}, kubernetes=kube(("w", 3, worker())))

    def test_fact_setting_and_check(self):
        ids = ("worker.replicas", "setting:instance:EXECUTIONS_MODE", "setting:instance:N8N_HOST", "check")
        built = build(self.SNAPSHOT, [finding()], withhold=ids)
        facts = {f["id"]: f for f in built["facts"]}
        self.assertEqual(
            facts["worker.replicas"],
            {"id": "worker.replicas", "value": None, "provenance": "withheld", "source": None, "detail": ""},
        )
        self.assertEqual(facts["executions.mode"]["value"], "queue")  # a fact and its setting are separate entries
        self.assertEqual(len(built["facts"]), len(profile.FACT_IDS))
        self.assertEqual(
            [s for s in built["settings"] if s["scope"] == "instance"],
            [
                {"scope": "instance", "name": "EXECUTIONS_MODE", "state": "withheld", "value": None},
                {"scope": "instance", "name": "N8N_HOST", "state": "withheld", "value": None},
            ],
        )
        self.assertEqual(built["check"], {"withheld": True})
        self.assertNotIn("A fixed title", profile.to_json(built))
        self.assertNotIn("A fixed title", profile.render_markdown(built))

    def test_an_unknown_fact_can_be_withheld_too(self):
        built = build(Snapshot(), withhold=("database.version",))
        self.assertEqual({f["id"]: f for f in built["facts"]}["database.version"]["provenance"], "withheld")

    def test_unknown_ids_are_refused(self):
        for bad in ("worker.replica", "setting:instance:N8N_PORT", "setting:main:EXECUTIONS_MODE", "run", "sources", "format", "created_at", "tool_version", "facts", ""):
            with self.subTest(id=bad), self.assertRaises(profile.ProfileError) as raised:
                build(self.SNAPSHOT, withhold=("worker.replicas", bad))
            self.assertIn("nothing in the profile has this id", str(raised.exception))

    def test_the_original_is_not_modified(self):
        built = build(self.SNAPSHOT)
        before = json.dumps(built)
        withheld = profile.apply_withholding(built, ["check", "worker.replicas"])
        self.assertEqual(json.dumps(built), before)
        self.assertNotEqual(json.dumps(withheld), before)


# --- files -------------------------------------------------------------------------


class FileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, data, name="profile.json"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def valid(self, **changes):
        snapshot = Snapshot(env={"EXECUTIONS_MODE": "queue", "N8N_HOST": "n8n"}, kubernetes=kube(("m", 1, container())))
        built = build(snapshot, [finding()], reason="incident", reference="ticket 42", withhold=("probes.main",))
        return {**built, **changes}

    def test_round_trip(self):
        built = self.valid()
        loaded = profile.load_profile(self.write(profile.to_json(built)))
        self.assertEqual(loaded, built)
        markdown = profile.render_markdown(loaded)
        self.assertTrue(markdown.startswith("# n8n environment profile\n"))
        for expected in (
            "- **Reason:** incident",
            "- **Reference:** ticket 42",
            "| kubernetes | collected |",
            "| redis | not provided |",
            "| executions.mode | queue | measured | env |  |",
            "| multi_main.enabled | no | measured | env | n8n default, variable not set |",
            "| database.version |  | unknown |  | Postgres was not provided |",
            "| probes.main |  | withheld |  |  |",
            "| resources.main | requests (cpu: 200m, memory: 512Mi), limits (cpu: not set, memory: 1Gi) | measured | kubernetes |  |",
            "| instance | EXECUTIONS_MODE | queue |",
            "| instance | N8N_HOST | set (value not written) |",
            "| reliability | 85/100 (partial) |",
            "- REL-002 (warning, reliability): A fixed title",
        ):
            self.assertIn(expected, markdown)

    def test_rendering_states(self):
        empty = profile.render_markdown(build(Snapshot()))
        self.assertIn("- **Reference:** none", empty)
        self.assertIn("No n8n setting is listed.", empty)
        self.assertIn("| scalability | n/a |", empty)
        self.assertIn("No findings.", empty)
        withheld = profile.render_markdown(build(Snapshot(env={"N8N_PORT": "1", "ACME": "1"}), withhold=("check", "setting:instance:N8N_PORT")))
        self.assertIn("| instance | N8N_PORT | withheld |", withheld)
        self.assertIn("## Check outcome\n\nWithheld.", withheld)
        self.assertIn("not listed, not even by name: 1.", withheld)

    def test_text_from_a_file_cannot_break_the_tables(self):
        data = self.valid()
        data["run"]["reference"] = "a | b"
        data["facts"][0]["detail"] = "one\ntwo | three"
        markdown = profile.render_markdown(profile.validate(data))
        self.assertIn("a \\| b", markdown)
        self.assertIn("one two \\| three |", markdown)

    def test_text_from_a_file_cannot_reach_the_terminal_or_become_markup(self):
        data = self.valid()
        data["run"]["reference"] = "<img src=x onerror=1> [click](http://x.example) `code` end\\"
        data["facts"][0]["detail"] = "\x1b[2J\x1b]0;title\x07 bell \u202e reversed"
        data["facts"][1]["source"] = "tail\\"
        data["settings"][0].update(state="value", value="a`b|c")
        data["check"]["findings"][0]["title"] = "<script>alert(1)</script>"
        markdown = profile.render_markdown(profile.validate(data))
        for raw in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(raw, markdown)
        # Every character that could start markup is escaped (table pipes aside).
        self.assertIsNone(re.search(r"(?<!\\)[<>\[\]`]", markdown))
        self.assertIn(r"\<img src=x onerror=1\> \[click\](http://x.example) \`code\` end\\", markdown)
        self.assertIn(r"| tail\\ |", markdown)  # the backslash is escaped, the cell still closes
        self.assertIn(r"a\`b\|c", markdown)
        self.assertIn(r"\<script\>alert(1)\</script\>", markdown)
        for line in markdown.splitlines():
            if line.startswith("| n8n.version"):
                self.assertEqual(len(re.findall(r"(?<!\\)\|", line)), 6)

    def test_a_fact_id_this_version_does_not_know_is_shown(self):
        data = self.valid()
        data["facts"].append({"id": "hosting.region", "value": "eu", "provenance": "declared", "source": "questionnaire", "detail": ""})
        self.assertIn("| hosting.region | eu | declared | questionnaire |  |", profile.render_markdown(profile.validate(data)))

    def test_a_file_from_this_version_opens_in_a_tool_with_a_larger_catalogue(self):
        # Adding a fact id does not raise the format version, so a later tool must still
        # open what 0.2.0 wrote: completeness is judged by the frozen version 1 ids.
        written_today = json.loads(profile.to_json(self.valid()))
        later = profile.CATALOGUE + (("hosting.region", lambda snapshot, failed: profile._unknown("not asked")),)
        with mock.patch.object(profile, "CATALOGUE", later), mock.patch.object(profile, "FACT_IDS", tuple(i for i, _ in later)):
            self.assertEqual(profile.validate(written_today), written_today)
            self.assertIn("| executions.mode | queue |", profile.render_markdown(written_today))
            newer = build(Snapshot())
            self.assertEqual(newer["facts"][-1]["id"], "hosting.region")
        self.assertEqual(profile.validate(newer), newer)  # and this version opens the later file

    def test_the_version_1_ids_are_frozen(self):
        self.assertEqual(len(profile.FORMAT_1_FACT_IDS), 32)
        self.assertLessEqual(profile.FORMAT_1_FACT_IDS, set(profile.FACT_IDS))  # the catalogue only grows
        self.assertEqual(
            sorted(profile.FORMAT_1_FACT_IDS)[:4],
            ["binary_data.mode", "database.engine", "database.execution_count", "database.execution_table_bytes"],
        )
        for missing in sorted(profile.FORMAT_1_FACT_IDS):
            data = self.valid()
            data["facts"] = [f for f in data["facts"] if f["id"] != missing]
            with self.subTest(missing=missing), self.assertRaises(profile.ProfileError):
                profile.validate(data)

    def test_newer_format_version_is_refused_with_a_clear_message(self):
        with self.assertRaises(profile.ProfileError) as raised:
            profile.load_profile(self.write(self.valid(format_version=2)))
        self.assertIn("newer than this tool reads", str(raised.exception))
        self.assertIn("update n8n-healthcheck", str(raised.exception))

    def test_invalid_files_are_refused(self):
        def without(key):
            data = self.valid()
            del data[key]
            return data

        def changed(path, value):
            data = self.valid()
            target = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            return data

        cases = [
            "",
            "not json",
            "[]",
            '"text"',
            '{"format": "n8n-healthcheck-profile", "format_version": NaN}',
            {"format": "something-else", "format_version": 1},
            self.valid(format="n8n-healthcheck-snapshot"),
            self.valid(format_version="1"),
            self.valid(format_version=0),
            self.valid(format_version=True),
            self.valid(settings_not_listed=-1),
            self.valid(check={}),
            self.valid(check={"withheld": False}),
            self.valid(facts={}),
            self.valid(settings="none"),
            changed(("run", "reason"), "because"),
            changed(("run", "reference"), None),
            changed(("sources", "redis"), "maybe"),
            changed(("facts", 0), "n8n.version"),
            changed(("facts", 0, "provenance"), "guessed"),
            changed(("facts", 0, "value"), ["a"]),
            changed(("facts", 0, "value"), {"a": {"b": {"c": 1}}}),
            changed(("facts", 0, "detail"), None),
            changed(("facts", 0, "source"), 3),
            changed(("settings", 0, "state"), "hidden"),
            changed(("settings", 0, "value"), 5),
            changed(("settings", 0, "name"), None),
            changed(("check", "scores"), [{"area": "security", "score": "high"}]),
            changed(("check", "findings"), [{"rule_id": "X"}]),
            changed(("check", "partial"), "yes"),
            changed(("check", "scores"), [{"area": "security", "partial": True}]),  # no score at all
            changed(("settings", 0), {"scope": "instance", "name": "N8N_PORT", "state": "set"}),  # no value key
            # Nothing may be silently absent from a version 1 file.
            changed(("facts",), self.valid()["facts"][:1]),
            changed(("facts",), self.valid()["facts"][1:]),
            changed(("facts",), []),
            changed(("sources",), {}),
            changed(("sources",), {"health": "collected"}),
            # The reference obeys the same rules as when it is typed.
            changed(("run", "reference"), "x" * 500),
            changed(("run", "reference"), "\x1b[2J\x1b]0;title\x07"),
            changed(("run", "reference"), "two\nlines"),
            changed(("run", "reference"), " padded "),
            changed(("run", "reference"), "\ud800"),
            changed(("facts", 0, "detail"), "\udfff"),
            changed(("facts", 0, "value"), float("nan")),
        ]
        cases += [without(key) for key in self.valid()]
        for index, case in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(profile.ProfileError) as raised:
                profile.load_profile(self.write(case))
            self.assertIn("not a valid profile", str(raised.exception))

    def test_a_file_that_is_not_text_is_refused(self):
        path = os.path.join(self.dir, "binary.json")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe\x00{")
        with self.assertRaises(profile.ProfileError):
            profile.load_profile(path)

    def test_a_file_nested_without_end_is_refused(self):
        with self.assertRaises(profile.ProfileError):
            profile.load_profile(self.write("[" * 200_000))


# --- command line ------------------------------------------------------------------

COLLECTED = Snapshot(
    health={"healthz": True, "readiness": True, "metrics_disabled": True},
    env={"EXECUTIONS_MODE": "queue", "DB_POSTGRESDB_HOST": "db.internal.example", "N8N_PORT": "5678"},
    n8n_version=None,
)
SOURCES = ["--url", "https://n8n.example.com", "--env-file", "n8n.env", "--redis-url", "redis://cache.example:6379"]


def warning_rule(snapshot):
    return finding()


class CliProfileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "n8n-profile.json")

    def run_cli(self, argv, rules=(), snapshot=COLLECTED):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", list(rules)):
            with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        code = cli.main(argv)
                    except SystemExit as exit_:  # a usage error
                        code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def saved(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_save_profile_next_to_the_report(self):
        code, out, _ = self.run_cli(SOURCES + ["--save-profile", self.path], [warning_rule])
        plain_code, plain_out, _ = self.run_cli(SOURCES, [warning_rule])
        self.assertEqual((code, out), (plain_code, plain_out))  # report and exit code unchanged
        self.assertEqual(code, 1)
        saved = self.saved()
        self.assertEqual(saved["run"], {"reason": "baseline", "reference": ""})
        self.assertEqual(saved["sources"]["redis"], "failed")
        self.assertEqual(saved["sources"]["metrics"], "disabled")
        self.assertEqual(saved["check"]["findings"][0]["rule_id"], "REL-002")
        self.assertEqual({f["id"]: f for f in saved["facts"]}["queue.redis_version"]["detail"], "Redis could not be collected")
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("example", text)
        self.assertEqual(profile.validate(saved), saved)

    def test_exit_codes_of_a_run_do_not_change(self):
        critical = lambda snapshot: finding("SEC-001", "critical", "security")
        for rules, expected in (([critical], 2), ([warning_rule], 1), ([], 0)):
            self.assertEqual(self.run_cli(SOURCES + ["--save-profile", self.path], rules)[0], expected)

    def test_reason_reference_and_withhold(self):
        argv = SOURCES + [
            "--save-profile", self.path, "--reason", "pre_upgrade", "--reference", "change 1234",
            "--withhold", "executions.mode", "--withhold", "setting:instance:N8N_PORT", "--withhold", "check",
        ]  # fmt: skip
        self.assertEqual(self.run_cli(argv, [warning_rule])[0], 1)
        saved = self.saved()
        self.assertEqual(saved["run"], {"reason": "pre_upgrade", "reference": "change 1234"})
        self.assertEqual({f["id"]: f for f in saved["facts"]}["executions.mode"]["provenance"], "withheld")
        self.assertIn({"scope": "instance", "name": "N8N_PORT", "state": "withheld", "value": None}, saved["settings"])
        self.assertEqual(saved["check"], {"withheld": True})

    def test_usage_errors_exit_3_and_write_nothing(self):
        save = ["--save-profile", self.path]
        for argv, message in (
            (["--snapshot", "snapshot.json"] + save, "--save-profile cannot be combined with --snapshot"),
            (SOURCES + ["--reason", "incident"], "--reason needs --save-profile"),
            (SOURCES + ["--reference", "x"], "--reference needs --save-profile"),
            (SOURCES + ["--withhold", "check"], "--withhold needs --save-profile"),
            (SOURCES + save + ["--reason", "because"], "invalid choice"),
            (SOURCES + save + ["--reference", "x" * 201], "--reference: the reference is longer than 200"),
            (SOURCES + save + ["--withhold", "worker.replica"], "nothing in the profile has the id 'worker.replica'"),
            (SOURCES + save + ["--withhold", "run"], "nothing in the profile has the id 'run'"),
            (SOURCES + save + ["--withhold", "setting:instance:N8N_NOT_SET"], "nothing in the profile has this id"),
            (save, "nothing to inspect"),
        ):
            with self.subTest(argv=argv[len(SOURCES) :] if argv[:1] == SOURCES[:1] else argv):
                report_path = os.path.join(self.dir, "report.md")
                code, out, err = self.run_cli(argv + ["-o", report_path])
                self.assertEqual((code, out), (3, ""))
                self.assertIn(message, err)
                self.assertFalse(os.path.exists(self.path))
                self.assertFalse(os.path.exists(report_path))

    def test_unwritable_profile_path_exits_3(self):
        code, _, err = self.run_cli(SOURCES + ["--save-profile", os.path.join(self.dir, "missing", "p.json")])
        self.assertEqual(code, 3)
        self.assertIn("cannot write", err)

    def test_show_profile(self):
        self.run_cli(SOURCES + ["--save-profile", self.path, "--reference", "ticket 42"], [warning_rule])
        with mock.patch("n8n_healthcheck.collectors.collect", side_effect=AssertionError("must not collect")):
            code, out, err = self.run_cli(["--show-profile", self.path])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("# n8n environment profile\n"))
        self.assertIn("- **Reference:** ticket 42", out)
        self.assertIn("| executions.mode | queue | measured | env |  |", out)
        # ... also when the outcome it records was a warning or a critical finding.
        output = os.path.join(self.dir, "profile.md")
        code, out, _ = self.run_cli(["--show-profile", self.path, "-o", output])
        self.assertEqual((code, out), (0, ""))
        with open(output, encoding="utf-8") as fh:
            self.assertIn("- REL-002 (warning, reliability): A fixed title", fh.read())

    def test_show_profile_takes_no_other_flag(self):
        self.run_cli(SOURCES + ["--save-profile", self.path])
        for extra in (["--url", "https://n8n.example.com"], ["--snapshot", "s.json"], ["--save-profile", "p.json"],
                      ["--reason", "incident"], ["--withhold", "check"], ["--save-snapshot", "s.json"]):  # fmt: skip
            with self.subTest(extra=extra):
                code, out, err = self.run_cli(["--show-profile", self.path] + extra)
                self.assertEqual((code, out), (3, ""))
                self.assertIn("--show-profile takes no other flag except -o", err)

    def test_show_profile_of_a_bad_file_exits_3(self):
        missing = os.path.join(self.dir, "missing.json")
        code, out, err = self.run_cli(["--show-profile", missing])
        self.assertEqual((code, out), (3, ""))
        self.assertIn("cannot read profile", err)
        for content, message in (
            ("not json", "not a valid profile"),
            (json.dumps({"format": "other"}), "not a valid profile"),
            (json.dumps({**build(Snapshot()), "format_version": 9}), "newer than this tool reads"),
        ):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(content)
            code, out, err = self.run_cli(["--show-profile", self.path])
            self.assertEqual((code, out), (3, ""))
            self.assertIn(message, err)

    def test_show_profile_never_ends_in_a_traceback(self):
        good = build(Snapshot())
        no_score = json.loads(json.dumps(good))
        del no_score["check"]["scores"][0]["score"]
        surrogate = json.loads(json.dumps(good))
        surrogate["facts"][0]["detail"] = "\ud800"
        for content in (json.dumps(no_score), json.dumps(surrogate), "[" * 200_000, '{"format": ' * 5000, "{}", "null", "1e999"):
            with self.subTest(content=content[:30]):
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                code, out, err = self.run_cli(["--show-profile", self.path])
                self.assertEqual((code, out), (3, ""))
                self.assertEqual(len(err.splitlines()), 1, err[:300])
                self.assertTrue(err.startswith("error: cannot show profile"))

    def test_error_messages_never_print_text_from_the_file(self):
        hostile = "x\x1b[2J\x1b]0;pwned\x07<b>"
        cases = []
        for key, bad in (("provenance", "guessed"), ("value", ["a"]), ("source", 3), ("detail", None)):
            data = build(Snapshot(env={"N8N_PORT": "1"}))
            data["facts"][2].update({"id": hostile, key: bad})
            cases.append(data)
        for key, bad in (("state", "hidden"), ("value", 5), ("scope", None)):
            data = build(Snapshot(env={"N8N_PORT": "1"}))
            data["settings"][0].update({"name": hostile, "scope": hostile, key: bad})
            cases.append(data)
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                with open(self.path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                code, out, err = self.run_cli(["--show-profile", self.path])
                self.assertEqual((code, out), (3, ""))
                for raw in ("\x1b", "\x07", "pwned", "<b>"):
                    self.assertNotIn(raw, err)
                self.assertRegex(err, r"not a valid profile: \w+ of (fact 3|setting 1)\n$|setting 1\n$")

    def test_show_profile_survives_a_renderer_that_fails(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(profile.to_json(build(Snapshot())))
        with mock.patch("n8n_healthcheck.profile.render_markdown", side_effect=KeyError("score")):
            code, out, err = self.run_cli(["--show-profile", self.path])
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(len(err.splitlines()), 1)

    def test_an_empty_profile_path_is_a_usage_error(self):
        for value in ("", "  "):
            code, out, err = self.run_cli(SOURCES + ["--save-profile", value])
            self.assertEqual((code, out), (3, ""))
            self.assertIn("--save-profile must not be empty", err)
            code, _, err = self.run_cli(SOURCES + ["--save-profile", value, "--reason", "incident"])
            self.assertEqual(code, 3)
            self.assertIn("--save-profile must not be empty", err)

    def test_abbreviations_that_worked_before_the_profile_flags_still_work(self):
        # Before the profile flags existed these prefixes were unambiguous.
        for argv, dest, value in (
            (["--save", "s.json"], "save_snapshot", "s.json"),
            (["--save-", "s.json"], "save_snapshot", "s.json"),
            (["--sav", "s.json"], "save_snapshot", "s.json"),
            (["--sa", "s.json"], "save_snapshot", "s.json"),
            (["--re", "redis://cache.example:6379"], "redis_url", "redis://cache.example:6379"),
            (["--r", "redis://cache.example:6379"], "redis_url", "redis://cache.example:6379"),
            (["--sn", "s.json"], "snapshot", "s.json"),
            (["--o", "r.md"], "output", "r.md"),
        ):
            with self.subTest(argv=argv):
                args = cli._parse_args(["--url", "https://n8n.example.com"] + argv)
                self.assertEqual(getattr(args, dest), value)
                self.assertIsNone(args.save_profile)
        # The new flags can be shortened where nothing older starts the same way.
        args = cli._parse_args(SOURCES + ["--save-p", "p.json", "--reas", "incident", "--ref", "x", "--w", "check"])
        self.assertEqual((args.save_profile, args.reason, args.reference, args.withhold), ("p.json", "incident", "x", ["check"]))
        self.assertEqual(cli._parse_args(["--sh", "p.json"]).show_profile, "p.json")
        # What was ambiguous before still is.
        for argv in (["--s", "x"], ["--k", "x"]):
            code, _, err = self.run_cli(argv)
            self.assertEqual(code, 3)
            self.assertIn("ambiguous option", err)

    def test_help_lists_the_profile_flags(self):
        code, out, _ = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        for flag in ("--save-profile", "--reason", "--reference", "--withhold", "--show-profile"):
            self.assertIn(flag, out)
        # Withholding a fact leaves the setting and the finding that say the same.
        self.assertIn("does not remove the setting", " ".join(out.split()))
        with open(os.path.join(os.path.dirname(DOCS), "..", "README.md"), encoding="utf-8") as fh:
            self.assertIn("does not remove the setting", " ".join(fh.read().split()))


if __name__ == "__main__":
    unittest.main()
