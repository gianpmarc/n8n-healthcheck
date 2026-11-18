"""Environment profile: the facts about one environment, each with its provenance.

Built from a Snapshot that was already collected; this module reads, it never collects.
The format is described for outside readers in docs/profile-format.md.

A wrong fact is worse than a missing one: anything that cannot be told for certain is
`unknown`, and nothing that can hold a host, URL, path, address or name is written.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Optional

from . import __version__, n8n_settings, report
from .models import REDACTED, Finding, Snapshot
from .rules._helpers import (
    VALUE_FROM,
    Env,
    command_words,
    concurrency_flag,
    container_env,
    env_flag,
    env_int,
    image_tag,
    image_version,
    is_queue_mode,
    n8n_containers,
    num,
    unknown,
)

FORMAT = "n8n-healthcheck-profile"
FORMAT_VERSION = 1

REASONS = ("baseline", "pre_upgrade", "post_upgrade", "incident", "other")
REFERENCE_MAX = 200
SOURCE_STATES = ("collected", "disabled", "failed", "not_provided")
# `declared` is reserved for a later version of the tool and never produced here.
PROVENANCES = ("measured", "declared", "unknown", "withheld")
SETTING_STATES = ("value", "set", "withheld")
ROLES = ("main", "worker", "webhook")

# Snapshot section -> how a fact's `detail` names it.
SOURCE_LABELS = {
    "health": "The health endpoints",
    "metrics": "The metrics endpoint",
    "env": "The n8n environment",
    "postgres": "Postgres",
    "redis": "Redis",
    "kubernetes": "Kubernetes",
}

# Without a registry, with Docker Hub's, or n8n's own. The image name is never written.
OFFICIAL_IMAGES = ("n8nio/n8n", "docker.io/n8nio/n8n", "docker.n8n.io/n8nio/n8n")

REDIS_POLICIES = (
    "noeviction", "allkeys-lru", "allkeys-lfu", "allkeys-lrm", "allkeys-random",
    "volatile-lru", "volatile-lfu", "volatile-lrm", "volatile-random", "volatile-ttl",
)  # fmt: skip

# The worker's --concurrency flag defaults to 10 (see rules/scalability.py).
DEFAULT_WORKER_CONCURRENCY = 10
CONCURRENCY_LIMIT = "N8N_CONCURRENCY_PRODUCTION_LIMIT"

# Settings: the shape of a value that is written for a variable known to hold a number.
_NUMBER = re.compile(r"-?[0-9]{1,15}(\.[0-9]{1,6})?")

# Not followed by a further digit or dot: three parts of a longer dotted number are not a version.
_VERSION = re.compile(r"[0-9]+\.[0-9]+(\.[0-9]+)?(?![0-9.])")
# Stricter than Kubernetes itself: one decimal point at most, so nothing dotted passes.
_QUANTITY = re.compile(r"[0-9]+(\.[0-9]+)?[a-zA-Z]{0,2}")
_UNSET = object()

NOT_SET_DETAIL = "not set, the n8n default for this version applies"


class ProfileError(ValueError):
    """A profile that cannot be built as asked, or a file that is not a usable profile."""


# --- facts -------------------------------------------------------------------------


def _measured(value, source: str, detail: str = "") -> dict:
    return {"value": value, "provenance": "measured", "source": source, "detail": detail}


def _unknown(detail: str) -> dict:
    return {"value": None, "provenance": "unknown", "source": None, "detail": detail}


def _missing(section: str, failed: tuple) -> dict:
    """Unknown because a whole Snapshot section is absent."""
    if section in failed:
        return _unknown(f"{SOURCE_LABELS[section]} could not be collected")
    return _unknown(f"{SOURCE_LABELS[section]} was not provided")


def _image_name(container: dict) -> str:
    """The image without its tag or digest."""
    image = (container.get("image") or "").split("@")[0]
    head, _, last = image.rpartition("/")
    return (head + "/" if head else "") + last.split(":")[0]


def _called_n8n(container: dict) -> bool:
    """True when the image is called exactly n8n, whatever the registry: n8n itself."""
    return _image_name(container).rpartition("/")[2] == "n8n"


def _role_containers(snapshot: Snapshot, role: str) -> list:
    """The (deployment, container) pairs of one role.

    A role is told by an image name that contains "n8n" (rules/_helpers.py), and anything
    without a worker or webhook command is "main": an exporter or another server whose
    image name merely contains "n8n" lands there. So when the main role has containers
    called exactly n8n, only those are n8n; the others are left out, never counted.
    A role where no container is called exactly n8n is returned whole: a build under
    another name cannot be told from an unrelated image, and the callers treat it so.
    """
    found = [(d, c) for d, c, r in n8n_containers(snapshot.kubernetes or {}) if r == role]
    exact = [(d, c) for d, c in found if _called_n8n(c)]
    return exact if exact and role == "main" else found


# Measured from Kubernetes without looking at the main containers.
NOT_FROM_MAIN = ("orchestrator", "worker.replicas", "webhook.replicas", "worker.concurrency", "resources.worker", "probes.worker")


def _fact(fact_id: str, build: Callable, snapshot: Snapshot, failed: tuple) -> dict:
    """One fact of the catalogue, saying so when containers were left out of the main role.

    A main process built under another name cannot be told from an exporter, so the reader
    must see that something was left out. Only how many: never an image or Deployment name.
    """
    fact = {"id": fact_id, **build(snapshot, failed)}
    if fact["source"] == "kubernetes" and fact_id not in NOT_FROM_MAIN:
        every = sum(role == "main" for _, _, role in n8n_containers(snapshot.kubernetes or {}))
        left_out = every - len(_role_containers(snapshot, "main"))
        if left_out:
            noun = "container with n8n in its image name was" if left_out == 1 else "containers with n8n in their image name were"
            note = f"{left_out} other {noun} left out"
            fact["detail"] = f"{fact['detail']}; {note}" if fact["detail"] else note
    return fact


def _official(container: dict) -> bool:
    return _image_name(container) in OFFICIAL_IMAGES


def _read_env(snapshot: Snapshot, role: str, name: str, failed: tuple = ()) -> tuple:
    """One variable as (raw, source, detail).

    The env file when one was given, as the rules do; otherwise the Kubernetes containers
    of `role`, compared on this one variable. `raw` is the value, _UNSET when the
    variable is provably not set, or None when it cannot be told (`detail` says why).
    """
    if snapshot.env is not None:
        envs, source = [Env(snapshot.env)], "env"
    elif snapshot.kubernetes is not None:
        envs = [container_env(d, c) for d, c in _role_containers(snapshot, role)]
        source = "kubernetes"
        if not envs:
            return None, None, f"no {role} container was found in Kubernetes"
    else:
        return None, None, _missing("env", failed)["detail"]
    seen = set()
    for env in envs:
        if name + "_FILE" in env:
            return None, None, "set through a file, which this tool does not read"
        if unknown(env, name):
            if name in env:
                return None, None, "set, but the value could not be read"
            return None, None, "part of the environment could not be read"
        seen.add(str(env[name]) if name in env else _UNSET)
    if len(seen) > 1:
        return None, None, f"the {role} containers disagree on this setting"
    return seen.pop(), source, ""


def _enum(*allowed: str) -> Callable:
    def parse(raw: str) -> Optional[str]:
        value = raw.strip().lower()
        return value if value in allowed else None

    return parse


def _bool(raw: str) -> Optional[bool]:
    return env_flag({"": raw}, "")


def _plain_int(raw) -> Optional[int]:
    """env_int() for text that is only ASCII digits with an optional minus.

    int() also reads "1_000" and digits of other scripts, which n8n does not read the same.
    """
    if not isinstance(raw, str) or not re.fullmatch(r"-?[0-9]{1,15}", raw.strip()):
        return None
    return env_int({"": raw}, "")


def _count(raw) -> Optional[int]:
    """A whole number that is not negative."""
    value = _plain_int(raw)
    return value if value is not None and value >= 0 else None


def _version(raw) -> Optional[str]:
    match = _VERSION.match(raw) if isinstance(raw, str) else None
    return match.group(0) if match else None


def _env_fact(name: str, parse: Callable, default=None) -> Callable:
    """A fact that is one variable of the main process.

    `default` is given only for the defaults that are the same in every n8n version; for
    any other variable, not set means unknown.
    """

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        raw, source, detail = _read_env(snapshot, "main", name, failed)
        if raw is None:
            return _unknown(detail)
        if raw is _UNSET:
            if default is None:
                return _unknown(NOT_SET_DETAIL)
            # An image that is not the official one may set the variable itself.
            if not all(_official(c) for _, c in _role_containers(snapshot, "main")):
                return _unknown("not set here, but the image is not the official one and may set it")
            return _measured(default, source, "n8n default, variable not set")
        value = parse(raw)
        if value is None:
            return _unknown("the value is not one this tool recognises")
        return _measured(value, source)

    return fact


def _section_fact(section: str, key: str, parse: Callable) -> Callable:
    """A fact that is one field of the Postgres or Redis section."""

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        data = getattr(snapshot, section)
        if data is None:
            return _missing(section, failed)
        value = parse(data.get(key))
        if value is None:
            return _unknown(f"{SOURCE_LABELS[section]} did not report it")
        return _measured(value, section)

    return fact


def _number(raw):
    value = num({"": raw}, "")
    if value is None or value < 0:
        return None
    return value if isinstance(value, int) else round(value, 2)


def _whole(raw) -> Optional[int]:
    value = _number(raw)
    return value if isinstance(value, int) else None


def _policy(raw) -> Optional[str]:
    return raw if raw in REDIS_POLICIES else None


def _oldest_execution_age(snapshot: Snapshot, failed: tuple) -> dict:
    found = _section_fact("postgres", "oldest_execution_age_days", _number)(snapshot, failed)
    # The collector stores 0.0 for an empty table: that is no age, not a fresh execution.
    if found["value"] == 0 and _whole((snapshot.postgres or {}).get("execution_count")) in (0, None):
        return _unknown("there are no stored executions")
    return found


def _n8n_version(snapshot: Snapshot, failed: tuple) -> dict:
    version = _version(snapshot.n8n_version)
    if version:
        return _measured(version, "metrics")
    if snapshot.kubernetes is None:
        return _unknown("n8n did not report its version and Kubernetes was not provided")
    mains = [c for _, c in _role_containers(snapshot, "main")]
    if not all(_official(c) for c in mains):
        # The tag of an image built by someone else is their own numbering.
        return _unknown("n8n did not report its version and the image is not the official one")
    versions = set()
    for container in mains:
        image = container.get("image")
        version = image_version(image)
        # The whole tag must start with a version, not with part of a longer dotted number.
        versions.add(version if version == _version((image_tag(image) or "").removeprefix("v")) else None)
    if len(versions) != 1 or None in versions:
        return _unknown("the image tags of the main containers do not give one version")
    return _measured(versions.pop(), "kubernetes", "from the image tag")


def _image_official(snapshot: Snapshot, failed: tuple) -> dict:
    if snapshot.kubernetes is None:
        return _missing("kubernetes", failed)
    official = {_official(c) for role in ROLES for _, c in _role_containers(snapshot, role)}
    if not official:
        return _unknown("no n8n container was found in Kubernetes")
    if len(official) > 1:
        return _unknown("the n8n containers do not all run the same kind of image")
    return _measured(official.pop(), "kubernetes")


def _orchestrator(snapshot: Snapshot, failed: tuple) -> dict:
    if snapshot.kubernetes is None:
        return _missing("kubernetes", failed)
    return _measured("kubernetes", "kubernetes")


def _kube_fact(role: str, read: Callable) -> Callable:
    """A fact read from the Kubernetes containers of one role.

    `read(deployments_and_containers)` returns the value, or None when it has no safe shape.
    """

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        if snapshot.kubernetes is None:
            return _missing("kubernetes", failed)
        found = _role_containers(snapshot, role)
        if not found:
            # Not 0: only Deployments are read, so a StatefulSet would be missed.
            return _unknown(f"no {role} container was found in the Deployments")
        if not all(_called_n8n(c) for _, c in found):
            return _unknown("a container of this role runs an image that may not be n8n itself")
        return read(found)

    return fact


def _replicas(found: list) -> dict:
    # Keyed by identity: a Deployment with two containers of the role counts once.
    deployments = {id(deployment): deployment.get("replicas") for deployment, _ in found}
    counts = [_whole(count) for count in deployments.values()]
    if None in counts:
        return _unknown("a Deployment does not state its replicas")
    return _measured(sum(counts), "kubernetes")


def _quantities(raw) -> Optional[dict]:
    """{"cpu", "memory"} of a requests or limits block; None when a value has no safe shape."""
    result = {}
    for key in ("cpu", "memory"):
        value = (raw or {}).get(key)
        if value is not None:
            value = str(value)
            if not _QUANTITY.fullmatch(value):
                return None
        result[key] = value
    return result


def _resources(found: list) -> dict:
    values = []
    for _deployment, container in found:
        value = {"requests": _quantities(container.get("requests")), "limits": _quantities(container.get("limits"))}
        if None in value.values():
            return _unknown("a resource quantity is not one this tool recognises")
        values.append(value)
    if any(value != values[0] for value in values):
        return _unknown("the containers of this role do not have the same resources")
    return _measured(values[0], "kubernetes")


def _probes(found: list) -> dict:
    values = []
    for _deployment, container in found:
        value = {"liveness": container.get("liveness_probe"), "readiness": container.get("readiness_probe")}
        if not all(isinstance(item, bool) for item in value.values()):
            return _unknown("Kubernetes did not report the probes")
        values.append(value)
    if any(value != values[0] for value in values):
        return _unknown("the containers of this role do not have the same probes")
    return _measured(values[0], "kubernetes")


def _worker_concurrency(snapshot: Snapshot, failed: tuple) -> dict:
    """Same precedence as rule SCAL-004 (rules/scalability.py, low_worker_concurrency)."""
    if snapshot.kubernetes is None:
        if snapshot.env is not None:
            return _unknown("needs the worker containers: an env file does not show the --concurrency flag")
        return _missing("kubernetes", failed)
    workers = _role_containers(snapshot, "worker")
    if not workers:
        return _unknown("no worker container was found in the Deployments")
    if not all(_called_n8n(c) for _, c in workers):
        return _unknown("a container of this role runs an image that may not be n8n itself")
    file_detail = "the limit variable is set through a file, which this tool does not read"
    # In queue mode the limit, when set to anything but -1, overrides the --concurrency
    # flag; an env file describes every process, so it is read before the containers.
    env = snapshot.env
    if env is not None and CONCURRENCY_LIMIT + "_FILE" in env:
        return _unknown(file_detail)
    if env is not None and is_queue_mode(env):
        if unknown(env, CONCURRENCY_LIMIT):
            return _unknown("set, but the value could not be read")
        limit = _plain_int(env.get(CONCURRENCY_LIMIT))
        if limit is not None and limit >= 1:
            return _measured(limit, "env", f"from {CONCURRENCY_LIMIT}")
    found = set()
    for deployment, container in workers:
        own_env = container_env(deployment, container)
        if CONCURRENCY_LIMIT + "_FILE" in own_env:
            return _unknown(file_detail)
        if unknown(own_env, CONCURRENCY_LIMIT):
            return _unknown("the limit variable on a worker could not be read")
        own_limit = _plain_int(own_env.get(CONCURRENCY_LIMIT))
        flag = concurrency_flag(container)
        if CONCURRENCY_LIMIT in own_env and (own_limit is None or own_limit == 0 or own_limit < -1):
            return _unknown("the limit variable on a worker is not a valid number")
        if own_limit is not None and own_limit >= 1:
            found.add((own_limit, f"from {CONCURRENCY_LIMIT}"))
        elif flag is not None:
            found.add((flag, "from the --concurrency flag"))
        elif any(word.startswith("--concurrency") for word in command_words(container)):
            # e.g. --concurrency=$WORKERS: the flag is there, its number is not.
            return _unknown("the --concurrency flag on a worker is not a plain number")
        elif not _official(container):
            # As for the other defaults: an image built by someone else may set either.
            return _unknown("neither is set here, but the image is not the official one and may set it")
        else:
            found.add((DEFAULT_WORKER_CONCURRENCY, "worker default, neither the limit variable nor the flag is set"))
    if len({value for value, _ in found}) > 1:
        return _unknown("the worker containers do not have the same concurrency")
    value, detail = sorted(found)[0]
    return _measured(value, "kubernetes", detail)


def _metrics_enabled(snapshot: Snapshot, failed: tuple) -> dict:
    if snapshot.metrics is not None:
        return _measured(True, "metrics")
    if (snapshot.health or {}).get("metrics_disabled") is True:
        return _measured(False, "health", "the metrics endpoint answered 404")
    return _missing("metrics", failed)


# (fact id, function(snapshot, failed) -> fact without its id), in the order of the file.
CATALOGUE = (
    ("n8n.version", _n8n_version),
    ("n8n.image_official", _image_official),
    ("orchestrator", _orchestrator),
    ("executions.mode", _env_fact("EXECUTIONS_MODE", _enum(*n8n_settings.ENUMS["EXECUTIONS_MODE"]), default="regular")),
    ("main.replicas", _kube_fact("main", _replicas)),
    ("worker.replicas", _kube_fact("worker", _replicas)),
    ("webhook.replicas", _kube_fact("webhook", _replicas)),
    ("worker.concurrency", _worker_concurrency),
    ("multi_main.enabled", _env_fact("N8N_MULTI_MAIN_SETUP_ENABLED", _bool, default=False)),
    ("task_runners.enabled", _env_fact("N8N_RUNNERS_ENABLED", _bool)),
    ("task_runners.mode", _env_fact("N8N_RUNNERS_MODE", _enum(*n8n_settings.ENUMS["N8N_RUNNERS_MODE"]))),
    (
        "binary_data.mode",
        _env_fact("N8N_DEFAULT_BINARY_DATA_MODE", _enum(*n8n_settings.ENUMS["N8N_DEFAULT_BINARY_DATA_MODE"])),
    ),
    ("metrics.enabled", _metrics_enabled),
    ("pruning.enabled", _env_fact("EXECUTIONS_DATA_PRUNE", _bool)),
    ("pruning.max_age_hours", _env_fact("EXECUTIONS_DATA_MAX_AGE", _count)),
    ("database.engine", _env_fact("DB_TYPE", _enum(*n8n_settings.ENUMS["DB_TYPE"]), default="sqlite")),
    ("database.version", _section_fact("postgres", "version", _version)),
    ("database.pool_size", _env_fact("DB_POSTGRESDB_POOL_SIZE", _count)),
    ("database.max_connections", _section_fact("postgres", "max_connections", _whole)),
    ("database.execution_count", _section_fact("postgres", "execution_count", _whole)),
    ("database.execution_table_bytes", _section_fact("postgres", "execution_table_bytes", _whole)),
    ("database.oldest_execution_age_days", _oldest_execution_age),
    ("queue.redis_version", _section_fact("redis", "version", _version)),
    ("queue.maxmemory_policy", _section_fact("redis", "maxmemory_policy", _policy)),
    ("queue.maxmemory_bytes", _section_fact("redis", "maxmemory_bytes", _whole)),
    ("queue.waiting_jobs", _section_fact("redis", "waiting_jobs", _whole)),
    ("queue.active_jobs", _section_fact("redis", "active_jobs", _whole)),
    ("queue.failed_jobs", _section_fact("redis", "failed_jobs", _whole)),
    ("resources.main", _kube_fact("main", _resources)),
    ("resources.worker", _kube_fact("worker", _resources)),
    ("probes.main", _kube_fact("main", _probes)),
    ("probes.worker", _kube_fact("worker", _probes)),
)
FACT_IDS = tuple(fact_id for fact_id, _ in CATALOGUE)

# The fact ids every file of format version 1 holds. Frozen: a later tool version may add
# ids to CATALOGUE without raising the format version, and must still open older files.
FORMAT_1_FACT_IDS = frozenset(
    """
    n8n.version n8n.image_official orchestrator executions.mode main.replicas worker.replicas
    webhook.replicas worker.concurrency multi_main.enabled task_runners.enabled task_runners.mode
    binary_data.mode metrics.enabled pruning.enabled pruning.max_age_hours database.engine
    database.version database.pool_size database.max_connections database.execution_count
    database.execution_table_bytes database.oldest_execution_age_days queue.redis_version
    queue.maxmemory_policy queue.maxmemory_bytes queue.waiting_jobs queue.active_jobs
    queue.failed_jobs resources.main resources.worker probes.main probes.worker
    """.split()
)


# --- settings ----------------------------------------------------------------------


def setting_listed(name) -> bool:
    """True when a variable may appear in the profile by name: only the ones n8n documents.

    `<NAME>_FILE` of a documented variable is listed too; its value is a path, never written.
    """
    if not isinstance(name, str):
        return False
    return name in n8n_settings.KNOWN or (name.endswith("_FILE") and name[:-5] in n8n_settings.KNOWN)


def value_readable(value) -> bool:
    """False for a value the collectors could not or must not read."""
    return isinstance(value, str) and value not in (REDACTED, VALUE_FROM)


def setting_value(name: str, value) -> Optional[str]:
    """The value to write for a setting, or None when only "set" may be said.

    Written only for a variable known to hold a boolean, a number or one word of a fixed
    list, and only when the value really is one.
    """
    if not value_readable(value):
        return None
    value = value.strip()
    if name in n8n_settings.BOOLEANS:
        return value.lower() if value.lower() in ("true", "false") else None
    if name in n8n_settings.NUMBERS:
        return value if _NUMBER.fullmatch(value) else None
    if value in n8n_settings.ENUMS.get(name, ()):
        return value
    return None


def _settings(snapshot: Snapshot) -> tuple:
    """(settings, how many variables were not listed)."""
    scopes = []
    if snapshot.env is not None:
        scopes.append(("instance", [snapshot.env]))
    for role in ROLES:
        envs = [container.get("env") or {} for _, container in _role_containers(snapshot, role)]
        if envs:
            scopes.append((role, envs))
    settings, not_listed = [], 0
    for scope, envs in scopes:
        for name in sorted({name for env in envs for name in env}, key=str):
            if not setting_listed(name):
                not_listed += 1
                continue
            # Containers of one role that disagree, one of them not having it included.
            values = {env[name] if name in env else _UNSET for env in envs}
            value = setting_value(name, values.pop()) if len(values) == 1 else None
            if value is not None:
                settings.append({"scope": scope, "name": name, "state": "value", "value": value})
            else:
                settings.append({"scope": scope, "name": name, "state": "set", "value": None})
    return settings, not_listed


def setting_id(setting: dict) -> str:
    """The id a setting is withheld by."""
    return f"setting:{setting['scope']}:{setting['name']}"


# --- building ----------------------------------------------------------------------


def _sources(snapshot: Snapshot, failed: tuple) -> dict:
    sources = {}
    for section in report.SECTIONS:
        if getattr(snapshot, section) is not None:
            state = "collected"
        elif section == "metrics" and (snapshot.health or {}).get("metrics_disabled") is True:
            state = "disabled"
        elif section in failed:
            state = "failed"
        else:
            state = "not_provided"
        sources[section] = state
    return sources


def check_reference(reference: str) -> str:
    """The reference as stored; ProfileError when it is too long or not a single line."""
    reference = reference.strip()
    if len(reference) > REFERENCE_MAX:
        raise ProfileError(f"the reference is longer than {REFERENCE_MAX} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in reference):
        raise ProfileError("the reference must be a single line of text")
    return reference


def build_profile(
    snapshot: Snapshot,
    findings: list[Finding],
    failed: tuple[str, ...] = (),
    reason: str = "baseline",
    reference: str = "",
    withhold: tuple[str, ...] = (),
    now: Optional[datetime] = None,
) -> dict:
    """The profile of one run, as JSON-serialisable data.

    `failed` is the list cli.failed_sections() computes. Raises ProfileError for a reason,
    reference or withheld id that is not valid.
    """
    if reason not in REASONS:
        raise ProfileError(f"the reason must be one of: {', '.join(REASONS)}")
    failed = tuple(failed)
    outcome = report.build_report(snapshot, findings, failed)
    settings, not_listed = _settings(snapshot)
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    profile = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "tool_version": __version__,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": {"reason": reason, "reference": check_reference(reference)},
        "sources": _sources(snapshot, failed),
        "facts": [_fact(fact_id, build, snapshot, failed) for fact_id, build in CATALOGUE],
        "settings": settings,
        "settings_not_listed": not_listed,
        # Without the evidence: it quotes raw values. Titles are fixed text of the rules.
        "check": {
            "partial": outcome["partial"],
            "scores": [{key: row[key] for key in ("area", "score", "partial")} for row in outcome["scores"]],
            "findings": [
                {"rule_id": f.rule_id, "severity": f.severity, "area": f.area, "title": f.title}
                for f in outcome["findings"]
            ],
        },
    }
    return apply_withholding(profile, withhold)


def apply_withholding(profile: dict, ids) -> dict:
    """A copy of `profile` with the given entries withheld.

    An id is a fact id, `setting:<scope>:<NAME>`, or `check`. A withheld entry stays in
    the file without its value, so the reader can tell removed from unknown. Raises
    ProfileError when an id matches nothing in this profile.
    """
    profile = copy.deepcopy(profile)
    ids = set(ids)
    for fact in profile["facts"]:
        if fact["id"] in ids:
            fact.update(value=None, provenance="withheld", source=None, detail="")
            ids.discard(fact["id"])
    for setting in profile["settings"]:
        if setting_id(setting) in ids:
            ids.discard(setting_id(setting))
            setting.update(state="withheld", value=None)
    if "check" in ids:
        profile["check"] = {"withheld": True}
        ids.discard("check")
    if ids:
        raise ProfileError("nothing in the profile has this id: " + ", ".join(sorted(ids)))
    return profile


def to_json(profile: dict) -> str:
    """The profile as the text of its file: UTF-8, indented, no NaN."""
    return json.dumps(profile, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# --- loading -----------------------------------------------------------------------


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise ProfileError(f"not a valid profile: {what}")


def _is_value(value, depth: int = 0) -> bool:
    """A fact value: null, a boolean, a number, a string, or an object of those."""
    if isinstance(value, dict):
        return depth < 2 and all(isinstance(k, str) and _is_value(v, depth + 1) for k, v in value.items())
    return value is None or isinstance(value, (bool, int, float, str))


def validate(data) -> dict:
    """`data` when it is a profile this version can read; ProfileError otherwise.

    Fact ids are not checked against the catalogue: a newer tool may add ids without
    raising the format version.
    """
    try:
        json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):  # lone surrogates, NaN, endless nesting
        raise ProfileError("not a valid profile: it holds something that is not JSON text") from None
    _require(isinstance(data, dict), "the file does not hold a JSON object")
    _require(data.get("format") == FORMAT, f'"format" is not "{FORMAT}"')
    version = data.get("format_version")
    _require(isinstance(version, int) and not isinstance(version, bool) and version >= 1, '"format_version"')
    if version > FORMAT_VERSION:
        raise ProfileError(
            f"this profile has format version {version}, newer than this tool reads "
            f"({FORMAT_VERSION}): update n8n-healthcheck to open it"
        )
    for key in ("tool_version", "created_at"):
        _require(isinstance(data.get(key), str), f'"{key}"')
    run = data.get("run")
    _require(isinstance(run, dict), '"run"')
    _require(run.get("reason") in REASONS, '"run.reason"')
    reference = run.get("reference")
    try:
        _require(isinstance(reference, str) and check_reference(reference) == reference, '"run.reference"')
    except ProfileError:
        raise ProfileError('not a valid profile: "run.reference"') from None
    sources = data.get("sources")
    _require(isinstance(sources, dict), '"sources"')
    for name, state in sources.items():
        _require(isinstance(name, str) and state in SOURCE_STATES, '"sources"')
    _require(set(report.SECTIONS) <= set(sources), "a source is missing")

    facts = data.get("facts")
    _require(isinstance(facts, list), '"facts"')
    # The messages name an entry by its position, never by text taken from the file.
    for number, fact in enumerate(facts, 1):
        _require(isinstance(fact, dict) and isinstance(fact.get("id"), str), f"fact {number} has no id")
        _require(fact.get("provenance") in PROVENANCES, f"provenance of fact {number}")
        _require("value" in fact and _is_value(fact["value"]), f"value of fact {number}")
        _require(fact.get("source") is None or isinstance(fact["source"], str), f"source of fact {number}")
        _require(isinstance(fact.get("detail"), str), f"detail of fact {number}")
    # Every fact of format version 1 is there, so nothing can be silently absent.
    _require(FORMAT_1_FACT_IDS <= {fact["id"] for fact in facts}, "a fact of the catalogue is missing")

    settings = data.get("settings")
    _require(isinstance(settings, list), '"settings"')
    for number, setting in enumerate(settings, 1):
        _require(isinstance(setting, dict), f"setting {number}")
        _require(isinstance(setting.get("scope"), str) and isinstance(setting.get("name"), str), f"setting {number}")
        _require(setting.get("state") in SETTING_STATES, f"state of setting {number}")
        _require("value" in setting, f"value of setting {number}")
        value = setting["value"]
        _require(value is None or isinstance(value, str), f"value of setting {number}")
    count = data.get("settings_not_listed")
    _require(isinstance(count, int) and not isinstance(count, bool) and count >= 0, '"settings_not_listed"')

    check = data.get("check")
    _require(isinstance(check, dict), '"check"')
    if check.get("withheld") is not True:
        _require(isinstance(check.get("partial"), bool), '"check.partial"')
        _require(isinstance(check.get("scores"), list), '"check.scores"')
        for row in check["scores"]:
            _require(isinstance(row, dict) and isinstance(row.get("area"), str), '"check.scores"')
            _require("score" in row, '"check.scores"')
            score = row["score"]
            _require(score is None or (isinstance(score, int) and not isinstance(score, bool)), '"check.scores"')
        _require(isinstance(check.get("findings"), list), '"check.findings"')
        for finding in check["findings"]:
            _require(isinstance(finding, dict), '"check.findings"')
            for key in ("rule_id", "severity", "area", "title"):
                _require(isinstance(finding.get(key), str), '"check.findings"')
    return data


def load_profile(path: str) -> dict:
    """Read and validate a saved profile. Raises OSError or ProfileError."""
    with open(path, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except (ValueError, RecursionError):  # includes a file that is not UTF-8
            raise ProfileError("not a valid profile: the file is not JSON") from None
    return validate(data)


# --- rendering ---------------------------------------------------------------------


_MARKUP = re.compile(r"([\\`|<>\[\]])")


def _cell(text) -> str:
    """Text from a file made harmless for a terminal and for a Markdown table cell.

    Control and format characters become spaces; the characters that start markup, a code
    span, a link or a new cell are escaped.
    """
    text = "".join(" " if unicodedata.category(char)[0] == "C" else char for char in str(text))
    return _MARKUP.sub(r"\\\1", " ".join(text.split()))


def _shown(value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            parts.append(f"{key} ({_shown(item)})" if isinstance(item, dict) else f"{key}: {_shown(item)}")
        return ", ".join(parts) or "empty"
    return str(value)


def render_markdown(profile: dict) -> str:
    """A saved profile (already validated) as Markdown."""
    run = profile["run"]
    lines = [
        "# n8n environment profile",
        "",
        f"- **Taken:** {_cell(profile['created_at'])}",
        f"- **Reason:** {run['reason']}",
        f"- **Reference:** {_cell(run['reference']) or 'none'}",
        f"- **Written by:** n8n-healthcheck {_cell(profile['tool_version'])}, "
        f"profile format {profile['format_version']}",
        "",
        "## Sources",
        "",
        "| Source | State |",
        "|---|---|",
    ]
    lines += [f"| {_cell(name)} | {state.replace('_', ' ')} |" for name, state in profile["sources"].items()]

    lines += ["", "## Facts", "", "| Fact | Value | Provenance | Source | Detail |", "|---|---|---|---|---|"]
    for fact in profile["facts"]:
        value = _shown(fact["value"]) if fact["provenance"] in ("measured", "declared") else ""
        lines.append(
            f"| {_cell(fact['id'])} | {_cell(value)} | {fact['provenance']} | "
            f"{_cell(fact['source'] or '')} | {_cell(fact['detail'])} |"
        )
    if not profile["facts"]:
        lines.append("| (none) | | | | |")

    lines += ["", "## Settings", ""]
    if profile["settings"]:
        lines += ["| Scope | Name | Value |", "|---|---|---|"]
    for setting in profile["settings"]:
        if setting["state"] == "value":
            value = _cell(setting["value"]) or "(empty)"
        elif setting["state"] == "set":
            value = "set (value not written)"
        else:
            value = "withheld"
        lines.append(f"| {_cell(setting['scope'])} | {_cell(setting['name'])} | {value} |")
    if not profile["settings"]:
        lines.append("No n8n setting is listed.")
    if profile["settings_not_listed"]:
        lines += ["", f"Other variables that were set and are not listed, not even by name: {profile['settings_not_listed']}."]

    check = profile["check"]
    lines += ["", "## Check outcome", ""]
    if check.get("withheld") is True:
        lines.append("Withheld.")
    else:
        lines += ["| Area | Score |", "|---|---|"]
        for row in check["scores"]:
            score = "n/a" if row["score"] is None else f"{row['score']}/100"
            if row.get("partial") and row["score"] is not None:
                score += " (partial)"
            lines.append(f"| {_cell(row['area'])} | {score} |")
        lines.append("")
        if check["partial"]:
            lines += ["The check was partial: not every source was collected or not every rule ran.", ""]
        for finding in check["findings"]:
            lines.append(
                f"- {_cell(finding['rule_id'])} ({_cell(finding['severity'])}, {_cell(finding['area'])}): "
                f"{_cell(finding['title'])}"
            )
        if not check["findings"]:
            lines.append("No findings.")
    lines.append("")
    return "\n".join(lines)
