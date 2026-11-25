"""Performance rules: what makes the instance slow or memory-hungry over time."""

from __future__ import annotations

from ..models import Finding, Outcome, Snapshot, Variant
from ._helpers import (
    NOT_STATED,
    PASSED,
    PASSED_BY_DEFAULT,
    UNREADABLE,
    effective_env,
    env_flag,
    flag_is,
    is_queue_mode,
    no_environment,
    num,
    rule,
    shown,
    shown_each,
    size,
    undecided,
    unknown,
    with_debug_info,
    worker_envs,
)


def _oldest(postgres: dict) -> str:
    """The age of the oldest execution as a reader would say it."""
    days = num(postgres, "oldest_execution_age_days")
    if days is None:
        return "age of the oldest unknown"
    if days >= 1:
        return f"oldest is {days:.1f} days old"
    hours = int(days * 24)
    if hours >= 1:
        return f"oldest is {hours} hour{'s' if hours != 1 else ''} old"
    return "oldest is less than an hour old"


NO_PRUNING = Variant(
    title="Execution data pruning is disabled",
    impact=(
        "Execution history and its binary data are never deleted, so the database grows "
        "without bound until queries and the executions list slow down and the disk "
        "fills."
    ),
    remedy=(
        "Set EXECUTIONS_DATA_PRUNE=true and choose retention with EXECUTIONS_DATA_MAX_AGE "
        "(hours, default 336) and EXECUTIONS_DATA_PRUNE_MAX_COUNT (default 10000). The "
        "first pruning passes on a large table take time; run them off-peak."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/manage-execution-data#enable-executions-pruning
@rule(
    "PERF-001",
    "performance",
    subject="Pruning of execution data",
    passed="Execution data pruning is enabled.",
    variants=(NO_PRUNING,),
    facts=("pruning.enabled", "database.execution_count", "database.execution_table_bytes", "database.oldest_execution_age_days"),
    settings=("EXECUTIONS_DATA_PRUNE",),
)
def pruning_disabled(snapshot: Snapshot) -> Finding | Outcome:
    env = with_debug_info(snapshot, "EXECUTIONS_DATA_PRUNE")
    if env is None:
        return no_environment(snapshot, text=True)
    if env_flag(env, "EXECUTIONS_DATA_PRUNE") is not False:
        return flag_is(env, "EXECUTIONS_DATA_PRUNE", good=True, default=True)
    evidence = f"{shown(env, 'EXECUTIONS_DATA_PRUNE')} (default: true).{env.note}"
    postgres = snapshot.postgres
    if postgres is not None and postgres.get("execution_count") is not None:
        evidence += (
            f" The database currently holds {postgres['execution_count']} executions "
            f"({size(num(postgres, 'execution_table_bytes'))}; {_oldest(postgres)})."
        )
    return pruning_disabled.info.finding(NO_PRUNING, "warning", evidence)


SAVING_PROGRESS = Variant(
    title="Execution progress is saved after every node",
    impact=(
        "n8n writes the full execution data to the database after each node instead of "
        "once at the end, so every execution costs one database write per node and "
        "throughput drops as workflows and payloads grow."
    ),
    remedy=(
        "Set EXECUTIONS_DATA_SAVE_ON_PROGRESS=false instance-wide, and turn on 'Save "
        "execution progress' in the workflow settings of the few workflows that need to "
        "resume from the failed node."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/manage-execution-data#reduce-saved-data
@rule(
    "PERF-002",
    "performance",
    subject="Saving of execution progress after every node",
    passed=(
        "Saving execution progress after every node is off instance-wide "
        "(EXECUTIONS_DATA_SAVE_ON_PROGRESS); a workflow can still turn it on for itself."
    ),
    variants=(SAVING_PROGRESS,),
    facts=("executions.mode",),
    settings=("EXECUTIONS_MODE", "EXECUTIONS_DATA_SAVE_ON_PROGRESS"),
    scopes=("instance", "main", "worker"),
)
def saving_execution_progress(snapshot: Snapshot) -> Finding | Outcome:
    name = "EXECUTIONS_DATA_SAVE_ON_PROGRESS"
    env = with_debug_info(snapshot, "EXECUTIONS_MODE")
    if env is None:
        return no_environment(snapshot, text=True)
    # The debug information holds the settings of the main process, and in queue mode it
    # is the workers that are judged: the text is read for this one in regular mode only.
    if not is_queue_mode(env) and not unknown(env, "EXECUTIONS_MODE"):
        env = with_debug_info(snapshot, "EXECUTIONS_MODE", name)
    judged = [env]
    # In queue mode production executions run on the workers, each with its own
    # environment in Kubernetes; main alone says nothing unless it is known to be regular.
    if env.source and (is_queue_mode(env) or unknown(env, "EXECUTIONS_MODE")):
        judged = worker_envs(snapshot.kubernetes)
    saving = [each for each in judged if env_flag(each, name) is True]
    if not saving:
        if not (len(judged) == 1 and judged[0] is env):
            # The workers are judged: which processes run executions must be known first.
            why = undecided(env, "EXECUTIONS_MODE")
            if why is not None:
                return why
        # Not saved by default, so every process judged must be known; none is no proof.
        outcomes = [flag_is(each, name, good=False, default=False) for each in judged] or [NOT_STATED]
        for outcome in (UNREADABLE, NOT_STATED, PASSED_BY_DEFAULT):
            if outcome in outcomes:
                return outcome
        return PASSED
    if saving[0] is env:
        evidence = f"{shown(env, name)} (default: false).{env.note}"
    else:
        evidence = f"{shown_each(saving, name)} (default: false)."
    return saving_execution_progress.info.finding(SAVING_PROGRESS, "warning", evidence)


def _binary_in_memory(fix: str) -> Variant:
    return Variant(
        title="Binary data is kept in memory",
        impact=(
            "Every file a workflow handles is held in the n8n process memory, so large or "
            "concurrent files cause out-of-memory crashes that take down all running "
            "executions in that process."
        ),
        remedy=fix,
    )


# One per execution mode, as the mode decides where the files can go instead.
BINARY_MODE_UNKNOWN = _binary_in_memory(
    "Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem (on a persistent volume) if this "
    "instance runs in regular mode. In queue mode use database, or s3 / azure if "
    "your licence includes external storage; filesystem only works there when main, "
    "workers and webhook processors mount the same shared volume."
)
BINARY_MODE_QUEUE = _binary_in_memory(
    "Set N8N_DEFAULT_BINARY_DATA_MODE=database, or s3 / azure if your licence "
    "includes external storage (preferred in queue mode, as database mode grows "
    "Postgres). filesystem only works in queue mode when main, workers and webhook "
    "processors mount the same shared volume."
)
BINARY_MODE_REGULAR = _binary_in_memory(
    "Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem so files are written to "
    "N8N_BINARY_DATA_STORAGE_PATH on a persistent volume instead of held in memory."
)
# The modes of n8n that keep binary data out of the process memory.
BINARY_MODES_OUT_OF_MEMORY = ("filesystem", "s3", "azure", "database")


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/handle-binary-data
@rule(
    "PERF-003",
    "performance",
    subject="Where binary data is kept",
    passed=(
        f"N8N_DEFAULT_BINARY_DATA_MODE is set to one of {', '.join(BINARY_MODES_OUT_OF_MEMORY)}, not to the "
        "in-memory mode; whether every process can reach that storage is not checked."
    ),
    variants=(BINARY_MODE_UNKNOWN, BINARY_MODE_QUEUE, BINARY_MODE_REGULAR),
    facts=("binary_data.mode", "executions.mode"),
    settings=("N8N_DEFAULT_BINARY_DATA_MODE", "EXECUTIONS_MODE"),
)
def binary_data_in_memory(snapshot: Snapshot) -> Finding | Outcome:
    name = "N8N_DEFAULT_BINARY_DATA_MODE"
    env = effective_env(snapshot)
    if env is None:
        return no_environment(snapshot)
    # Only an explicit `default` is flagged. When the variable is unset the effective
    # mode depends on the n8n version and execution mode (filesystem in regular mode
    # from n8n 2.0, database in queue mode), which the environment alone cannot tell.
    mode = str(env.get(name, "")).strip().lower()
    if mode != "default":
        return undecided(env, name) or (PASSED if mode in BINARY_MODES_OUT_OF_MEMORY else NOT_STATED)
    if unknown(env, "EXECUTIONS_MODE"):
        variant = BINARY_MODE_UNKNOWN
    elif is_queue_mode(env):
        variant = BINARY_MODE_QUEUE
    else:
        variant = BINARY_MODE_REGULAR
    return binary_data_in_memory.info.finding(
        variant,
        "warning",
        f"{shown(env, 'N8N_DEFAULT_BINARY_DATA_MODE')}; {shown(env, 'EXECUTIONS_MODE')} "
        f"(default: regular).{env.note}",
    )


RULES = [pruning_disabled, saving_execution_progress, binary_data_in_memory]
