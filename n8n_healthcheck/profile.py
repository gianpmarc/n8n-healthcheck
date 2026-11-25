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
from datetime import date, datetime, timezone
from typing import Callable, Optional

from . import __version__, debug_info, engine, n8n_settings, questions, report, usage
from .models import REDACTED, SEVERITIES, Check, Finding, Snapshot
from .rules._helpers import (
    VALUE_FROM,
    Env,
    command_words,
    compared_scopes,
    concurrency_flag,
    container_env,
    env_flag,
    env_int,
    image_tag,
    image_version,
    image_versions,
    is_queue_mode,
    n8n_containers,
    num,
    stated,
    unknown,
)

FORMAT = "n8n-healthcheck-profile"
FORMAT_VERSION = 1

REASONS = ("baseline", "pre_upgrade", "post_upgrade", "incident", "other")
REFERENCE_MAX = 200
SOURCE_STATES = ("collected", "disabled", "failed", "not_provided")
# `declared` is an answer to an operator question (questions.py), set by apply_answers().
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
NOT_ANSWERED_DETAIL = "not answered"
# The facts only a debug information fills (added in 0.4.0).
NO_DEBUG_INFO_DETAIL = "The debug information was not provided"
NOT_IN_DEBUG_INFO_DETAIL = "the debug information does not state it"
ONLY_WHEN_INSECURE_DETAIL = "n8n reports this only when it is insecure"
STATED_ELSEWHERE_DETAIL = "another source of this run states this setting, so the debug information was not used for it"
VERSIONS_ELSEWHERE_DETAIL = "another source of this run states the versions, so the debug information was not used for it"


# check.checks (added in tool version 0.6.0): what a reader may find in an entry.
CHECK_OUTCOMES = ("finding", "passed", "not_applicable", "not_assessed", "withheld")
# `needs` id -> the Snapshot section that says whether that source failed.
NEEDED_SECTION = {"url": "health", "env": "env", "postgres": "postgres", "redis": "redis", "kubernetes": "kubernetes"}
# A profile older than this is still read, with a line on the call sheet.
SHEET_STALE_DAYS = 30


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


# The facts whose value a container left out of the main role could change, were it a
# main process after all: what is counted or read off the containers themselves. A fact
# read from a variable is not among them; the note on these is enough to warn the reader.
SHAPED_BY_MAIN_CONTAINERS = ("main.replicas", "resources.main", "probes.main", "n8n.version", "n8n.image_official")


def _fact(fact_id: str, build: Callable, snapshot: Snapshot, failed: tuple) -> dict:
    """One fact of the catalogue, saying so when containers were left out of the main role.

    A main process built under another name cannot be told from an exporter, so the reader
    must see that something was left out. Only how many: never an image or Deployment name.
    """
    fact = {"id": fact_id, **build(snapshot, failed)}
    if fact["source"] == "kubernetes" and fact_id in SHAPED_BY_MAIN_CONTAINERS:
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


# --- facts from the debug information ----------------------------------------------
# A probe wins, value by value: the text fills a fact only where no other source of the
# run states that value (rules/_helpers.py, stated() and with_debug_info(), which the
# rules use, so that a rule and the fact about the same thing agree).


def _from_debug_info(snapshot: Snapshot, value, note: str = "") -> dict:
    """Measured by n8n itself; the detail says how old the text is. Never text from it."""
    detail = debug_info.label(debug_info.of(snapshot))
    return _measured(value, "debug_info", f"{detail}; {note}" if note else detail)


def _or_debug_info(build: Callable, read: Callable, variable: Optional[str] = None) -> Callable:
    """An existing fact, taken from the debug information where no probe states it.

    `read(section)` is the value in the text, None when it does not state one.
    `variable` is the n8n variable the probes read for this fact; without one, the text
    is used when the probes leave the fact unknown.
    """

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        value = read(debug_info.of(snapshot)) if debug_info.of(snapshot) is not None else None
        if value is None or (variable is not None and stated(snapshot, variable)):
            return build(snapshot, failed)
        if variable is None:
            found = build(snapshot, failed)
            if found["provenance"] == "measured":
                return found
        return _from_debug_info(snapshot, value)

    return fact


def _replicas_fact(role: str) -> Callable:
    """The replicas of one role: from Kubernetes, else the instances n8n listed.

    Kubernetes states the count as soon as it shows a container of the role, the same
    line rule SCAL-002 draws. A type with no instance listed is unknown, never 0.
    """
    from_kubernetes = _kube_fact(role, _replicas)

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        if debug_info.of(snapshot) is None or _role_containers(snapshot, role):
            return from_kubernetes(snapshot, failed)
        cluster = debug_info.of(snapshot).get("cluster")
        if cluster is None:
            return _unknown(debug_info.NO_CLUSTER)
        count = _whole((cluster.get("instances") or {}).get(role))
        if not count:
            return _unknown(f"the debug information lists no {role} instance")
        return _from_debug_info(snapshot, count, "counted from the instances n8n listed")

    return fact


