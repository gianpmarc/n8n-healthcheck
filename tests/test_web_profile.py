"""The profile in the web page: what the browser does to it must still be a valid profile.

The page (n8n_healthcheck/web/static/app.js) blanks withheld entries and adds the reason
and the reference; it never computes a fact. There is no JavaScript test runner in this
project, so the page's own code is checked in two ways:

- PageSourceTest reads app.js as text. It always runs. It pins the constants, the few
  lines that blank an entry, the reference rules and the wiring between the controls
  and those lines, so that a change to any of them has to be made here as well.
- PageCodeTest runs the part of app.js between "profile-core:start" and
  "profile-core:end" under Node.js and compares what it returns with profile.py.
- PageBehaviourTest runs the whole of app.js under Node.js against a stand-in for the
  browser's document (tests/page_harness.js), presses its controls and reads what it
  shows and saves.

The last two are skipped where Node.js is not installed; the skip message says what is
then unchecked. Both run the page twice where they can: with JSON.rawJSON, which keeps
numbers as they were written, and with it removed, as an older browser has it.
"""

import copy
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from n8n_healthcheck import cli, profile, report
from n8n_healthcheck.models import Finding, Snapshot
from n8n_healthcheck.web import server as web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
CORE = re.search(r"// profile-core:start\n(.*?)// profile-core:end", APP_JS, re.S).group(1)
HAS = re.search(r"\n  const has = .*?;\n", APP_JS).group(0)  # the one helper the core takes from outside
HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_harness.js")
NODE = shutil.which("node")
NODE_ENV = {"PATH": os.environ.get("PATH", "")}
# Whether this Node.js can keep a number as the text it was written in (Node.js 22 and later).
EXACT = bool(NODE) and subprocess.run(
    [NODE, "-p", "typeof JSON.rawJSON"], capture_output=True, text=True, env=NODE_ENV, timeout=30
).stdout.strip() == "function"
NO_NODE = (
    "Node.js is not installed, so the page's own code was NOT run. Unchecked on this machine: that the page "
    "refuses every file profile.validate refuses and no other; that the file it saves is the one profile.py "
    "writes; that its controls do what they say (withhold, withhold all, download, open, a new run). Checked "
    "as text all the same (PageSourceTest): the constants, the lines that blank an entry, the reference rules, "
    "the lines that wire the controls."
)
needs_exact = unittest.skipUnless(
    EXACT,
    "this Node.js has no JSON.rawJSON (it came with Node.js 22), so the path that keeps numbers as written was "
    "NOT run: 0.0 saved as 0.0, 1.0 refused as a whole number. The path without it was run.",
)
LONE = chr(0xD83D)  # half of a surrogate pair

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
    # An oldest execution a few minutes old is the float 0.0, which JavaScript would write as 0.
    postgres={"version": "16.4", "execution_count": 2**60, "oldest_execution_age_days": 0.001, "max_connections": 100},
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
    chr(0xA0) + chr(0x2003) + "wide spaces" + chr(0x3000),
    "trailing next-line" + chr(0x85),
    chr(0xFEFF) + "byte-order mark is kept",
    "x" * 200,
    "x" * 201,
    " " + "x" * 200 + " ",
    chr(0x1F600) * 200,  # 200 characters, 400 UTF-16 units
    chr(0x1F600) * 201,
    "two\nlines",
    "a\ttab",
    "bell" + chr(7),
    "delete" + chr(0x7F),
    chr(0x1F) + "edge" + chr(0x1C),  # stripped by Python, so it is fine
]


def built():
    return profile.build_profile(SNAPSHOT, [FINDING], ("postgres",))


def js_constant(name):
    """The array or object literal assigned to a constant of app.js, as Python data."""
    match = re.search(rf"const {name} = (\[.*?\]|\{{.*?\}});", APP_JS, re.S)
    literal = re.sub(r",\s*([\]}])", r"\1", match.group(1))  # trailing commas
    literal = re.sub(r"([{,]\s*)(\w+): ", r'\1"\2": ', literal)  # bare keys
    return json.loads(literal)


def js_function(name):
    """The source of a function declared at the top level of app.js's wrapper."""
    return re.search(rf"\n  (?:async )?function {name}\(.*?\n  \}}\n", APP_JS, re.S).group(0)


