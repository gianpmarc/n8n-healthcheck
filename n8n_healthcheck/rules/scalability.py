"""Scalability rules: can the instance take more load without falling over."""

from __future__ import annotations

from .. import debug_info
from ..models import Finding, Outcome, Snapshot, Variant
from ._helpers import (
    NOT_APPLICABLE,
    NOT_QUEUE,
    NOT_STATED,
    PASSED,
    PASSED_BY_DEFAULT,
    SOURCES_DISAGREE,
    UNREADABLE,
    _same,
    command_words,
    concurrency_flag,
    container_env,
    effective_env,
    env_flag,
    env_int,
    is_queue_mode,
    looks_like_production,
    n8n_containers,
    no_environment,
    num,
    queue_only,
    rule,
    shown,
    source_missing,
    told,
    undecided,
    unknown,
    with_debug_info,
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

CONCURRENCY_LIMIT = "N8N_CONCURRENCY_PRODUCTION_LIMIT"

INVALID_LIMIT = Variant(
    title="Invalid production concurrency limit",
    impact=(
        "n8n raises an error when it sets up concurrency control with a limit of 0, "
        "so the setting does not limit anything and can stop the instance starting."
    ),
    remedy=(
        "Set N8N_CONCURRENCY_PRODUCTION_LIMIT to a positive number (for example 20) "
        "to cap concurrent production executions, or to -1 to disable the limit."
    ),
)
NO_LIMIT = Variant(
    title="Regular mode with no limit on concurrent production executions",
    impact=(
        "In regular mode every execution runs inside the single main process, so a burst "
        "of webhooks or triggers can saturate its event loop and make the editor, API and "
        "webhooks unresponsive."
    ),
    remedy=(
        "Set N8N_CONCURRENCY_PRODUCTION_LIMIT (for example 20) so excess production "
        "executions queue instead of running all at once. To scale beyond one process, "
        "move to queue mode: EXECUTIONS_MODE=queue with Redis and worker processes."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/control-concurrency
@rule(
    "SCAL-001",
    "scalability",
    subject="Limit on concurrent production executions in regular mode",
    passed="Regular mode has a limit on concurrent production executions.",
    variants=(INVALID_LIMIT, NO_LIMIT),
    facts=("executions.mode", "database.engine", "main.concurrency_limit"),
    settings=("EXECUTIONS_MODE", "DB_TYPE", CONCURRENCY_LIMIT),
)
def regular_mode_unbounded_concurrency(snapshot: Snapshot) -> Finding | Outcome:
    info = regular_mode_unbounded_concurrency.info
    env = with_debug_info(snapshot, "EXECUTIONS_MODE", "DB_TYPE", "N8N_CONCURRENCY_PRODUCTION_LIMIT")
    if env is None:
        return no_environment(snapshot, text=True)
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")
    if limit == 0:
        # n8n's ConcurrencyControlService throws InvalidConcurrencyLimitError on 0,
        # before it looks at the execution mode, so this holds in queue mode too.
        return info.finding(
            INVALID_LIMIT,
            "warning",
            "N8N_CONCURRENCY_PRODUCTION_LIMIT=0, which n8n rejects with "
            "InvalidConcurrencyLimitError (valid: -1 to disable, or a positive number)."
            f"{env.note}",
        )
    if is_queue_mode(env) or unknown(env, "EXECUTIONS_MODE"):
        return undecided(env, "EXECUTIONS_MODE") or NOT_APPLICABLE
    # A stock SQLite install keeps this documented default and is not worth flagging.
    if not looks_like_production(env):
        # Not a stock install for certain when the database type itself cannot be told.
        return undecided(env, "DB_TYPE") or NOT_APPLICABLE
    if (limit is not None and limit > 0) or unknown(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT"):
        return undecided(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT") or undecided(env, "EXECUTIONS_MODE") or PASSED
    return info.finding(
        NO_LIMIT,
        # info, not warning: no limit is the documented default, and whether it hurts
        # depends on load the environment cannot show.
        "info",
        f"{shown(env, 'EXECUTIONS_MODE')} (default: regular); {shown(env, 'DB_TYPE')}; "
        f"{shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} (default: -1, no limit)."
        f"{env.note}",
    )


ONE_WORKER = Variant(
    title="Only one queue-mode worker",
    impact=(
        "A single worker runs every production execution, so its restart, eviction or "
        "an out-of-memory kill pauses all workflows, and throughput is capped at one "
        "process."
    ),
    remedy=(
        f"Run at least {MIN_WORKER_REPLICAS} worker replicas (Helm chart: "
        "queueMode.workerReplicaCount), and scale on queue depth if load varies."
    ),
)


# https://github.com/n8n-io/n8n-hosting/blob/main/charts/n8n/README.md#architecture
@rule(
    "SCAL-002",
    "scalability",
    subject="Number of queue-mode workers",
    passed=(
        f"There are at least {MIN_WORKER_REPLICAS} workers: replicas that the worker Deployments ask for, "
        "or worker instances that n8n lists as registered."
    ),
    variants=(ONE_WORKER,),
    facts=("worker.replicas", "executions.mode"),
    settings=("EXECUTIONS_MODE",),
)
def single_worker(snapshot: Snapshot) -> Finding | Outcome:
    info = single_worker.info
    workers = {}
    for deployment, _container, role in n8n_containers(snapshot.kubernetes or {}):
        if role == "worker":
            workers[deployment["name"]] = deployment.get("replicas") or 0
    total = sum(workers.values())
    if workers:
        # Zero is not flagged here: workers may be scaled to zero by an autoscaler or run
        # outside this namespace. A queue that nobody consumes is caught by REL-004.
        if total == 0:
            return NOT_STATED
        if total >= MIN_WORKER_REPLICAS:
            # Kubernetes asks for enough replicas while n8n itself lists fewer workers.
            listed = (((debug_info.of(snapshot) or {}).get("cluster") or {}).get("instances") or {}).get("worker")
            fewer = isinstance(listed, int) and not isinstance(listed, bool) and 1 <= listed < MIN_WORKER_REPLICAS
            # Workers run in queue mode only, while the env file, which describes every
            # process, or the debug information says the instance is in regular mode.
            modes = [told(snapshot.env, "EXECUTIONS_MODE")] if snapshot.env is not None else []
            modes.append((debug_info.of(snapshot) or {}).get("execution_mode"))
            regular = any(mode is not None and not _same(mode, "queue") for mode in modes)
            return SOURCES_DISAGREE if fewer or regular else PASSED
        names = ", ".join(f"{name} (replicas: {count})" for name, count in workers.items())
        evidence = f"Worker Deployments: {names}; {total} worker replica in total."
    else:
        # Kubernetes shows no worker (or was not given): the instances n8n itself listed
        # in the debug information, which says nothing unless the mode is known to be queue.
        cluster = (debug_info.of(snapshot) or {}).get("cluster") or {}
        env = with_debug_info(snapshot, "EXECUTIONS_MODE", rests_on_text=True)
        count = (cluster.get("instances") or {}).get("worker")
        if count != 1 or env is None or not is_queue_mode(env):
            if env is not None and queue_only(env) is NOT_QUEUE:
                return NOT_QUEUE
            if snapshot.kubernetes is None and debug_info.of(snapshot) is None:
                return source_missing("kubernetes", "debug_info")
            # No worker listed is not zero workers: only Deployments and registered instances are seen.
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                return NOT_STATED
            if env is None:
                return NOT_STATED
            return queue_only(env) or PASSED
        evidence = (
            f"The {debug_info.phrase(debug_info.of(snapshot))} lists 1 worker instance; "
            f"{shown(env, 'EXECUTIONS_MODE')}.{env.note}"
        )
    return info.finding(ONE_WORKER, "warning", evidence)


MANUAL_ON_MAIN = Variant(
    title="Manual executions run on the main process",
    impact=(
        "Executions started from the editor run inside the main process, so one heavy "
        "manual test run competes with the UI, the API, webhook intake and triggers for "
        "the same CPU and memory."
    ),
    remedy=(
        "Set OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true on the main process so manual "
        "executions go through the queue like production ones. n8n does not recommend "
        "running production with offloading disabled."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/set-up-task-runners
# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/queue-mode
@rule(
    "SCAL-003",
    "scalability",
    subject="Where manual executions run in queue mode",
    passed="Manual executions are set to be offloaded to the workers (OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS is true).",
    variants=(MANUAL_ON_MAIN,),
    facts=("executions.mode",),
    settings=("EXECUTIONS_MODE", "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"),
)
def manual_executions_on_main(snapshot: Snapshot) -> Finding | Outcome:
    env = effective_env(snapshot)
    if env is None:
        return no_environment(snapshot)
    if not is_queue_mode(env):
        return queue_only(env)
    offload = "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"
    if env_flag(env, offload) is True or unknown(env, offload):
        return undecided(env, offload) or PASSED
    return manual_executions_on_main.info.finding(
        MANUAL_ON_MAIN,
        "warning",
        f"{shown(env, 'EXECUTIONS_MODE')}; "
        f"{shown(env, 'OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS')} (default: false).{env.note}",
    )


LOW_CONCURRENCY = Variant(
    title="Worker concurrency below n8n's recommended minimum",
    impact=(
        "Reaching a given throughput with low-concurrency workers takes many more worker "
        "processes, each holding its own database connections, which can exhaust the "
        "Postgres connection pool and cause delays and failed executions."
    ),
    remedy=(
        f"Raise worker concurrency to {MIN_WORKER_CONCURRENCY} or higher and run fewer, "
        "larger workers. It is set by the worker's --concurrency flag (Helm chart: "
        "queueMode.workerConcurrency; default 10), unless "
        "N8N_CONCURRENCY_PRODUCTION_LIMIT is set, which overrides the flag."
    ),
)


def _low_concurrency(deployment: dict, container: dict, own_env: dict):
    """The evidence of one worker set below the minimum; None when it is not, or cannot be told."""
    # A limit that is set but unreadable, or possibly set in a Secret, may
    # override the flag: nothing can be said about this worker.
    if unknown(own_env, CONCURRENCY_LIMIT):
        return None
    own_limit = env_int(own_env, CONCURRENCY_LIMIT)
    flag = concurrency_flag(container)
    if own_limit is not None and own_limit >= 1:
        value, source = own_limit, f"N8N_CONCURRENCY_PRODUCTION_LIMIT={own_limit}"
    elif flag is not None:
        value, source = flag, f"--concurrency={flag}"
    else:
        return None
    if 1 <= value < MIN_WORKER_CONCURRENCY:
        return f"{deployment['name']}/{container['name']}: {source}"
    return None


def _worker_concurrency_verified(own_env: dict, container: dict) -> Outcome:
    """The outcome for one worker that raised no finding: only a number that is known passes."""
    why = undecided(own_env, CONCURRENCY_LIMIT)
    if why is not None:
        return why
    own_limit = env_int(own_env, CONCURRENCY_LIMIT)
    if own_limit is not None and own_limit >= MIN_WORKER_CONCURRENCY:
        return PASSED
    # Set to something that is neither a limit nor -1 (no limit): n8n may refuse it.
    if CONCURRENCY_LIMIT in own_env and own_limit != -1:
        return NOT_STATED
    flag = concurrency_flag(container)
    if flag is not None:
        return PASSED if flag >= MIN_WORKER_CONCURRENCY else NOT_STATED
    # e.g. --concurrency=$WORKERS: the flag is there, its number is not.
    if any(word.startswith("--concurrency") for word in command_words(container)):
        return NOT_STATED
    return PASSED_BY_DEFAULT  # neither is set: the worker default, 10, applies


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/enable-queue-mode#concurrency-and-scaling-recommendations
@rule(
    "SCAL-004",
    "scalability",
    subject="Worker concurrency",
    passed=f"The concurrency set for every worker that was read is {MIN_WORKER_CONCURRENCY} or higher.",
    variants=(LOW_CONCURRENCY,),
    facts=("worker.concurrency", "executions.mode"),
    settings=("EXECUTIONS_MODE", CONCURRENCY_LIMIT),
    scopes=("instance", "worker"),
)
def low_worker_concurrency(snapshot: Snapshot) -> Finding | Outcome:
    # Deliberately the env file only, not effective_env(): a file describes every process,
    # while the main container's limit says nothing about the workers, which are read
    # one by one below.
    env = snapshot.env
    if env is None and snapshot.kubernetes is None:
        return source_missing("env", "kubernetes")
    # In queue mode N8N_CONCURRENCY_PRODUCTION_LIMIT, when set to anything but -1,
    # overrides the worker --concurrency flag; otherwise the flag (default 10) applies.
    limit = env_int(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT") if env and is_queue_mode(env) else None
    observed = []
    verified = []  # the outcome of each worker that raised no finding
    if limit is not None and limit >= 1:
        if limit < MIN_WORKER_CONCURRENCY:
            observed.append(
                f"{shown(env, 'N8N_CONCURRENCY_PRODUCTION_LIMIT')} "
                f"({shown(env, 'EXECUTIONS_MODE')})"
            )
        verified.append(PASSED)
    elif snapshot.kubernetes is not None:
        for deployment, container, role in n8n_containers(snapshot.kubernetes):
            if role != "worker":
                continue
            own_env = container_env(deployment, container)
            verified.append(_worker_concurrency_verified(own_env, container))
            low = _low_concurrency(deployment, container, own_env)
            if low is not None:
                observed.append(low)
    if not observed:
        # The env file gives every worker a limit, and a worker container of Kubernetes,
        # which is not read then, is set below the minimum by itself.
        if limit is not None and limit >= 1 and any(
            _low_concurrency(deployment, container, container_env(deployment, container))
            for deployment, container, role in n8n_containers(snapshot.kubernetes or {})
            if role == "worker"
        ):
            return SOURCES_DISAGREE
        if env is not None:
            # The file describes every process: its mode and its limit come first.
            why = undecided(env, "EXECUTIONS_MODE") or (undecided(env, CONCURRENCY_LIMIT) if is_queue_mode(env) else None)
            if why is not None:
                return why
            # Set to something that is neither a limit nor -1 (no limit): n8n may refuse it.
            if is_queue_mode(env) and CONCURRENCY_LIMIT in env and limit != -1 and not (limit or 0) >= 1:
                return NOT_STATED
        if not verified:
            main = effective_env(snapshot)
            if main is not None and queue_only(main) is NOT_QUEUE:
                return NOT_QUEUE
            # An env file does not show the --concurrency flag; Kubernetes showed no worker.
            return source_missing("kubernetes") if snapshot.kubernetes is None else NOT_STATED
        for reason in (UNREADABLE, NOT_STATED):
            if reason in verified:
                return reason
        return PASSED_BY_DEFAULT if PASSED_BY_DEFAULT in verified else PASSED
    return low_worker_concurrency.info.finding(
        LOW_CONCURRENCY,
        "warning",
        "; ".join(observed)
        + f" (recommended: {MIN_WORKER_CONCURRENCY} or higher; worker default: 10).",
    )


CONNECTIONS_NEAR_LIMIT = Variant(
    title="Postgres connections close to max_connections",
    impact=(
        "Once max_connections is reached Postgres rejects new connections, so adding a "
        "worker or doing a rolling restart makes n8n processes fail to start or "
        "executions fail to save."
    ),
    remedy=(
        "Budget connections as DB_POSTGRESDB_POOL_SIZE (default 2) times the number of "
        "n8n processes (main, workers, webhook processors) and check what else shares "
        "the server. Then either raise Postgres max_connections, or lower "
        "DB_POSTGRESDB_POOL_SIZE / run fewer workers with higher concurrency."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/database
@rule(
    "SCAL-005",
    "scalability",
    subject="Postgres connections in use",
    passed=f"Postgres connections in use are below {PG_CONNECTION_USAGE_WARN:.0%} of max_connections.",
    variants=(CONNECTIONS_NEAR_LIMIT,),
    facts=("database.max_connections",),
)
def postgres_connections_near_limit(snapshot: Snapshot) -> Finding | Outcome:
    postgres = snapshot.postgres
    if postgres is None:
        return source_missing("postgres")
    used = num(postgres, "connections")
    maximum = num(postgres, "max_connections")
    if used is None or not maximum:
        return NOT_STATED
    usage = used / maximum
    if usage < PG_CONNECTION_USAGE_WARN:
        return PASSED
    return postgres_connections_near_limit.info.finding(
        CONNECTIONS_NEAR_LIMIT,
        "warning",
        f"{used} of {maximum} Postgres connections in use ({usage:.0%}; "
        f"warning threshold {PG_CONNECTION_USAGE_WARN:.0%}).",
    )


RULES = [
    regular_mode_unbounded_concurrency,
    single_worker,
    manual_executions_on_main,
    low_worker_concurrency,
    postgres_connections_near_limit,
]
