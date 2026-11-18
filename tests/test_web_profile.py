"""The profile in the web page: what the browser does to it must still be a valid profile.

The page (n8n_healthcheck/web/static/app.js) blanks withheld entries and adds the reason
and the reference; it never computes a fact. There is no JavaScript test runner in this
project, so the page's own code is checked in two ways: its constants are read from the
file and compared with profile.py, and, where Node.js is installed, the block marked
"profile-edit" is run and its result compared with profile.py's.
"""

import json
import os
import re
import shutil
import subprocess
import unittest

from n8n_healthcheck import profile
from n8n_healthcheck.models import Finding, Snapshot
from n8n_healthcheck.web import server as web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

CONTAINER = {
    "name": "n8n",
    "image": "n8nio/n8n:2.40.1",
    "command": [],
    "env": {"EXECUTIONS_MODE": "queue", "N8N_HOST": "n8n.example.com"},
    "requests": {"cpu": "200m", "memory": "512Mi"},
    "limits": {"memory": "1Gi"},
    "liveness_probe": True,
    "readiness_probe": True,
    "env_complete": True,
}
SNAPSHOT = Snapshot(
    health={"healthz": True, "readiness": True},
    metrics={},
    env={"EXECUTIONS_MODE": "queue", "EXECUTIONS_DATA_PRUNE": "true", "WEBHOOK_URL": "https://n8n.example.com/"},
    redis={"version": "7.2.4", "waiting_jobs": 3, "active_jobs": 1, "failed_jobs": 0, "maxmemory_bytes": 0, "maxmemory_policy": "noeviction"},
    kubernetes={"deployments": [{"name": "main", "replicas": 2, "ready_replicas": 2, "containers": [CONTAINER]}]},
    n8n_version="2.40.1",
)
FINDING = Finding(
    rule_id="REL-002", area="reliability", severity="warning", title="A title", evidence="e", impact="i", recommendation="r"
)

# A fact, a setting of each scope and the check: every kind of entry the page can blank.
WITHHELD = ["n8n.version", "resources.main", "setting:instance:EXECUTIONS_MODE", "setting:main:N8N_HOST", "check"]

# References the page must store, or refuse, exactly as profile.check_reference does.
REFERENCES = [
    "",
    "upgrade to 2.50, change 1234",
    "  padded  ",
    "\u00a0\u2003wide spaces\u3000",
    "trailing next-line\u0085",
    "\ufeffbyte-order mark is kept",
    "x" * 200,
    "x" * 201,
    " " + "x" * 200 + " ",
    "\U0001f600" * 200,  # 200 characters, 400 UTF-16 units
    "\U0001f600" * 201,
    "two\nlines",
    "a\ttab",
    "bell\u0007",
    "delete\u007f",
    "\u001fedge\u001c",  # stripped by Python, so it is fine
]


def built():
    return profile.build_profile(SNAPSHOT, [FINDING], ("postgres",))


def js_constant(name):
    """The array or object literal assigned to a constant of app.js, as Python data."""
    match = re.search(rf"const {name} = (\[.*?\]|\{{.*?\}});", APP_JS, re.S)
    literal = re.sub(r",\s*([\]}])", r"\1", match.group(1))  # trailing commas
    literal = re.sub(r"^(\s*)(\w+): ", r'\1"\2": ', literal, flags=re.M)  # bare keys
    return json.loads(literal)


class PageConstantsTest(unittest.TestCase):
    """The page validates an opened file with its own copy of these; they must not drift."""

    def test_constants_equal_profile_py(self):
        self.assertIn(f'const PROFILE_FORMAT = "{profile.FORMAT}";', APP_JS)
        self.assertIn(f"const PROFILE_VERSION = {profile.FORMAT_VERSION};", APP_JS)
        self.assertIn(f"const REFERENCE_MAX = {profile.REFERENCE_MAX};", APP_JS)
        self.assertEqual(set(js_constant("FORMAT_1_FACT_IDS")), set(profile.FORMAT_1_FACT_IDS))
        self.assertEqual(tuple(js_constant("PROFILE_REASONS")), profile.REASONS)
        self.assertEqual(tuple(js_constant("SOURCE_STATES")), profile.SOURCE_STATES)
        self.assertEqual(tuple(js_constant("PROVENANCES")), profile.PROVENANCES)
        self.assertEqual(tuple(js_constant("SETTING_STATES")), profile.SETTING_STATES)

    def test_page_names_every_source_of_a_profile(self):
        for section in built()["sources"]:
            self.assertRegex(APP_JS, rf"\n    {section}: \{{ label: ")

    def test_text_of_a_file_is_never_inserted_as_html(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, APP_JS)

    def test_page_stores_nothing_and_loads_nothing_else(self):
        for store in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            self.assertNotIn(store, APP_JS)
        # The profile is read and written in the browser: the only requests are the two that existed.
        self.assertEqual(sorted(set(re.findall(r'fetch\("([^"]+)"', APP_JS))), ["/api/meta", "/api/run"])
        self.assertEqual(web.CONTENT_SECURITY_POLICY.split(";")[0], "default-src 'none'")
        self.assertIn("script-src 'self'; style-src 'self'", INDEX_HTML)


