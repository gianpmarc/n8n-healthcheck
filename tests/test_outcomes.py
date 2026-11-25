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
DISAGREE = ("not_assessed", "sources_disagree", ())


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


def beside_env_file(env, main_env, **overrides):
    """An env file, and Kubernetes with one main container read in the same run."""
    return Snapshot(env=env, kubernetes=main(main_env, **overrides))


QUEUE_MAIN = beside_env_file(dict(PRODUCTION), dict(QUEUE))  # the file leaves the mode unset: regular by default


def text_listing(*versions, **sections):
    """A debug information whose cluster section lists these versions."""
    snapshot = TEXT(**sections)
    snapshot.debug_info["cluster"]["versions"] = list(versions)
    return snapshot


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
        ("queue mode in the env file, regular in a text", TEXT(pasted.REGULAR, env=dict(QUEUE)), DISAGREE),
        ("a limit in a text that the env file does not set", TEXT(pasted.REGULAR, ("concurrency: -1", "concurrency: 20"), env=dict(PRODUCTION)), DISAGREE),
        ("queue mode on a main container that the env file leaves unset", QUEUE_MAIN, FINDING),  # as in 0.5.0: the file wins
        ("a stock install in the env file, Postgres on a main container", beside_env_file({}, dict(PRODUCTION)), DISAGREE),
        ("a limit in the env file, a main container read in part", beside_env_file({**PRODUCTION, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}, {}, env_complete=False), UNREADABLE),
        ("a limit in the env file, the same on a main container", beside_env_file({**PRODUCTION, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}, {**PRODUCTION, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "20"}), READ),
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
        ("two replicas in Kubernetes, one worker listed by n8n", TEXT(pasted.ONE_WORKER, kubernetes=healthy_kube()), DISAGREE),
        ("two replicas in Kubernetes, two workers listed by n8n", TEXT(kubernetes=kube(("n8n-main", 1, container(env=QUEUE)), ("n8n-worker", 2, container(command=["worker"])))), READ),
        ("two replicas in Kubernetes, a text in queue mode, a main container that is not", TEXT(kubernetes=healthy_kube()), DISAGREE),
        ("two replicas in Kubernetes, an env file in regular mode", Snapshot(env=dict(REGULAR), kubernetes=kube(("w", 2, container(command=["worker"])))), DISAGREE),
        ("two replicas in Kubernetes, an env file that leaves the mode unset", Snapshot(env={}, kubernetes=kube(("w", 2, container(command=["worker"])))), DISAGREE),
        ("two replicas in Kubernetes, an env file in queue mode", Snapshot(env=dict(QUEUE), kubernetes=kube(("w", 2, container(command=["worker"])))), READ),
        ("two replicas in Kubernetes, the mode of the env file in a file", Snapshot(env={"EXECUTIONS_MODE_FILE": "x"}, kubernetes=kube(("w", 2, container(command=["worker"])))), READ),
        ("two replicas in Kubernetes, a text in regular mode", TEXT(pasted.REGULAR, kubernetes=kube(("w", 2, container(command=["worker"])))), DISAGREE),
        ("one replica in Kubernetes, an env file in regular mode", Snapshot(env=dict(REGULAR), kubernetes=kube(("w", 1, container(command=["worker"])))), FINDING),  # as in 0.5.0
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
    ],
    "SCAL-003": [
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("a text alone", TEXT(), ENV_OR_KUBE),
        ("queue mode", Snapshot(env=dict(QUEUE)), FINDING),
        ("offloading on", Snapshot(env={**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"}), READ),
        ("regular mode by default in the env file, queue in a text", TEXT(env={}), DISAGREE),
        ("queue mode in the env file and in a text", TEXT(env={**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "true"}), READ),
        ("regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("offloading in a Secret", Snapshot(kubernetes=main({**QUEUE, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": SECRET})), UNREADABLE),
        ("the mode read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("Kubernetes without a main container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "SCAL-004": [
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
        ("an env file with a limit of 10, a worker read in part", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=with_workers(QUEUE, dict(QUEUE), env_complete=False)), UNREADABLE),
        ("an env file with a limit of 10, workers set to 10", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=with_workers(QUEUE, {**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"})), READ),
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
        ("an env file with a limit of 10, a worker set to 2", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=with_workers(QUEUE, {"N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"})), DISAGREE),
        ("an env file with a limit of 10, a worker with a flag of 2", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=with_workers(QUEUE, {}, command=["worker", "--concurrency=2"])), DISAGREE),
        # Not set on the worker is n8n's default there, no limit, and not the 10 of the file.
        ("an env file with a limit of 10, workers that set neither", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}, kubernetes=with_workers(QUEUE, {})), DISAGREE),
    ],
    "SCAL-005": [
        ("nothing given", Snapshot(), missing("postgres")),
        ("95 of 100 in use", Snapshot(postgres={"connections": 95, "max_connections": 100}), FINDING),
        ("10 of 100 in use", Snapshot(postgres={"connections": 10, "max_connections": 100}), READ),
        ("no count", Snapshot(postgres={"max_connections": 100}), NOT_STATED),
    ],
    "REL-001": [
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
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
        ("regular by default in the env file, a main container read in part", beside_env_file({}, {}, env_complete=False), UNREADABLE),
        ("regular by default in the env file and on a main container", beside_env_file({}, {}), NOT_APPLICABLE_),
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
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
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
        ("nothing given", Snapshot(), missing("redis")),
        ("no Redis, regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("90% in use, noeviction", Snapshot(redis={**REDIS, "used_memory_bytes": 900}), FINDING),
        ("90% in use, a policy that evicts", Snapshot(redis={**REDIS, "used_memory_bytes": 900, "maxmemory_policy": "allkeys-lru"}), FINDING),
        ("10% in use", Snapshot(redis=dict(REDIS)), READ),
        ("no memory limit", Snapshot(redis={**REDIS, "maxmemory_bytes": 0}), NOT_APPLICABLE_),
        ("no memory reported", Snapshot(redis={"maxmemory_bytes": 1000}), NOT_STATED),
    ],
    "REL-004": [
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
        ("nothing given", Snapshot(), missing("redis")),
        ("no Redis, regular mode", Snapshot(env={}), NOT_APPLICABLE_),
        ("50 waiting, none active", Snapshot(redis={**REDIS, "waiting_jobs": 50, "active_jobs": 0}), FINDING),
        ("500 waiting", Snapshot(redis={**REDIS, "waiting_jobs": 500, "active_jobs": 10}), FINDING),
        ("a queue that moves", Snapshot(redis=dict(REDIS)), READ),
        ("nine waiting, none active", Snapshot(redis={**REDIS, "waiting_jobs": 9, "active_jobs": 0}), READ),  # what the sentence says, no more
        ("no queue key", Snapshot(redis={"waiting_jobs": None, "active_jobs": None}), NOT_STATED),
    ],
    "REL-005": [
        ("nothing given", Snapshot(), missing("url")),
        ("not ready", Snapshot(health={"healthz": True, "readiness": False}), FINDING),
        ("ready", Snapshot(health={"healthz": True, "readiness": True}), READ),
        ("no answer", Snapshot(health={"healthz": True}), NOT_STATED),
    ],
    "REL-006": [
        ("regular by default in the env file, queue on a main container", QUEUE_MAIN, DISAGREE),
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
        ("the default in the env file, the opposite on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_PRUNE": "false"}), DISAGREE),
        ("the default in the env file, the same set on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_PRUNE": "true"}), DEFAULT),
        ("the default in the env file, on a main container in a Secret", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_PRUNE": SECRET}), UNREADABLE),
        ("the default in the env file, a main container read in part", beside_env_file(dict(PRODUCTION), dict(PRODUCTION), env_complete=False), UNREADABLE),
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
        ("the default in the env file, the opposite on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "true"}), DISAGREE),
        ("the default in the env file, the same set on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false"}), DEFAULT),
        ("the default in the env file, on a main container in a Secret", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "EXECUTIONS_DATA_SAVE_ON_PROGRESS": SECRET}), UNREADABLE),
        ("the default in the env file, a main container read in part", beside_env_file(dict(PRODUCTION), dict(PRODUCTION), env_complete=False), UNREADABLE),
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
        ("on the filesystem in the env file, in memory in a text", TEXT(("binaryMode: filesystem", "binaryMode: memory"), env={**QUEUE, "DB_TYPE": "postgresdb", "N8N_DEFAULT_BINARY_DATA_MODE": "filesystem"}), DISAGREE),
        ("on the filesystem in the env file and in a text", TEXT(env={**QUEUE, "DB_TYPE": "postgresdb", "N8N_DEFAULT_BINARY_DATA_MODE": "filesystem"}), READ),
        ("on the filesystem in the env file, not set on a main container", beside_env_file({"N8N_DEFAULT_BINARY_DATA_MODE": "filesystem"}, {}), DISAGREE),
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
        ("set in the env file, a main container that does not set it", beside_env_file({"N8N_METRICS": "true"}, {}), DISAGREE),
        ("set in the env file and on a main container", beside_env_file({"N8N_METRICS": "true"}, {"N8N_METRICS": "true"}), READ),
        ("nothing given", Snapshot(), missing("env", "kubernetes", "url")),
        ("a URL whose metrics are off", Snapshot(health={"healthz": True, "metrics_disabled": True}), ENV_OR_KUBE),
        ("metrics scraped, nothing else", Snapshot(metrics={"n8n_version_info": 1}), READ),
        ("not set", Snapshot(env={}), FINDING),
        ("not set, queue mode", Snapshot(env=dict(QUEUE)), FINDING),
        ("set", Snapshot(env={"N8N_METRICS": "true"}), READ),
        ("set and scraped", Snapshot(env={"N8N_METRICS": "true"}, metrics={"n8n_version_info": 1}), READ),
        ("not set but scraped", Snapshot(env={}, metrics={"n8n_version_info": 1}), DISAGREE),
        ("set to false but scraped", Snapshot(env={"N8N_METRICS": "false"}, metrics={"n8n_version_info": 1}), DISAGREE),
        ("set, and the URL answers 404 on /metrics", Snapshot(env={"N8N_METRICS": "true"}, health={"healthz": True, "readiness": True, "metrics_disabled": True}), DISAGREE),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_METRICS": SECRET})), UNREADABLE),
        ("Kubernetes without a main container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "MAINT-002": [
        ("nothing given", Snapshot(), missing("kubernetes")),
        ("latest", Snapshot(kubernetes=main({}, image="n8nio/n8n:latest")), FINDING),
        ("a version", Snapshot(kubernetes=healthy_kube()), READ),
        ("a tag that is none of the five compared", Snapshot(kubernetes=main({}, image="registry.example/n8n:prod")), READ),  # what the sentence says, no more
        ("no n8n container", Snapshot(kubernetes=kube()), NOT_STATED),
    ],
    "MAINT-003": [
        ("nothing given", Snapshot(), missing("kubernetes", "debug_info")),
        ("two versions", Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(image="n8nio/n8n:2.39.0", command=["worker"])))), FINDING),
        ("one version everywhere", Snapshot(kubernetes=healthy_kube()), READ),
        ("one image pinned by digest", Snapshot(kubernetes=kube(("a", 1, container()), ("b", 1, container(image="n8nio/n8n@sha256:abc", command=["worker"])))), NOT_STATED),
        ("no tag names a version", Snapshot(kubernetes=main({}, image="n8nio/n8n:latest")), NOT_STATED),
        ("a text with one version", TEXT(), READ),
        ("one version in the images, two listed by n8n", text_listing("1.2.3", "1.3.0", kubernetes=main({}, image="n8nio/n8n:1.2.3")), DISAGREE),
        ("one version in the images, another listed by n8n", text_listing("1.3.0", kubernetes=main({}, image="n8nio/n8n:1.2.3")), DISAGREE),
        ("one version in the images, the same listed by n8n", text_listing("3.3.0", kubernetes=main({}, image="n8nio/n8n:3.3.0")), READ),
        ("one version in the images, the same listed by n8n, another as the version of the text", text_listing("1.2.3", kubernetes=main({}, image="n8nio/n8n:1.2.3")), DISAGREE),
        ("one version in the images, another read from /metrics", Snapshot(kubernetes=main({}, image="n8nio/n8n:1.2.3"), n8n_version="1.5.0"), DISAGREE),
        ("one version in the images, the same read from /metrics", Snapshot(kubernetes=main({}, image="n8nio/n8n:1.2.3-build7"), n8n_version="1.2.3"), READ),
        ("one version in the images, another in a text without a cluster section", TEXT(base=pasted.NO_CLUSTER, kubernetes=main({}, image="n8nio/n8n:1.2.3")), DISAGREE),
        ("a text with one version, another read from /metrics", TEXT(n8n_version="1.5.0"), DISAGREE),
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
        ("the default in the env file, the opposite on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "N8N_SECURE_COOKIE": "false"}), DISAGREE),
        ("the default in the env file, the same set on a main container", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "N8N_SECURE_COOKIE": "true"}), DEFAULT),
        ("the default in the env file, on a main container in a Secret", beside_env_file(dict(PRODUCTION), {**PRODUCTION, "N8N_SECURE_COOKIE": SECRET}), UNREADABLE),
        ("the default in the env file, a main container read in part", beside_env_file(dict(PRODUCTION), dict(PRODUCTION), env_complete=False), UNREADABLE),
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("set to false", Snapshot(env={"N8N_SECURE_COOKIE": "false"}), FINDING),
        ("set to true", Snapshot(env={"N8N_SECURE_COOKIE": "true"}), READ),
        ("not set", Snapshot(env={}), DEFAULT),
        ("in a Secret", Snapshot(kubernetes=main({"N8N_SECURE_COOKIE": SECRET})), UNREADABLE),
        ("read in part", Snapshot(kubernetes=main({}, env_complete=False)), UNREADABLE),
        ("a text that flags it", TEXT(), FINDING),
        # The text reports it only when it is insecure: its silence is never a pass.
        ("a text that does not flag it", TEXT(pasted.SECURE), NOT_STATED),
        ("set to true, a text that flags it", TEXT(env={**QUEUE, "N8N_SECURE_COOKIE": "true"}), DISAGREE),
    ],
    "SEC-002": [
        ("nothing given", Snapshot(), ENV_OR_KUBE),
        ("not set", Snapshot(env={}), FINDING),
        ("external, the version not known", Snapshot(env={"N8N_RUNNERS_MODE": "external"}), NOT_STATED),
        ("external on n8n 1.x", Snapshot(env={"N8N_RUNNERS_MODE": "external"}, n8n_version="1.80.0"), NOT_STATED),
        ("external on n8n 2.x", Snapshot(env={"N8N_RUNNERS_MODE": "external"}, n8n_version="2.1.0"), READ),
        ("external on n8n 3.x, the version from a text", TEXT(env={**QUEUE, "DB_TYPE": "postgresdb", "N8N_RUNNERS_MODE": "external"}), READ),
        ("external, runners enabled", Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "true"}), READ),
        ("external on n8n 1.x, runners enabled", Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "true"}, n8n_version="1.80.0"), READ),
        ("external on n8n 2.x, runners enabled set to a word n8n does not know", Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "yes"}, n8n_version="2.1.0"), NOT_STATED),
        ("external on n8n 2.x, runners disabled", Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "false"}, n8n_version="2.1.0"), NOT_STATED),
        ("external in the env file on n8n 2.x, a main container that does not set it", Snapshot(env={"N8N_RUNNERS_MODE": "external"}, kubernetes=main(dict(PRODUCTION)), n8n_version="2.1.0"), DISAGREE),
        ("external in the env file on n8n 2.x, a main container read in part", Snapshot(env={"N8N_RUNNERS_MODE": "external"}, kubernetes=main({"N8N_RUNNERS_MODE": "external"}, env_complete=False), n8n_version="2.1.0"), UNREADABLE),
        ("external, runners disabled", Snapshot(env={"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "false"}), NOT_STATED),
        ("external on every process, runners disabled on a worker", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": "0"})), NOT_STATED),
        ("external, runners enabled in a Secret", Snapshot(kubernetes=main({"N8N_RUNNERS_MODE": "external", "N8N_RUNNERS_ENABLED": SECRET})), UNREADABLE),
        ("external in an env file, internal on a worker of Kubernetes", Snapshot(env={**QUEUE, "N8N_RUNNERS_MODE": "external"}, kubernetes=with_workers(QUEUE, {"N8N_RUNNERS_MODE": "internal"})), DISAGREE),
        ("external on main, internal on a worker", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {})), FINDING),
        ("external on main and on every worker", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {**QUEUE, "N8N_RUNNERS_MODE": "external"})), READ),
        # The database is one for the instance: a worker that leaves DB_TYPE to its default is on SQLite.
        ("external on main and on every worker, a worker on another database", Snapshot(kubernetes=with_workers({**QUEUE, "N8N_RUNNERS_MODE": "external"}, {"N8N_RUNNERS_MODE": "external"})), DISAGREE),
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
        ("a text that flags it, a probe that says true", TEXT(env={**QUEUE, "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true"}), DISAGREE),
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

