import unittest

from n8n_healthcheck import engine
from n8n_healthcheck.models import AREAS, Finding, Snapshot


def finding(severity, area="reliability", rule_id="TEST-001"):
    return Finding(
        rule_id=rule_id,
        area=area,
        severity=severity,
        title=f"{severity} title",
        evidence="evidence",
        impact="impact",
        recommendation="recommendation",
    )


def rule_returning(result):
    return lambda snapshot: result


class RunTest(unittest.TestCase):
    def test_sorts_critical_then_warning_then_info(self):
        rules = [rule_returning(finding(s)) for s in ("info", "critical", "warning", "critical")]
        severities = [f.severity for f in engine.run(Snapshot(), rules)]
        self.assertEqual(severities, ["critical", "critical", "warning", "info"])

    def test_keeps_rule_order_within_a_severity(self):
        rules = [rule_returning(finding("warning", rule_id=i)) for i in ("B", "A", "C")]
        self.assertEqual([f.rule_id for f in engine.run(Snapshot(), rules)], ["B", "A", "C"])

    def test_drops_rules_that_return_none(self):
        self.assertEqual(engine.run(Snapshot(), [rule_returning(None)]), [])

    def test_passes_the_snapshot_to_each_rule(self):
        snapshot = Snapshot(env={"EXECUTIONS_MODE": "queue"})
        seen = []
        engine.run(snapshot, [lambda s: seen.append(s)])
        self.assertEqual(seen, [snapshot])

    def test_raising_rule_is_recorded_and_the_run_continues(self):
        def broken_rule(snapshot):
            raise KeyError("version")

        findings = engine.run(Snapshot(), [broken_rule, rule_returning(finding("critical"))])

        self.assertEqual([f.severity for f in findings], ["critical", "info"])
        skipped = findings[1]
        self.assertEqual(skipped.rule_id, engine.SKIPPED_RULE_ID)
        self.assertIn("broken_rule", skipped.title)
        self.assertIn("KeyError", skipped.evidence)


class ScoreTest(unittest.TestCase):
    def test_no_findings_scores_100_for_every_area(self):
        self.assertEqual(engine.score([]), {area: 100 for area in AREAS})

    def test_subtracts_per_severity_within_the_area_only(self):
        scores = engine.score(
            [
                finding("critical", "security"),
                finding("warning", "security"),
                finding("info", "performance"),
            ]
        )
        self.assertEqual(scores["security"], 100 - 40 - 15)
        self.assertEqual(scores["performance"], 100 - 5)
        self.assertEqual(scores["scalability"], 100)

    def test_floors_at_zero(self):
        self.assertEqual(engine.score([finding("critical")] * 4)["reliability"], 0)

    def test_skipped_rules_do_not_lower_a_score(self):
        def broken_rule(snapshot):
            raise RuntimeError("boom")

        findings = engine.run(Snapshot(), [broken_rule])
        self.assertEqual(engine.score(findings), {area: 100 for area in AREAS})


if __name__ == "__main__":
    unittest.main()
