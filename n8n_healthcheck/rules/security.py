"""Security rules: only settings n8n documents and the environment can prove."""

from __future__ import annotations

from .. import debug_info
from ..models import Finding, Outcome, Snapshot, Variant
from ._helpers import (
    NOT_STATED,
    PASSED,
    UNREADABLE,
    effective_env,
    env_flag,
    flag_is,
    is_queue_mode,
    looks_like_production,
    n8n_major,
    no_environment,
    rule,
    shown,
    shown_each,
    source_missing,
    undecided,
    unknown,
    with_debug_info,
    worker_envs,
)


INSECURE_COOKIE = Variant(
    title="Session cookie is not restricted to HTTPS",
    impact=(
        "The browser sends the n8n session cookie over plain HTTP, so anyone on the "
        "network path can capture it and act as that user, including the instance owner."
    ),
    remedy=(
        "Serve n8n over HTTPS (terminate TLS at a reverse proxy or load balancer and set "
        "N8N_PROXY_HOPS accordingly), then remove N8N_SECURE_COOKIE=false so the default "
        "of true applies."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/security
@rule(
    "SEC-001",
    "security",
    subject="Session cookie restricted to HTTPS",
    passed="The session cookie is restricted to HTTPS.",
    variants=(INSECURE_COOKIE,),
    facts=("security.secure_cookie",),
    settings=("N8N_SECURE_COOKIE",),
)
def insecure_cookie(snapshot: Snapshot) -> Finding | Outcome:
    env = with_debug_info(snapshot, "N8N_SECURE_COOKIE")
    if env is None:
        # The debug information reports this setting only when it is insecure: its silence
        # proves nothing, and only the environment would let the rule decide.
        return no_environment(snapshot, text=True, needs=("env", "kubernetes"))
    if env_flag(env, "N8N_SECURE_COOKIE") is not False:
        return flag_is(env, "N8N_SECURE_COOKIE", good=True, default=True)
    return insecure_cookie.info.finding(
        INSECURE_COOKIE, "warning", f"{shown(env, 'N8N_SECURE_COOKIE')} (default: true).{env.note}"
    )


RUNNERS_NOT_EXTERNAL = Variant(
    title="Code node runs without external task runners",
    impact=(
        "Code node scripts run in a child process of n8n (or, on n8n 1.x with runners "
        "disabled, inside n8n itself) as the same user on the same host, so code that "
        "escapes the sandbox can read the encryption key, stored credentials and "
        "environment variables."
    ),
    remedy=(
        "Set N8N_RUNNERS_MODE=external and N8N_RUNNERS_AUTH_TOKEN, and run the "
        "n8nio/runners image (same version as n8n) as a sidecar next to every worker, "
        "and next to main unless OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true. On n8n 1.x "
        "also set N8N_RUNNERS_ENABLED=true. Internal mode is deprecated from n8n 3.0."
    ),
)


def _external_verified(env: dict, major) -> Outcome:
    """The outcome for one process whose runner mode raised no finding.

    External mode says where the runners are, not that they are on. The rule's own remedy
    says that n8n 1.x also needs N8N_RUNNERS_ENABLED=true, so a variable that is not set
    passes only when the version read (`major`) is 2 or later: with 1.x, or a version
    that is not known, the rule cannot say. Nobody checked n8n itself for the default on
    1.x, nor whether n8n 2.x still reads the variable: `false` never passes, on any version.
    """
    enabled = "N8N_RUNNERS_ENABLED"
    why = undecided(env, "N8N_RUNNERS_MODE") or undecided(env, enabled)
    if why is not None:
        return why
    if enabled in env:
        return PASSED if env_flag(env, enabled) is True else NOT_STATED
    return PASSED if major is not None and major >= 2 else NOT_STATED


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/set-up-task-runners
@rule(
    "SEC-002",
    "security",
    subject="Task runners for the Code node",
    passed=(
        "N8N_RUNNERS_MODE is external on every process that was read and runs Code nodes, and "
        "N8N_RUNNERS_ENABLED is not set to false there: it is set to true, or it is not set and "
        "the n8n version read is 2.x or later."
    ),
    variants=(RUNNERS_NOT_EXTERNAL,),
    facts=("executions.mode", "n8n.version"),
    # DB_TYPE: with the mode, it decides the severity of the finding.
    settings=("N8N_RUNNERS_MODE", "N8N_RUNNERS_ENABLED", "EXECUTIONS_MODE", "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS", "DB_TYPE"),
    scopes=("instance", "main", "worker"),
)
def task_runners_not_external(snapshot: Snapshot) -> Finding | Outcome:
    env = effective_env(snapshot)
    if env is None:
        return no_environment(snapshot)
    mode = "N8N_RUNNERS_MODE"
    # The processes that run Code nodes. An env file describes all of them at once.
    judged = [env]
    main_unknown = None  # why main could not be judged, when it could not
    if env.source:  # Kubernetes: main and each worker have their own environment
        if unknown(env, "EXECUTIONS_MODE"):
            return undecided(env, "EXECUTIONS_MODE") or UNREADABLE
        if is_queue_mode(env):
            # Main only runs Code nodes for manual executions it does not offload; when
            # that cannot be told, it is not judged.
            offload = "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"
            main_runs_code = env_flag(env, offload) is not True and not unknown(env, offload)
            main_unknown = undecided(env, offload)
            judged = ([env] if main_runs_code else []) + worker_envs(snapshot.kubernetes)
    internal = [
        each
        for each in judged
        if not unknown(each, mode) and str(each.get(mode, "")).strip().lower() != "external"
    ]
    if not internal:
        # External is not the default: it passes only where it was read, on every process judged.
        major = n8n_major(snapshot)
        outcomes = [_external_verified(each, major) for each in judged] + ([main_unknown] if main_unknown else [])
        if not judged:
            return main_unknown or NOT_STATED  # no worker was found, and main does not run Code nodes
        for outcome in (UNREADABLE, NOT_STATED):
            if outcome in outcomes:
                return outcome
        return undecided(env, "EXECUTIONS_MODE") or PASSED
    if len(internal) == 1 and internal[0] is env:
        evidence = (
            f"{shown(env, mode)} (default: internal); "
            f"{shown(env, 'N8N_RUNNERS_ENABLED')}.{env.note}"
        )
    else:
        evidence = f"{shown_each(internal, mode)} (default: internal)."
    return task_runners_not_external.info.finding(
        RUNNERS_NOT_EXTERNAL,
        # Internal mode is the default, so a stock trial install (regular mode on SQLite)
        # gets an info; a setup that looks like production gets the warning.
        "warning" if looks_like_production(env) else "info",
        evidence,
    )


BLOCK_FILE_ACCESS = "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES"
FILE_ACCESS_OPEN = Variant(
    title="Access to n8n's own files is not blocked",
    impact=(
        "Nodes that read files from disk can open n8n's own directory and configuration "
        "files, which hold the instance's encryption key, so anyone who can build a "
        "workflow can read the key that protects every stored credential."
    ),
    remedy=(
        f"Remove {BLOCK_FILE_ACCESS}=false so the default of true applies. If workflows need files "
        "on disk, keep them outside n8n's directory and limit the nodes to that place "
        "with N8N_RESTRICT_FILE_ACCESS_TO."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/security
@rule(
    "SEC-003",
    "security",
    subject="Access to n8n's own files from the nodes",
    passed="Access to n8n's own files is set to be blocked.",
    variants=(FILE_ACCESS_OPEN,),
    facts=("security.file_access_blocked",),
    settings=(BLOCK_FILE_ACCESS,),
)
def file_access_not_blocked(snapshot: Snapshot) -> Finding | Outcome:
    name = BLOCK_FILE_ACCESS
    # Fires only on what the debug information flags. A probe that states the variable
    # still wins: set to true there, the rule is silent.
    if not ((debug_info.of(snapshot) or {}).get("insecure") or {}).get("file_access_blocked"):
        # The text reports this setting only when it is insecure, so its silence proves
        # nothing, and the rule never rests on the default: it passes only on a probe that
        # states the variable as true.
        stated_here = effective_env(snapshot)
        if stated_here is not None and env_flag(stated_here, name) is True:
            return PASSED
        return NOT_STATED if debug_info.of(snapshot) is not None else source_missing("debug_info")
    env = with_debug_info(snapshot, name, rests_on_text=True)
    if env is None or env_flag(env, name) is not False:
        if env is not None and env_flag(env, name) is True:
            return PASSED
        return (undecided(env, name) if env is not None else None) or NOT_STATED
    return file_access_not_blocked.info.finding(
        FILE_ACCESS_OPEN,
        "warning",
        f"The {debug_info.phrase(debug_info.of(snapshot))} flags blockFileAccessToN8nFiles as false; "
        f"{shown(env, name)} (default: true).{env.note}",
    )


RULES = [insecure_cookie, task_runners_not_external, file_access_not_blocked]