def _debug_info_fact(read: Callable, variable: Optional[str] = None, absent: str = NOT_IN_DEBUG_INFO_DETAIL) -> Callable:
    """A fact only the debug information fills. `absent` says why `read` gave None."""

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        if debug_info.of(snapshot) is None:
            return _unknown(NO_DEBUG_INFO_DETAIL)
        if variable is not None and stated(snapshot, variable):
            return _unknown(STATED_ELSEWHERE_DETAIL)
        value = read(debug_info.of(snapshot))
        return _unknown(absent) if value is None else _from_debug_info(snapshot, value)

    return fact


def _cluster_fact(read: Callable, absent: str) -> Callable:
    """A fact read from the cluster section, which older n8n versions do not write."""

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        if debug_info.of(snapshot) is not None and debug_info.of(snapshot).get("cluster") is None:
            return _unknown(debug_info.NO_CLUSTER)
        return _debug_info_fact(lambda section: read(section["cluster"]), absent=absent)(snapshot, failed)

    return fact


def _key(name: str) -> Callable:
    return lambda section: section.get(name)


def _pruning(name: str) -> Callable:
    return lambda section: (section.get("pruning") or {}).get(name)


def _flagged(name: str) -> Callable:
    """`false` when the text flags the setting as insecure; never `true`."""
    return lambda section: False if (section.get("insecure") or {}).get(name) else None


def _versions_aligned(snapshot: Snapshot, failed: tuple) -> dict:
    """Gives way where rule MAINT-003 does: when Kubernetes names the versions."""
    if debug_info.of(snapshot) is not None and image_versions(snapshot.kubernetes):
        return _unknown(VERSIONS_ELSEWHERE_DETAIL)

    def aligned(cluster: dict) -> Optional[bool]:
        return len(cluster["versions"]) == 1 if cluster["versions"] else None

    return _cluster_fact(aligned, "the debug information lists no version of an instance")(snapshot, failed)


def _checks_failed(cluster: dict) -> Optional[int]:
    failed = cluster.get("checks_failed")
    return None if failed is None else len(failed)


def _usage_fact(name: str) -> Callable:
    """A usage number read through the n8n API (usage.numbers()). It describes; no rule reads it."""

    def fact(snapshot: Snapshot, failed: tuple) -> dict:
        found = usage.numbers(snapshot)
        if found is None:
            return _unknown(usage.NOT_READ_DETAIL if "usage" in failed else usage.NOT_PROVIDED_DETAIL)
        value, detail = found[name]
        return _unknown(detail) if value is None else _measured(value, "api", detail)

    return fact


def _not_answered(snapshot: Snapshot, failed: tuple) -> dict:
    """A fact no probe can fill: only an answer to its operator question gives it a value."""
    return _unknown(NOT_ANSWERED_DETAIL)


