"""Usage numbers read through the n8n API: the Snapshot section, checked, and what it says.

collectors/api.py writes the section. A snapshot can also be loaded from a file or built
in code, so the facts and the report read it only through of() and numbers(), which keep
a value only when the collector could have written it. The numbers describe: no rule
reads them.
"""

from __future__ import annotations

import math
from typing import Optional

from . import debug_info
from .rules._helpers import effective_env, stated, unknown

# The period the executions are counted over, in days before the run. It is the default
# of n8n's Insights summary, and the longest range every licence allows.
PERIOD_DAYS = 7
# In the order they are read: the heaviest answers last.
NUMBERS = ("executions", "users", "active_workflows")
METHODS = ("insights", "listed")
ESTIMATE_REASONS = ("page_limit", "short_history", "saved_only")
# Statuses of a listed execution that count, and those among them that failed. Running,
# waiting, new, canceled and unknown executions are in neither number.
COUNTED = ("success", "error", "crashed")
FAILED = ("error", "crashed")

KEY_MAX = 4096

NOT_PROVIDED_DETAIL = "The n8n API key was not provided"
NOT_READ_DETAIL = "The n8n API could not be read"
# Why a number is unknown: the word the collector stored, or an HTTP status.
REFUSED_WORDS = {
    "limit": "more than 10 000 of them",
    "slow": "the instance answered too slowly",
    "large": "the answer was too large",
    "short": "less than one day of executions is stored",
    "busy": "more than 10 000 executions in less than one day, the bound of this tool",
    "unreachable": "the n8n API could not be reached",
    "invalid": "the n8n API gave an answer this tool does not recognise",
    "redirect": "the n8n API redirected to another address, which was not followed",
    "https": "the instance redirects to https, which was not followed: give the URL with https",
}
LIMIT = {"active_workflows": "more than 10 000 active workflows", "users": "more than 10 000 users"}
USERS_FORBIDDEN = "the API key cannot list users: its scopes leave that out, or the licence limits the number of users"
PARTIAL = "as far as this key's user can see"
ESTIMATE = {
    "partial_view": f"only {PARTIAL}",
    "page_limit": "the list was read up to 10 000 executions, the bound of this tool",
    "short_history": "the list ends before 7 days (pruning, or a young instance)",
    "saved_only": "only executions n8n saved and has not pruned are in the list",
}
INSIGHTS_DETAIL = "from n8n's Insights summary of the last 7 days, which counts what Insights counts"
INSIGHTS_LAG = "Insights can lag behind by up to an hour"
# A zero is not shown as an exact number: executions of the last hour may not be in it yet.
INSIGHTS_ZERO = "n8n's Insights counted no execution in the last 7 days; it can lag behind by up to an hour"
NO_EXECUTION = "no execution in the period"
NO_OUTCOME = "this n8n version does not report the outcome of an execution in the list"
SUCCESS_NOT_SAVED = "successful executions are not saved, so the list cannot count them"
ERROR_NOT_SAVED = "failed executions are not saved, so the list cannot count them"


class KeyRefused(ValueError):
    """Text that cannot be an n8n API key. The message never holds a part of it."""


def check_key(text: str) -> str:
    """The key in `text`, without the white space around it; KeyRefused otherwise.

    One line of visible ASCII: the key goes into a request header as it is.
    """
    key = text.strip()
    if not key:
        raise KeyRefused("the API key is empty")
    if "\n" in key or "\r" in key:
        raise KeyRefused("the API key must be a single line")
    if len(key) > KEY_MAX:
        raise KeyRefused(f"the API key is longer than {KEY_MAX} characters")
    if not all("!" <= char <= "~" for char in key):
        raise KeyRefused("this is not an n8n API key: it holds a space or a character that is not plain ASCII")
    return key


