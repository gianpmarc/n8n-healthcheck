"""Report: turn a Snapshot and its findings into a Markdown document."""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import debug_info
from .engine import SKIPPED_RULE_ID, score
from .models import AREAS, SEVERITIES, Finding, Snapshot
from .rules._helpers import effective_env

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

# A debug information older than this is still read, with a warning.
DEBUG_INFO_STALE_DAYS = 30


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _one_line(text: str) -> str:
    """Collapse newlines so the text cannot break out of a Markdown bullet."""
    return " ".join(str(text).split())


def _summary(findings: list[Finding], missing: int, skipped: int, sources: int) -> str:
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
            gaps.append(f"only {sources - missing} of {sources} data sources were assessed")
        if skipped:
            gaps.append(f"{_plural(skipped, 'check')} failed to run")
        sentences.append(f"This report is partial: {' and '.join(gaps)}; see \"Not assessed\".")
    return " ".join(sentences)


def _debug_info_summary(section: dict, alone: bool, failed: tuple[str, ...], today: date) -> str:
    """The Summary sentences about the debug information. It is not one of SECTIONS."""
    day = section.get("generated_on")
    sentences = [f"Debug information generated on {day} was read." if day else "Debug information was read, date unknown."]
    if day:
        age = (today - date.fromisoformat(day)).days
        if age > DEBUG_INFO_STALE_DAYS:
            sentences.append(f"It was generated {age} days ago, the instance may have changed.")
    else:
        sentences.append("It may be old, and the instance may have changed.")
    if section.get("cluster") is None:
        sentences.append(
            "The number of replicas and whether every process runs the same version cannot "
            f"be told from it: {debug_info.NO_CLUSTER}."
        )
    if alone:
        sentences.append(
            "The check rests on the debug information only: none of the six data sources was "
            f"{'collected' if failed else 'given'}."
        )
    return " ".join(sentences)


def _not_run_summary(failed: tuple[str, ...], reasons: tuple[str, ...]) -> str:
    names = ", ".join(label.split(" (")[0] for name, label in SECTIONS.items() if name in failed)
    sentences = [f"This health check could not run: none of the requested data sources could be collected ({names})."]
    if reasons:
        sentences.append("Reported: " + "; ".join(_one_line(reason) for reason in reasons) + ".")
    sentences.append(
        "Nothing was assessed, so the absence of findings says nothing about the instance. "
        "Check the connection details and run it again."
    )
    return " ".join(sentences)


