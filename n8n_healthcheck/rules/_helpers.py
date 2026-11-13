"""Small readers shared by the rule modules. No rule logic lives here."""

from __future__ import annotations

from typing import Iterator, Optional

DOCS = "https://docs.n8n.io/deploy/host-n8n"


def env_flag(env: dict, name: str) -> Optional[bool]:
    """Boolean n8n variable: True/False when set to a recognised value, else None."""
    value = str(env.get(name, "")).strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    return None


def env_int(env: dict, name: str) -> Optional[int]:
    try:
        return int(str(env[name]).strip())
    except (KeyError, ValueError):
        return None


def is_queue_mode(env: dict) -> bool:
    # EXECUTIONS_MODE defaults to `regular`.
    return str(env.get("EXECUTIONS_MODE", "regular")).strip().lower() == "queue"


def shown(env: dict, name: str) -> str:
    """NAME=value as observed, for evidence strings."""
    return f"{name}={env[name]}" if name in env else f"{name} is not set"


def image_tag(image: str) -> Optional[str]:
    """Tag of an image reference; None when untagged. Digest-pinned images return 'sha256:...'."""
    if "@" in image:
        return image.split("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def n8n_role(container: dict) -> Optional[str]:
    """'main', 'worker' or 'webhook' for an n8n container, None for anything else.

    All three roles run the same image and differ only by the start command
    (`n8n`, `n8n worker`, `n8n webhook`). The image name (last path segment, without
    tag) must contain "n8n", which excludes the n8nio/runners sidecar, Redis, proxies.
    """
    name = container.get("image", "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    if "n8n" not in name.lower():
        return None
    command = container.get("command") or []
    for role in ("worker", "webhook"):
        if role in command:
            return role
    return "main"


def n8n_containers(kubernetes: dict) -> Iterator[tuple[dict, dict, str]]:
    """Yield (deployment, container, role) for every n8n container."""
    for deployment in kubernetes.get("deployments", []):
        for container in deployment.get("containers", []):
            role = n8n_role(container)
            if role:
                yield deployment, container, role
