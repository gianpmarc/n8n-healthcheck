"""Engine: run rules against a Snapshot and score the result."""

from __future__ import annotations

from .models import AREAS, SEVERITIES, Finding, Rule, Snapshot

# Points subtracted from an area's score for each finding of that severity.
PENALTY = {"critical": 40, "warning": 15, "info": 5}

# rule_id of the placeholder Finding recorded when a rule raises. The report
# lists these under "Not assessed"; they never count towards a score.
SKIPPED_RULE_ID = "ENGINE-SKIPPED"


def run(snapshot: Snapshot, rules: list[Rule]) -> list[Finding]:
    """Call every rule and return its findings, most severe first.

    A rule that raises does not stop the run: it is recorded as an info
    Finding with rule_id SKIPPED_RULE_ID so the reader knows the check did
    not happen.
    """
    findings = []
    for rule in rules:
        try:
            finding = rule(snapshot)
        except Exception as exc:
            name = getattr(rule, "__name__", repr(rule))
            finding = Finding(
                rule_id=SKIPPED_RULE_ID,
                area="maintainability",
                severity="info",
                title=f"Rule {name} could not run",
                evidence=f"{type(exc).__name__}: {exc}",
                impact="This check was skipped, so the risk it covers was not assessed.",
                recommendation="Report this as a bug in n8n-healthcheck.",
            )
        if finding is not None:
            findings.append(finding)
    # sorted() is stable: within a severity, findings keep rule order.
    return sorted(findings, key=lambda f: SEVERITIES.index(f.severity))


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
