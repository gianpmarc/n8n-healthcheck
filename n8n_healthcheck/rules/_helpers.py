"""Small readers shared by the rule modules. No rule logic lives here."""

from __future__ import annotations

import re
from typing import Iterator, Optional

from ..models import REDACTED, Snapshot

# Leading x.y.z of an image tag: "2.40.1", "v2.40.1" and "2.40.1-build7" are one release.
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")


# Value of a Kubernetes container variable that is set but could not be read (a Secret).
VALUE_FROM = "<valueFrom>"


class Env(dict):
    """The n8n environment variables, plus how far they can be trusted.

    `complete` is False when part of the environment could not be read, so a variable
    missing here may still be set. `source` names the Kubernetes container the values
    came from; it is empty for an env file.
    """

    complete = True
    source = ""

    @property
    def note(self) -> str:
        """Sentence appended to evidence so the reader can find the values."""
        if not self.source:
            return ""
        note = f" Environment read from {self.source}."
        if not self.complete:
            note += " Part of it could not be read (a Secret or an unavailable ConfigMap)."
        return note


def container_env(deployment: dict, container: dict) -> Env:
    env = Env(container.get("env") or {})
    # Anything but an explicit True (e.g. a snapshot saved before the field existed) is incomplete.
    env.complete = container.get("env_complete") is True
    env.source = f"Deployment {deployment.get('name')}, container {container.get('name')}"
    return env


def worker_envs(kubernetes: dict) -> list:
    """The environment of every n8n worker container."""
    return [
        container_env(deployment, container)
        for deployment, container, role in n8n_containers(kubernetes)
        if role == "worker"
    ]


def effective_env(snapshot: Snapshot) -> Optional[Env]:
    """The environment of the n8n main process, or None when it is not known.

    The env file wins. Otherwise it is the environment of the main n8n container in
    Kubernetes; with no main container, or several that disagree, there is no answer.
    """
    if snapshot.env is not None:
        return Env(snapshot.env)
    if snapshot.kubernetes is None:
        return None
    mains = [
        container_env(deployment, container)
        for deployment, container, role in n8n_containers(snapshot.kubernetes)
        if role == "main"
    ]
    if not mains or any((env, env.complete) != (mains[0], mains[0].complete) for env in mains):
        return None
    return mains[0]


def unknown(env: dict, name: str) -> bool:
    """True when the value of `name` cannot be told: neither a readable value nor provably unset.

    Rules that would conclude something from the variable's value or its absence must
    return None instead.
    """
    if name in env:
        return env[name] in (VALUE_FROM, REDACTED)
    return not getattr(env, "complete", True)


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
    if unknown(env, name):
        return f"{name} is set, value not readable" if name in env else f"{name} not readable"
    return f"{name}={env[name]}" if name in env else f"{name} is not set"


def shown_each(envs: list, name: str) -> str:
    """shown() for several Kubernetes containers, each prefixed with where it runs."""
    return "; ".join(f"{env.source}: {shown(env, name)}" for env in envs)


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
