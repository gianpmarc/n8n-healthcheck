"""Scalability rules: can the instance take more load without falling over."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import DOCS, env_flag, env_int, is_queue_mode, n8n_containers, shown

# n8n's own recommendation: "setting concurrency to 5 or higher for your worker
# instances", because many low-concurrency workers exhaust the database connection pool.
MIN_WORKER_CONCURRENCY = 5

# Postgres refuses new connections at max_connections (minus the reserved superuser
# slots, 3 by default). 80% leaves room for a worker scale-out or a rolling restart,
# when old and new pods hold connections at the same time. Heuristic, not an n8n number.
PG_CONNECTION_USAGE_WARN = 0.80

# The official Helm chart lists workers as "2+ replicas": with one, any restart or
# node drain stops all production executions.
MIN_WORKER_REPLICAS = 2


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/control-concurrency
def regular_mode_unbounded_concurrency(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or is_queue_mode(env):
        return None
    # Postgres is the signal that this is a production setup rather than a default
    # trial install; a stock SQLite instance is not worth flagging.
    if str(env.get("DB_TYPE", "sqlite")).strip().lower() != "postgresdb":
        return None
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")
    if limit is not None and limit > 0:
        return None
    return Finding(
        rule_id="SCAL-001",
        area="scalability",
        severity="warning",
        title="Regular mode with no limit on concurrent production executions",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')} (default: regular); {shown(env, 'DB_TYPE')}; "
            f"{shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} (default: -1, no limit)."
        ),
        impact=(
            "In regular mode every execution runs inside the single main process, so a burst "
            "of webhooks or triggers can saturate its event loop and make the editor, API and "
            "webhooks unresponsive."
        ),
        recommendation=(
            "Set N8N_CONCURRENCY_PRODUCTION_LIMIT (for example 20) so excess production "
            "executions queue instead of running all at once. To scale beyond one process, "
            "move to queue mode: EXECUTIONS_MODE=queue with Redis and worker processes."
        ),
    )


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/README.md#architecture
def single_worker(snapshot: Snapshot) -> Optional[Finding]:
    if snapshot.kubernetes is None:
        return None
    workers = {}
    for deployment, _container, role in n8n_containers(snapshot.kubernetes):
        if role == "worker":
            workers[deployment["name"]] = deployment.get("replicas", 0)
    total = sum(workers.values())
    # Zero is not flagged here: workers may be scaled to zero by an autoscaler or run
    # outside this namespace. A queue that nobody consumes is caught by REL-004.
    if not workers or total == 0 or total >= MIN_WORKER_REPLICAS:
        return None
    names = ", ".join(f"{name} (replicas: {count})" for name, count in workers.items())
    return Finding(
        rule_id="SCAL-002",
        area="scalability",
        severity="warning",
        title="Only one queue-mode worker",
        evidence=f"Worker Deployments: {names}; {total} worker replica in total.",
        impact=(
            "A single worker runs every production execution, so its restart, eviction or "
            "an out-of-memory kill pauses all workflows, and throughput is capped at one "
            "process."
        ),
        recommendation=(
            f"Run at least {MIN_WORKER_REPLICAS} worker replicas (Helm chart: "
            "queueMode.workerReplicaCount), and scale on queue depth if load varies."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/set-up-task-runners
# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/queue-mode
def manual_executions_on_main(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or not is_queue_mode(env):
        return None
    if env_flag(env, "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS") is True:
        return None
    return Finding(
        rule_id="SCAL-003",
        area="scalability",
        severity="warning",
        title="Manual executions run on the main process",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')}; "
            f"{shown(env, 'OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS')} (default: false)."
        ),
        impact=(
            "Executions started from the editor run inside the main process, so one heavy "
            "manual test run competes with the UI, the API, webhook intake and triggers for "
            "the same CPU and memory."
        ),
        recommendation=(
            "Set OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true on the main process so manual "
            "executions go through the queue like production ones. n8n does not recommend "
            "running production with offloading disabled."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#concurrency-and-scaling-recommendations
def low_worker_concurrency(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or not is_queue_mode(env):
        return None
    # In queue mode a value other than -1 overrides the worker --concurrency flag.
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")
    if limit is None or limit < 1 or limit >= MIN_WORKER_CONCURRENCY:
        return None
    return Finding(
        rule_id="SCAL-004",
        area="scalability",
        severity="warning",
        title="Worker concurrency below n8n's recommended minimum",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')}; {shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} "
            f"(recommended: {MIN_WORKER_CONCURRENCY} or higher; worker default: 10)."
        ),
        impact=(
            "Reaching a given throughput with low-concurrency workers takes many more worker "
            "processes, each holding its own database connections, which can exhaust the "
            "Postgres connection pool and cause delays and failed executions."
        ),
        recommendation=(
            f"Set N8N_CONCURRENCY_PRODUCTION_LIMIT to {MIN_WORKER_CONCURRENCY} or higher "
            "(or remove it and use the worker's --concurrency flag, default 10), and run "
            "fewer, larger workers."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/database
def postgres_connections_near_limit(snapshot: Snapshot) -> Optional[Finding]:
    postgres = snapshot.postgres
    if postgres is None:
        return None
    used = postgres.get("connections")
    maximum = postgres.get("max_connections")
    if used is None or not maximum:
        return None
    usage = used / maximum
    if usage < PG_CONNECTION_USAGE_WARN:
        return None
    return Finding(
        rule_id="SCAL-005",
        area="scalability",
        severity="warning",
        title="Postgres connections close to max_connections",
        evidence=(
            f"{used} of {maximum} Postgres connections in use ({usage:.0%}; "
            f"warning threshold {PG_CONNECTION_USAGE_WARN:.0%})."
        ),
        impact=(
            "Once max_connections is reached Postgres rejects new connections, so adding a "
            "worker or doing a rolling restart makes n8n processes fail to start or "
            "executions fail to save."
        ),
        recommendation=(
            "Budget connections as DB_POSTGRESDB_POOL_SIZE (default 2) times the number of "
            "n8n processes (main, workers, webhook processors) and check what else shares "
            "the server. Then either raise Postgres max_connections, or lower "
            "DB_POSTGRESDB_POOL_SIZE / run fewer workers with higher concurrency."
        ),
    )


RULES = [
    regular_mode_unbounded_concurrency,
    single_worker,
    manual_executions_on_main,
    low_worker_concurrency,
    postgres_connections_near_limit,
]
