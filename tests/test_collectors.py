import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

from n8n_healthcheck.collectors import collect, envfile, http, kubernetes, postgres, redis
from n8n_healthcheck.models import REDACTED, Target

METRICS_TEXT = """\
# HELP n8n_version_info n8n version info.
# TYPE n8n_version_info gauge
n8n_nodejs_version_info{version="v22.11.0",major="22",minor="11",patch="0"} 1
n8n_version_info{version="v1.70.3",major="1",minor="70",patch="3"} 1
n8n_scaling_mode_queue_jobs_waiting 12
n8n_http_request_duration_seconds_bucket{le="+Inf",path="/a b"} 4 1700000000000
n8n_process_start_time_seconds 1.7e9
garbage line
"""

ENV_TEXT = """\
# comment

EXECUTIONS_MODE=queue
export N8N_METRICS = true
QUOTED="a b"
SINGLE='x=y'
EMPTY=
N8N_ENCRYPTION_KEY=abc123
DB_POSTGRESDB_PASSWORD="pg secret"
N8N_SMTP_PASS=
HTTP_PROXY=http://u:hunter2@proxy:3128
N8N_LICENSE_CERT=MIIBkTCB
WEBHOOK_URL=https://n8n.example.com/
not a variable
"""

INFO_TEXT = (
    "# Server\r\nredis_version:7.2.4\r\n\r\n# Memory\r\nused_memory:1048576\r\n"
    "maxmemory:0\r\nmaxmemory_policy:noeviction\r\n"
)

DEPLOYMENTS = {
    "items": [
        {
            "metadata": {"name": "n8n-worker"},
            "spec": {
                "replicas": 3,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "n8n",
                                "image": "n8nio/n8n:1.70.3",
                                "command": ["n8n"],
                                "args": ["worker"],
                                "env": [
                                    {"name": "EXECUTIONS_MODE", "value": "queue"},
                                    {"name": "N8N_ENCRYPTION_KEY", "valueFrom": {"secretKeyRef": {}}},
                                    {"name": "DB_POSTGRESDB_PASSWORD", "value": "hunter2"},
                                    {"name": "N8N_RUNNERS_AUTH_TOKEN", "value": ""},
                                    {"name": "N8N_PROXY_HOPS"},
                                    {"name": "HTTP_PROXY", "value": "http://u:hunter2@proxy:3128"},
                                    {"name": "N8N_LICENSE_CERT", "value": "MIIBkTCB"},
                                    {"name": "WEBHOOK_URL", "value": "https://n8n.example.com/"},
                                ],
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "512Mi"},
                                    "limits": {"memory": "1Gi"},
                                },
                                "readinessProbe": {"httpGet": {"path": "/healthz"}},
                            }
                        ]
                    }
                },
            },
            "status": {"readyReplicas": 2},
        },
        {
            "metadata": {"name": "bare"},
            "spec": {"template": {"spec": {"containers": [{"name": "c"}]}}},
            "status": {},
        },
    ]
}


CONFIGMAPS = {
    "items": [
        {
            "metadata": {"name": "n8n-config"},
            "data": {
                "EXECUTIONS_MODE": "queue",
                "N8N_METRICS": "false",
                "DB_POSTGRESDB_PASSWORD": "hunter2",
            },
        },
        {"metadata": {"name": "extra"}, "data": {"N8N_METRICS": "true", "API_TOKEN": "abc"}},
        {"metadata": {"name": "override"}, "data": {"N8N_METRICS": "true"}},
        {"metadata": {"name": "empty"}},
    ]
}
_NOT_GIVEN = object()


def deployment(container_spec: dict) -> dict:
    """A `kubectl get deployments` item with one n8n container carrying `container_spec`."""
    container = {"name": "n8n", "image": "n8nio/n8n:2.40.1", **container_spec}
    return {
        "metadata": {"name": "n8n-main"},
        "spec": {"template": {"spec": {"containers": [container]}}},
    }


def config_map(name: str, **extra) -> dict:
    """An `envFrom` entry for a ConfigMap."""
    return {"configMapRef": {"name": name}, **extra}


