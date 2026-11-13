"""Small readers shared by the rule modules. No rule logic lives here."""

from __future__ import annotations

import re
from typing import Iterator, Optional

# Leading x.y.z of an image tag: "2.40.1", "v2.40.1" and "2.40.1-build7" are one release.
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")


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


def num(section: dict, key: str):
    """Numeric field of a Snapshot section; None when absent or not a number."""
    try:
        value = float(section.get(key))
    except (TypeError, ValueError):
        return None
    return int(value) if value.is_integer() else value


def is_queue_mode(env: dict) -> bool:
    # EXECUTIONS_MODE defaults to `regular`.
    return str(env.get("EXECUTIONS_MODE", "regular")).strip().lower() == "queue"


def looks_like_production(env: dict) -> bool:
    """Queue mode or Postgres: someone set this up on purpose.

    A stock install (regular mode on SQLite) is a trial or a small setup; rules that
    would otherwise flag a documented default use this to stay quiet or drop to `info`.
    """
    return is_queue_mode(env) or str(env.get("DB_TYPE", "")).strip().lower() == "postgresdb"


def shown(env: dict, name: str) -> str:
    """NAME=value as observed, for evidence strings."""
    return f"{name}={env[name]}" if name in env else f"{name} is not set"


def image_tag(image: str) -> Optional[str]:
    """Tag of an image reference; None when untagged. Digest-pinned images return 'sha256:...'."""
    image = image or ""
    if "@" in image:
        return image.split("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return last.split(":", 1)[1] if ":" in last else None


def image_version(image: str) -> Optional[str]:
    """Leading x.y.z of the image tag; None for digests, mutable tags and no tag."""
    match = _VERSION.match(image_tag(image) or "")
    return match.group(1) if match else None


def command_words(container: dict) -> list:
    """The start command as words, so shell form (`sh -c "n8n worker"`) reads like exec form."""
    return " ".join(str(part) for part in container.get("command") or []).split()


def concurrency_flag(container: dict) -> Optional[int]:
    """Value of the worker `--concurrency` flag (`--concurrency=5` or `--concurrency 5`)."""
    words = command_words(container)
    for index, word in enumerate(words):
        value = None
        if word.startswith("--concurrency="):
            value = word.split("=", 1)[1]
        elif word == "--concurrency" and index + 1 < len(words):
            value = words[index + 1]
        if value is not None and value.isdigit():
            return int(value)
    return None


def n8n_role(container: dict) -> Optional[str]:
    """'main', 'worker' or 'webhook' for an n8n container, None for anything else.

    All three roles run the same image and differ only by the start command
    (`n8n`, `n8n worker`, `n8n webhook`). The image name (last path segment, without
    tag) must contain "n8n", which excludes the n8nio/runners sidecar, Redis, proxies.
    """
    name = (container.get("image") or "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    if "n8n" not in name.lower():
        return None
    words = command_words(container)
    for role in ("worker", "webhook"):
        if role in words:
            return role
    return "main"


def n8n_containers(kubernetes: dict) -> Iterator[tuple[dict, dict, str]]:
    """Yield (deployment, container, role) for every n8n container."""
    for deployment in kubernetes.get("deployments") or []:
        for container in deployment.get("containers") or []:
            role = n8n_role(container)
            if role:
                yield deployment, container, role