def run_page_code(calls):
    """Run the "profile-edit" block of app.js under Node.js; `calls` is JavaScript giving the result."""
    block = re.search(r"// profile-edit:start.*?\n(.*?)// profile-edit:end", APP_JS, re.S).group(1)
    script = f'"use strict";\n{block}\nconst input = JSON.parse(require("fs").readFileSync(0, "utf8"));\nprocess.stdout.write(JSON.stringify({calls}));\n'
    payload = {"profile": built(), "withheld": WITHHELD, "references": REFERENCES}
    done = subprocess.run(
        [NODE, "-e", script], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )  # fmt: skip
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


@unittest.skipUnless(NODE, "Node.js is not installed: the page's own code cannot be run")
class PageCodeTest(unittest.TestCase):
    def test_blanking_in_the_page_equals_apply_withholding(self):
        result = run_page_code('withholdEntries(input.profile, new Set(input.withheld), "pre_upgrade", "  change 1234 ")')
        expected = profile.apply_withholding(built(), WITHHELD)
        expected["run"] = {"reason": "pre_upgrade", "reference": "change 1234"}
        expected["created_at"] = result["created_at"]
        self.assertEqual(result, expected)
        self.assertIs(profile.validate(result), result)
        # What the page saves is the text profile.to_json() writes.
        saved = run_page_code('JSON.stringify(withholdEntries(input.profile, new Set(input.withheld), "pre_upgrade", "change 1234"), null, 2) + "\\n"')
        self.assertEqual(saved, profile.to_json(expected))

    def test_everything_withheld_is_still_a_valid_profile(self):
        everything = [f["id"] for f in built()["facts"]] + [profile.setting_id(s) for s in built()["settings"]] + ["check"]
        self.assertGreater(len(built()["settings"]), 3)
        result = run_page_code(
            f'withholdEntries(input.profile, new Set({json.dumps(everything)}), "other", "{"x" * 200}")'
        )
        profile.validate(result)
        self.assertEqual({f["provenance"] for f in result["facts"]}, {"withheld"})
        self.assertEqual({s["state"] for s in result["settings"]}, {"withheld"})
        self.assertEqual(result["check"], {"withheld": True})
        self.assertIn("Withheld.", profile.render_markdown(result))

    def test_nothing_withheld_changes_only_the_run(self):
        result = run_page_code('withholdEntries(input.profile, new Set(), "baseline", "")')
        self.assertEqual({**result, "created_at": ""}, {**built(), "created_at": ""})

    def test_reference_rules_equal_check_reference(self):
        answers = run_page_code(
            "input.references.map((text) => { const kept = stripReference(text); return [kept, referenceProblem(kept) === null]; })"
        )
        for text, (kept, accepted) in zip(REFERENCES, answers):
            try:
                expected = profile.check_reference(text)
            except profile.ProfileError:
                expected = None
            with self.subTest(reference=text[:20]):
                self.assertEqual(accepted, expected is not None)
                if accepted:
                    self.assertEqual(kept, expected)

    def test_half_a_surrogate_pair_is_refused_like_validate_does(self):
        self.assertFalse(run_page_code(r'referenceProblem("lone \ud83d half") === null'))
        broken = built()
        broken["run"]["reference"] = "lone \ud83d half"
        with self.assertRaises(profile.ProfileError):
            profile.validate(broken)


if __name__ == "__main__":
    unittest.main()
