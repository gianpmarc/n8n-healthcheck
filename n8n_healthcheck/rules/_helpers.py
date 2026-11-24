"""Small readers shared by the rule modules, and how a rule states its outcome. No rule logic lives here."""

from __future__ import annotations

import math
import re
from typing import Iterator, Optional

from .. import debug_info
from ..models import REDACTED, Outcome, RuleInfo, Snapshot

# Leading x.y.z of an image tag: "2.40.1", "v2.40.1" and "2.40.1-build7" are one release.
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")


# Value of a Kubernetes container variable that is set but could not be read (a Secret).
VALUE_FROM = "<valueFrom>"


# --- outcomes ----------------------------------------------------------------------
# A rule returns a Finding, or one of these. The line that matters is between PASSED and
# the rest: PASSED only when the rule read the values it needs and they are good. A
# silence kept on purpose is NOT_APPLICABLE, anything the rule could not tell is not assessed.

PASSED = Outcome("passed", basis="read")
# The variable is not set in an environment read in full, and the n8n default is the good value.
PASSED_BY_DEFAULT = Outcome("passed", basis="default")
NOT_APPLICABLE = Outcome("not_applicable")
# A source the rule reads was given and read, and does not say what the rule needs.
NOT_STATED = Outcome("not_assessed", reason="not_stated")
# A source holds the value, but it could not be read: a Secret, a file, a redacted value,
# or an environment of which a part is missing.
UNREADABLE = Outcome("not_assessed", reason="value_unreadable")


def source_missing(*needs: str) -> Outcome:
    """None of the sources where the rule looks was given; any one of `needs` would let it decide."""
    return Outcome("not_assessed", reason="source_missing", needs=tuple(needs))


def rule(rule_id: str, area: str, *, subject: str, passed: str, variants=(), facts=(), settings=(), scopes=("instance", "main")):
    """Declare a rule: its id, its area and its fixed texts (models.RuleInfo), as `function.info`."""

    def declare(function):
        function.info = RuleInfo(rule_id, area, subject, passed, tuple(variants), tuple(facts), tuple(settings), tuple(scopes))
        return function

    return declare


class Env(dict):
    """The n8n environment variables, plus how far they can be trusted.

    `complete` is False when part of the environment could not be read, so a variable
    missing here may still be set. `source` names the Kubernetes container the values
    came from; it is empty for an env file. `from_debug_info` lists the variables whose
    value was taken from the debug information instead (see with_debug_info()), and
    `debug_info_label` names that text by its date. `from_env_file` lists the other
    variables a finding reads when it rests on an env file and on the text, so that its
    evidence names both.
    """

    complete = True
    source = ""
    from_env_file: tuple = ()
    from_debug_info: tuple = ()
    debug_info_label = ""

    @property
    def note(self) -> str:
        """Sentence appended to evidence so the reader can find the values."""
        note = ""
        if self.source:
            note = f" Environment read from {self.source}."
            if not self.complete:
                note += " Part of it could not be read (a Secret or an unavailable ConfigMap)."
        if self.from_env_file:
            note += f" From the env file: {', '.join(self.from_env_file)}."
        if self.from_debug_info:
            note += f" From the {self.debug_info_label}: {', '.join(self.from_debug_info)}."
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


def debug_info_env(section: dict) -> dict:
    """What a debug information states, as the n8n variables the rules read.

    The text reports effective values, so each is written as n8n would read the variable.
    A value the text does not state has no entry. The two security variables are there
    only as `false`: n8n reports them only when they are insecure.
    """
    pruning, insecure = section.get("pruning") or {}, section.get("insecure") or {}
    limit = section.get("concurrency_limit")
    values = {
        "EXECUTIONS_MODE": section.get("execution_mode"),
        "N8N_MULTI_MAIN_SETUP_ENABLED": section.get("multi_main"),
        "DB_TYPE": section.get("database"),
        "N8N_CONCURRENCY_PRODUCTION_LIMIT": -1 if limit == "unlimited" else limit,
        "EXECUTIONS_DATA_PRUNE": pruning.get("enabled"),
        "EXECUTIONS_DATA_SAVE_ON_PROGRESS": section.get("save_progress"),
        "N8N_SECURE_COOKIE": False if insecure.get("secure_cookie") else None,
        "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": False if insecure.get("file_access_blocked") else None,
    }
    return {name: str(value).lower() for name, value in values.items() if value is not None}


def stated(snapshot: Snapshot, name: str) -> bool:
    """True when a probe of this run states `name` for the main process.

    Stated means set: readable or not (a Secret), or through `<NAME>_FILE`, in the env
    file or, without one, in any main container of Kubernetes. The debug information is
    used for a value only when this is False: a probe wins, value by value. A variable
    that is simply not set is not stated, because the default the tool would assume is
    weaker evidence than the effective value n8n reports.
    """
    if snapshot.env is not None:
        envs = [snapshot.env]
    else:
        envs = [c.get("env") or {} for _, c, role in n8n_containers(snapshot.kubernetes or {}) if role == "main"]
    return any(name in env or name + "_FILE" in env for env in envs)