def build_report(
    snapshot: Snapshot,
    findings: list[Finding],
    failed: tuple[str, ...] = (),
    could_not_run: tuple[str, ...] | None = None,
    today: date | None = None,
) -> dict:
    """Everything the report says, as data. render_markdown() and the web API both read it.

    `failed` names the Snapshot sections that were requested but came back
    empty, so they are not described as merely absent. `could_not_run` is given
    (the collectors' failure lines, possibly none) when no requested source was
    collected: the summary then says so instead of counting zero findings. The
    CLI stops before the report in that case; the web API still returns one.

    A debug information (snapshot.debug_info) is not one of the six data sources: it adds
    its own sentences to the summary, and a run that has nothing else is still assessed,
    with partial scores. `today` is the day of the run in UTC, for the age of that text.

    Returns {"n8n_version", "summary", "partial", "scores": [{"area", "score"
    (None when nothing was collected), "partial", "findings"}], "not_assessed":
    [{"id", "label", "reason", "detail"}], "findings": [Finding] most severe first}.
    """
    skipped = [f for f in findings if f.rule_id == SKIPPED_RULE_ID]
    # Stable sort, so this also works for findings that did not come from engine.run().
    findings = sorted(
        (f for f in findings if f.rule_id != SKIPPED_RULE_ID),
        key=lambda f: SEVERITIES.index(f.severity),
    )
    missing = [name for name in SECTIONS if getattr(snapshot, name) is None]
    # /metrics answered 404: the endpoint is disabled on the instance, which
    # is a setting, not a source that could not be assessed. Any other failed
    # scrape (or an older snapshot without the flag) stays a missing source.
    metrics_disabled = snapshot.metrics is None and bool((snapshot.health or {}).get("metrics_disabled"))
    if metrics_disabled:
        missing.remove("metrics")
    partial = bool(missing or skipped)

    area_scores = score(findings)
    # Rules are not tied to one data source, so any gap makes every score
    # partial; with no data at all there is no score to show.
    alone = len(missing) == len(SECTIONS) and debug_info.of(snapshot) is not None
    no_data = len(missing) == len(SECTIONS) and not alone
    scores = [
        {
            "area": area,
            "score": None if no_data else area_scores[area],
            "partial": partial,
            "findings": sum(f.area == area for f in findings),
        }
        for area in AREAS
    ]

    not_assessed = []
    if metrics_disabled:
        not_assessed.append(
            {
                "id": "metrics",
                "label": "Prometheus metrics",
                "reason": "disabled",
                "detail": "endpoint disabled on the instance",
            }
        )
    for name in missing:
        reason, details = "not_provided", []
        if name == "env" and effective_env(snapshot) is not None:
            reason = "from_kubernetes"
            details.append("read from the main n8n Deployment in Kubernetes instead")
        if name in failed:
            reason = "failed"
            details.insert(0, "requested but could not be collected")
        not_assessed.append({"id": name, "label": SECTIONS[name], "reason": reason, "detail": " — ".join(details)})
    not_assessed += [
        {"id": f.rule_id, "label": _one_line(f.title), "reason": "rule_skipped", "detail": _one_line(f.evidence)}
        for f in skipped
    ]

    if could_not_run is not None:
        summary = _not_run_summary(failed, could_not_run)
    else:
        summary = _summary(findings, len(missing), len(skipped), len(SECTIONS) - metrics_disabled)
        if debug_info.of(snapshot) is not None:
            today = today or datetime.now(timezone.utc).date()
            summary += " " + _debug_info_summary(debug_info.of(snapshot), alone, failed, today)

    version = snapshot.n8n_version
    if version is None and debug_info.of(snapshot) is not None:
        # The version in the text, under the precedence of the fact `n8n.version`: not
        # when Kubernetes names one. Imported here: profile imports this module.
        from . import profile

        version = profile.text_version(snapshot)

    return {
        "n8n_version": version,
        "summary": summary,
        "partial": partial,
        "scores": scores,
        "not_assessed": not_assessed,
        "findings": findings,
    }


def render_markdown(
    snapshot: Snapshot,
    findings: list[Finding],
    failed: tuple[str, ...] = (),
    could_not_run: tuple[str, ...] | None = None,
    today: date | None = None,
) -> str:
    """Render the report built by build_report() (same arguments) as Markdown."""
    data = build_report(snapshot, findings, failed, could_not_run, today)
    findings = data["findings"]

    title = "# n8n health check report"
    if data["n8n_version"]:
        title += f" — n8n {data['n8n_version']}"
    lines = [title, "", "## Summary", "", data["summary"], ""]

    lines += ["## Scores", "", "| Area | Score | Findings |", "|---|---|---|"]
    for row in data["scores"]:
        if row["score"] is None:
            value = "n/a"
        elif row["partial"]:
            value = f"{row['score']}/100 (partial)"
        else:
            value = f"{row['score']}/100"
        lines.append(f"| {row['area'].capitalize()} | {value} | {row['findings']} |")
    lines.append("")
    if data["partial"]:
        lines += [
            "Scores marked *partial* only reflect the checks that ran. "
            "They say nothing about what is listed under \"Not assessed\".",
            "",
        ]

    lines += ["## Not assessed", ""]
    for entry in data["not_assessed"]:
        if entry["reason"] == "rule_skipped":
            lines.append(f"- {entry['label']} ({entry['detail']})")
        elif entry["detail"]:
            lines.append(f"- {entry['label']} — {entry['detail']}")
        else:
            lines.append(f"- {entry['label']}")
    if not data["not_assessed"]:
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