# (fact id, function(snapshot, failed) -> fact without its id), in the order of the file.
CATALOGUE = (
    ("n8n.version", _or_debug_info(_n8n_version, _key("n8n_version"))),
    ("n8n.image_official", _image_official),
    ("orchestrator", _orchestrator),
    (
        "executions.mode",
        _or_debug_info(
            _env_fact("EXECUTIONS_MODE", _enum(*n8n_settings.ENUMS["EXECUTIONS_MODE"]), default="regular"),
            _key("execution_mode"),
            "EXECUTIONS_MODE",
        ),
    ),
    ("main.replicas", _replicas_fact("main")),
    ("worker.replicas", _replicas_fact("worker")),
    ("webhook.replicas", _replicas_fact("webhook")),
    ("worker.concurrency", _worker_concurrency),
    (
        "multi_main.enabled",
        _or_debug_info(
            _env_fact("N8N_MULTI_MAIN_SETUP_ENABLED", _bool, default=False),
            _key("multi_main"),
            "N8N_MULTI_MAIN_SETUP_ENABLED",
        ),
    ),
    ("task_runners.enabled", _env_fact("N8N_RUNNERS_ENABLED", _bool)),
    ("task_runners.mode", _env_fact("N8N_RUNNERS_MODE", _enum(*n8n_settings.ENUMS["N8N_RUNNERS_MODE"]))),
    (
        "binary_data.mode",
        _or_debug_info(
            _env_fact("N8N_DEFAULT_BINARY_DATA_MODE", _enum(*n8n_settings.ENUMS["N8N_DEFAULT_BINARY_DATA_MODE"])),
            _key("binary_mode"),
            "N8N_DEFAULT_BINARY_DATA_MODE",
        ),
    ),
    ("metrics.enabled", _metrics_enabled),
    (
        "pruning.enabled",
        _or_debug_info(_env_fact("EXECUTIONS_DATA_PRUNE", _bool), _pruning("enabled"), "EXECUTIONS_DATA_PRUNE"),
    ),
    (
        "pruning.max_age_hours",
        _or_debug_info(
            _env_fact("EXECUTIONS_DATA_MAX_AGE", _count), _pruning("max_age_hours"), "EXECUTIONS_DATA_MAX_AGE"
        ),
    ),
    (
        "database.engine",
        _or_debug_info(
            _env_fact("DB_TYPE", _enum(*n8n_settings.ENUMS["DB_TYPE"]), default="sqlite"), _key("database"), "DB_TYPE"
        ),
    ),
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
# Added in tool version 0.3.0: the operator questions no probe can answer, after the rest.
CATALOGUE += tuple(
    (question["id"], _not_answered) for question in questions.OPERATOR if question["id"] not in dict(CATALOGUE)
)
# Added in tool version 0.4.0: what only a debug information states, after the rest.
CATALOGUE += (
    ("n8n.node_version", _debug_info_fact(_key("node_version"))),
    ("n8n.platform", _debug_info_fact(_key("platform"))),
    ("license.type", _debug_info_fact(_key("license"))),
    ("main.concurrency_limit", _debug_info_fact(_key("concurrency_limit"), CONCURRENCY_LIMIT)),
    ("executions.save_success", _debug_info_fact(_key("save_success"), "EXECUTIONS_DATA_SAVE_ON_SUCCESS")),
    ("executions.save_error", _debug_info_fact(_key("save_error"), "EXECUTIONS_DATA_SAVE_ON_ERROR")),
    ("executions.save_progress", _debug_info_fact(_key("save_progress"), "EXECUTIONS_DATA_SAVE_ON_PROGRESS")),
    ("executions.save_manual", _debug_info_fact(_key("save_manual"), "EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS")),
    ("pruning.max_count", _debug_info_fact(_pruning("max_count"), "EXECUTIONS_DATA_PRUNE_MAX_COUNT")),
    ("cluster.versions_aligned", _versions_aligned),
    ("cluster.checks_failed", _cluster_fact(_checks_failed, "the debug information lists no cluster check")),
    (
        "security.secure_cookie",
        _debug_info_fact(_flagged("secure_cookie"), "N8N_SECURE_COOKIE", ONLY_WHEN_INSECURE_DETAIL),
    ),
    (
        "security.file_access_blocked",
        _debug_info_fact(
            _flagged("file_access_blocked"), "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES", ONLY_WHEN_INSECURE_DETAIL
        ),
    ),
)
# Added in tool version 0.5.0: usage numbers read through the n8n API with a key, after the rest.
CATALOGUE += tuple(
    (f"usage.{name}", _usage_fact(name))
    for name in (
        "executions_per_day",
        "failure_rate_percent",
        "period_days",
        "method",
        "estimate",
        "active_workflows",
        "active_workflows_partial",
        "users",
    )
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


def text_version(snapshot: Snapshot) -> Optional[str]:
    """The n8n version of the debug information, when the fact `n8n.version` rests on it.

    None when a probe measured the version: the report's title then shows what it shows
    without a debug information, so the title and the fact cannot disagree.
    """
    fact = dict(CATALOGUE)["n8n.version"](snapshot, ())
    return fact["value"] if fact["source"] == "debug_info" else None


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
    """(settings, how many distinct variables were not listed)."""
    scopes = []
    if snapshot.env is not None:
        scopes.append(("instance", [snapshot.env]))
    for role in ROLES:
        envs = [container.get("env") or {} for _, container in _role_containers(snapshot, role)]
        if envs:
            scopes.append((role, envs))
    settings, not_listed = [], set()
    for scope, envs in scopes:
        for name in sorted({name for env in envs for name in env}, key=str):
            if not setting_listed(name):
                not_listed.add(name)  # once, in however many scopes it is set
                continue
            # Containers of one role that disagree, one of them not having it included.
            values = {env[name] if name in env else _UNSET for env in envs}
            value = setting_value(name, values.pop()) if len(values) == 1 else None
            if value is not None:
                settings.append({"scope": scope, "name": name, "state": "value", "value": value})
            else:
                settings.append({"scope": scope, "name": name, "state": "set", "value": None})
    return settings, len(not_listed)


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
    # Not one of the six a file must hold (report.SECTIONS). Never "failed": a text that
    # cannot be read stops the run before anything is collected.
    sources["debug_info"] = "collected" if debug_info.of(snapshot) is not None else "not_provided"
    # Not one of the six either. Collected when at least one number was read.
    section = usage.of(snapshot)
    if section is not None and any(section[name] is not None for name in usage.NUMBERS):
        sources["api"] = "collected"
    else:
        sources["api"] = "failed" if section is not None or "usage" in failed else "not_provided"
    return sources


def check_reference(reference: str) -> str:
    """The reference as stored; ProfileError when it is too long or not a single line."""
    reference = reference.strip()
    if len(reference) > REFERENCE_MAX:
        raise ProfileError(f"the reference is longer than {REFERENCE_MAX} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in reference):
        raise ProfileError("the reference must be a single line of text")
    return reference


def rests_on(info, snapshot: Snapshot, settings: list, facts: list, by_mode: bool = False) -> list:
    """The ids of the profile entries a rule rests on: facts, then settings that are listed.

    Ids only, the ones `--withhold` takes; the values are those already in the file. A
    fact is named only when the file holds a value for it: a fact that is unknown reads
    another source than the rule did (the facts only a debug information fills, when the
    rule read the environment), or nothing at all. A variable is named in every scope the
    rule read it in: the env file when one was given, and the Kubernetes roles the rule
    judges, which next to an env file are the containers it is compared with (see
    rules/_helpers.py, against_sources()). A variable that is not set has no entry.
    `by_mode` adds the execution mode, for a rule that does not apply because of it.
    """
    known = {fact["id"] for fact in facts if fact["provenance"] != "unknown"}
    listed = {setting_id(setting) for setting in settings}
    mode = by_mode and "EXECUTIONS_MODE" not in info.settings
    names = info.settings + (("EXECUTIONS_MODE",) if mode else ())
    ids = [
        f"setting:{scope}:{name}"
        for name in names
        for scope in compared_scopes(name, info.scopes)
        if scope != "instance" or snapshot.env is not None
    ]
    wanted = info.facts + (("executions.mode",) if by_mode and "executions.mode" not in info.facts else ())
    return [item for item in wanted if item in known] + [item for item in ids if item in listed]


def _check_entries(checks: list, snapshot: Snapshot, failed: tuple, settings: list, facts: list) -> list:
    """`check.checks`: one entry per declared rule, in rule order. Every text is a literal of the rule."""
    entries = []
    for check in checks:
        info = check.info
        if info is None:
            continue
        entry = {"rule_id": info.rule_id, "area": info.area, "outcome": check.outcome}
        if check.outcome == "finding":
            found = check.finding
            entry.update(severity=found.severity, text=found.title, impact=found.impact, remedy=found.recommendation)
        elif check.outcome == "passed":
            entry.update(basis=check.basis, text=info.passed)
        else:
            entry["text"] = info.subject
            if check.outcome == "not_assessed":
                reason, needs = check.reason, list(check.needs)
                # A source that would state it was asked for and could not be collected.
                asked = [need for need in needs if NEEDED_SECTION.get(need) in failed]
                if reason == "source_missing" and asked:
                    reason, needs = "source_failed", asked
                entry.update(reason=reason, needs=needs)
        # Whatever the outcome, so that a withheld fact or setting names every check that
        # tells something about it: one that does not apply, and one that lacks a source,
        # read values on the way there. Only a rule that failed to run read nothing.
        raised = check.reason == "rule_error"
        entry["rests_on"] = [] if raised else rests_on(info, snapshot, settings, facts, check.by_mode)
        entries.append(entry)
    return entries


def build_profile(
    snapshot: Snapshot,
    findings: list[Finding],
    failed: tuple[str, ...] = (),
    reason: str = "baseline",
    reference: str = "",
    withhold: tuple[str, ...] = (),
    now: Optional[datetime] = None,
    checks: Optional[list[Check]] = None,
) -> dict:
    """The profile of one run, as JSON-serialisable data.

    `failed` is the list cli.failed_sections() computes. `checks` is what engine.assess()
    returned: with it the profile records the outcome of every rule (`check.checks`), and
    `findings` is not read, so that the two lists of the file come from one. Raises
    ProfileError for a reason, reference or withheld id that is not valid.
    """
    if reason not in REASONS:
        raise ProfileError(f"the reason must be one of: {', '.join(REASONS)}")
    failed = tuple(failed)
    if checks is not None:
        findings = engine.findings_of(checks)
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
    if checks is not None:
        # The fixed texts of the rules, never the evidence. See docs/rules.md.
        profile["check"]["checks"] = _check_entries(checks, snapshot, failed, settings, profile["facts"])
    return apply_withholding(profile, withhold)


def apply_withholding(profile: dict, ids) -> dict:
    """A copy of `profile` with the given entries withheld.

    An id is a fact id, `setting:<scope>:<NAME>`, `check:<RULE_ID>` for one check, or
    `check` for the whole outcome. A withheld entry stays in the file without its value,
    so the reader can tell removed from unknown; a withheld check keeps its rule id and
    its area, its finding leaves `check.findings`, and the score of its area is withheld
    with it, whatever the check had stated: a score kept, or computed again without the
    finding, would tell that the check was one. Raises ProfileError when an id matches
    nothing in this profile.
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
    check = profile["check"]
    for entry in check.get("checks") or []:
        item = f"check:{entry['rule_id']}"
        if item in ids:
            ids.discard(item)
            withheld = {"rule_id": entry["rule_id"], "area": entry["area"], "outcome": "withheld", "text": ""}
            entry.clear()
            entry.update(withheld)
            check["findings"] = [finding for finding in check["findings"] if finding["rule_id"] != entry["rule_id"]]
            for row in check["scores"]:
                if row["area"] == entry["area"]:
                    row.update(score=None, withheld=True)
    if "check" in ids:
        profile["check"] = {"withheld": True}
        ids.discard("check")
    if ids:
        raise ProfileError("nothing in the profile has this id: " + ", ".join(sorted(ids)))
    return profile


def check_answers(answers: dict) -> dict:
    """{fact id: the value to store} for answers to operator questions; ProfileError otherwise.

    An answer is one value of the question's list as a string, or a list of them for a
    `multi` question. Nothing but a value of the catalogue can come out of here: an id
    or a value must be exactly a `str`, since a subclass can claim to equal anything.
    """
    values = {}
    for fact_id, answer in answers.items():
        question = questions.OPERATOR_BY_ID.get(fact_id) if type(fact_id) is str else None
        if question is None:
            raise ProfileError(f"no question the tool asks has the id {fact_id!r}")
        allowed = [value for value, _label in question["values"]]

        def listed(item) -> bool:
            return type(item) is str and item in allowed

        if question["kind"] == "multi":
            chosen = list(answer) if isinstance(answer, (list, tuple)) else [None]
            # Membership first: once every item is a value of the list, they can be compared.
            if not chosen or not all(listed(item) for item in chosen) or len(set(chosen)) != len(chosen):
                raise ProfileError(f"the answer to {fact_id} must be one or more of: {', '.join(allowed)}")
            if "none" in chosen and len(chosen) > 1:
                raise ProfileError(f'the answer to {fact_id} cannot be "none" together with another value')
            values[fact_id] = {value: True for value in allowed if value in chosen}
        elif not listed(answer):
            raise ProfileError(f"the answer to {fact_id} must be one of: {', '.join(allowed)}")
        else:
            values[fact_id] = answer == "yes" if question["kind"] == "yes_no" else answer
    return values


def apply_answers(profile: dict, answers: dict) -> tuple:
    """(a copy of `profile` with the answers as declared facts, the ids whose answer was dropped).

    To be called after the probes and before apply_withholding(). An answer is taken
    only for a fact that is `unknown`: a measured fact wins, and its answer is dropped.
    Raises ProfileError for an answer check_answers() refuses, or a fact the profile lacks.
    """
    values = check_answers(answers)
    profile = copy.deepcopy(profile)
    facts = {}
    for fact in profile["facts"]:
        facts.setdefault(fact["id"], fact)
    dropped = []
    for fact_id, value in values.items():
        if fact_id not in facts:
            raise ProfileError(f"nothing in the profile has this id: {fact_id}")
        if facts[fact_id]["provenance"] != "unknown":
            dropped.append(fact_id)
            continue
        facts[fact_id].update(value=value, provenance="declared", source=None, detail="")
    return profile, dropped


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
        # Added in tool version 0.6.0; a file written before has none. What an entry
        # holds besides these four is read as it is found: see check_entries().
        if "checks" in check:
            _require(isinstance(check["checks"], list), '"check.checks"')
            for entry in check["checks"]:
                _require(isinstance(entry, dict), '"check.checks"')
                for key in ("rule_id", "area", "outcome", "text"):
                    _require(isinstance(entry.get(key), str), '"check.checks"')
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
        lines += ["", f"Other distinct variables that were set and are not listed, not even by name: {profile['settings_not_listed']}."]

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
            if score_withheld(row):
                score = "withheld"
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
        lines += _checks_review(profile)
    lines.append("")
    return "\n".join(lines)


# --- checks, as a reader finds them ------------------------------------------------

OUTCOME_LABELS = {
    "finding": "finding",
    "passed": "passed",
    "not_applicable": "does not apply",
    "not_assessed": "not assessed",
    "withheld": "withheld",
}
BY_DEFAULT = "by n8n's default, not set explicitly"
REASON_LABELS = {
    "source_missing": "no source that states it was given",
    "source_failed": "a source that states it was asked for and could not be collected",
    "not_stated": "a source was read and does not say",
    "value_unreadable": "a source holds the value, which could not be read",
    "sources_disagree": "two sources given in the run disagree on what it reads",
    "rule_error": "the rule failed to run",
}
NOT_KNOWN = "not known to this version of n8n-healthcheck"
# How `needs` names what the operator gives.
NEED_LABELS = {
    "url": "the n8n URL",
    "env": "the env file",
    "postgres": "Postgres",
    "redis": "Redis",
    "kubernetes": "Kubernetes",
    "debug_info": "the debug information",
}


def score_withheld(row: dict) -> bool:
    """True for the score of an area that was withheld with one of its checks: not "no data"."""
    return row["score"] is None and row.get("withheld") is True


def check_entries(profile: dict) -> Optional[list]:
    """The entries of `check.checks`, each made safe to render; None when the file has none.

    A file is validated only for the four strings every entry holds, so that a later
    version can add to it. Whatever else an entry holds is read here: text that is not
    text is empty, a list that is not a list of strings is empty, and an outcome or a
    reason this version does not know makes the entry `known: False`.
    """
    check = profile["check"]
    if check.get("withheld") is True or "checks" not in check:
        return None

    def text(value) -> str:
        return value if isinstance(value, str) else ""

    def strings(value) -> list:
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    entries = []
    for entry in check["checks"]:
        outcome, reason = entry["outcome"], text(entry.get("reason"))
        known = outcome in CHECK_OUTCOMES and (outcome != "not_assessed" or reason in REASON_LABELS)
        entries.append(
            {
                "rule_id": entry["rule_id"],
                "area": entry["area"],
                "outcome": outcome,
                "text": entry["text"],
                "known": known,
                "severity": text(entry.get("severity")),
                "impact": text(entry.get("impact")),
                "remedy": text(entry.get("remedy")),
                "by_default": entry.get("basis") == "default",
                "reason": reason,
                "needs": strings(entry.get("needs")),
                "rests_on": strings(entry.get("rests_on")),
            }
        )
    return entries


def _need(need: str) -> str:
    return NEED_LABELS[need] if need in NEED_LABELS else _cell(need)


def _any_of(needs: list) -> str:
    """'Redis', 'Kubernetes or the debug information', 'any one of the env file, Kubernetes or the debug information'."""
    names = [_need(need) for need in needs]
    if len(names) < 3:
        return " or ".join(names)
    return f"any one of {', '.join(names[:-1])} or {names[-1]}"


def rested_on_withheld(profile: dict) -> dict:
    """{id of a withheld fact or setting: the rule ids of the checks in the file that rest on it}."""
    withheld = [fact["id"] for fact in profile["facts"] if fact["provenance"] == "withheld"]
    withheld += [setting_id(setting) for setting in profile["settings"] if setting["state"] == "withheld"]
    found = {}
    for entry in check_entries(profile) or []:
        for item in entry["rests_on"]:
            if item in withheld and entry["rule_id"] not in found.setdefault(item, []):
                found[item].append(entry["rule_id"])
    return found


def _checks_review(profile: dict) -> list:
    """The lines of `--show-profile` for `check.checks`: every entry with every text it holds."""
    entries = check_entries(profile)
    if entries is None:
        return []
    lines = [
        "",
        "### Checks",
        "",
        "What every rule stated, with every text written to the file. Each text is a fixed "
        "sentence of the rule, never a value of the instance.",
        "",
    ]
    for entry in entries:
        head = f"- **{_cell(entry['rule_id'])}** ({_cell(entry['area'])}): "
        if not entry["known"]:
            lines.append(f"{head}{NOT_KNOWN}. {_cell(entry['text'])}")
            continue
        outcome = OUTCOME_LABELS[entry["outcome"]]
        if entry["outcome"] == "withheld":
            lines.append(f"{head}{outcome}.")
            continue
        if entry["outcome"] == "finding":
            outcome += f", {_cell(entry['severity'])}"
        elif entry["outcome"] == "passed" and entry["by_default"]:
            outcome += f", {BY_DEFAULT}"
        elif entry["outcome"] == "not_assessed":
            outcome += f", {REASON_LABELS[entry['reason']]}"
            if entry["needs"]:
                outcome += f" (it needs {_any_of(entry['needs'])})"
        lines.append(f"{head}{outcome}. {_cell(entry['text'])}")
        if entry["outcome"] == "finding":
            lines += [f"  - Impact: {_cell(entry['impact'])}", f"  - Remedy: {_cell(entry['remedy'])}"]
        if entry["rests_on"]:
            lines.append("  - Rests on: " + ", ".join(f"`{_cell(item)}`" for item in entry["rests_on"]))
    if not entries:
        lines.append("No check is listed.")
    rested = rested_on_withheld(profile)
    if rested:
        lines += ["", "Withheld entries that a check in the file still rests on:", ""]
        lines += [f"- `{_cell(item)}`: {', '.join(_cell(rule_id) for rule_id in rules)}" for item, rules in rested.items()]
        lines += [
            "",
            "The text of such a check can say something about what was withheld. To leave a "
            "check out as well, withhold it by its rule id: `--withhold check:RULE-ID`.",
        ]
    return lines


def render_questions(profile: dict) -> str:
    """The question sheet of a saved profile (already validated) as Markdown.

    What is still unknown, by who can answer it, then the questions that are only ever
    asked in conversation. A fact the file does not hold counts as unknown: a profile
    written before a question existed still gets it on its sheet.
    """
    run = profile["run"]
    lines = [
        "# n8n environment profile: questions still open",
        "",
        f"- **Taken:** {_cell(profile['created_at'])}",
        f"- **Reason:** {run['reason']}",
        f"- **Reference:** {_cell(run['reference']) or 'none'}",
        "",
        *_question_lines(profile, "##"),
    ]
    lines.append("")
    return "\n".join(lines)


def _question_lines(profile: dict, heading: str) -> list:
    """The three parts of the question sheet, under headings of the level given."""
    facts = {}
    for fact in profile["facts"]:
        facts.setdefault(fact["id"], fact)

    def is_open(fact_id: str) -> bool:
        return fact_id not in facts or facts[fact_id]["provenance"] == "unknown"

    lines = [
        f"{heading} 1. Could be measured",
        "",
        "Facts the tool measures by itself when it can. Each says why it could not this time.",
        "",
    ]
    measurable = [fact_id for fact_id in FACT_IDS if fact_id not in questions.OPERATOR_BY_ID and is_open(fact_id)]
    for fact_id in measurable:
        detail = _cell(facts[fact_id]["detail"]) if fact_id in facts else ""
        lines.append(f"- **{questions.sentence(fact_id)}** (`{fact_id}`)" + (f": {detail}" if detail else ""))
    if not measurable:
        lines.append("Nothing is open here.")

    lines += [
        "",
        f"{heading} 2. Ask whoever runs the platform",
        "",
        "Closed questions no probe can answer. The tool can store each answer as a declared fact.",
        "",
    ]
    asked = [question for question in questions.OPERATOR if is_open(question["id"])]
    lines += [questions.line(question) for question in asked]
    if not asked:
        lines.append("Nothing is open here.")

    lines += [
        "",
        f"{heading} 3. Ask in conversation",
        "",
        "Never asked or stored by the tool. Record the answers where you keep your own notes.",
        "",
    ]
    lines += [f"- **{question}** {why}" for question, why in questions.CONVERSATION]

    withheld = [_cell(fact["id"]) for fact in profile["facts"] if fact["provenance"] == "withheld"]
    if withheld:
        lines += ["", "Withheld by the sender, not questions to press on: " + ", ".join(withheld) + "."]
    return lines


# --- the call sheet ----------------------------------------------------------------

# The facts of "The environment in brief", in order, with how the sheet names each.
BRIEF = (
    ("n8n.version", "n8n version"),
    ("executions.mode", "Execution mode"),
    ("main.replicas", "Main replicas"),
    ("worker.replicas", "Worker replicas"),
    ("database.engine", "Database type"),
    ("usage.executions_per_day", "Executions per day"),
    ("usage.failure_rate_percent", "Failure rate"),
    ("usage.active_workflows", "Active workflows"),
    ("usage.users", "Users"),
)
# The two usage numbers the profile can mark as an estimate (the fact usage.estimate).
ESTIMATED = ("usage.executions_per_day", "usage.failure_rate_percent")
SHEET_NO_CHECKS = (
    "This file was written by a version of n8n-healthcheck that does not record what passed, "
    "the impact and the remedy of a finding, what was not assessed or what does not apply."
)
SHEET_NOT_IN_PROFILE = "Rests on values that are not in the profile: they are in the report the operator keeps."
SHEET_PARTIAL = "The check was partial: not every source was collected or not every rule ran."


def _age_days(created_at: str, today: date) -> Optional[int]:
    """Whole days from the day a profile was taken to `today`; None when the file's date is not one."""
    try:
        return (today - date.fromisoformat(created_at[:10])).days
    except ValueError:
        return None


def _known(fact: Optional[dict]) -> bool:
    return fact is not None and fact["provenance"] in ("measured", "declared")


def render_sheet(profile: dict, today: Optional[date] = None) -> str:
    """The call sheet of a saved profile (already validated) as Markdown.

    An outline for whoever supports the operator: what works well, what does not, what
    to improve, what was not assessed and what would be needed for it. It is a rendering
    of the file and holds nothing the file does not. `today` is the day in UTC, for the
    age of the profile.
    """
    run, check = profile["run"], profile["check"]
    facts = {}
    for fact in profile["facts"]:
        facts.setdefault(fact["id"], fact)
    settings = {}
    for setting in profile["settings"]:
        settings.setdefault(setting_id(setting), setting)

    lines = [
        "# n8n call sheet",
        "",
        "## Run",
        "",
        f"- **Taken:** {_cell(profile['created_at'])}",
        f"- **Reason:** {run['reason']}",
        f"- **Reference:** {_cell(run['reference']) or 'none'}",
        f"- **Written by:** n8n-healthcheck {_cell(profile['tool_version'])}",
        "- **Sources:** "
        + "; ".join(f"{_cell(name)} {state.replace('_', ' ')}" for name, state in profile["sources"].items()),
    ]
    age = _age_days(profile["created_at"], today or datetime.now(timezone.utc).date())
    if age is not None and age > SHEET_STALE_DAYS:
        lines += ["", f"This profile was taken {age} days ago: the environment may have changed since."]

    lines += ["", "## The environment in brief", ""]
    estimate = facts.get("usage.estimate")
    for fact_id, label in BRIEF:
        fact = facts.get(fact_id)
        if not _known(fact):
            # A fact the file does not hold (an older version wrote it) is unknown.
            lines.append(f"- **{label}:** {'withheld' if fact is not None and fact['provenance'] == 'withheld' else 'unknown'}")
            continue
        value = _cell(_shown(fact["value"])) + ("%" if fact_id == "usage.failure_rate_percent" else "")
        note = fact["provenance"]
        if fact_id in ESTIMATED:
            if not _known(estimate):
                note += ", may be an estimate"
            elif estimate["value"] is True:
                value = f"about {value}"
        lines.append(f"- **{label}:** {value} ({note})")

    entries = check_entries(profile)
    if check.get("withheld") is True:
        lines += ["", "## Check outcome", "", "The operator withheld the check outcome: what passed, the findings and what was not assessed are not in the file."]
    elif entries is None:
        lines += ["", "## What does not", ""]
        lines += [
            f"{number}. **{_cell(finding['rule_id'])}** ({_cell(finding['severity'])}, {_cell(finding['area'])}): {_cell(finding['title'])}"
            for number, finding in enumerate(check["findings"], 1)
        ]
        if not check["findings"]:
            lines.append("No finding. " + (SHEET_PARTIAL if check["partial"] else "Every source was collected and every rule ran."))
        lines += ["", SHEET_NO_CHECKS]
    else:
        lines += _sheet_checks(entries, check["partial"], facts, settings, check["scores"])

    lines += ["", "## Questions still open", "", *_question_lines(profile, "###"), ""]
    return "\n".join(lines)


def _rested_value(item: str, facts: dict, settings: dict) -> str:
    """One id of `rests_on` with the value the file holds for it, withheld or not as the operator chose."""
    name = f"`{_cell(item)}`"
    if item in settings:
        setting = settings[item]
        if setting["state"] == "value":
            return f"{name} = {_cell(setting['value']) or '(empty)'}"
        return f"{name} {'withheld' if setting['state'] == 'withheld' else 'is set (value not written)'}"
    if item in facts:
        fact = facts[item]
        return f"{name} = {_cell(_shown(fact['value']))}" if _known(fact) else f"{name} {fact['provenance']}"
    return f"{name} is not in the file"


def _sheet_checks(entries: list, partial: bool, facts: dict, settings: dict, scores: list) -> list:
    """Sections 3 to 7 of the call sheet, from the entries of `check.checks`."""
    of = lambda outcome: [entry for entry in entries if entry["known"] and entry["outcome"] == outcome]
    item = lambda entry: f"{_cell(entry['rule_id'])} {_cell(entry['text'])}"

    lines = ["", "## What works well", "", "Checks that read the values they need and found the good condition.", ""]
    passed = of("passed")
    for area in dict.fromkeys(entry["area"] for entry in passed):
        lines += [f"### {_cell(area)[:1].upper()}{_cell(area)[1:]}", ""]
        lines += [
            f"- {item(entry)}" + (f" ({BY_DEFAULT})" if entry["by_default"] else "")
            for entry in passed
            if entry["area"] == area
        ]
        lines.append("")
    if not passed:
        lines += ["No check passed.", ""]

    def severity(entry: dict) -> int:
        return SEVERITIES.index(entry["severity"]) if entry["severity"] in SEVERITIES else len(SEVERITIES)

    findings = sorted(of("finding"), key=severity)  # stable: rule order within a severity
    lines += ["## What does not", ""]
    for number, entry in enumerate(findings, 1):
        lines += [
            f"{number}. **{_cell(entry['rule_id'])}** ({_cell(entry['severity'])}, {_cell(entry['area'])}): {_cell(entry['text'])}",
            f"   - Impact: {_cell(entry['impact'])}",
            "   - "
            + (
                "Rests on: " + "; ".join(_rested_value(rested, facts, settings) for rested in entry["rests_on"])
                if entry["rests_on"]
                else SHEET_NOT_IN_PROFILE
            ),
        ]
    if not findings:
        lines.append("No finding. " + (SHEET_PARTIAL if partial else "Every source was collected and every rule ran."))

    lines += ["", "## What to improve", ""]
    lines += [
        f"{number}. **{_cell(entry['rule_id'])}, {_cell(entry['text'])}:** {_cell(entry['remedy'])}"
        for number, entry in enumerate(findings, 1)
    ]
    if not findings:
        lines.append("Nothing to improve was found.")

    lines += ["", "## Not assessed", ""]
    groups = {}  # heading -> entries, in the order the sheet prints them
    not_assessed = of("not_assessed")
    for reason, heading in (("source_missing", "With {}"), ("source_failed", "Asked for and not collected: {}")):
        for entry in not_assessed:
            if entry["reason"] == reason:
                needs = _any_of(entry["needs"]) or "a source that states it"
                groups.setdefault(heading.format(needs), []).append(entry)
    for reason, heading in (
        ("not_stated", "A source was read and does not say"),
        ("value_unreadable", "The value could not be read (a Secret, a file, a redacted value or an incomplete environment)"),
        ("sources_disagree", "Two sources given in the run disagree on what the check reads"),
        ("rule_error", "The rule failed to run, which is a bug in n8n-healthcheck"),
    ):
        for entry in not_assessed:
            if entry["reason"] == reason:
                groups.setdefault(heading, []).append(entry)
    for entry in entries:
        if not entry["known"]:
            groups.setdefault(NOT_KNOWN.capitalize(), []).append(entry)
    lines += [f"- **{heading}:** {'; '.join(item(entry) for entry in group)}" for heading, group in groups.items()]
    if not groups:
        lines.append("Every check that applies was assessed.")

    lines += ["", "## Does not apply", ""]
    not_applicable = of("not_applicable")
    lines.append("; ".join(item(entry) for entry in not_applicable) if not_applicable else "No check was left out as not applying.")
    withheld = len(of("withheld"))
    said = [f"Checks withheld by the operator: {withheld}."] if withheld else []
    areas = [_cell(row["area"]) for row in scores if score_withheld(row)]
    if areas:
        # Whatever the check had stated: the score would tell whether it was a finding.
        said.append(f"The score of its area is withheld with a check: {', '.join(areas)}.")
    if said:
        lines += ["", " ".join(said)]
    return lines