class PageSourceTest(unittest.TestCase):
    """Always runs: the page's source, read as text."""

    # --- what the page knows about the format ---------------------------------------

    def test_constants_equal_profile_py(self):
        self.assertIn(f'const PROFILE_FORMAT = "{profile.FORMAT}";', APP_JS)
        self.assertIn(f"const PROFILE_VERSION = {profile.FORMAT_VERSION};", APP_JS)
        self.assertIn(f"const REFERENCE_MAX = {profile.REFERENCE_MAX};", APP_JS)
        self.assertEqual(set(js_constant("FORMAT_1_FACT_IDS")), set(profile.FORMAT_1_FACT_IDS))
        self.assertEqual(tuple(js_constant("PROFILE_REASONS")), profile.REASONS)
        self.assertEqual(tuple(js_constant("SOURCE_STATES")), profile.SOURCE_STATES)
        self.assertEqual(tuple(js_constant("PROVENANCES")), profile.PROVENANCES)
        self.assertEqual(tuple(js_constant("SETTING_STATES")), profile.SETTING_STATES)
        self.assertEqual(tuple(js_constant("PROFILE_SOURCES")), tuple(report.SECTIONS))

    def test_page_names_every_source_of_a_profile(self):
        for section in built()["sources"]:
            self.assertRegex(APP_JS, rf"\n    {section}: \{{ label: ")

    # --- blanking: few enough lines to be checked as text ---------------------------

    def test_a_withheld_entry_is_left_with_what_apply_withholding_leaves(self):
        blanked = profile.apply_withholding(built(), WITHHELD)
        fact = next(f for f in blanked["facts"] if f["id"] == "n8n.version")
        setting = next(s for s in blanked["settings"] if profile.setting_id(s) == "setting:main:N8N_HOST")
        self.assertEqual(js_constant("WITHHELD_FACT"), {k: v for k, v in fact.items() if k != "id"})
        self.assertEqual(js_constant("WITHHELD_SETTING"), {k: v for k, v in setting.items() if k not in ("scope", "name")})
        self.assertEqual(js_constant("WITHHELD_CHECK"), blanked["check"])

    def test_blanking_is_these_lines_and_nothing_else(self):
        body = js_function("withholdEntries")
        for line in (
            "      ...profile,",
            "      run: { reason, reference: stripReference(reference) },",
            "      facts: profile.facts.map((fact) => (ids.has(fact.id) ? { ...fact, ...WITHHELD_FACT } : fact)),",
            "      settings: profile.settings.map((setting) => (ids.has(settingKey(setting)) ? { ...setting, ...WITHHELD_SETTING } : setting)),",
            '      check: ids.has("check") ? WITHHELD_CHECK : profile.check,',
        ):
            self.assertIn(line + "\n", body)
        # Signature, "return {", the five lines above, "};" and the closing brace.
        self.assertEqual(len(body.strip().splitlines()), 9)

    def test_a_setting_is_withheld_by_the_id_profile_py_gives_it(self):
        template = re.search(r"const settingKey = \(setting\) => `(.*?)`;", APP_JS).group(1)
        setting = {"scope": "main", "name": "N8N_HOST"}
        as_python = template.replace("${setting.scope}", "{scope}").replace("${setting.name}", "{name}")
        self.assertEqual(as_python.format(**setting), profile.setting_id(setting))
        # The id is spelled in that one place: the checkbox, the bulk button and the blanking all call it.
        self.assertEqual(APP_JS.count("setting:"), 1)

    def test_the_saved_text_is_spelled_like_to_json(self):
        self.assertIn("const profileText = (profile) => `${JSON.stringify(profile, null, 2)}\\n`;", CORE)

    # --- the reference: the same characters as profile.check_reference --------------

    def test_reference_is_stripped_of_what_python_strips(self):
        edges = re.search(r"const REFERENCE_EDGES = /\^\[(.*?)\]\+\|\[(.*?)\]\+\$/g;", APP_JS)
        self.assertEqual(edges.group(1), edges.group(2))
        stripped_by_page = re.compile(f"[{edges.group(1)}]")
        for code in range(sys.maxunicode + 1):
            if bool(stripped_by_page.fullmatch(chr(code))) != (chr(code).strip() == ""):
                self.fail(f"U+{code:04X} is stripped by only one of the page and Python")
        self.assertIn('const stripReference = (text) => text.replace(REFERENCE_EDGES, "");', APP_JS)

    def test_reference_is_refused_for_what_python_refuses(self):
        body = js_function("referenceProblem")
        self.assertIn("const length = [...reference].length;", body)  # characters, as len() counts them
        self.assertIn("if (length > REFERENCE_MAX) return", body)
        refused = re.search(r"if \(/\[(.*?)\]\|\\p\{Cs\}/u\.test\(reference\)\) return", body).group(1)
        refused_by_page = re.compile(f"[{refused}]")
        for code in range(0x3000):
            self.assertEqual(bool(refused_by_page.fullmatch(chr(code))), code < 32 or code == 127, hex(code))

    # --- wiring: the controls reach the lines above ---------------------------------

    def test_download_saves_the_blanked_profile_and_nothing_else(self):
        self.assertIn(
            'return withholdEntries(report.profile, withheld, $("f-reason").value, $("f-reference").value);',
            js_function("currentProfile"),
        )
        body = js_function("downloadProfile")
        self.assertLess(body.index("if (!checkReference())"), body.index("const profile = currentProfile();"))
        self.assertIn("profileText(profile), \"share-status\");", body)
        for other_source in ("report.profile", "JSON.stringify", "report.snapshot"):
            self.assertNotIn(other_source, body)
        self.assertEqual(APP_JS.count("downloadProfile()"), 2)  # its definition and the form's submit

    def test_every_checkbox_carries_the_id_it_withholds(self):
        sections = js_function("profileSections")
        self.assertIn('el("input", { type: "checkbox", id, "data-withhold": key, checked: withheld.has(key) })', sections)
        self.assertIn("tick(`w-fact-${index}`, fact.id, ", sections)
        self.assertIn("tick(`w-setting-${index}`, settingKey(setting), ", sections)
        self.assertIn('"data-withhold": "check", checked: withheld.has("check")', sections)
        self.assertEqual(sections.count("tick("), 2)
        handler = APP_JS[APP_JS.index('$("share-body").addEventListener("change"'):]
        handler = handler[: handler.index("\n  });\n")]
        for line in (
            "const key = event.target.dataset.withhold;",
            "if (event.target.checked) withheld.add(key);",
            "else withheld.delete(key);",
            "renderShare();",
        ):
            self.assertIn(line, handler)
        self.assertIn("report.profile.facts.map((fact) => fact.id)", APP_JS)
        self.assertIn("report.profile.settings.map(settingKey)", APP_JS)

    def test_a_new_run_starts_with_no_choice_reason_or_reference_of_the_last(self):
        body = js_function("resetShare")
        for line in (
            "withheld.clear();",
            '$("f-reason").value = Object.keys(PROFILE_REASONS)[0];',
            '$("f-reference").value = "";',
        ):
            self.assertIn(line, body)
        self.assertEqual(next(iter(js_constant("PROFILE_REASONS"))), "baseline")
        report_body = js_function("renderReport")
        self.assertLess(report_body.index("resetShare();"), report_body.index("renderShare();"))
        self.assertEqual(APP_JS.count("report = {"), 1)  # one place receives a run, and it renders the report
        self.assertRegex(js_function("submit"), r"report = \{ \.\.\.data, finishedAt: new Date\(\) \};\n    renderReport\(\);")

    def test_an_opened_file_is_shown_only_after_it_passed_the_check(self):
        body = js_function("openProfile")
        self.assertIn("data = parseExact(new TextDecoder(\"utf-8\", { fatal: true, ignoreBOM: true })", body)
        self.assertIn("problem = running ? RUNNING_NO_PROFILE : profileProblem(data);", body)
        refusal = body.index("if (problem) {")
        self.assertLess(refusal, body.index("return;", refusal))
        self.assertLess(body.index("return;", refusal), body.index("profileSections(data, false)"))
        self.assertEqual(APP_JS.count("profileSections(data, false)"), 1)
        self.assertEqual(APP_JS.count("profileSections("), 3)  # its definition, the share step, the viewer
        # The check itself is run by PageCodeTest; here, only that it can still say no.
        self.assertGreater(js_function("profileProblem").count("return bad("), 20)
        self.assertTrue(js_function("profileProblem").rstrip().endswith("return null;\n  }"))

    def test_no_profile_can_be_opened_while_a_check_runs(self):
        body = js_function("setRunning")
        self.assertIn('$("f-profile").disabled = on;', body)
        self.assertIn('$("h-profile").hidden = !on;', body)
        self.assertIn('id="h-profile" hidden>A health check is running.', INDEX_HTML)
        self.assertIn('aria-describedby="h-profile e-profile"', INDEX_HTML)
        self.assertIn("if (running) {", js_function("openProfile"))
        # A run's error is written to the form, and the form is on screen when it is.
        failure = js_function("submit")
        self.assertLess(failure.index('if ($("view-form").hidden) showView("form");'), failure.index("showFormError(failure[0]"))

    # --- text of a file or of the server is only ever text --------------------------

    def test_nothing_is_parsed_as_html(self):
        for sink in (
            "innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment", "DOMParser", "parseFromString",
            "setHTML", "srcdoc", "document.write", "document.open", "eval(", "Function(", "setTimeout(\"", "setTimeout('",
            "javascript:", "import(", "createRange", "XSLTProcessor", "<template", "cloneNode", "createHTML",
        ):  # fmt: skip
            self.assertNotIn(sink, APP_JS, sink)
        # Elements are made in one place, el(), which adds children with append(): strings become text nodes.
        self.assertEqual(APP_JS.count("document.createElement("), 1)
        self.assertEqual(APP_JS.count("document.createElementNS("), 2)  # the icons: <svg> and <path>
        self.assertIn("node.append(...children.filter((child) => child != null));", js_function("el"))
        # ... and the tag is never text from outside: a literal, or the one choice between two literals.
        tags = set(re.findall(r"\bel\(([^,)]+)", APP_JS))
        self.assertEqual({tag for tag in tags if not re.fullmatch(r'"[a-z0-9]+"', tag)}, {"tag", 'edit ? "h4" : "h3"'})
        # No attribute that runs or loads something gets a value at all.
        for attribute in ("href:", "src:", "action:", "formaction:", "style:", "onclick", "onerror", "onload"):
            self.assertNotIn(attribute, CORE + js_function("profileSections") + js_function("openProfile"), attribute)

    def test_no_way_to_parse_html_is_named_at_all(self):
        # Wider than the list above: every such call of the platform has HTML in its name
        # (innerHTML, setHTMLUnsafe, Document.parseHTMLUnsafe, insertAdjacentHTML, ...).
        self.assertNotIn("HTML", APP_JS)
        self.assertNotIn("Document", APP_JS)
        used = set(re.findall(r"\bdocument\.(\w+)", APP_JS))
        self.assertEqual(used, {"getElementById", "createElement", "createElementNS", "querySelectorAll", "body", "activeElement", "execCommand"})

    def test_nothing_newer_than_the_page_needed_before_can_stop_it_loading(self):
        # A lookbehind is a syntax error where it is not supported (Safari before 16.4): the whole
        # script fails to load, the form and the health check with it. Never use one.
        for syntax in ("(?<=", "(?<!", "(?<", "/v.", "/v;", "/v)", "#private", "??=", "||=", "&&=", " using "):
            self.assertNotIn(syntax, APP_JS, syntax)
        # Calls newer than the rest of the page; none is needed.
        for call in ("Object.hasOwn", ".at(", "structuredClone", ".replaceAll(", ".findLast", ".toSorted(", ".toReversed(", ".isWellFormed(", "Object.groupBy", ".hasOwn(", "Array.fromAsync", "Promise.withResolvers"):  # fmt: skip
            self.assertNotIn(call, APP_JS, call)
        # The one newer call that is used is asked for before it is used, in one place each.
        self.assertEqual(APP_JS.count("JSON.rawJSON("), 1)
        self.assertEqual(APP_JS.count("JSON.isRawJSON("), 1)
        self.assertIn('const isRaw = (value) => typeof JSON.isRawJSON === "function" && JSON.isRawJSON(value);', CORE)
        exact = js_function("parseExact")
        self.assertLess(exact.index('if (typeof JSON.rawJSON !== "function") return JSON.parse(text);'), exact.index("JSON.rawJSON("))

    def test_page_stores_nothing_and_loads_nothing_else(self):
        for store in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "console.log", "console.error"):
            self.assertNotIn(store, APP_JS)
        # The profile is read and written in the browser: the only requests are the two that existed.
        self.assertEqual(sorted(set(re.findall(r'fetch\("([^"]+)"', APP_JS))), ["/api/meta", "/api/run"])
        self.assertEqual(APP_JS.count("fetch("), 2)
        for other_way_out in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", ".submit("):
            self.assertNotIn(other_way_out, APP_JS)
        self.assertEqual(web.CONTENT_SECURITY_POLICY.split(";")[0], "default-src 'none'")
        self.assertIn("script-src 'self'; style-src 'self'", INDEX_HTML)

    def test_the_part_run_under_node_touches_no_page_element(self):
        for name in ("document", "window", "$(", "el(", "report.", "withheld."):
            self.assertNotIn(name, re.sub(r"//.*", "", CORE), name)


def run_page_code(expression, raw=True, **data):
    """Run the "profile-core" part of app.js under Node.js; `expression` is JavaScript giving the result."""
    script = (
        f'"use strict";\n{"" if raw else "delete JSON.rawJSON; delete JSON.isRawJSON;"}{HAS}{CORE}\nconst input = JSON.parse(require("fs").readFileSync(0, "utf8"));\n'
        f"process.stdout.write(JSON.stringify({expression}));\n"
    )
    payload = {"withheld": WITHHELD, "references": REFERENCES, **data}
    done = subprocess.run(
        [NODE, "-e", script], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=60,
        env=NODE_ENV,
    )  # fmt: skip
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def response_text(built_profile):
    """A profile as it travels inside the answer of POST /api/run."""
    return json.dumps({"profile": built_profile}, allow_nan=False)


# What the page does from the answer of a run to the text of the downloaded file.
SAVED = "profileText(withholdEntries(parseExact(input.response).profile, new Set(input.ids), input.reason, input.reference))"


def mutated_files():
    """(label, file text): one valid profile, and that profile with each part replaced or removed."""
    base = profile.apply_withholding(built(), ["n8n.version", "setting:main:N8N_HOST"])
    files = [("valid", profile.to_json(base)), ("check withheld", profile.to_json(profile.apply_withholding(base, ["check"])))]
    replacements = (None, True, 0, 1, -1, 2, 1.5, "", "x", " x", "baseline", "measured", "collected", "value", LONE, [], {}, [1], ["baseline"], ["measured"], ["value"], ["collected"], {"a": 1}, {"a": {"b": 1}}, {"a": {"b": {"c": 1}}})  # fmt: skip

    def paths(node, here=()):
        yield here
        items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, child in items:
            # One fact, setting, score and finding stand for the rest of their list.
            if not (isinstance(node, list) and key > 1):
                yield from paths(child, here + (key,))

    for path in paths(base):
        if not path:
            continue
        for index in range(len(replacements) + 1):
            changed = copy.deepcopy(base)
            holder = changed
            for key in path[:-1]:
                holder = holder[key]
            if index == len(replacements):
                del holder[path[-1]]
                label = f"{path} removed"
            else:
                holder[path[-1]] = copy.deepcopy(replacements[index])
                label = f"{path} = {replacements[index]!r}"
            files.append((label, json.dumps(changed)))  # a lone surrogate is written as an escape

    text = profile.to_json(base)
    for old, new in (
        ('"format_version": 1,', '"format_version": 1.0,'),
        ('"format_version": 1,', '"format_version": 1e0,'),
        ('"format_version": 1,', '"format_version": 2,'),
        ('"format_version": 1,', '"format_version": 2.0,'),
        ('"format_version": 1,', '"format_version": 0,'),
        ('"format_version": 1,', '"format_version": 10000000000000000000000,'),
        ('"settings_not_listed": 0,', '"settings_not_listed": 0.0,'),
        ('"settings_not_listed": 0,', '"settings_not_listed": 1e0,'),
        ('"settings_not_listed": 0,', '"settings_not_listed": 90071992547409930,'),
        ('"score": 85,', '"score": 85.0,'),
        ('"score": 85,', '"score": 8.5e1,'),
        ('"score": 85,', '"score": 1E400,'),
        ('"score": 85,', '"score": 1e+21,'),
        ('"score": 85,', '"score": -1e+21,'),
        ('"score": 85,', '"score": 1.7976931348623157e+308,'),
        ('"settings_not_listed": 0,', '"settings_not_listed": 1e+21,'),
        ('"settings_not_listed": 0,', '"settings_not_listed": 1e21,'),
        ('"format_version": 1,', '"format_version": 1e+21,'),
        ('"value": "queue"', '"value": 1e+21'),
        ('"value": "queue"', '"value": {"a": {"b": 1.0}, "c": 2.5, "d": 3}'),
        ('"partial": false', '"partial": 1E400'),
        ('"value": "queue"', '"value": 1E400'),
        ('"value": "queue"', '"value": -1E400'),
        ('"value": "queue"', '"value": NaN'),
        ('"value": "queue"', '"value": 0.0'),
        ('"value": "queue"', '"value": 1e2'),
        ('"value": "queue"', '"value": 123456789012345678901234567890'),
        ('"value": "queue"', '"value": {"a": {"b": 0.0}}'),
        ('"value": "queue"', '"value": {"a": {"b": {"c": 0.0}}}'),
        ('"reference": ""', '"reference": "tab\\there"'),
        ('"reference": ""', '"reference": "' + "x" * 201 + '"'),
        ('"reference": ""', '"reference": "' + chr(0x1F600) * 200 + '"'),
    ):
        assert old in text, old
        files.append((f"{old} -> {new[:40]}", text.replace(old, new, 1)))
    files += [(f"NUMBER {label}", changed) for label, changed in files[-33:]]  # marked: an older browser reads these differently
    del files[-66:-33]
    files += [(repr(raw), raw) for raw in ("", "null", "[]", "{}", "1", '"text"', "{", text[:300], chr(0xFEFF) + text, text + "x")]
    return files


def python_accepts(text):
    try:
        profile.validate(json.loads(text))
    except (ValueError, RecursionError):  # ProfileError is a ValueError
        return False
    return True


@unittest.skipUnless(NODE, NO_NODE)
class PageCodeTest(unittest.TestCase):
    @needs_exact
    def test_the_saved_file_is_the_one_profile_py_writes(self):
        base = built()
        saved = run_page_code(SAVED, response=response_text(base), ids=WITHHELD, reason="pre_upgrade", reference="  change 1234 ")
        expected = profile.apply_withholding(base, WITHHELD)
        expected["run"] = {"reason": "pre_upgrade", "reference": "change 1234"}
        self.assertEqual(saved, profile.to_json(expected))
        self.assertIs(profile.validate(expected), expected)
        self.assertEqual(profile.validate(json.loads(saved)), expected)
        # None of what was withheld is left anywhere in the file.
        self.assertNotIn("2.40.1", saved)
        self.assertNotIn('"cpu"', saved)

    def test_without_rawjson_the_saved_file_is_the_same_profile_as_data(self):
        # An older browser: numbers are JavaScript's. The file is still valid, holds nothing of a
        # withheld entry and reads back as the same data; only a number's spelling differs (0.0 as 0).
        base = built()
        count = next(f for f in base["facts"] if f["id"] == "database.execution_count")
        # The one exception, stated in the page's comment: a whole number of 2^53 or more is saved
        # as JavaScript spells its nearest number, which Python reads as a different number. It
        # takes an execution table no instance has; it is pinned here so that it stays known.
        huge = run_page_code(SAVED, raw=False, response=response_text(base), ids=[], reason="baseline", reference="")
        self.assertEqual(count["value"], 2**60)
        self.assertIn('"value": 1152921504606847000,', huge)
        profile.validate(json.loads(huge))
        count["value"] = 1200
        saved = run_page_code(SAVED, raw=False, response=response_text(base), ids=WITHHELD, reason="pre_upgrade", reference="  change 1234 ")
        expected = profile.apply_withholding(base, WITHHELD)
        expected["run"] = {"reason": "pre_upgrade", "reference": "change 1234"}
        self.assertEqual(profile.validate(json.loads(saved)), expected)
        self.assertNotIn("2.40.1", saved)
        self.assertNotIn('"cpu"', saved)
        self.assertIn('"value": 0,', saved)  # where this is the exact path's 0.0, the test above fails instead
        if EXACT:
            self.assertNotEqual(saved, profile.to_json(expected))

    @needs_exact
    def test_numbers_are_saved_as_the_server_wrote_them(self):
        base = built()
        facts = {f["id"]: f["value"] for f in base["facts"]}
        # The two spellings JavaScript would change: a float that is whole, a whole number too large for it.
        self.assertEqual(repr(facts["database.oldest_execution_age_days"]), "0.0")
        self.assertGreater(facts["database.execution_count"], 2**53)
        saved = run_page_code(SAVED, response=response_text(base), ids=[], reason="baseline", reference="")
        self.assertEqual(saved, profile.to_json(base))
        self.assertIn('"value": 0.0,', saved)
        # ... and shown as --show-profile shows them.
        shown = run_page_code("parseExact(input.response).profile.facts.map((fact) => shown(fact.value))", response=response_text(base))
        self.assertEqual(shown, [profile._shown(f["value"]) for f in base["facts"]])

    def test_a_value_is_worded_as_show_profile_words_it(self):
        # Spellings Python writes back the same way; a hand-written 2.50 is shown as written, Python shows 2.5.
        values = [{"a": {"b": 1.0}, "c": 2.5, "d": 3}, {"requests": {"cpu": "200m", "memory": None}, "limits": {}}, {}, None, True, False, 0, 0.0, 45.5, 2**60, 1e21, "queue", ""]
        for raw in (True, False) if EXACT else (False,):
            shown = run_page_code("parseExact(input.values).map(shown)", raw=raw, values=json.dumps(values))
            expected = [profile._shown(value) for value in values]
            if not raw:  # JavaScript's own spelling of the three it cannot keep
                expected[0], expected[7], expected[9] = "a (b: 1), c: 2.5, d: 3", "0", "1152921504606847000"
            self.assertEqual(shown, expected, f"JSON.rawJSON: {raw}")

    def test_everything_withheld_is_still_a_valid_profile(self):
        base = built()
        everything = [f["id"] for f in base["facts"]] + [profile.setting_id(s) for s in base["settings"]] + ["check"]
        self.assertGreater(len(base["settings"]), 3)
        saved = run_page_code(SAVED, response=response_text(base), ids=everything, reason="other", reference="x" * 200)
        result = profile.validate(json.loads(saved))
        self.assertEqual(saved, profile.to_json(profile.apply_withholding({**base, "run": {"reason": "other", "reference": "x" * 200}}, everything)))
        self.assertEqual({f["provenance"] for f in result["facts"]}, {"withheld"})
        self.assertEqual({(f["value"], f["source"], f["detail"]) for f in result["facts"]}, {(None, None, "")})
        self.assertEqual({(s["state"], s["value"]) for s in result["settings"]}, {("withheld", None)})
        self.assertEqual(result["check"], {"withheld": True})
        self.assertIn("Withheld.", profile.render_markdown(result))

    @needs_exact
    def test_withholding_does_not_change_the_profile_the_server_sent(self):
        # The page keeps the server's profile and blanks a copy on every change: unticking brings the entry back.
        again = run_page_code(
            "(() => { const sent = parseExact(input.response).profile;"
            " withholdEntries(sent, new Set(input.withheld), 'other', 'x');"
            " return profileText(withholdEntries(sent, new Set(), 'baseline', '')); })()",
            response=response_text(built()),
        )
        self.assertEqual(again, profile.to_json(built() | {"created_at": json.loads(again)["created_at"]}))

    def test_a_setting_is_withheld_in_its_own_scope_only(self):
        ids = run_page_code("parseExact(input.response).profile.settings.map(settingKey)", response=response_text(built()))
        self.assertEqual(ids, [profile.setting_id(s) for s in built()["settings"]])
        self.assertIn("setting:instance:EXECUTIONS_MODE", ids)
        self.assertIn("setting:main:EXECUTIONS_MODE", ids)
        saved = run_page_code(SAVED, response=response_text(built()), ids=["setting:main:EXECUTIONS_MODE"], reason="baseline", reference="")
        states = {profile.setting_id(s): s["state"] for s in json.loads(saved)["settings"]}
        self.assertEqual(states["setting:main:EXECUTIONS_MODE"], "withheld")
        self.assertEqual(states["setting:instance:EXECUTIONS_MODE"], "value")

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
        self.assertFalse(run_page_code("referenceProblem(input.reference) === null", reference="lone " + LONE + " half"))
        broken = built()
        broken["run"]["reference"] = "lone " + LONE + " half"
        with self.assertRaises(profile.ProfileError):
            profile.validate(broken)

    def test_without_rawjson_the_page_differs_only_on_how_a_number_is_written(self):
        files = mutated_files()
        answers = run_page_code(
            "input.files.map((text) => { try { return profileProblem(parseExact(text)) === null; } catch { return false; } })",
            raw=False, files=[text for _, text in files],
        )  # fmt: skip
        differ = {label: page for (label, text), page in zip(files, answers) if page != python_accepts(text)}
        # Never a refusal of a file Python accepts; accepted wrongly: only a float standing for a whole number.
        self.assertEqual(set(differ.values()) - {True}, set())
        self.assertTrue(all(label.startswith("NUMBER ") for label in differ), sorted(differ))
        self.assertEqual(len(differ), 11, sorted(differ))

    @needs_exact
    def test_the_page_opens_exactly_the_files_validate_accepts(self):
        files = mutated_files()
        answers = run_page_code(
            "input.files.map((text) => { try { return profileProblem(parseExact(text)) === null; } catch { return false; } })",
            files=[text for _, text in files],
        )
        expected = [python_accepts(text) for _, text in files]
        differ = [label for (label, _), page, python in zip(files, answers, expected) if page != python]
        self.assertEqual(differ, [])
        # The comparison means something only if both answers occur, and the valid file is one of the accepted.
        self.assertTrue(expected[0] and expected[1])
        self.assertGreater(expected.count(True), 20)
        self.assertGreater(expected.count(False), 500)

    def test_a_refusal_never_quotes_the_file(self):
        marker = "zq-from-the-file"
        base = built()
        base["facts"][2]["provenance"] = marker
        said = run_page_code("profileProblem(parseExact(input.file))", file=json.dumps(base))
        self.assertIn("provenance of fact 3", said)
        self.assertNotIn(marker, said)
        newer = run_page_code("profileProblem(parseExact(input.file))", file=json.dumps({**built(), "format_version": 2}))
        self.assertIn("newer than this page reads", newer)
        other = run_page_code("profileProblem(parseExact(input.file))", file=json.dumps({**built(), "format": marker}))
        self.assertNotIn(marker, other)



# --- the whole page, driven through its controls ------------------------------------

# Without the number no JavaScript has: the path without JSON.rawJSON saves that one differently.
PLAIN = dataclasses.replace(SNAPSHOT, postgres={**SNAPSHOT.postgres, "execution_count": 1200})
PATHS = (True, False) if EXACT else (False,)


def run_response(snapshot=PLAIN):
    """The text POST /api/run answers with, from the real server code and a synthetic snapshot."""
    with mock.patch("n8n_healthcheck.collectors.collect", return_value=snapshot):
        with mock.patch("n8n_healthcheck.rules.ALL_RULES", [lambda _snapshot: FINDING]):
            return json.dumps(cli.json_safe(web.run_check({"url": "https://n8n.example.com"})), allow_nan=False)


def run_page(scenario, raw=True, **data):
    """Load app.js in tests/page_harness.js and run `scenario`, an async JavaScript function of (page, input)."""
    request = {
        "app": str(web.STATIC_DIR / "app.js"),
        "index": str(web.STATIC_DIR / "index.html"),
        "scenario": scenario,
        "input": data,
        "withoutRawJSON": not raw,
    }
    done = subprocess.run(
        [NODE, HARNESS], input=json.dumps(request), capture_output=True, text=True, encoding="utf-8", timeout=120, env=NODE_ENV
    )
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def all_ids(sent):
    return [f["id"] for f in sent["facts"]] + [profile.setting_id(s) for s in sent["settings"]] + ["check"]


@unittest.skipUnless(NODE, NO_NODE)
class PageBehaviourTest(unittest.TestCase):
    """What the page does when its controls are used. Not covered here, only by PageSourceTest
    or by the check in a real browser: layout, focus rings, what a screen reader announces, the
    operating system's file dialog and the save to disk."""

    def assert_saved(self, text, expected, raw):
        """The saved text is profile.py's; without JSON.rawJSON, the same data in JavaScript's spelling."""
        self.assertEqual(profile.validate(json.loads(text)), expected)
        if raw:
            self.assertEqual(text, profile.to_json(expected))

    def test_download_saves_exactly_what_is_ticked(self):
        response = run_response()
        sent = json.loads(response)["profile"]
        for raw in PATHS:
            got = run_page(
                """async (page, input) => {
                  await page.run(input.response);
                  const boxes = Object.keys(page.boxes());
                  for (const id of input.ids) page.toggle(page.boxes()[id]);
                  page.$("f-reason").value = "incident";
                  page.type("f-reference", "  ticket 4711 <b>x</b> ");
                  const first = await page.download();
                  const status = page.$("share-status").textContent;
                  page.toggle(page.boxes()[input.ids[0]]);  // changed my mind about the first one
                  return { rawJSON: page.rawJSON, view: page.view(), boxes, first, status, second: await page.download(),
                           summary: page.$("share-summary").textContent };
                }""",
                raw=raw, response=response, ids=WITHHELD,
            )  # fmt: skip
            with self.subTest(rawJSON=raw):
                self.assertEqual(got["rawJSON"], raw)
                self.assertEqual(got["view"], "report")
                self.assertEqual(sorted(got["boxes"]), sorted(all_ids(sent)))  # one box per entry, none missing
                run = {"reason": "incident", "reference": "ticket 4711 <b>x</b>"}
                self.assert_saved(got["first"], profile.apply_withholding({**sent, "run": run}, WITHHELD), raw)
                self.assert_saved(got["second"], profile.apply_withholding({**sent, "run": run}, WITHHELD[1:]), raw)
                self.assertNotIn("2.40.1", got["first"])
                self.assertEqual(got["status"], f"Download started: n8n-profile-{sent['created_at'][:10]}.json")
                self.assertIn("1 of 32 facts, 2 of ", got["summary"])

    def test_withhold_all_and_none_reach_every_entry(self):
        response = run_response()
        sent = json.loads(response)["profile"]
        got = run_page(
            """async (page, input) => {
              await page.run(input.response);
              const saved = {};
              for (const name of ["Withhold all facts", "Withhold all settings", "Withhold no facts", "Withhold no settings"]) {
                page.button(name).click();
                saved[name] = await page.download();
              }
              return saved;
            }""",
            raw=PATHS[0], response=response,
        )  # fmt: skip
        facts = [f["id"] for f in sent["facts"]]
        settings = [profile.setting_id(s) for s in sent["settings"]]
        self.assertEqual(len(facts), 32)
        self.assertGreater(len(settings), 3)
        for name, ids in (
            ("Withhold all facts", facts),
            ("Withhold all settings", facts + settings),
            ("Withhold no facts", settings),
            ("Withhold no settings", []),
        ):
            with self.subTest(button=name):
                self.assert_saved(got[name], profile.apply_withholding(sent, ids), PATHS[0])

    def test_a_reference_that_cannot_be_stored_stops_the_download(self):
        got = run_page(
            """async (page, input) => {
              await page.run(input.response);
              page.type("f-reference", "x".repeat(201));
              const refused = { saved: await page.download(), error: page.visible("e-reference") && page.$("e-reference").textContent,
                                invalid: page.$("f-reference").getAttribute("aria-invalid") };
              page.type("f-reference", "x".repeat(200));
              return { refused, errorAfter: page.visible("e-reference"), saved: JSON.parse(await page.download()).run.reference.length };
            }""",
            raw=PATHS[0], response=run_response(),
        )  # fmt: skip
        self.assertIsNone(got["refused"]["saved"])
        self.assertIn("201 characters", got["refused"]["error"])
        self.assertEqual(got["refused"]["invalid"], "true")
        self.assertFalse(got["errorAfter"])
        self.assertEqual(got["saved"], 200)

    def test_a_new_run_keeps_nothing_of_the_one_before(self):
        first, second = run_response(), run_response(dataclasses.replace(PLAIN, n8n_version="2.41.0"))
        for raw in PATHS:
            got = run_page(
                """async (page, input) => {
                  await page.run(input.first);
                  for (const id of input.ids) page.toggle(page.boxes()[id]);
                  page.$("f-reason").value = "incident";
                  page.type("f-reference", "ticket of the first run");
                  page.$("act-again").click();
                  await page.run(input.second);
                  return { ticked: Object.values(page.boxes()).filter((box) => box.checked).length, reason: page.$("f-reason").value,
                           reference: page.$("f-reference").value, saved: await page.download() };
                }""",
                raw=raw, first=first, second=second, ids=WITHHELD,
            )  # fmt: skip
            with self.subTest(rawJSON=raw):
                self.assertEqual((got["ticked"], got["reason"], got["reference"]), (0, "baseline", ""))
                self.assert_saved(got["saved"], json.loads(second)["profile"], raw)
                self.assertNotIn("first run", got["saved"])

    def test_open_a_profile_is_off_during_a_run_and_back_on_after_it_however_it_ends(self):
        saved_file = profile.to_json(built())
        got = run_page(
            """async (page, input) => {
              const field = page.$("f-profile");
              const state = () => ({ view: page.view(), off: field.disabled, why: page.visible("h-profile"),
                                     stale: page.visible("e-profile"), error: page.visible("form-error") });
              const seen = { before: state() };
              const endings = {
                "a report": () => page.finishRun(input.response),
                "no answer": () => page.failRun(),
                "an error of the server": () => page.finishRun('{"error": "boom"}', 500),
                "an answer that is no report": () => page.finishRun("{}"),
                "an answer that is not JSON": () => page.finishRun("<html>"),
              };
              for (const [name, end] of Object.entries(endings)) {
                await page.startRun();
                const during = state();
                during.opened = await page.open(input.file);  // a file that arrives all the same
                await end();
                seen[name] = { during, after: state() };
                if (page.view() === "report") page.$("act-again").click();
              }
              seen.openedAfter = await page.open(input.file);
              return seen;
            }""",
            raw=PATHS[0], response=run_response(), file=saved_file,
        )  # fmt: skip
        idle = {"view": "form", "off": False, "why": False, "stale": False, "error": False}
        self.assertEqual(got.pop("before"), idle)
        self.assertEqual(got.pop("openedAfter")["view"], "profile")
        self.assertEqual(len(got), 5)
        for name, seen in got.items():
            with self.subTest(run_ends_with=name):
                opened = seen["during"].pop("opened")
                self.assertEqual(seen["during"], {**idle, "off": True, "why": True})
                self.assertEqual((opened["view"], opened["elements"]), ("form", 0))
                self.assertIn("A health check is running", opened["error"])
                # Afterwards the field is usable again, and the result or the error is on screen.
                if name == "a report":
                    self.assertEqual(seen["after"], {**idle, "view": "report"})
                else:
                    self.assertEqual(seen["after"], {**idle, "error": True})

    def test_a_file_is_shown_only_when_validate_accepts_it(self):
        files = mutated_files()
        refused = [(label, text) for label, text in files if not python_accepts(text)]
        accepted = [(label, text) for label, text in files if python_accepts(text)]
        # Every way a number can be written, and a spread of the rest: the full set is compared in PageCodeTest.
        chosen = [f for f in files if f[0].startswith("NUMBER ")] + refused[::9] + accepted[::9]
        self.assertGreater(len(chosen), 150)
        for raw in PATHS:
            got = run_page(
                """async (page, input) => {
                  const seen = [];
                  for (const text of input.files) {
                    const opened = await page.open(text);
                    opened.meta = page.$("profile-meta").textContent;
                    if (opened.view === "profile") page.$("profile-close").click();
                    opened.left = page.walk(page.$("profile-body")).length + page.$("profile-meta").textContent.length;
                    seen.push(opened);
                  }
                  return seen;
                }""",
                raw=raw, files=[text for _, text in chosen],
            )  # fmt: skip
            for (label, text), opened in zip(chosen, got):
                if not raw and label.startswith("NUMBER "):
                    continue  # an older browser cannot tell 1.0 from 1: compared in PageCodeTest
                with self.subTest(rawJSON=raw, file=label):
                    if python_accepts(text):
                        self.assertEqual((opened["view"], opened["error"]), ("profile", ""))
                        self.assertGreater(opened["elements"], 100)
                        self.assertIn("n8n-profile.json", opened["meta"])
                    else:
                        # Refused: a message, and nothing of the file anywhere on the page.
                        self.assertEqual((opened["view"], opened["elements"], opened["shown"], opened["meta"]), ("form", 0, "", ""))
                        self.assertNotEqual(opened["error"], "")
                    self.assertEqual(opened["left"], 0)  # and nothing is kept after "Back to start"

    def test_an_opened_file_shows_what_show_profile_prints(self):
        base = profile.apply_withholding(built(), ["n8n.version", "setting:main:N8N_HOST"])
        base["run"] = {"reason": "pre_upgrade", "reference": "change 1234"}
        got = run_page("async (page, input) => (await page.open(input.file)).shown", raw=PATHS[0], file=profile.to_json(base))
        printed = profile.render_markdown(base)
        self.assertIn("change 1234", got)
        self.assertIn("Before an upgrade (pre_upgrade)", got)
        for fact in base["facts"]:
            self.assertIn(fact["id"], got)
            if fact["provenance"] == "measured" and isinstance(fact["value"], (bool, str)):
                self.assertIn(f"{fact['id']}{profile._shown(fact['value'])}measured", got)
                self.assertIn(f"| {fact['id']} | {profile._shown(fact['value'])} | measured |", printed)
            if fact["detail"]:
                self.assertIn(fact["detail"], got)
        self.assertIn("n8n.versionwithheld", got)
        self.assertIn("N8N_HOSTwithheld", got)
        self.assertIn("WEBHOOK_URLset (value not written)", got)
        self.assertIn("reliability85/100", got)
        self.assertIn("A title", got)

    def test_text_of_a_file_only_ever_becomes_text(self):
        mark = "<img src=x onerror=alert(1)><script>alert(2)</script>"
        hostile = built()
        hostile["tool_version"] = hostile["created_at"] = mark
        hostile["sources"][mark] = "failed"
        hostile["facts"].append({"id": mark + ".x", "value": {mark: {mark: mark}}, "provenance": "declared", "source": mark, "detail": mark})
        hostile["settings"].append({"scope": mark, "name": mark, "state": "value", "value": mark})
        hostile["check"] = {
            "partial": True,
            "scores": [{"area": mark, "score": None}],
            "findings": [{"rule_id": mark, "severity": name, "area": mark, "title": mark} for name in (mark, "constructor", "__proto__")],
        }
        profile.validate(hostile)
        got = run_page(
            """async (page, input) => {
              const opened = await page.open(input.file, input.mark + ".json");
              const made = page.walk(page.$("profile-body"));
              return { view: opened.view, shown: opened.shown, tags: page.tagsMade(), meta: page.$("profile-meta").textContent,
                       attributes: made.flatMap((node) => Object.entries(node.attributes).map(([name, value]) => name + "=" + value)) };
            }""",
            raw=PATHS[0], file=json.dumps(hostile), mark=mark,
        )  # fmt: skip
        self.assertEqual(got["view"], "profile")
        self.assertGreater(got["shown"].count(mark), 15)  # it is all there, as text
        self.assertIn(mark, got["meta"])
        # No element is named by the file, and no attribute holds text of the file.
        self.assertEqual(
            set(got["tags"]) - {"a", "option"},  # made by the page at start-up and on a download, not from a file
            {"section", "h3", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "th", "td", "p", "span", "ul", "li", "wbr", "svg", "path"},
        )
        self.assertEqual([entry for entry in got["attributes"] if "<" in entry or "alert" in entry], [])
        self.assertLessEqual({entry.split("=")[0] for entry in got["attributes"]}, {"scope", "colspan", "translate", "viewBox", "aria-hidden", "focusable", "d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray"})


if __name__ == "__main__":
    unittest.main()
