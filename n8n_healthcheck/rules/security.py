"""Security rules: only settings n8n documents and the environment can prove."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import (
    effective_env,
    env_flag,
    is_queue_mode,
    looks_like_production,
    shown,
    shown_each,
    unknown,
    worker_envs,
)


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/security
def insecure_cookie(snapshot: Snapshot) -> Optional[Finding]:
    env = effective_env(snapshot)
    if env is None or env_flag(env, "N8N_SECURE_COOKIE") is not False:
        return None
    return Finding(
        rule_id="SEC-001",
        area="security",
        severity="warning",
        title="Session cookie is not restricted to HTTPS",
        evidence=f"{shown(env, 'N8N_SECURE_COOKIE')} (default: true).{env.note}",
        impact=(
            "The browser sends the n8n session cookie over plain HTTP, so anyone on the "
            "network path can capture it and act as that user, including the instance owner."
        ),
        recommendation=(
            "Serve n8n over HTTPS (terminate TLS at a reverse proxy or load balancer and set "
            "N8N_PROXY_HOPS accordingly), then remove N8N_SECURE_COOKIE=false so the default "
            "of true applies."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/configure-n8n/set-up-task-runners
def task_runners_not_external(snapshot: Snapshot) -> Optional[Finding]:
    env = effective_env(snapshot)
    if env is None:
        return None
    mode = "N8N_RUNNERS_MODE"
    # The processes that run Code nodes. An env file describes all of them at once.
    judged = [env]
    if env.source:  # Kubernetes: main and each worker have their own environment
        if unknown(env, "EXECUTIONS_MODE"):
            return None
        if is_queue_mode(env):
            # Main only runs Code nodes for manual executions it does not offload; when
            # that cannot be told, it is not judged.
            offload = "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS"
            main_runs_code = env_flag(env, offload) is not True and not unknown(env, offload)
            judged = ([env] if main_runs_code else []) + worker_envs(snapshot.kubernetes)
    internal = [
        each
        for each in judged
        if not unknown(each, mode) and str(each.get(mode, "")).strip().lower() != "external"
    ]
    if not internal:
        return None
    if len(internal) == 1 and internal[0] is env:
        evidence = (
            f"{shown(env, mode)} (default: internal); "
            f"{shown(env, 'N8N_RUNNERS_ENABLED')}.{env.note}"
        )
    else:
        evidence = f"{shown_each(internal, mode)} (default: internal)."
    return Finding(
        rule_id="SEC-002",
        area="security",
        # Internal mode is the default, so a stock trial install (regular mode on SQLite)
        # gets an info; a setup that looks like production gets the warning.
        severity="warning" if looks_like_production(env) else "info",
        title="Code node runs without external task runners",
        evidence=evidence,
        impact=(
            "Code node scripts run in a child process of n8n (or, on n8n 1.x with runners "
            "disabled, inside n8n itself) as the same user on the same host, so code that "
            "escapes the sandbox can read the encryption key, stored credentials and "
            "environment variables."
        ),
        recommendation=(
            "Set N8N_RUNNERS_MODE=external and N8N_RUNNERS_AUTH_TOKEN, and run the "
            "n8nio/runners image (same version as n8n) as a sidecar next to every worker, "
            "and next to main unless OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true. On n8n 1.x "
            "also set N8N_RUNNERS_ENABLED=true. Internal mode is deprecated from n8n 3.0."
        ),
    )


RULES = [insecure_cookie, task_runners_not_external]
