"""Scalability rules: can the instance take more load without falling over."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import (
    concurrency_flag,
    container_env,
    effective_env,
    env_flag,
    env_int,
    is_queue_mode,
    looks_like_production,
    n8n_containers,
    num,
    shown,
    unknown,
)

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
    env = effective_env(snapshot)
    if env is None or is_queue_mode(env) or unknown(env, "EXECUTIONS_MODE"):
        return None
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")
    if limit == 0:
        # n8n's ConcurrencyControlService throws InvalidConcurrencyLimitError on 0.
        return Finding(
            rule_id="SCAL-001",
            area="scalability",
            severity="warning",
            title="Invalid production concurrency limit",
            evidence=(
                "N8N_CONCURRENCY_PRODUCTION_LIMIT=0, which n8n rejects with "
                "InvalidConcurrencyLimitError (valid: -1 to disable, or a positive number)."
                f"{env.note}"
            ),
            impact=(
                "n8n raises an error when it sets up concurrency control with a limit of 0, "
                "so the setting does not limit anything and can stop the instance starting."
            ),
            recommendation=(
                "Set N8N_CONCURRENCY_PRODUCTION_LIMIT to a positive number (for example 20) "
                "to cap concurrent production executions, or to -1 to disable the limit."
            ),
        )
    # A stock SQLite install keeps this documented default and is not worth flagging.
    if not looks_like_production(env):
        return None
    if (limit is not None and limit > 0) or unknown(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT"):
        return None
    return Finding(
        rule_id="SCAL-001",
        area="scalability",
        # info, not warning: no limit is the documented default, and whether it hurts
        # depends on load the environment cannot show.
        severity="info",
        title="Regular mode with no limit on concurrent production executions",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')} (default: regular); {shown(env, 'DB_TYPE')}; "
            f"{shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} (default: -1, no limit)."
            f"{env.note}"
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
            workers[deployment["name"]] = deployment.get("replicas") or 0
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
    env = effective_env(snapshot)
    if env is None or not is_queue_mode(env):
        return None
    offload = "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"
    if env_flag(env, offload) is True or unknown(env, offload):
        return None
    return Finding(
        rule_id="SCAL-003",
        area="scalability",
        severity="warning",
        title="Manual executions run on the main process",
        evidence=(
            f"{shown(env, 'EXECUTIONS_MODE')}; "
            f"{shown(env, 'OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS')} (default: false).{env.note}"
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
    # Deliberately the env file only, not effective_env(): a file describes every process,
    # while the main container's limit says nothing about the workers, which are read
    # one by one below.
    env = snapshot.env
    if env is None and snapshot.kubernetes is None:
        return None
    # In queue mode N8N_CONCURRENCY_PRODUCTION_LIMIT, when set to anything but -1,
    # overrides the worker --concurrency flag; otherwise the flag (default 10) applies.
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT") if env and is_queue_mode(env) else None
    observed = []
    if limit is not None and limit >= 1:
        if limit < MIN_WORKER_CONCURRENCY:
            observed.append(
                f"{shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} "
                f"({shown(env, 'EXECUTIONS_MODE')})"
            )
    elif snapshot.kubernetes is not None:
        for deployment, container, role in n8n_containers(snapshot.kubernetes):
            if role != "worker":
                continue
            own_env = container_env(deployment, container)
            # A limit that is set but unreadable, or possibly set in a Secret, may
            # override the flag: nothing can be said about this worker.
            if unknown(own_env, "N8N_CONCURRENCY_PRODUCTION_LIMIT"):
                continue
            own_limit = env_int(own_env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")
            flag = concurrency_flag(container)
            if own_limit is not None and own_limit >= 1:
                value, source = own_limit, f"N8N_CONCURRENCY_PRODUCTION_LIMIT={own_limit}"
            elif flag is not None:
                value, source = flag, f"--concurrency={flag}"
            else:
                continue
            if 1 <= value < MIN_WORKER_CONCURRENCY:
                observed.append(f"{deployment['name']}/{container['name']}: {source}")
    if not observed:
        return None
    return Finding(
        rule_id="SCAL-004",
        area="scalability",
        severity="warning",
        title="Worker concurrency below n8n's recommended minimum",
        evidence=(
            "; ".join(observed)
            + f" (recommended: {MIN_WORKER_CONCURRENCY} or higher; worker default: 10)."
        ),
        impact=(
            "Reaching a given throughput with low-concurrency workers takes many more worker "
            "processes, each holding its own database connections, which can exhaust the "
            "Postgres connection pool and cause delays and failed executions."
        ),
        recommendation=(
            f"Raise worker concurrency to {MIN_WORKER_CONCURRENCY} or higher and run fewer, "
            "larger workers. It is set by the worker's --concurrency flag (Helm chart: "
            "queueMode.workerConcurrency; default 10), unless "
            "N8N_CONCURRENCY_PRODUCTION_LIMIT is set, which overrides the flag."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/database
def postgres_connections_near_limit(snapshot: Snapshot) -> Optional[Finding]:
    postgres = snapshot.postgres
    if postgres is None:
        return None
    used = num(postgres, "connections")
    maximum = num(postgres, "max_connections")
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