def _whole(value) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _executions(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("method") not in METHODS:
        return None
    listed = raw["method"] == "listed"
    total, failed, period = _whole(raw.get("total")), _whole(raw.get("failed")), raw.get("period_days")
    if total is None or (failed is None and not (listed and raw.get("failed") is None)) or (failed or 0) > total:
        return None
    if type(period) not in (int, float) or not math.isfinite(period) or not 1 <= period <= PERIOD_DAYS:
        return None
    reason = raw.get("estimate_reason")
    if (listed and reason not in ESTIMATE_REASONS) or (not listed and period != PERIOD_DAYS):
        return None
    return {
        "method": raw["method"],
        "total": total,
        "failed": failed,
        "period_days": period,
        "estimate_reason": reason if listed else None,
    }


def checked(section) -> Optional[dict]:
    """The section as the collector writes it, or None. Anything else in it is dropped."""
    if not isinstance(section, dict):
        return None
    refused = section.get("refused") if isinstance(section.get("refused"), dict) else {}

    def why(name: str):
        value = refused.get(name)
        if type(value) is int:
            return value if 100 <= value <= 599 else None
        return value if isinstance(value, str) and value in REFUSED_WORDS else None

    return {
        "executions": _executions(section.get("executions")),
        "active_workflows": _whole(section.get("active_workflows")),
        "users": _whole(section.get("users")),
        "refused": {name: why(name) for name in NUMBERS},
        "whole_instance": section.get("whole_instance") is True,
        "key_unencrypted": section.get("key_unencrypted") is True,
    }


def of(snapshot) -> Optional[dict]:
    """The usage section of a Snapshot, checked; None when the run has none."""
    return checked(snapshot.usage)


def _why(section: dict, name: str) -> str:
    """Why the number `name` could not be read."""
    refused = section["refused"][name]
    if refused == 401:
        return "the API key was refused"
    if refused == 403:
        return USERS_FORBIDDEN if name == "users" else "the API key is not allowed to read it"
    if isinstance(refused, int):
        return f"the n8n API answered with HTTP status {refused}"
    if refused == "limit":
        return LIMIT.get(name, REFUSED_WORDS["limit"])
    return REFUSED_WORDS.get(refused, "it was not read")


def _rounded(value: float, digits: int):
    value = round(value, digits)
    return int(value) if value == int(value) else value


def _not_saved(snapshot, variable: str, key: str) -> bool:
    """True when another source of the run states that this kind of execution is not saved.

    With the precedence of the debug information: a probe that sets the variable wins,
    and the text (its `key`) is read only when none does.
    """
    if stated(snapshot, variable):
        env = effective_env(snapshot)
        return env is not None and not unknown(env, variable) and str(env.get(variable, "")).strip().lower() == "none"
    section = debug_info.of(snapshot)
    return section is not None and section.get(key) == "none"


def numbers(snapshot) -> Optional[dict]:
    """What the usage section says, for the facts and the report; None without a section.

    {name: (value, detail)} for the eight facts, without their `usage.` prefix. A value of
    None is unknown and its detail says why; otherwise the detail says how it was
    obtained. Every detail is fixed text of this module, with numbers. One more entry,
    `estimate_reason`, is (the word, its sentence) for a listed count, else (None, "").
    """
    section = of(snapshot)
    if section is None:
        return None
    partial = not section["whole_instance"]
    result = {}

    executions = section["executions"]
    per_day = rate = reason = None
    if executions is None:
        about = day_about = rate_about = _why(section, "executions")
    elif executions["method"] == "insights":
        about = day_about = rate_about = INSIGHTS_DETAIL
        per_day = _rounded(executions["total"] / PERIOD_DAYS, 2)
        if executions["total"]:
            rate = _rounded(100 * executions["failed"] / executions["total"], 1)
        else:
            day_about = rate_about = INSIGHTS_ZERO
    else:
        reason = "partial_view" if partial else executions["estimate_reason"]
        about = day_about = rate_about = f"estimate, counted from the executions list: {ESTIMATE[reason]}"
        if _not_saved(snapshot, "EXECUTIONS_DATA_SAVE_ON_SUCCESS", "save_success"):
            about = day_about = rate_about = SUCCESS_NOT_SAVED
        else:
            per_day = _rounded(executions["total"] / executions["period_days"], 2)
            if executions["failed"] is None:
                rate_about = NO_OUTCOME
            elif _not_saved(snapshot, "EXECUTIONS_DATA_SAVE_ON_ERROR", "save_error"):
                rate_about = ERROR_NOT_SAVED
            elif not executions["total"]:
                rate_about = NO_EXECUTION
            else:
                rate = _rounded(100 * executions["failed"] / executions["total"], 1)
    result["executions_per_day"] = (per_day, day_about)
    result["failure_rate_percent"] = (rate, rate_about)
    known = per_day is not None
    listed = known and executions["method"] == "listed"
    result["period_days"] = (_rounded(executions["period_days"], 2) if known else None, about)
    result["method"] = (executions["method"] if known else None, about)
    result["estimate"] = (listed if known else None, about)
    result["estimate_reason"] = (reason, ESTIMATE[reason]) if listed else (None, "")

    workflows = section["active_workflows"]
    if workflows is None:
        about = _why(section, "active_workflows")
        result["active_workflows"] = result["active_workflows_partial"] = (None, about)
    else:
        result["active_workflows"] = (workflows, PARTIAL if partial else "")
        result["active_workflows_partial"] = (
            partial,
            "the key could not read a user's detail, which only an owner or an admin may" if partial else "",
        )

    users = section["users"]
    result["users"] = (users, "without pending invitations" if users is not None else _why(section, "users"))
    return result
