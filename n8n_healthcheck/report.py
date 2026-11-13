"""Report: turn a Snapshot and its findings into a Markdown document."""

from __future__ import annotations

from .engine import SKIPPED_RULE_ID, score
from .models import AREAS, SEVERITIES, Finding, Snapshot

# Snapshot section -> how to describe it to the reader.
SECTIONS = {
    "health": "Health endpoints (`--url`)",
    "metrics": "Prometheus metrics (`--url`, needs `N8N_METRICS=true`)",
    "env": "n8n environment variables (`--env-file`)",
    "postgres": "Postgres (`--postgres-dsn`)",
    "redis": "Redis (`--redis-url`)",
    "kubernetes": "Kubernetes (`--kube-namespace`)",
}

SEVERITY_HEADINGS = {"critical": "Critical", "warning": "Warnings", "info": "Info"}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _one_line(text: str) -> str:
    """Collapse newlines so the text cannot break out of a Markdown bullet."""
    return " ".join(str(text).split())


def _summary(findings: list[Finding], missing: int, skipped: int) -> str:
    partial = bool(missing or skipped)
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
    elif partial:
        sentences.append("That covers only the part that could be checked, so it is not a clean bill of health.")
    else:
        sentences.append("Nothing needs attention right now.")
    if findings:
        top = findings[0]
        sentences.append(f"The most urgent fix: {_one_line(top.title)} — {_one_line(top.recommendation)}")
    if partial:
        gaps = []
        if missing:
            gaps.append(f"only {len(SECTIONS) - missing} of {len(SECTIONS)} data sources were assessed")
        if skipped:
            gaps.append(f"{_plural(skipped, 'check')} failed to run")
        sentences.append(f"This report is partial: {' and '.join(gaps)}; see \"Not assessed\".")
    return " ".join(sentences)


def render_markdown(snapshot: Snapshot, findings: list[Finding], failed: tuple[str, ...] = ()) -> str:
    """Render the report.

    `failed` names the Snapshot sections that were requested on the command
    line but came back empty, so they are not described as merely absent.
    """
    skipped = [f for f in findings if f.rule_id == SKIPPED_RULE_ID]
    # Stable sort, so this also works for findings that did not come from engine.run().
    findings = sorted(
        (f for f in findings if f.rule_id != SKIPPED_RULE_ID),
        key=lambda f: SEVERITIES.index(f.severity),
    )
    missing = [name for name in SECTIONS if getattr(snapshot, name) is None]
    partial = bool(missing or skipped)

    title = "# n8n health check report"
    if snapshot.n8n_version:
        title += f" — n8n {snapshot.n8n_version}"
    lines = [title, "", "## Summary", "", _summary(findings, len(missing), len(skipped)), ""]

    lines += ["## Scores", "", "| Area | Score | Findings |", "|---|---|---|"]
    scores = score(findings)
    for area in AREAS:
        count = sum(f.area == area for f in findings)
        # Rules are not tied to one data source, so any gap makes every score
        # partial; with no data at all there is no score to show.
        if len(missing) == len(SECTIONS):
            value = "n/a"
        elif partial:
            value = f"{scores[area]}/100 (partial)"
        else:
            value = f"{scores[area]}/100"
        lines.append(f"| {area.capitalize()} | {value} | {count} |")
    lines.append("")
    if partial:
        lines += [
            "Scores marked *partial* only reflect the checks that ran. "
            "They say nothing about what is listed under \"Not assessed\".",
            "",
        ]

    lines += ["## Not assessed", ""]
    if partial:
        for name in missing:
            suffix = " — requested but could not be collected" if name in failed else ""
            lines.append(f"- {SECTIONS[name]}{suffix}")
        lines += [f"- {_one_line(f.title)} ({_one_line(f.evidence)})" for f in skipped]
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
                f"#### {f.rule_id} — {_one_line(f.title)}",
                "",
                f"*Area: {f.area}*",
                "",
                f"- **Evidence:** {_one_line(f.evidence)}",
                f"- **Impact:** {_one_line(f.impact)}",
                f"- **Recommendation:** {_one_line(f.recommendation)}",
                "",
            ]
    return "\n".join(lines)
