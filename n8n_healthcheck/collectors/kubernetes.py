"""Kubernetes: the Deployments in the n8n namespace, via `kubectl`."""

import json

from ..models import Target
from ._util import TIMEOUT, run


def collect(target: Target) -> dict:
    args = ["kubectl"]
    if target.kube_context:
        args += ["--context", target.kube_context]
    args += ["get", "deployments", "-n", target.kube_namespace, "-o", "json"]
    return {"kubernetes": parse_deployments(run(args + [f"--request-timeout={TIMEOUT}s"]))}


def parse_deployments(output: str) -> dict:
    return {"deployments": [_deployment(item) for item in json.loads(output)["items"]]}


def _deployment(item: dict) -> dict:
    return {
        "name": item["metadata"]["name"],
        "replicas": item["spec"].get("replicas", 1),  # Kubernetes defaults to 1
        "ready_replicas": item.get("status", {}).get("readyReplicas", 0),  # absent when 0
        "containers": [_container(c) for c in item["spec"]["template"]["spec"]["containers"]],
    }


def _container(container: dict) -> dict:
    resources = container.get("resources", {})
    return {
        "name": container["name"],
        "image": container.get("image", ""),
        # command + args, so `args: ["worker"]` and `command: ["n8n", "worker"]` look alike
        "command": container.get("command", []) + container.get("args", []),
        # values set through a Secret/ConfigMap reference are not resolved
        "env": {e["name"]: e.get("value", "<valueFrom>") for e in container.get("env", [])},
        "requests": resources.get("requests", {}),
        "limits": resources.get("limits", {}),
        "liveness_probe": "livenessProbe" in container,
        "readiness_probe": "readinessProbe" in container,
    }
