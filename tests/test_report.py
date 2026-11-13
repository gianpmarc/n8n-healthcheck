import dataclasses
import unittest

from n8n_healthcheck import engine
from n8n_healthcheck.models import Finding, Snapshot
from n8n_healthcheck.report import SECTIONS, render_markdown

FULL = Snapshot(health={}, metrics={}, env={}, postgres={}, redis={}, kubernetes={}, n8n_version="1.70.0")

CRITICAL = Finding(
    rule_id="REL-001",
    area="reliability",
    severity="critical",
    title="Execution data is never pruned",
    evidence="EXECUTIONS_DATA_PRUNE=false, 4200000 executions stored",
    impact="The database grows until the disk is full.",
    recommendation="Set EXECUTIONS_DATA_PRUNE=true.",
)
WARNING = Finding(
    rule_id="SEC-001",
    area="security",
    severity="warning",
    title="Metrics endpoint is public",
    evidence="/metrics answered without authentication",
    impact="Anyone can read internal metrics.",
    recommendation="Restrict /metrics at the ingress.",
)


def section(markdown, heading):
    """Text between '## heading' and the next '## ' heading."""
    return markdown.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]


class RenderTest(unittest.TestCase):
    def test_title_includes_version_when_known(self):
        self.assertTrue(render_markdown(FULL, []).startswith("# n8n health check report — n8n 1.70.0\n"))
        self.assertTrue(render_markdown(Snapshot(), []).startswith("# n8n health check report\n"))

    def test_sections_appear_in_order(self):
        markdown = render_markdown(FULL, [CRITICAL])
        positions = [markdown.index(h) for h in ("## Summary", "## Scores", "## Not assessed", "## Findings")]
        self.assertEqual(positions, sorted(positions))

    def test_summary_counts_and_names_the_most_urgent_fix(self):
        summary = section(render_markdown(FULL, [WARNING, CRITICAL]), "Summary")
        self.assertIn("1 critical issue, 1 warning and 0 informational notes", summary)
        self.assertIn("The most urgent fix: Execution data is never pruned", summary)
        self.assertIn("Set EXECUTIONS_DATA_PRUNE=true.", summary)

    def test_score_table_reflects_findings(self):
        scores = section(render_markdown(FULL, [CRITICAL, WARNING]), "Scores")
        self.assertIn("| Reliability | 60/100 | 1 |", scores)
        self.assertIn("| Security | 85/100 | 1 |", scores)
        self.assertIn("| Scalability | 100/100 | 0 |", scores)

    def test_not_assessed_names_each_missing_section(self):
        markdown = render_markdown(Snapshot(env={}, postgres={}), [])
        not_assessed = section(markdown, "Not assessed")
        for name in ("health", "metrics", "redis", "kubernetes"):
            self.assertIn(SECTIONS[name], not_assessed)
        for name in ("env", "postgres"):
            self.assertNotIn(SECTIONS[name], not_assessed)
        self.assertIn("only 2 of 6 data sources were assessed", section(markdown, "Summary"))

    def test_one_section_snapshot_shows_no_clean_score(self):
        markdown = render_markdown(Snapshot(env={}), [WARNING])
        scores = section(markdown, "Scores")
        for line in scores.splitlines():
            if "/100" in line:
                self.assertIn("(partial)", line)
        self.assertIn("| Security | 85/100 (partial) | 1 |", scores)
        self.assertIn("| Scalability | 100/100 (partial) | 0 |", scores)

    def test_partial_run_without_findings_does_not_claim_all_clear(self):
        summary = section(render_markdown(Snapshot(env={}), []), "Summary")
        self.assertNotIn("Nothing needs attention", summary)
        self.assertIn("not a clean bill of health", summary)
        self.assertIn("only 1 of 6 data sources were assessed", summary)

    def test_failed_sources_are_worded_as_requested_but_not_collected(self):
        snapshot = Snapshot(health={}, env={})
        not_assessed = section(render_markdown(snapshot, [], failed=("postgres",)), "Not assessed")
        self.assertIn(f"- {SECTIONS['postgres']} — requested but could not be collected", not_assessed)
        self.assertIn(f"- {SECTIONS['redis']}\n", not_assessed + "\n")

    def test_newlines_in_finding_text_stay_inside_the_bullet(self):
        multiline = dataclasses.replace(CRITICAL, evidence="line one\n\n## Fake heading\nline three")
        findings = section(render_markdown(FULL, [multiline]), "Findings")
        self.assertIn("- **Evidence:** line one ## Fake heading line three\n", findings)

    def test_empty_snapshot_shows_no_perfect_scores(self):
        scores = section(render_markdown(Snapshot(), []), "Scores")
        self.assertNotIn("100/100", scores)
        self.assertIn("| Security | n/a | 0 |", scores)

    def test_full_snapshot_has_nothing_unassessed(self):
        markdown = render_markdown(FULL, [])
        self.assertIn("Every data source was collected", section(markdown, "Not assessed"))
        self.assertNotIn("partial", markdown)
        self.assertIn("Nothing needs attention right now.", section(markdown, "Summary"))
        self.assertIn("No findings.", section(markdown, "Findings"))

    def test_sections_cover_every_snapshot_field(self):
        fields = {f.name for f in dataclasses.fields(Snapshot)} - {"n8n_version"}
        self.assertEqual(set(SECTIONS), fields)

    def test_findings_are_grouped_by_severity_with_all_fields(self):
        findings = section(render_markdown(FULL, [WARNING, CRITICAL]), "Findings")
        self.assertLess(findings.index("### Critical"), findings.index("### Warnings"))
        self.assertNotIn("### Info", findings)
        self.assertIn("#### REL-001 — Execution data is never pruned", findings)
        self.assertIn("- **Evidence:** EXECUTIONS_DATA_PRUNE=false, 4200000 executions stored", findings)
        self.assertIn("- **Impact:** The database grows until the disk is full.", findings)
        self.assertIn("- **Recommendation:** Set EXECUTIONS_DATA_PRUNE=true.", findings)

    def test_skipped_rule_is_listed_as_not_assessed_not_as_a_finding(self):
        def broken_rule(snapshot):
            raise KeyError("version")

        markdown = render_markdown(FULL, engine.run(FULL, [broken_rule]))
        self.assertIn("Rule broken_rule could not run (KeyError: 'version')", section(markdown, "Not assessed"))
        self.assertIn("No findings.", section(markdown, "Findings"))
        self.assertIn("0 informational notes", section(markdown, "Summary"))
        summary = section(markdown, "Summary")
        self.assertIn("This report is partial: 1 check failed to run", summary)
        self.assertNotIn("data sources", summary)
        self.assertNotIn("Nothing needs attention", summary)
        self.assertIn("| Security | 100/100 (partial) | 0 |", section(markdown, "Scores"))


if __name__ == "__main__":
    unittest.main()