def with_debug_info(snapshot: Snapshot, *names: str, rests_on_text: bool = False) -> Optional[Env]:
    """effective_env(), with each of `names` no probe states taken from the debug information.

    A rule asks only for the variables it reads, so its evidence names exactly the values
    that came from the text. With no probe at all the result holds those values alone and
    is not `complete`: a variable the text does not state is unknown, never a default.
    None when neither a probe nor the text says anything.

    A finding that rests on two sources names both. Kubernetes is always named by
    `Env.note`; the env file is named, with the variables read from it, as soon as one
    value came from the text, or always when the caller says the finding `rests_on_text`
    for something that is not a variable (an instance count, a flag).
    """
    env = effective_env(snapshot)
    section = debug_info.of(snapshot)
    if section is None:
        return env
    values = debug_info_env(section)
    taken = tuple(name for name in names if name in values and not stated(snapshot, name))
    others = tuple(name for name in names if name not in taken)
    from_file = others if env is not None and not env.source and (taken or rests_on_text) else ()
    if not taken and not from_file:
        return env
    merged = Env(env or {})
    merged.complete = env.complete if env is not None else False
    merged.source = env.source if env is not None else ""
    merged.update({name: values[name] for name in taken})
    merged.from_env_file = from_file
    merged.from_debug_info = taken
    merged.debug_info_label = debug_info.phrase(section)
    return merged


def image_versions(kubernetes: Optional[dict]) -> dict:
    """{version: ["Deployment (role)", ...]} for the n8n containers whose image tag names one.

    Compared on the leading x.y.z, so 2.40.1 and 2.40.1-build7 are one release. Digests,
    mutable tags and custom tags name no version and are skipped: a digest may well be
    the same release as a tag next to it. Empty when Kubernetes names no version: only
    then are the versions the debug information lists used, by rule MAINT-003 and by the
    fact cluster.versions_aligned alike.
    """
    by_tag: dict = {}
    for deployment, container, role in n8n_containers(kubernetes or {}):
        version = image_version(container.get("image"))
        if version is not None:
            by_tag.setdefault(version, []).append(f"{deployment['name']} ({role})")
    return by_tag


def unknown(env: dict, name: str) -> bool:
    """True when the value of `name` cannot be told: neither a readable value nor provably unset.

    Rules that would conclude something from the variable's value or its absence must
    return None instead.
    """
    if name in env:
        return env[name] in (VALUE_FROM, REDACTED)
    return not getattr(env, "complete", True)


def undecided(env: dict, name: str) -> Optional[Outcome]:
    """Why `name` cannot be told, or None when it is readable or provably not set.

    Stricter than unknown(): a variable given through `<NAME>_FILE` cannot be told either.
    For the path of a rule that has no finding; what raises a finding stays with unknown().
    """
    if name in env:
        return UNREADABLE if env[name] in (VALUE_FROM, REDACTED) else None
    if name + "_FILE" in env:
        return UNREADABLE
    if getattr(env, "complete", True):
        return None
    # Kubernetes with a part of the environment missing; else the debug information alone.
    return UNREADABLE if getattr(env, "source", "") else NOT_STATED


def no_environment(snapshot: Snapshot, text: bool = False, needs: tuple = ()) -> Outcome:
    """The outcome of a rule that has no environment to read.

    `text` says the rule also reads the debug information for it. Not stated when one of
    those sources was given (Kubernetes without a main container, or with mains that
    disagree; a text that does not say); else `needs`, by default all of them.
    """
    given = snapshot.env is not None or snapshot.kubernetes is not None
    if given or (text and debug_info.of(snapshot) is not None):
        return NOT_STATED
    return source_missing(*(needs or ("env", "kubernetes") + (("debug_info",) if text else ())))


def queue_only(env: dict) -> Optional[Outcome]:
    """For a rule about queue mode: why it says nothing, or None when the mode is queue."""
    return undecided(env, "EXECUTIONS_MODE") or (None if is_queue_mode(env) else NOT_APPLICABLE)


def flag_is(env: dict, name: str, good: bool, default: bool) -> Outcome:
    """The outcome of a boolean variable that must be `good`, once a finding is ruled out."""
    why = undecided(env, name)
    if why is not None:
        return why
    if name not in env:
        return PASSED_BY_DEFAULT if default is good else NOT_STATED
    return PASSED if env_flag(env, name) is good else NOT_STATED


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


def size(count) -> str:
    """A byte count as a reader would say it: '950 bytes', '776 KB', '64 MB', '5 GB'."""
    if count is None:
        return "unknown size"
    value, unit = float(count), "bytes"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            break
        value /= 1024
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"


def num(section: dict, key: str):
    """Numeric field of a Snapshot section; None when absent or not a finite number."""
    try:
        value = float(section.get(key))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
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
