"""Kubernetes: the Deployments in the n8n namespace, via `kubectl`.

ConfigMaps are read to resolve the container environment. Secrets are never read.
"""

from __future__ import annotations

import json

from ..models import Target
from ._util import TIMEOUT, redact, run, warn

# A variable that is set but whose value this collector cannot or must not read.
UNKNOWN = "<valueFrom>"


def collect(target: Target) -> dict:
    def get(kind: str) -> str:
        args = ["kubectl"]
        if target.kube_context:
            args += ["--context", target.kube_context]
        args += ["get", kind, "-n", target.kube_namespace, "-o", "json"]
        return run(args + [f"--request-timeout={TIMEOUT}s"])

    deployments = get("deployments")
    try:
        configmaps = parse_configmaps(get("configmaps"))
    except Exception as error:  # e.g. RBAC without `get configmaps`: keep the Deployments
        warn("kube_namespace (configmaps)", error)
        configmaps = None
    return {"kubernetes": parse_deployments(deployments, configmaps)}


def parse_configmaps(output: str) -> dict:
    """ConfigMap name -> its `data` ({key: value})."""
    return {
        item["metadata"]["name"]: item.get("data") or {} for item in json.loads(output)["items"]
    }


def parse_deployments(output: str, configmaps: dict | None = None) -> dict:
    """`configmaps` is the parse_configmaps() result, or None when they could not be read."""
    items = json.loads(output)["items"]
    return {"deployments": [_deployment(item, configmaps) for item in items]}


def _deployment(item: dict, configmaps: dict | None) -> dict:
    containers = item["spec"]["template"]["spec"]["containers"]
    return {
        "name": item["metadata"]["name"],
        "replicas": item["spec"].get("replicas", 1),  # Kubernetes defaults to 1
        "ready_replicas": item.get("status", {}).get("readyReplicas", 0),  # absent when 0
        "containers": [_container(c, configmaps) for c in containers],
    }


def _container(container: dict, configmaps: dict | None) -> dict:
    resources = container.get("resources", {})
    env, env_complete = _environment(container, configmaps)
    return {
        "name": container["name"],
        "image": container.get("image", ""),
        # command + args, so `args: ["worker"]` and `command: ["n8n", "worker"]` look alike
        "command": container.get("command", []) + container.get("args", []),
        "env": env,
        "requests": resources.get("requests", {}),
        "limits": resources.get("limits", {}),
        "liveness_probe": "livenessProbe" in container,
        "readiness_probe": "readinessProbe" in container,
        "env_complete": env_complete,
    }


def _environment(container: dict, configmaps: dict | None) -> tuple[dict, bool]:
    """The container's variables and whether all of them could be read.

    Follows Kubernetes precedence: `envFrom` sources in order (a later one wins), then
    `env` entries, which override `envFrom`. `configmaps` is None when not fetched.
    """
    fetched = configmaps is not None
    configmaps = configmaps or {}
    env: dict = {}
    complete = True
    for source in container.get("envFrom") or []:
        ref = source.get("configMapRef") or {}
        data = configmaps.get(ref.get("name"))
        if data is None and fetched and ref.get("optional") is True:
            continue  # Kubernetes skips a missing optional ConfigMap
        if data is None:  # a secretRef, or a ConfigMap that is missing or was not fetched
            complete = False
            # It may override any key read so far, so none of those values can be trusted.
            env = dict.fromkeys(env, UNKNOWN)
            continue
        for key, value in data.items():
            name = source.get("prefix", "") + key
            env[name] = redact(name, value)
    for entry in container.get("env") or []:
        name = entry["name"]
        if "valueFrom" not in entry:
            value = entry.get("value") or ""
            # Kubernetes expands $(OTHER_VAR) in a literal, so the text is not the value.
            env[name] = UNKNOWN if "$(" in value else redact(name, value)
            continue
        ref = entry["valueFrom"].get("configMapKeyRef")
        if not ref:  # secretKeyRef, fieldRef, resourceFieldRef: never resolved
            env[name] = UNKNOWN
            continue
        data = configmaps.get(ref.get("name")) or {}
        if ref.get("key") in data:
            env[name] = redact(name, data[ref["key"]])
        elif fetched and ref.get("optional") is True:
            # Kubernetes skips the entry, so the variable keeps whatever envFrom gave it.
            continue
        else:  # ConfigMap or key missing, or ConfigMaps not fetched
            env[name] = UNKNOWN
            complete = False
    return env, complete
