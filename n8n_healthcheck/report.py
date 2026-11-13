"""Report: turn a Snapshot and its findings into a Markdown document."""

from __future__ import annotations

from .engine import SKIPPED_RULE_ID, score
from .models import AREAS, SEVERITIES, Finding, Snapshot

# Snapshot section -> how to describe it to the reader.
SECTIONS = {
    "health": "Health endpoints (`--url`)",
    "metrics": "Prometheus metrics (`--url`)",
    "env": "n8n environment variables (`--env-file`)",
    "postgres": "Postgres (`--postgres-dsn`)",
    "redis": "Redis (`--redis-url`)",
    "kubernetes": "Kubernetes (`--kube-namespace`)",
}

SEVERITY_HEADINGS = {"critical": "Critical", "warning": "Warnings", "info": "Info"}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _summary(findings: list[Finding], missing: list[str], skipped: int) -> str:
    counts = {s: sum(f.severity == s for f in findings) for s in SEVERITIES}
    sentences = [
        "This health check found "
        f"{_plural(counts['critical'], 'critical issue')}, "
        f"{_plural(counts['warning'], 'warning')} and "
        f"{_plural(counts['info'], 'informational note')}."
    ]
    if counts["critical"]:
        sentences.append(
            "Critical issues can cause outages or data loss and should be fixed "
            "before the instance takes more load."
        )
    elif counts["warning"]:
        sentences.append(
            "Nothing needs emergency action, but the warnings will become "
            "problems as usage grows."
        )
    else:
        sentences.append("Nothing that was checked needs attention right now.")
    if findings:
        top = findings[0]
        sentences.append(f"The most urgent fix: {top.title} — {top.recommendation}")
    if missing or skipped:
        sentences.append(
            f"{len(missing)} of {len(SECTIONS)} data sources were not provided"
            + (f" and {_plural(skipped, 'check')} failed to run" if skipped else "")
            + ", so this report is partial: see \"Not assessed\"."
        )
    return " ".join(sentences)


def render_markdown(snapshot: Snapshot, findings: list[Finding]) -> str:
    skipped = [f for f in findings if f.rule_id == SKIPPED_RULE_ID]
    # Stable sort, so this also works for findings that did not come from engine.run().
    findings = sorted(
        (f for f in findings if f.rule_id != SKIPPED_RULE_ID),
        key=lambda f: SEVERITIES.index(f.severity),
    )
    missing = [label for name, label in SECTIONS.items() if getattr(snapshot, name) is None]
    nothing_collected = len(missing) == len(SECTIONS)

    title = "# n8n health check report"
    if snapshot.n8n_version:
        title += f" — n8n {snapshot.n8n_version}"
    lines = [title, "", "## Summary", "", _summary(findings, missing, len(skipped)), ""]

    lines += ["## Scores", "", "| Area | Score | Findings |", "|---|---|---|"]
    scores = score(findings)
    for area in AREAS:
        count = sum(f.area == area for f in findings)
        # With no data at all, 100/100 would be a lie.
        value = "n/a" if nothing_collected else f"{scores[area]}/100"
        lines.append(f"| {area.capitalize()} | {value} | {count} |")
    lines.append("")
    if missing:
        lines += [
            "Scores only reflect the data sources that were collected. "
            "A high score does not cover anything listed under \"Not assessed\".",
            "",
        ]

    lines += ["## Not assessed", ""]
    if missing or skipped:
        lines += [f"- {label}" for label in missing]
        lines += [f"- {f.title} ({f.evidence})" for f in skipped]
    else:
        lines.append("Every data source was collected and every check ran.")
    lines.append("")

    lines += ["## Findings", ""]
    if not findings:
        lines += ["No findings.", ""]
    for severity in SEVERITIES:
        group = [f for f in findings if f.severity == severity]
        if not group:
            continue
        lines += [f"### {SEVERITY_HEADINGS[severity]}", ""]
        for f in group:
            lines += [
                f"#### {f.rule_id} — {f.title}",
                "",
                f"*Area: {f.area}*",
                "",
                f"- **Evidence:** {f.evidence}",
                f"- **Impact:** {f.impact}",
                f"- **Recommendation:** {f.recommendation}",
                "",
            ]
    return "\n".join(lines)
