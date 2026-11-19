"""The rules by which tool version 0.2.0 accepts a profile file, kept as they were.

A copy of `validate` and what it uses from n8n_healthcheck/profile.py at version 0.2.0,
with report.SECTIONS written out. It is here so that a file written by a later version
can be shown to open in 0.2.0. Do not update it when profile.py changes: that is the point.
"""

import json

SECTIONS = ("health", "metrics", "env", "postgres", "redis", "kubernetes")

FORMAT = "n8n-healthcheck-profile"
FORMAT_VERSION = 1

REASONS = ("baseline", "pre_upgrade", "post_upgrade", "incident", "other")
REFERENCE_MAX = 200
SOURCE_STATES = ("collected", "disabled", "failed", "not_provided")
# `declared` is reserved for a later version of the tool and never produced here.
PROVENANCES = ("measured", "declared", "unknown", "withheld")
SETTING_STATES = ("value", "set", "withheld")
ROLES = ("main", "worker", "webhook")

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


class ProfileError(ValueError):
    """A profile that cannot be built as asked, or a file that is not a usable profile."""


def check_reference(reference: str) -> str:
    """The reference as stored; ProfileError when it is too long or not a single line."""
    reference = reference.strip()
    if len(reference) > REFERENCE_MAX:
        raise ProfileError(f"the reference is longer than {REFERENCE_MAX} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in reference):
        raise ProfileError("the reference must be a single line of text")
    return reference


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
    _require(set(SECTIONS) <= set(sources), "a source is missing")

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
