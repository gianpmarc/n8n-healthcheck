"""Performance rules: what makes the instance slow or memory-hungry over time."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import effective_env, env_flag, is_queue_mode, shown, unknown


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/manage-execution-data#enable-executions-pruning
def pruning_disabled(snapshot: Snapshot) -> Optional[Finding]:
    env = effective_env(snapshot)
    if env is None or env_flag(env, "EXECUTIONS_DATA_PRUNE") is not False:
        return None
    evidence = f"{shown(env, 'EXECUTIONS_DATA_PRUNE')} (default: true).{env.note}"
    postgres = snapshot.postgres
    if postgres is not None and postgres.get("execution_count") is not None:
        evidence += (
            f" The database currently holds {postgres['execution_count']} executions "
            f"({postgres.get('execution_table_bytes')} bytes; oldest is "
            f"{postgres.get('oldest_execution_age_days')} days old)."
        )
    return Finding(
        rule_id="PERF-001",
        area="performance",
        severity="warning",
        title="Execution data pruning is disabled",
        evidence=evidence,
        impact=(
            "Execution history and its binary data are never deleted, so the database grows "
            "without bound until queries and the executions list slow down and the disk "
            "fills."
        ),
        recommendation=(
            "Set EXECUTIONS_DATA_PRUNE=true and choose retention with EXECUTIONS_DATA_MAX_AGE "
            "(hours, default 336) and EXECUTIONS_DATA_PRUNE_MAX_COUNT (default 10000). The "
            "first pruning passes on a large table take time; run them off-peak."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/manage-execution-data#reduce-saved-data
def saving_execution_progress(snapshot: Snapshot) -> Optional[Finding]:
    env = effective_env(snapshot)
    if env is None or env_flag(env, "EXECUTIONS_DATA_SAVE_ON_PROGRESS") is not True:
        return None
    return Finding(
        rule_id="PERF-002",
        area="performance",
        severity="warning",
        title="Execution progress is saved after every node",
        evidence=f"{shown(env, 'EXECUTIONS_DATA_SAVE_ON_PROGRESS')} (default: false).{env.note}",
        impact=(
            "n8n writes the full execution data to the database after each node instead of "
            "once at the end, so every execution costs one database write per node and "
            "throughput drops as workflows and payloads grow."
        ),
        recommendation=(
            "Set EXECUTIONS_DATA_SAVE_ON_PROGRESS=false instance-wide, and turn on 'Save "
            "execution progress' in the workflow settings of the few workflows that need to "
            "resume from the failed node."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/scaling/handle-binary-data
def binary_data_in_memory(snapshot: Snapshot) -> Optional[Finding]:
    env = effective_env(snapshot)
    if env is None:
        return None
    # Only an explicit `default` is flagged. When the variable is unset the effective
    # mode depends on the n8n version and execution mode (filesystem in regular mode
    # from n8n 2.0, database in queue mode), which the environment alone cannot tell.
    if str(env.get("N8N_DEFAULT_BINARY_DATA_MODE", "")).strip().lower() != "default":
        return None
    if unknown(env, "EXECUTIONS_MODE"):
        fix = (
            "Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem (on a persistent volume) if this "
            "instance runs in regular mode. In queue mode use database, or s3 / azure if "
            "your licence includes external storage; filesystem only works there when main, "
            "workers and webhook processors mount the same shared volume."
        )
    elif is_queue_mode(env):
        fix = (
            "Set N8N_DEFAULT_BINARY_DATA_MODE=database, or s3 / azure if your licence "
            "includes external storage (preferred in queue mode, as database mode grows "
            "Postgres). filesystem only works in queue mode when main, workers and webhook "
            "processors mount the same shared volume."
        )
    else:
        fix = (
            "Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem so files are written to "
            "N8N_BINARY_DATA_STORAGE_PATH on a persistent volume instead of held in memory."
        )
    return Finding(
        rule_id="PERF-003",
        area="performance",
        severity="warning",
        title="Binary data is kept in memory",
        evidence=(
            f"{shown(env, 'N8N_DEFAULT_BINARY_DATA_MODE')}; {shown(env, 'EXECUTIONS_MODE')} "
            f"(default: regular).{env.note}"
        ),
        impact=(
            "Every file a workflow handles is held in the n8n process memory, so large or "
            "concurrent files cause out-of-memory crashes that take down all running "
            "executions in that process."
        ),
        recommendation=fix,
    )


RULES = [pruning_disabled, saving_execution_progress, binary_data_in_memory]