def container_env(container_spec: dict, configmaps=_NOT_GIVEN):
    """(env, env_complete) the collector derives for a container; CONFIGMAPS unless overridden."""
    if configmaps is _NOT_GIVEN:
        configmaps = kubernetes.parse_configmaps(json.dumps(CONFIGMAPS))
    output = json.dumps({"items": [deployment(container_spec)]})
    parsed = kubernetes.parse_deployments(output, configmaps)["deployments"][0]["containers"][0]
    return parsed["env"], parsed["env_complete"]


def response(body: str, status: int = 200):
    handle = mock.MagicMock()
    handle.__enter__.return_value = handle
    handle.status = status
    handle.read.return_value = body.encode()
    return handle


def http_error(code: int):
    return urllib.error.HTTPError("http://n8n", code, "error", None, None)


class FakeStream(io.BytesIO):
    """Stands in for sock.makefile(): replays canned replies and records what was sent."""

    def __init__(self, replies: bytes):
        super().__init__(replies)
        self.sent = b""

    def write(self, data: bytes) -> int:
        self.sent += data
        return len(data)


def quiet_collect(target: Target):
    """Run collect() and return (snapshot, stderr text)."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        snapshot = collect(target)
    return snapshot, stderr.getvalue()


class ParserTests(unittest.TestCase):
    def test_prometheus_metrics(self):
        metrics = http.parse_metrics(METRICS_TEXT)
        self.assertEqual(metrics["n8n_scaling_mode_queue_jobs_waiting"], 12.0)
        self.assertEqual(metrics["n8n_process_start_time_seconds"], 1.7e9)
        self.assertEqual(metrics['n8n_http_request_duration_seconds_bucket{le="+Inf",path="/a b"}'], 4.0)
        self.assertEqual(len(metrics), 5)
        self.assertEqual(http.version_from_metrics(metrics), "1.70.3")
        self.assertIsNone(http.version_from_metrics({"up": 1.0}))

    def test_env_file(self):
        self.assertEqual(
            envfile.parse_env(ENV_TEXT),
            {
                "EXECUTIONS_MODE": "queue",
                "N8N_METRICS": "true",
                "QUOTED": "a b",
                "SINGLE": "x=y",
                "EMPTY": "",
                "N8N_ENCRYPTION_KEY": REDACTED,
                "DB_POSTGRESDB_PASSWORD": REDACTED,
                "N8N_SMTP_PASS": "",  # unset stays distinguishable from set
                "HTTP_PROXY": REDACTED,  # credentials inside the value
                "N8N_LICENSE_CERT": REDACTED,
                "WEBHOOK_URL": "https://n8n.example.com/",  # ordinary URL left intact
            },
        )

    def test_psql_row(self):
        row = postgres.parse_row("16.4 (Debian 16.4-1.pgdg120+2)|1500000|8589934592|412.5|37|100\n")
        self.assertEqual(
            row,
            {
                "version": "16.4 (Debian 16.4-1.pgdg120+2)",
                "execution_count": 1500000,
                "execution_table_bytes": 8589934592,
                "oldest_execution_age_days": 412.5,
                "connections": 37,
                "max_connections": 100,
            },
        )
        self.assertEqual(postgres.parse_row("16.4|0|8192||1|100\n")["oldest_execution_age_days"], 0.0)

    def test_dsn_password_is_split_out(self):
        uri, password = postgres.split_dsn("postgresql://n8n:s%40cret@db:5432/n8n?sslmode=require")
        self.assertEqual(uri, "postgresql://n8n@db:5432/n8n?sslmode=require")
        self.assertEqual(password, "s@cret")
        self.assertEqual(postgres.split_dsn("postgres://db/n8n"), ("postgres://db/n8n", ""))
        self.assertEqual(postgres.split_dsn("postgresql:///n8n"), ("postgresql:///n8n", ""))
        self.assertEqual(
            postgres.split_dsn("postgresql://n8n@db/n8n?password=s%26cret&sslmode=require&options=-c%20x%3D1"),
            ("postgresql://n8n@db/n8n?sslmode=require&options=-c%20x%3D1", "s&cret"),
        )
        with self.assertRaises(ValueError):
            postgres.split_dsn("host=db password=secret")

    def test_resp_replies(self):
        stream = io.BytesIO(b"+OK\r\n:42\r\n$5\r\nhe\r\nl\r\n$-1\r\n*2\r\n:1\r\n$1\r\na\r\n-ERR nope\r\n")
        self.assertEqual(redis.read_reply(stream), "OK")
        self.assertEqual(redis.read_reply(stream), 42)
        self.assertEqual(redis.read_reply(stream), "he\r\nl")
        self.assertIsNone(redis.read_reply(stream))
        self.assertEqual(redis.read_reply(stream), [1, "a"])
        with self.assertRaisesRegex(RuntimeError, "ERR nope"):
            redis.read_reply(stream)
        with self.assertRaises(ConnectionError):
            redis.read_reply(stream)

    def test_redis_info(self):
        info = redis.parse_info(INFO_TEXT)
        self.assertEqual(info["redis_version"], "7.2.4")
        self.assertEqual(info["maxmemory_policy"], "noeviction")
        self.assertNotIn("# Server", info)

    def test_kubectl_deployments(self):
        worker, bare = kubernetes.parse_deployments(json.dumps(DEPLOYMENTS))["deployments"]
        self.assertEqual((worker["name"], worker["replicas"], worker["ready_replicas"]), ("n8n-worker", 3, 2))
        self.assertEqual(
            worker["containers"][0],
            {
                "name": "n8n",
                "image": "n8nio/n8n:1.70.3",
                "command": ["n8n", "worker"],
                "env": {
                    "EXECUTIONS_MODE": "queue",
                    "N8N_ENCRYPTION_KEY": "<valueFrom>",
                    "DB_POSTGRESDB_PASSWORD": REDACTED,
                    "N8N_RUNNERS_AUTH_TOKEN": "",
                    "N8N_PROXY_HOPS": "",
                    "HTTP_PROXY": REDACTED,
                    "N8N_LICENSE_CERT": REDACTED,
                    "WEBHOOK_URL": "https://n8n.example.com/",
                },
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"memory": "1Gi"},
                "liveness_probe": False,
                "readiness_probe": True,
                "env_complete": True,
            },
        )
        self.assertEqual((bare["replicas"], bare["ready_replicas"]), (1, 0))
        self.assertEqual(bare["containers"][0]["command"], [])
        self.assertEqual(bare["containers"][0]["requests"], {})
        self.assertEqual(bare["containers"][0]["env"], {})
        self.assertTrue(bare["containers"][0]["env_complete"])

    def test_configmap_env_from_with_and_without_prefix(self):
        env, complete = container_env(
            {"envFrom": [config_map("n8n-config"), config_map("extra", prefix="X_")]}
        )
        self.assertEqual(
            env,
            {
                "EXECUTIONS_MODE": "queue",
                "N8N_METRICS": "false",
                "DB_POSTGRESDB_PASSWORD": REDACTED,
                "X_N8N_METRICS": "true",
                "X_API_TOKEN": REDACTED,  # redaction sees the prefixed name
            },
        )
        self.assertTrue(complete)

    def test_later_env_from_source_wins(self):
        env, _ = container_env(
            {"envFrom": [config_map("n8n-config"), config_map("override")]}
        )
        self.assertEqual(env["N8N_METRICS"], "true")

    def test_configmap_key_ref_is_resolved_and_redacted(self):
        def ref(key):
            return {"valueFrom": {"configMapKeyRef": {"name": "n8n-config", "key": key}}}

        env, complete = container_env(
            {
                "env": [
                    {"name": "MODE", **ref("EXECUTIONS_MODE")},
                    {"name": "DB_PASS", **ref("DB_POSTGRESDB_PASSWORD")},
                ]
            }
        )
        self.assertEqual(env, {"MODE": "queue", "DB_PASS": REDACTED})
        self.assertTrue(complete)

    def test_env_overrides_env_from(self):
        env, complete = container_env(
            {
                "envFrom": [{"configMapRef": {"name": "n8n-config"}}],
                "env": [
                    {"name": "N8N_METRICS", "value": "true"},
                    {"name": "EXECUTIONS_MODE", "valueFrom": {"secretKeyRef": {"name": "s"}}},
                ],
            }
        )
        self.assertEqual((env["N8N_METRICS"], env["EXECUTIONS_MODE"]), ("true", "<valueFrom>"))
        self.assertTrue(complete)

    def test_secrets_and_field_refs_stay_unknown_but_complete(self):
        env, complete = container_env(
            {
                "env": [
                    {"name": "A", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
                    {"name": "B", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                    {"name": "C", "valueFrom": {"resourceFieldRef": {"resource": "limits.cpu"}}},
                    {"name": "D", "value": "http://$(B):5678"},  # expanded by Kubernetes
                ]
            }
        )
        self.assertEqual(env, dict.fromkeys("ABCD", "<valueFrom>"))
        self.assertTrue(complete)

    def test_secret_env_from_makes_the_environment_incomplete(self):
        env, complete = container_env(
            {"envFrom": [config_map("n8n-config"), {"secretRef": {"name": "n8n-secrets"}}]}
        )
        # The Secret may override any ConfigMap key, so none of them is reported as read.
        self.assertEqual(set(env.values()), {"<valueFrom>"})
        self.assertEqual(set(env), {"EXECUTIONS_MODE", "N8N_METRICS", "DB_POSTGRESDB_PASSWORD"})
        self.assertFalse(complete)

    def test_unreadable_env_from_only_hides_the_sources_before_it(self):
        for unreadable in ({"secretRef": {"name": "n8n-secrets"}}, config_map("gone")):
            with self.subTest(unreadable=unreadable):
                env, complete = container_env(
                    {
                        "envFrom": [config_map("n8n-config"), unreadable, config_map("override")],
                        "env": [{"name": "EXECUTIONS_MODE", "value": "regular"}],
                    }
                )
                self.assertEqual(env["DB_POSTGRESDB_PASSWORD"], "<valueFrom>")  # before it
                self.assertEqual(env["N8N_METRICS"], "true")  # from the source after it
                self.assertEqual(env["EXECUTIONS_MODE"], "regular")  # explicit env entry
                self.assertFalse(complete)

    def test_optional_configmap_key_ref_that_is_missing_leaves_the_variable_unset(self):
        def entry(name, key, **ref):
            ref = {"name": name, "key": key, **ref}
            return {"name": "N8N_METRICS", "valueFrom": {"configMapKeyRef": ref}}

        unknown = ({"N8N_METRICS": "<valueFrom>"}, False)
        for name, key in (("gone", "A"), ("n8n-config", "NO_SUCH_KEY")):
            with self.subTest(configmap=name):
                optional = {"env": [entry(name, key, optional=True)]}
                self.assertEqual(container_env(optional), ({}, True))
                # Not optional, or ConfigMaps not fetched: still set-but-unknown.
                self.assertEqual(container_env({"env": [entry(name, key)]}), unknown)
                self.assertEqual(container_env(optional, configmaps=None), unknown)
        # The skipped entry does not hide the value envFrom gave the same name.
        spec = {
            "envFrom": [config_map("n8n-config")],
            "env": [entry("gone", "A", optional=True)],
        }
        self.assertEqual(container_env(spec)[0]["N8N_METRICS"], "false")

    def test_missing_configmap_makes_the_environment_incomplete(self):
        env, complete = container_env({"envFrom": [config_map("gone")]})
        self.assertEqual((env, complete), ({}, False))
        for ref in ({"name": "gone", "key": "A"}, {"name": "n8n-config", "key": "NO_SUCH_KEY"}):
            with self.subTest(ref=ref):
                entry = {"name": "A", "valueFrom": {"configMapKeyRef": ref}}
                env, complete = container_env({"env": [entry]})
                self.assertEqual((env, complete), ({"A": "<valueFrom>"}, False))

    def test_configmaps_not_fetched_makes_references_incomplete(self):
        spec = {"envFrom": [config_map("n8n-config")], "env": [{"name": "A", "value": "1"}]}
        self.assertEqual(container_env(spec, configmaps=None), ({"A": "1"}, False))


class CollectTests(unittest.TestCase):
    def test_empty_target_collects_nothing(self):
        with mock.patch("subprocess.run") as run, mock.patch("socket.create_connection") as connect:
            snapshot, stderr = quiet_collect(Target())
        self.assertEqual(vars(snapshot), dict.fromkeys(vars(snapshot)))
        self.assertEqual(stderr, "")
        run.assert_not_called()
        connect.assert_not_called()

    def test_http_healthy_with_metrics(self):
        def urlopen(url, timeout):
            return response(METRICS_TEXT if url.endswith("/metrics") else "{}")

        with mock.patch("urllib.request.urlopen", urlopen):
            snapshot, stderr = quiet_collect(Target(url="http://n8n:5678/"))
        self.assertEqual(snapshot.health, {"healthz": True, "readiness": True})
        self.assertEqual(snapshot.metrics["n8n_scaling_mode_queue_jobs_waiting"], 12.0)
        self.assertEqual(snapshot.n8n_version, "1.70.3")
        self.assertEqual(stderr, "")

    def test_http_not_ready_and_metrics_disabled(self):
        def urlopen(url, timeout):
            if url.endswith("/healthz"):
                return response("{}")
            raise http_error(503 if url.endswith("/readiness") else 404)

        with mock.patch("urllib.request.urlopen", urlopen):
            snapshot, stderr = quiet_collect(Target(url="http://n8n:5678"))
        self.assertEqual(snapshot.health, {"healthz": True, "readiness": False})
        self.assertIsNone(snapshot.metrics)
        self.assertEqual(stderr, "")  # 404 on /metrics is "disabled", not a failure

    def test_http_unreachable(self):
        error = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            snapshot, stderr = quiet_collect(Target(url="http://n8n:5678"))
        self.assertIsNone(snapshot.health)
        self.assertIsNone(snapshot.metrics)
        self.assertIn("url: not collected", stderr)

    def test_http_url_with_credentials_is_rejected_without_leaking(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            snapshot, stderr = quiet_collect(Target(url="http://admin:hunter2@n8n.example.invalid"))
        urlopen.assert_not_called()
        self.assertIsNone(snapshot.health)
        self.assertIn("url: not collected (credentials in the URL are not supported", stderr)
        self.assertNotIn("hunter2", stderr)

    def test_env_file_read_and_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "n8n.env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(ENV_TEXT)
            snapshot, _ = quiet_collect(Target(env_file=path))
            self.assertEqual(snapshot.env["EXECUTIONS_MODE"], "queue")
            snapshot, stderr = quiet_collect(Target(env_file=path + ".missing"))
            self.assertIsNone(snapshot.env)
            self.assertIn("env_file: not collected", stderr)

    def test_postgres_password_only_in_environment(self):
        done = subprocess.CompletedProcess([], 0, stdout="16.4|10|8192|1.5|3|100\n", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            snapshot, _ = quiet_collect(Target(postgres_dsn="postgresql://n8n:hunter2@db/n8n"))
        self.assertEqual(snapshot.postgres["execution_count"], 10)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "psql")
        self.assertNotIn("hunter2", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "hunter2")
        self.assertIn("default_transaction_read_only=on", kwargs["env"]["PGOPTIONS"])
        self.assertNotIn("SET ", args[0][args[0].index("-c") + 1])

    def test_postgres_query_string_password_only_in_environment(self):
        done = subprocess.CompletedProcess([], 0, stdout="16.4|10|8192|1.5|3|100\n", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            quiet_collect(Target(postgres_dsn="postgresql://n8n@db/n8n?password=hunter2&sslmode=require"))
        args, kwargs = run.call_args
        self.assertNotIn("hunter2", " ".join(args[0]))
        self.assertEqual(args[0][-1], "postgresql://n8n@db/n8n?sslmode=require")
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "hunter2")

    def test_missing_binaries_do_not_abort_the_run(self):
        target = Target(postgres_dsn="postgresql://n8n:hunter2@db/n8n", kube_namespace="n8n")
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("psql")):
            snapshot, stderr = quiet_collect(target)
        self.assertIsNone(snapshot.postgres)
        self.assertIsNone(snapshot.kubernetes)
        self.assertIn("psql not found", stderr)
        self.assertIn("kubectl not found", stderr)
        self.assertNotIn("hunter2", stderr)

    def test_subprocess_timeout_and_failure(self):
        target = Target(postgres_dsn="postgresql://n8n:hunter2@db/n8n")
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("psql", 10)):
            snapshot, stderr = quiet_collect(target)
        self.assertIsNone(snapshot.postgres)
        self.assertIn("timed out", stderr)
        self.assertNotIn("hunter2", stderr)

        failed = subprocess.CompletedProcess([], 2, stdout="", stderr="FATAL: auth failed\ndetail\n")
        with mock.patch("subprocess.run", return_value=failed):
            snapshot, stderr = quiet_collect(target)
        self.assertIsNone(snapshot.postgres)
        self.assertEqual(stderr, "postgres_dsn: not collected (FATAL: auth failed)\n")

    def test_kubectl_arguments_and_mapping(self):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(DEPLOYMENTS), stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            snapshot, _ = quiet_collect(Target(kube_namespace="n8n", kube_context="prod"))
        self.assertEqual(len(snapshot.kubernetes["deployments"]), 2)
        self.assertEqual(
            [call[0][0][:8] for call in run.call_args_list],
            [
                ["kubectl", "--context", "prod", "get", kind, "-n", "n8n", "-o"]
                for kind in ("deployments", "configmaps")
            ],
        )

    def test_kubectl_resolves_configmaps(self):
        deployments = {"items": [deployment({"envFrom": [config_map("n8n-config")]})]}

        def kubectl(args, **kwargs):
            body = CONFIGMAPS if "configmaps" in args else deployments
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr="")

        with mock.patch("subprocess.run", side_effect=kubectl) as run:
            snapshot, stderr = quiet_collect(Target(kube_namespace="n8n"))
        container = snapshot.kubernetes["deployments"][0]["containers"][0]
        self.assertEqual(container["env"]["EXECUTIONS_MODE"], "queue")
        self.assertTrue(container["env_complete"])
        self.assertEqual(stderr, "")
        self.assertFalse(any("secret" in " ".join(call[0][0]) for call in run.call_args_list))

    def test_failing_configmap_call_keeps_the_deployments(self):
        deployments = {"items": [deployment({"envFrom": [config_map("n8n-config")]})]}

        def kubectl(args, **kwargs):
            if "configmaps" in args:
                error = "Error: configmaps is forbidden\n"
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=error)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(deployments), stderr="")

        with mock.patch("subprocess.run", side_effect=kubectl):
            snapshot, stderr = quiet_collect(Target(kube_namespace="n8n"))
        container = snapshot.kubernetes["deployments"][0]["containers"][0]
        self.assertEqual((container["env"], container["env_complete"]), ({}, False))
        self.assertIn("configmaps is forbidden", stderr)

    def test_redis_conversation(self):
        replies = b"+OK\r\n+OK\r\n" + b"$%d\r\n%s\r\n" % (len(INFO_TEXT), INFO_TEXT.encode())
        replies += b":1\r\n:7\r\n:2\r\n:31\r\n"
        sock = mock.MagicMock()
        stream = sock.makefile.return_value = FakeStream(replies)
        with mock.patch("socket.create_connection", return_value=sock) as connect:
            snapshot, stderr = quiet_collect(Target(redis_url="redis://:p%40ss@cache/2"))
        self.assertEqual(stderr, "")
        self.assertEqual(connect.call_args[0][0], ("cache", 6379))
        self.assertEqual(
            snapshot.redis,
            {
                "version": "7.2.4",
                "waiting_jobs": 7,
                "active_jobs": 2,
                "failed_jobs": 31,
                "used_memory_bytes": 1048576,
                "maxmemory_bytes": 0,
                "maxmemory_policy": "noeviction",
            },
        )
        sent = stream.sent
        self.assertTrue(sent.startswith(b"*2\r\n$4\r\nAUTH\r\n$4\r\np@ss\r\n*2\r\n$6\r\nSELECT\r\n$1\r\n2\r\n"))
        for key in (b"bull:jobs:id", b"bull:jobs:wait", b"bull:jobs:active", b"bull:jobs:failed"):
            self.assertIn(key, sent)
        sock.close.assert_called_once()

    def test_redis_missing_queue_keeps_info_but_not_counts(self):
        replies = b"$%d\r\n%s\r\n:0\r\n" % (len(INFO_TEXT), INFO_TEXT.encode())
        sock = mock.MagicMock()
        stream = sock.makefile.return_value = FakeStream(replies)
        with mock.patch("socket.create_connection", return_value=sock):
            snapshot, stderr = quiet_collect(Target(redis_url="redis://cache"))
        self.assertEqual(snapshot.redis["version"], "7.2.4")
        self.assertEqual(snapshot.redis["maxmemory_policy"], "noeviction")
        for field in ("waiting_jobs", "active_jobs", "failed_jobs"):
            self.assertIsNone(snapshot.redis[field])
        self.assertIn('redis queue: not collected ("bull:jobs" not found', stderr)
        self.assertNotIn(b"LLEN", stream.sent)

    def test_redis_uses_bull_prefix_from_env_file(self):
        replies = b"$%d\r\n%s\r\n" % (len(INFO_TEXT), INFO_TEXT.encode()) + b":1\r\n:7\r\n:2\r\n:31\r\n"
        sock = mock.MagicMock()
        stream = sock.makefile.return_value = FakeStream(replies)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "n8n.env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("QUEUE_BULL_PREFIX=acme\n")
            with mock.patch("socket.create_connection", return_value=sock):
                snapshot, _ = quiet_collect(Target(env_file=path, redis_url="redis://cache"))
        self.assertEqual(snapshot.redis["waiting_jobs"], 7)
        for key in (b"acme:jobs:id", b"acme:jobs:wait", b"acme:jobs:active", b"acme:jobs:failed"):
            self.assertIn(key, stream.sent)
        self.assertNotIn(b"bull:", stream.sent)

    def test_redis_failures(self):
        target = Target(redis_url="redis://:hunter2@cache:6379")
        with mock.patch("socket.create_connection", side_effect=TimeoutError("timed out")):
            snapshot, stderr = quiet_collect(target)
        self.assertIsNone(snapshot.redis)
        self.assertIn("redis_url: not collected (timed out)", stderr)

        sock = mock.MagicMock()
        sock.makefile.return_value = FakeStream(b"-WRONGPASS invalid username-password pair\r\n")
        with mock.patch("socket.create_connection", return_value=sock):
            snapshot, stderr = quiet_collect(target)
        self.assertIsNone(snapshot.redis)
        self.assertIn("WRONGPASS", stderr)
        self.assertNotIn("hunter2", stderr)

    def test_credentials_are_scrubbed_from_any_error(self):
        # the password "p@ss/w:rd", percent-encoded as a URL requires
        target = Target(redis_url="redis://user:p%40ss%2Fw%3Ard@cache:6379")
        for message in (
            "cannot reach redis://user:p%40ss%2Fw%3Ard@cache:6379",  # the URL as given
            "auth failed with password 'p@ss/w:rd'",  # the decoded value on its own
            "nonnumeric port: 'p@ss/w:rd@cache'",  # a fragment, as urllib reports it
        ):
            with mock.patch("socket.create_connection", side_effect=RuntimeError(message)):
                _, stderr = quiet_collect(target)
            self.assertNotIn("p@ss", stderr)
            self.assertNotIn("p%40ss", stderr)
            self.assertIn(REDACTED, stderr)

    def test_unparseable_url_password_is_still_scrubbed(self):
        # an unencoded "/" breaks URL parsing, so the literal secret is unknown
        url = "redis://user:p@ss/word@cache:6379"
        with mock.patch("socket.create_connection", side_effect=RuntimeError(f"cannot reach {url}")):
            _, stderr = quiet_collect(Target(redis_url=url))
        self.assertNotIn("ss/word", stderr)
        self.assertIn(f"redis://{REDACTED}@cache:6379", stderr)


if __name__ == "__main__":
    unittest.main()
