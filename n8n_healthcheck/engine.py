"""Engine: run rules against a Snapshot and score the result."""

from __future__ import annotations

from .models import (
    AREAS,
    BASES,
    NOT_ASSESSED_REASONS,
    OUTCOMES,
    SEVERITIES,
    SOURCE_IDS,
    Check,
    Finding,
    Outcome,
    Rule,
    Snapshot,
)

# Points subtracted from an area's score for each finding of that severity.
PENALTY = {"critical": 40, "warning": 15, "info": 5}

# rule_id of the placeholder Finding recorded when a rule raises. The report
# lists these under "Not assessed"; they never count towards a score.
SKIPPED_RULE_ID = "ENGINE-SKIPPED"


def assess(snapshot: Snapshot, rules: list[Rule]) -> list[Check]:
    """Call every rule and return what each one states, in rule order.

    A rule that raises, or returns something that is neither a valid Finding nor a valid
    Outcome, does not stop the run: its Check is `not_assessed` for the reason
    `rule_error`, and carries an info Finding with rule_id SKIPPED_RULE_ID so the reader
    of the report knows the check did not happen. A declared rule (one with `info`) must
    state an outcome; a bare callable may still return None, which is left out by run().
    """
    checks = []
    for rule in rules:
        info = getattr(rule, "info", None)
        try:
            result = rule(snapshot)
            if isinstance(result, Outcome):
                if (
                    result.outcome not in OUTCOMES[1:]
                    or (result.outcome == "passed") != (result.basis in BASES)
                    or (result.outcome == "not_assessed") != (result.reason in NOT_ASSESSED_REASONS[:-1])
                    or not all(need in SOURCE_IDS for need in result.needs)
                ):
                    raise ValueError("returned an Outcome that is not valid")
                check = Check(info, result.outcome, None, result.basis, result.reason, tuple(result.needs))
            elif result is None and info is None:
                check = Check(None, "not_applicable")
            else:
                if not isinstance(result, Finding):
                    raise TypeError(f"returned {type(result).__name__}, not a Finding")
                if result.severity not in SEVERITIES or result.area not in AREAS:
                    raise ValueError(f"unknown severity {result.severity!r} or area {result.area!r}")
                check = Check(info, "finding", result)
        except Exception as exc:
            name = getattr(rule, "__name__", repr(rule))
            placeholder = Finding(
                rule_id=SKIPPED_RULE_ID,
                area="maintainability",
                severity="info",
                title=f"Rule {name} could not run",
                evidence=f"{type(exc).__name__}: {exc}",
                impact="This check was skipped, so the risk it covers was not assessed.",
                recommendation="Report this as a bug in n8n-healthcheck.",
            )
            check = Check(info, "not_assessed", placeholder, reason="rule_error")
        checks.append(check)
    return checks


def findings_of(checks: list[Check]) -> list[Finding]:
    """The findings of assess(), most severe first, with the placeholder of each rule that raised."""
    findings = [check.finding for check in checks if check.finding is not None]
    # sorted() is stable: within a severity, findings keep rule order.
    return sorted(findings, key=lambda f: SEVERITIES.index(f.severity))


def run(snapshot: Snapshot, rules: list[Rule]) -> list[Finding]:
    """Call every rule and return its findings, most severe first: findings_of(assess())."""
    return findings_of(assess(snapshot, rules))


def score(findings: list[Finding]) -> dict[str, int]:
    """Score each area from 0 to 100.

    Every area starts at 100 and loses PENALTY[severity] points per finding
    (critical 40, warning 15, info 5), never going below 0. Skipped rules are
    ignored: a check that did not run says nothing about the instance.
    """
    scores = {area: 100 for area in AREAS}
    for finding in findings:
        if finding.rule_id != SKIPPED_RULE_ID:
            scores[finding.area] = max(0, scores[finding.area] - PENALTY[finding.severity])
    return scores