class ReviewTest(unittest.TestCase):
    """What the review of 0.6.0 found: a pass says no more than what was read, and never against a source."""

    def test_a_passed_sentence_states_only_the_condition_that_was_checked(self):
        said = {rule_id: rule.info.passed for rule_id, rule in RULES.items()}
        # REL-004 passes with nine jobs waiting and none active: the sentence gives the two thresholds, nothing more.
        self.assertIn("Fewer than 100 jobs wait", said["REL-004"])
        self.assertIn("10 or more wait while none is active", said["REL-004"])
        # MAINT-002 passes on a tag such as `prod`: the sentence names the five it compares, and says so.
        for tag in ("latest", "stable", "beta", "next", "nightly"):
            self.assertIn(tag, said["MAINT-002"])
        self.assertIn("Only these are compared", said["MAINT-002"])
        # PERF-003 passes on filesystem in queue mode: whether the volume is shared is not read.
        self.assertTrue(said["PERF-003"].startswith("N8N_DEFAULT_BINARY_DATA_MODE is set to one of filesystem, s3, azure, database"))
        self.assertIn("is not checked", said["PERF-003"])
        # SEC-002 passes on the two variables it read, on the processes it read.
        self.assertIn("N8N_RUNNERS_ENABLED is not set to false", said["SEC-002"])
        self.assertIn("it is not set and every n8n version read is 2.x or later", said["SEC-002"])
        self.assertNotIn("enabled", said["SEC-002"].replace("N8N_RUNNERS_ENABLED", ""))
        self.assertIn("that was read", said["SEC-002"])
        for rule_id, stronger in (
            ("REL-004", "is being consumed"), ("REL-004", "no backlog"), ("MAINT-002", "a tag that moves ("), ("PERF-003", "kept out of the process memory"),
            ("SEC-002", "uses external task runners"), ("SCAL-002", "run with at least"), ("SCAL-004", "Every worker runs"), ("MAINT-003", "Every n8n process runs"),
            ("PERF-002", "is not saved after every node."), ("REL-006", "setup is enabled."), ("SCAL-003", "are offloaded"),
        ):  # fmt: skip
            self.assertNotIn(stronger, said[rule_id], rule_id)

    def test_two_sources_that_disagree_never_pass_and_never_hide_a_finding(self):
        seen = set()
        for rule_id, cases in CASES.items():
            for label, snapshot, expected in cases:
                if expected == DISAGREE:
                    seen.add(rule_id)
                    # Only an outcome without a finding changes: the rule called alone is silent, as in 0.5.0.
                    self.assertIsNone(RULES[rule_id](snapshot), (rule_id, label))
        self.assertLessEqual({"MAINT-003", "MAINT-001", "SEC-001", "SEC-003", "SCAL-004"}, seen)  # the five of the review
        # Where 0.5.0 raises a finding, the finding stays, whatever the other source says.
        for rule_id, snapshot in (
            ("SEC-001", TEXT(pasted.SECURE, env={**QUEUE, "N8N_SECURE_COOKIE": "false"})),
            ("REL-001", TEXT(env={"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite"})),
            ("SCAL-004", Snapshot(env={**QUEUE, "N8N_CONCURRENCY_PRODUCTION_LIMIT": "2"}, kubernetes=with_workers(QUEUE, {"N8N_CONCURRENCY_PRODUCTION_LIMIT": "10"}))),
            ("MAINT-001", Snapshot(env={"N8N_METRICS": "false"}, health={"healthz": True, "readiness": True, "metrics_disabled": True})),
        ):
            (check,) = engine.assess(snapshot, [RULES[rule_id]])
            self.assertEqual(check.outcome, "finding", rule_id)

    def test_what_counts_as_two_sources_that_disagree(self):
        from n8n_healthcheck.rules._helpers import SOURCES_DISAGREE, UNREADABLE as UNTOLD, against_sources

        both = ("instance", "main", "worker")
        limit = "N8N_CONCURRENCY_PRODUCTION_LIMIT"
        DIFFER = SOURCES_DISAGREE
        cases = [
            # The env file and a container are compared on what each states.
            (Snapshot(env={limit: "10"}, kubernetes=with_workers({limit: "10"}, {limit: "2"})), (limit,), both, DIFFER),
            (Snapshot(env={limit: "10"}, kubernetes=with_workers({limit: "10"}, {limit: " 10 "})), (limit,), both, None),
            (Snapshot(env={limit: "10"}, kubernetes=with_workers({limit: "10"}, {limit: "2"})), (limit,), ("instance", "main"), None),  # a role the rule does not judge
            (Snapshot(env={"N8N_METRICS": "1"}, kubernetes=main({"N8N_METRICS": "TRUE"})), ("N8N_METRICS",), both, None),
            # Not set, in an environment read in full, states the n8n default: on either side.
            (Snapshot(env={limit: "10"}, kubernetes=main({})), (limit,), both, DIFFER),
            (Snapshot(env={}, kubernetes=main({limit: "10"})), (limit,), both, DIFFER),
            (Snapshot(env={}, kubernetes=main({limit: "-1"})), (limit,), both, None),
            (Snapshot(env={}, kubernetes=main({})), (limit, "N8N_SECURE_COOKIE", "N8N_RUNNERS_MODE", "N8N_METRICS"), both, None),
            (Snapshot(env={}, kubernetes=main({"N8N_SECURE_COOKIE": "false"})), ("N8N_SECURE_COOKIE",), both, DIFFER),
            (Snapshot(env={"N8N_RUNNERS_MODE": "external"}, kubernetes=main({})), ("N8N_RUNNERS_MODE",), both, DIFFER),
            (Snapshot(env={"N8N_METRICS": "true"}, kubernetes=main({})), ("N8N_METRICS",), both, DIFFER),
            (Snapshot(env={}, kubernetes=main({"N8N_RUNNERS_MODE": "internal", "N8N_METRICS": "false", "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "0"})), ("N8N_RUNNERS_MODE", "N8N_METRICS", "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"), both, None),
            (Snapshot(env={"N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true"}, kubernetes=main({})), ("N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES",), both, None),
            # A variable without a default that holds on every version: not set states only that.
            (Snapshot(env={"N8N_DEFAULT_BINARY_DATA_MODE": "s3"}, kubernetes=main({})), ("N8N_DEFAULT_BINARY_DATA_MODE",), both, DIFFER),
            (Snapshot(env={}, kubernetes=main({})), ("N8N_DEFAULT_BINARY_DATA_MODE", "N8N_RUNNERS_ENABLED"), both, None),
            # A container that cannot be told, next to an env file, cannot be said to agree.
            (Snapshot(env={limit: "10"}, kubernetes=main({limit: SECRET})), (limit,), both, UNTOLD),
            (Snapshot(env={}, kubernetes=main({}, env_complete=False)), (limit,), both, UNTOLD),
            (Snapshot(env={}, kubernetes=main({limit + "_FILE": "x"})), (limit,), both, UNTOLD),
            (Snapshot(env={}, kubernetes=main({limit: "-1"}, env_complete=False)), (limit,), both, None),  # set where it was read
            (Snapshot(env={}, kubernetes=with_workers({limit: "5"}, {}, env_complete=False)), (limit,), both, DIFFER),  # a difference comes first
            (Snapshot(env={limit + "_FILE": "x"}, kubernetes=main({limit: "2"})), (limit,), both, None),  # the file itself does not tell: the rule says so
            # The execution mode is always read on main as well.
            (Snapshot(env={}, kubernetes=main(dict(QUEUE))), ("EXECUTIONS_MODE",), ("instance", "worker"), DIFFER),
            (Snapshot(env={}, kubernetes=main({limit: "2"})), (limit,), ("instance", "worker"), None),
            # The mode and the database are one for the instance: every container, whatever the roles the rule judges.
            (Snapshot(env={}, kubernetes=with_workers({}, dict(QUEUE))), ("EXECUTIONS_MODE",), ("instance", "main"), DIFFER),
            (Snapshot(env=dict(QUEUE), kubernetes=with_workers(QUEUE, {"EXECUTIONS_MODE": "queue"})), ("DB_TYPE",), ("instance", "main"), DIFFER),  # the worker is on SQLite by default
            (Snapshot(env=dict(QUEUE), kubernetes=with_workers(QUEUE, {"DB_TYPE": "postgresdb"})), ("EXECUTIONS_MODE", "DB_TYPE"), ("instance", "main"), None),  # a worker that does not set the mode says nothing about it
            (Snapshot(env=dict(QUEUE), kubernetes=with_workers(QUEUE, {"EXECUTIONS_MODE": SECRET})), ("EXECUTIONS_MODE",), ("instance", "main"), UNTOLD),
            # ... and between the containers of Kubernetes themselves, without an env file.
            (Snapshot(kubernetes=with_workers({}, {"EXECUTIONS_MODE": "queue"})), ("EXECUTIONS_MODE",), ("instance", "main"), DIFFER),
            (Snapshot(kubernetes=with_workers(QUEUE, {"EXECUTIONS_MODE": "queue"})), ("DB_TYPE",), ("instance", "main"), DIFFER),
            (Snapshot(kubernetes=with_workers(QUEUE, {"DB_TYPE": "postgresdb"})), ("EXECUTIONS_MODE", "DB_TYPE"), ("instance", "main"), None),
            (Snapshot(kubernetes=with_workers(QUEUE, {"EXECUTIONS_MODE": SECRET, "DB_TYPE": SECRET})), ("EXECUTIONS_MODE", "DB_TYPE"), ("instance", "main"), None),  # no file that says how every process is set
            # For any other variable two containers are two processes, not two sources.
            (Snapshot(kubernetes=with_workers({limit: "10"}, {limit: "2"})), (limit,), both, None),
            (Snapshot(kubernetes=main({}, env_complete=False)), (limit,), both, None),
            # The text states effective values of the main process.
            (TEXT(env={}), ("EXECUTIONS_MODE",), both, DIFFER),  # the text says queue
            (TEXT(pasted.REGULAR, env={}), ("EXECUTIONS_MODE",), both, None),
            (TEXT(env={"EXECUTIONS_MODE": "queue"}), ("EXECUTIONS_MODE", "DB_TYPE"), both, DIFFER),  # the text says Postgres
            (TEXT(env=dict(QUEUE)), ("EXECUTIONS_MODE", "DB_TYPE"), both, None),
            (TEXT(kubernetes=main({})), ("EXECUTIONS_MODE",), both, DIFFER),
            (TEXT(kubernetes=main({}, env_complete=False)), ("EXECUTIONS_MODE",), both, None),  # the text fills what was not read
            (TEXT(env={"EXECUTIONS_MODE_FILE": "x"}), ("EXECUTIONS_MODE",), both, None),
            (TEXT(kubernetes=main({"EXECUTIONS_MODE": SECRET})), ("EXECUTIONS_MODE",), both, None),
            (TEXT(kubernetes=with_workers(QUEUE, {})), ("EXECUTIONS_MODE",), both, None),  # the text describes main, not a worker
            (TEXT(env={}), ("N8N_RUNNERS_MODE",), both, None),  # the text does not state it
            (TEXT(env={}), (), both, None),
            (TEXT(env={"N8N_DEFAULT_BINARY_DATA_MODE": "s3"}), ("N8N_DEFAULT_BINARY_DATA_MODE",), both, DIFFER),  # the text says filesystem
            (TEXT(env={}), ("N8N_DEFAULT_BINARY_DATA_MODE",), both, None),  # not set, and no default to hold against the text
            # The two security settings are in the text only as false.
            (TEXT(env={}), ("N8N_SECURE_COOKIE",), both, DIFFER),  # the default, true, against the flag
            (TEXT(pasted.SECURE, env={}), ("N8N_SECURE_COOKIE",), both, None),
            (TEXT(env={"N8N_SECURE_COOKIE": "false"}), ("N8N_SECURE_COOKIE",), both, None),
            (TEXT(pasted.SECURE, env={"N8N_SECURE_COOKIE": "true"}), ("N8N_SECURE_COOKIE",), both, None),
        ]
        for number, (snapshot, names, scopes, expected) in enumerate(cases):
            with self.subTest(case=number):
                self.assertIs(against_sources(snapshot, names, scopes), expected)

    def test_the_third_review(self):
        limit = "N8N_CONCURRENCY_PRODUCTION_LIMIT"
        external = {"N8N_RUNNERS_MODE": "external"}
        worker = lambda env, replicas=1, **more: ("worker", replicas, container(command=["n8n", "worker"], env=env, **more))
        on = lambda env, **more: ("main", 1, container(env=env, **more))
        regular_file = lambda *others: Snapshot(env=dict(REGULAR), kubernetes=kube(on(dict(REGULAR)), *others))
        queue_rules = ("SCAL-003", "REL-001", "REL-002", "REL-003", "REL-004", "REL-006")
        cases = [
            # 1. SEC-002 leans on the version: every version read, not the first that answers.
            ("the only container on 1.x, /metrics on 2.x", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:1.80.0")), n8n_version="2.1.0"), {"SEC-002": DISAGREE}),
            ("/metrics on 2.x, the text on 1.x", TEXT(("n8nVersion: 3.3.0", "n8nVersion: 1.80.0"), env={**QUEUE, **external}, n8n_version="2.1.0"), {"SEC-002": DISAGREE}),
            ("main on 2.x, the workers on 1.x", Snapshot(kubernetes=kube(on({**QUEUE, **external}, image="n8nio/n8n:2.1.0"), worker({**QUEUE, **external}, 2, image="n8nio/n8n:1.80.0"))), {"SEC-002": DISAGREE}),
            ("the text on 3.x, an instance it lists on 1.x", text_listing("3.3.0", "1.80.0", env={**QUEUE, **external}), {"SEC-002": DISAGREE}),
            ("every version read on 1.x", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:1.80.0")), n8n_version="1.79.2"), {"SEC-002": NOT_STATED}),
            ("every version read on 2.x or later", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:v2.1.0-exp.0")), n8n_version="3.0.1"), {"SEC-002": READ}),
            ("an image that names no version, /metrics on 2.x", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:latest")), n8n_version="2.1.0"), {"SEC-002": READ}),
            ("the only container on 1.x, runners enabled", Snapshot(kubernetes=kube(on({**external, "N8N_RUNNERS_ENABLED": "true"}, image="n8nio/n8n:1.80.0")), n8n_version="2.1.0"), {"SEC-002": READ}),
            # 2. The mode and the database are one for the instance: the env file against every container.
            ("a worker in queue mode, the env file in regular mode", regular_file(worker(dict(QUEUE))), dict.fromkeys(("SCAL-001", *queue_rules), DISAGREE)),
            ("a webhook processor in queue mode, the env file in regular mode", regular_file(("webhook", 1, container(command=["n8n", "webhook"], env={"EXECUTIONS_MODE": "queue"}))),
             dict.fromkeys(("SCAL-001", "SCAL-002", "SCAL-004", "PERF-002", *queue_rules), DISAGREE)),
            ("workers on SQLite, the env file and main on Postgres", Snapshot(env=dict(QUEUE), kubernetes=kube(on(dict(QUEUE)), worker({"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite"}, 2))), {"REL-001": DISAGREE}),
            ("a worker whose mode is in a Secret, next to an env file", regular_file(worker({"EXECUTIONS_MODE": SECRET})), {"SCAL-003": UNREADABLE, "REL-002": UNREADABLE}),
            ("a worker that does not set the mode says nothing about it", Snapshot(env=dict(QUEUE), kubernetes=kube(on(dict(QUEUE)), worker(dict(PRODUCTION), 2))), {"REL-001": READ, "SCAL-002": READ}),
            # 3. SCAL-004 on the limit of the env file alone, against the limit of the text.
            ("a limit of 10 in the env file, of 3 in the text", TEXT(("concurrency: -1", "concurrency: 3"), env={**QUEUE, limit: "10"}), {"SCAL-004": DISAGREE}),
            ("a limit of 10 in the env file and in the text", TEXT(("concurrency: -1", "concurrency: 10"), env={**QUEUE, limit: "10"}), {"SCAL-004": READ}),
            ("a limit of 10 in the env file and on the workers, of 3 in the text", TEXT(("concurrency: -1", "concurrency: 3"), env={**QUEUE, limit: "10"}, kubernetes=kube(worker({**QUEUE, limit: "10"}))), {"SCAL-004": READ}),
            # 4. The same within Kubernetes alone.
            ("main in regular mode, workers in queue mode", Snapshot(kubernetes=kube(on({}), worker({"EXECUTIONS_MODE": "queue"}, 2))),
             dict.fromkeys(("SCAL-002", "SCAL-003", "REL-001", "REL-002", "REL-006", "PERF-002"), DISAGREE)),
            ("main on Postgres, workers on SQLite", Snapshot(kubernetes=kube(on(dict(QUEUE)), worker({"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite"}, 2))), {"REL-001": DISAGREE}),
            ("main and workers in queue mode on Postgres", Snapshot(kubernetes=kube(on(dict(QUEUE)), worker(dict(QUEUE), 2))), {"REL-001": READ, "SCAL-002": READ}),
            # A finding of 0.5.0 stays, whatever another container says.
            ("one worker replica in queue mode, main in regular mode", Snapshot(kubernetes=kube(on({}), worker({"EXECUTIONS_MODE": "queue"}))), {"SCAL-002": FINDING}),
            ("queue mode on SQLite on main, workers on Postgres", Snapshot(kubernetes=kube(on({"EXECUTIONS_MODE": "queue"}), worker(dict(QUEUE)))), {"REL-001": FINDING}),
        ]
        for label, snapshot, expected in cases:
            for rule_id, outcome in expected.items():
                with self.subTest(case=label, rule=rule_id):
                    (check,) = engine.assess(snapshot, [RULES[rule_id]])
                    self.assertEqual(stated(check), outcome)
                    self.assertEqual(RULES[rule_id](snapshot) is not None, outcome == FINDING)

    def test_the_fourth_review(self):
        external = {"N8N_RUNNERS_MODE": "external"}
        worker = lambda env, replicas=2, **more: ("worker", replicas, container(command=["n8n", "worker"], env=env, **more))
        webhook = lambda env, **more: ("webhook", 1, container(command=["n8n", "webhook"], env=env, **more))
        on = lambda env, **more: ("main", 1, container(env=env, **more))
        queue = {"EXECUTIONS_MODE": "queue"}
        runners = lambda *deployments: Snapshot(kubernetes=kube(*deployments), n8n_version="2.1.0")
        cases = [
            # 1. The debug information against every container, for the mode and the database type.
            ("workers in queue mode, the text in regular mode", TEXT(pasted.REGULAR, kubernetes=kube(worker(queue))), dict.fromkeys(("REL-001", "REL-002", "REL-006"), DISAGREE)),
            ("the same next to a main container read in part", TEXT(pasted.REGULAR, kubernetes=kube(on({}, env_complete=False), worker(queue))),
             dict.fromkeys(("REL-001", "REL-002", "REL-006"), DISAGREE)),
            ("a webhook processor in queue mode, the text in regular mode", TEXT(pasted.REGULAR, kubernetes=kube(webhook(queue))), dict.fromkeys(("REL-001", "REL-006"), DISAGREE)),
            ("workers on SQLite, the text in queue mode on Postgres", TEXT(kubernetes=kube(worker({**queue, "DB_TYPE": "sqlite"}))), {"REL-001": DISAGREE}),
            ("workers and the text in queue mode on Postgres", TEXT(kubernetes=kube(worker(dict(QUEUE)))), {"REL-001": READ}),
            # 2. A worker read in part may set the mode in what was not read.
            ("a worker read in part that shows no mode, next to an env file", Snapshot(env=dict(REGULAR), kubernetes=kube(on(dict(REGULAR)), worker({}, env_complete=False))),
             {"SCAL-003": UNREADABLE, "REL-006": UNREADABLE}),
            ("a worker read in full that does not set the mode", Snapshot(env=dict(REGULAR), kubernetes=kube(on(dict(REGULAR)), worker({}))), {"SCAL-003": NOT_APPLICABLE_, "REL-006": NOT_APPLICABLE_}),
            # 3. SEC-002: a tag that starts with a version number is a version read, on a webhook processor too.
            ("main on 2.x, a webhook processor on 1.x", runners(on(external, image="n8nio/n8n:2.1.0"), webhook({}, image="n8nio/n8n:1.80.0")), {"SEC-002": DISAGREE}),
            ("the only container on the tag 1", runners(on(external, image="n8nio/n8n:1")), {"SEC-002": DISAGREE}),
            ("the only container on the tag 1.80", runners(on(external, image="n8nio/n8n:1.80")), {"SEC-002": DISAGREE}),
            ("the only container on the tag 2, no other version", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:2"))), {"SEC-002": READ}),
            ("a tag that is a date or a build number names no version", Snapshot(kubernetes=kube(on(external, image="n8nio/n8n:20261105"))), {"SEC-002": NOT_STATED}),
            ("a tag that starts with a digit and is a word names no version", runners(on(external, image="n8nio/n8n:1a2b3c")), {"SEC-002": READ}),
        ]
        for label, snapshot, expected in cases:
            for rule_id, outcome in expected.items():
                with self.subTest(case=label, rule=rule_id):
                    (check,) = engine.assess(snapshot, [RULES[rule_id]])
                    self.assertEqual(stated(check), outcome)
                    self.assertEqual(RULES[rule_id](snapshot) is not None, outcome == FINDING)

    def test_what_makes_a_rule_not_apply_is_carried_to_the_profile(self):
        by_mode = lambda snapshot, rule_id: engine.assess(snapshot, [RULES[rule_id]])[0].by_mode
        self.assertTrue(by_mode(Snapshot(env={}), "REL-002"))  # no Redis, and the mode is regular
        self.assertTrue(by_mode(Snapshot(env={}), "SCAL-003"))
        self.assertFalse(by_mode(Snapshot(redis={**REDIS, "maxmemory_bytes": 0}), "REL-003"))  # no memory limit: the mode was not read
        self.assertFalse(by_mode(Snapshot(env=dict(QUEUE)), "REL-001"))


if __name__ == "__main__":
    unittest.main()
