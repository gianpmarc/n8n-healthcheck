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

import test_debug_info as pasted  # the synthetic debug information and its marker; imported as a module so its tests run once
from n8n_healthcheck import cli, debug_info, profile, questions, report
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
    "writes, with answers and without; that the question sheet it copies is the one profile.render_questions "
    "prints; that its controls do what they say (answer, withhold, withhold all, download, open, copy, a new "
    "run). Checked as text all the same (PageSourceTest): the constants, the lines that blank an entry, the "
    "reference rules, the lines that wire the controls."
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


# The catalogue as GET /api/meta serves it: all the page knows of the questions.
CATALOGUE = web.meta()["questions"]
MULTI = "observability.tools"
# An answer to every operator question, in the shape profile.apply_answers takes.
EVERY_ANSWER = {
    q["id"]: [v for v, _ in q["values"]][2::-1] if q["kind"] == "multi" else q["values"][-1][0] for q in questions.OPERATOR
}


def bare():
    """A profile of a run that was given an env file and nothing else: nearly every fact is unknown."""
    return profile.build_profile(Snapshot(env={"EXECUTIONS_MODE": "queue"}), [], ())


def sheet_files():
    """(label, profile): the files the page's question sheet is compared with profile.render_questions on."""
    answered = profile.apply_answers(bare(), EVERY_ANSWER)[0]
    nothing_open = copy.deepcopy(answered)
    for fact in nothing_open["facts"]:
        if fact["provenance"] == "unknown":
            fact.update(value=1, provenance="measured", source="env")
    withheld = profile.apply_withholding(answered, ["n8n.version", "hosting.model", MULTI, "executions.mode"])
    older = bare()
    older["tool_version"] = "0.2.0"
    older["facts"] = [fact for fact in older["facts"] if fact["id"] in profile.FORMAT_1_FACT_IDS]
    text = "a|b <b>bold</b> [link](x) `code` back\\slash *star* _under_ # <script>alert(1)</script>"
    hostile = bare()
    hostile["created_at"] = text
    hostile["run"]["reference"] = text
    hostile["facts"][0]["detail"] = text
    hostile["facts"][3] = {**hostile["facts"][3], "provenance": "unknown", "value": None, "detail": " \ttab\nline\x00nul\x1f\x7f \u00a0\u2028\u200b\ufeff\u3000 wide \U0001F600 "}
    hostile["facts"][4]["detail"] = " \n\u200b "  # nothing is left of it: no colon after the id
    hostile["facts"].append({"id": text, "value": None, "provenance": "withheld", "source": None, "detail": ""})
    hostile["facts"].append({"id": "x\n|y", "value": None, "provenance": "withheld", "source": None, "detail": ""})
    hostile["facts"].append({"id": text + "2", "value": None, "provenance": "unknown", "source": None, "detail": text})  # no question: not on the sheet
    # Two entries with one id: the first counts, whichever it is.
    hostile["facts"].append({"id": "hosting.model", "value": "hybrid", "provenance": "declared", "source": None, "detail": ""})
    hostile["facts"].insert(0, {"id": "proxy.kind", "value": "nginx", "provenance": "declared", "source": None, "detail": ""})
    files = [
        ("nothing answered", bare()),
        ("everything answered", answered),
        ("nothing open", nothing_open),
        ("withheld facts", withheld),
        ("written by 0.2.0", older),
        ("Markdown and HTML in its text", hostile),
        ("measured by every source", built()),
    ]
    for _, data in files:
        profile.validate(data)
    return files


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
        # The six a file must hold, `debug_info`, which a profile lists from 0.4.0 on, and `api`, from 0.5.0 on.
        self.assertEqual(list(built()["sources"]), [*report.SECTIONS, "debug_info", "api"])
        for section in built()["sources"]:
            self.assertRegex(APP_JS, rf"\n    {section}: \{{ label: ")

    def test_the_debug_information_is_the_first_source_of_the_form(self):
        self.assertEqual(
            re.findall(r'<fieldset class="source" data-source="(\w+)">', INDEX_HTML),
            ["debug_info", "url", "env", "postgres", "redis", "kubernetes"],
        )
        self.assertEqual(web.meta()["sources"][0]["id"], "debug_info")
        hint = web.meta()["sources"][0]["hint"]
        for said in ("Help > About n8n > Debug information > Copy", "instance ids, host ids and the browser's user agent", "discarded"):
            self.assertIn(said, hint)
        field = re.search(r'<textarea id="f-debug"[^>]*>', INDEX_HTML).group(0)
        # Labelled, tied to its error, and never sent to a spelling service or kept by the browser.
        for attribute in ('name="debug_info_text"', 'spellcheck="false"', 'autocomplete="off"', 'aria-describedby="e-debug"'):
            self.assertIn(attribute, field)
        self.assertIn('<label for="f-debug">', INDEX_HTML)
        self.assertIn('<p class="field__error" id="e-debug" hidden></p>', INDEX_HTML)
        self.assertIn(f"const MAX_DEBUG_INFO_BYTES = {debug_info.MAX_BYTES // 1024} * 1024;", APP_JS)

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
            '      check: ids.has("check") ? WITHHELD_CHECK : withholdChecks(profile.check, ids),',
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
            'return withholdEntries(declareAnswers(report.profile, operatorQuestions(), answers), withheld, $("f-reason").value, $("f-reference").value);',
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
        self.assertIn("tick(`w-check-${index}`, checkKey(entry), ", sections)
        self.assertEqual(sections.count("tick("), 3)
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
            "answers.clear();",
            '$("f-reason").value = Object.keys(PROFILE_REASONS)[0];',
            '$("f-reference").value = "";',
        ):
            self.assertIn(line, body)
        self.assertEqual(next(iter(js_constant("PROFILE_REASONS"))), "baseline")
        report_body = js_function("renderReport")
        self.assertLess(report_body.index("resetShare();"), report_body.index("renderQuestions();"))
        self.assertLess(report_body.index("renderQuestions();"), report_body.index("renderShare();"))
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

    # --- questions: the page asks what the server lists and stores what profile.py stores

    def test_an_answered_fact_is_left_with_what_apply_answers_leaves(self):
        answered = profile.apply_answers(bare(), {"hosting.model": "hybrid"})[0]
        fact = next(f for f in answered["facts"] if f["id"] == "hosting.model")
        self.assertEqual(js_constant("DECLARED_FACT"), {k: v for k, v in fact.items() if k not in ("id", "value")})
        body = js_function("declareAnswers")
        for line in (
            '        const question = fact.provenance === "unknown" ? questions.find((item) => item.id === fact.id) : undefined;',
            "        const value = question && answers.has(fact.id) ? answerValue(question, answers.get(fact.id)) : undefined;",
            "        return value === undefined ? fact : { ...fact, value, ...DECLARED_FACT };",
        ):
            self.assertIn(line + "\n", body)
        self.assertEqual(APP_JS.count("declareAnswers("), 2)  # its definition, and the one profile the share step shows and saves
        self.assertEqual(APP_JS.count('"declared"'), 5)  # the format's list, this constant, and three times where a known fact is shown or counted

    def test_the_page_holds_no_copy_of_the_catalogue(self):
        page = APP_JS + INDEX_HTML
        for question in questions.OPERATOR:
            self.assertNotIn(question["question"], page)
            if question["id"] not in profile.FORMAT_1_FACT_IDS:
                self.assertNotIn(question["id"], page)
        for sentence in list(questions.MEASURED.values()) + [text for pair in questions.CONVERSATION for text in pair]:
            self.assertNotIn(sentence, page)
        for value, label in questions.OPERATOR_BY_ID[MULTI]["values"]:
            if value != "none":  # the one value with a rule of its own
                self.assertNotIn(f'"{value}"', page)
            self.assertNotIn(f'"{label}"', page)
        self.assertEqual(APP_JS.count("meta.questions"), 4)  # the share step's questions, and the viewer's two sheets

    def test_the_sheets_fixed_text_is_the_one_render_questions_prints(self):
        block = re.search(r"const SHEET = \{\n(.*?)\n  \};", APP_JS, re.S).group(1)
        texts = [json.loads(text) for text in re.findall(r'"(?:[^"\\]|\\.)*"', block)]
        self.assertEqual(len(texts), 9)
        printed = profile.render_questions(dict(sheet_files())["nothing open"]) + profile.render_questions(dict(sheet_files())["withheld facts"])
        for text in texts:
            self.assertIn(text, printed)

    def test_a_question_is_a_labelled_group_with_nothing_chosen(self):
        body = js_function("renderQuestions")
        self.assertIn('return el("fieldset", { class: "question" },\n        el("legend", null,', body)
        self.assertIn('type: multi ? "checkbox" : "radio"', body)
        self.assertIn('checked: !multi && value === "",', body)  # only "No answer" starts selected
        self.assertIn('multi ? null : option("", "No answer", 0),', body)
        self.assertIn("...question.values.map((item, number) => option(item.value, item.label, number + 1))", body)
        self.assertIn('report.profile.facts.some((fact) => fact.id === question.id && fact.provenance === "unknown")', body)
        for other_control in ('"select"', '"option"', '"text"', "textarea"):
            self.assertNotIn(other_control, body)
        self.assertIn('id="questions-block" aria-labelledby="questions-title" hidden>', INDEX_HTML)
        self.assertLess(INDEX_HTML.index('id="questions-list"'), INDEX_HTML.index('id="share-body"'))

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


SAVED_ANSWERED = (
    "profileText(withholdEntries(declareAnswers(parseExact(input.response).profile, input.questions,"
    " new Map(Object.entries(input.answers))), new Set(input.ids), input.reason, input.reference))"
)


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

    def test_the_saved_file_with_answers_is_the_one_profile_py_writes(self):
        # A choice, both yes/no values, a multi ticked out of order, and an answer to a fact that was measured.
        answers = {"hosting.model": "private_cloud", "worker.autoscaling": "no", "n8n.image_modified": "yes", MULTI: ["splunk", "prometheus"], "orchestrator": "nomad"}
        base = profile.build_profile(PLAIN, [FINDING], ("postgres",))  # no number an older browser spells differently
        declared, dropped = profile.apply_answers(base, answers)
        self.assertEqual(dropped, ["orchestrator"])
        for raw in PATHS:
            for ids in ([], ["worker.autoscaling", MULTI, "n8n.version", "check"]):
                saved = run_page_code(SAVED_ANSWERED, raw=raw, response=response_text(base), ids=ids, reason="incident", reference=" x ", answers=answers, questions=CATALOGUE["operator"])
                expected = profile.apply_withholding(declared, ids)
                expected["run"] = {"reason": "incident", "reference": "x"}
                with self.subTest(rawJSON=raw, withheld=ids):
                    self.assertEqual(profile.validate(json.loads(saved)), expected)
                    if raw:
                        self.assertEqual(saved, profile.to_json(expected))
                    self.assertIn('"value": "kubernetes"', saved)  # measured wins
                    self.assertNotIn("nomad", saved)
                    self.assertEqual('"splunk": true' in saved, not ids)
        facts = {f["id"]: f for f in declared["facts"]}
        self.assertIs(facts["worker.autoscaling"]["value"], False)
        self.assertEqual(list(facts[MULTI]["value"].items()), [("prometheus", True), ("splunk", True)])  # catalogue order

    def test_every_question_answered_is_the_file_profile_py_writes(self):
        base = bare()
        saved = run_page_code(SAVED_ANSWERED, raw=PATHS[0], response=response_text(base), ids=[], reason="baseline", reference="", answers=EVERY_ANSWER, questions=CATALOGUE["operator"])
        expected, dropped = profile.apply_answers(base, EVERY_ANSWER)
        self.assertEqual(dropped, [])
        self.assertEqual(profile.validate(json.loads(saved)), expected)
        self.assertEqual(sum(f["provenance"] == "declared" for f in expected["facts"]), 15)
        if PATHS[0]:
            self.assertEqual(saved, profile.to_json(expected))

    def test_an_answer_is_stored_or_refused_as_check_answers_does(self):
        cases = [
            ("hosting.model", "hybrid"), ("hosting.model", "nope"), ("hosting.model", ""), ("hosting.model", ["hybrid"]),
            ("hosting.model", "Hybrid"), ("hosting.model", True), ("hosting.model", None), ("hosting.model", {"hybrid": True}),
            ("worker.autoscaling", "yes"), ("worker.autoscaling", "no"), ("worker.autoscaling", True), ("worker.autoscaling", "true"),
            (MULTI, ["none"]), (MULTI, ["none", "grafana"]), (MULTI, ["grafana", "none"]), (MULTI, []), (MULTI, "grafana"),
            (MULTI, ["grafana", "grafana"]), (MULTI, ["splunk", "prometheus"]), (MULTI, ["bogus"]), (MULTI, ["grafana", "bogus"]),
            (MULTI, ["grafana", True]), (MULTI, None), (MULTI, {"grafana": True}),
            (MULTI, [v for v, _ in questions.OPERATOR_BY_ID[MULTI]["values"] if v != "none"]),
        ]  # fmt: skip
        got = run_page_code(
            "input.cases.map(([id, answer]) => { const value = answerValue(input.questions.find((q) => q.id === id), answer);"
            " return value === undefined ? null : JSON.stringify(value); })",
            cases=cases, questions=CATALOGUE["operator"],
        )  # fmt: skip
        accepted = 0
        for (fact_id, answer), stored in zip(cases, got):
            try:
                expected = json.dumps(profile.check_answers({fact_id: answer})[fact_id], separators=(",", ":"))
                accepted += 1
            except profile.ProfileError:
                expected = None
            with self.subTest(question=fact_id, answer=answer):
                self.assertEqual(stored, expected)
        self.assertEqual(accepted, 6)

    def test_the_sheet_is_the_one_render_questions_prints(self):
        files = sheet_files()
        for raw in PATHS:
            got = run_page_code(
                "input.files.map((text) => sheetMarkdown(parseExact(text), input.catalogue))",
                raw=raw, files=[profile.to_json(data) for _, data in files], catalogue=CATALOGUE,
            )  # fmt: skip
            for (label, data), sheet in zip(files, got):
                with self.subTest(rawJSON=raw, file=label):
                    self.assertEqual(sheet, profile.render_questions(data))
        # The comparison covers every shape of the sheet.
        printed = {label: profile.render_questions(data) for label, data in files}
        self.assertEqual(printed["nothing open"].count("Nothing is open here."), 2)
        self.assertEqual(printed["everything answered"].count("Nothing is open here."), 1)
        self.assertNotIn("Withheld by the sender", printed["nothing answered"])
        self.assertIn("Withheld by the sender, not questions to press on: n8n.version, executions.mode, hosting.model, observability.tools.", printed["withheld facts"])
        older = dict(files)["written by 0.2.0"]
        self.assertEqual(len(older["facts"]), 32)
        unknown = sum(fact["provenance"] == "unknown" for fact in older["facts"])  # orchestrator among them
        self.assertEqual(printed["written by 0.2.0"].count("\n- **"), 3 + unknown + 14 + 13 + 8 + 10)  # the header, and the 14 + 13 + 8 it never held
        self.assertIn("\\<script\\>", printed["Markdown and HTML in its text"])
        self.assertIn("(`executions.mode`): tab line nul wide", printed["Markdown and HTML in its text"])

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
    data = dict(data)
    request = {
        "app": str(web.STATIC_DIR / "app.js"),
        "index": str(web.STATIC_DIR / "index.html"),
        "scenario": scenario,
        "input": data,
        "withoutRawJSON": not raw,
        "meta": {} if data.pop("no_questions", False) else {"questions": CATALOGUE},
        "noClipboard": data.pop("no_clipboard", False),
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

    # --- the debug information ------------------------------------------------------

    def test_the_pasted_debug_information_is_sent_as_it_is_and_alone(self):
        got = run_page(
            """async (page, input) => {
              await page.submit();
              const empty = { sent: page.sent.length, error: page.visible("form-error") && page.$("form-error-text").textContent };
              page.type("f-debug", input.text);
              await page.submit();
              return { empty, sent: page.sent.map((body) => JSON.parse(body)), busy: page.$("run-button").getAttribute("aria-disabled") };
            }""",
            text=pasted.FULL,
        )
        self.assertEqual(got["empty"], {"sent": 0, "error": "Fill in at least one source above, then run the health check."})
        self.assertEqual(got["sent"], [{"debug_info_text": pasted.FULL}])
        self.assertEqual(got["busy"], "true")

    def test_a_refused_debug_information_is_shown_next_to_its_field(self):
        for sentence in (debug_info.NOT_DEBUG_INFO, debug_info.SEVERAL, debug_info.TOO_LARGE):
            got = run_page(
                """async (page, input) => {
                  page.type("f-debug", input.text);
                  await page.submit();
                  await page.finishRun(JSON.stringify({ error: input.sentence, field: "debug_info_text" }), 400);
                  const refused = { view: page.view(), error: page.visible("e-debug") && page.$("e-debug").textContent,
                                    invalid: page.$("f-debug").getAttribute("aria-invalid"), focus: document.activeElement.id,
                                    banner: page.visible("form-error"), kept: page.$("f-debug").value === input.text,
                                    busy: page.$("run-button").getAttribute("aria-disabled") };
                  return { refused };
                }""",
                text=f"hello {pasted.MARK}", sentence=sentence,
            )  # fmt: skip
            with self.subTest(sentence=sentence):
                said = sentence[0].upper() + sentence[1:] + "."
                self.assertEqual(got["refused"], {"view": "form", "error": said, "invalid": "true", "focus": "f-debug", "banner": False, "kept": True, "busy": None})

    def test_a_400_about_something_else_is_still_the_banner(self):
        got = run_page(
            """async (page, input) => {
              page.type("f-debug", input.text);
              await page.submit();
              await page.finishRun(JSON.stringify({ error: "url must be an http:// or https:// URL with a host" }), 400);
              return { field: page.visible("e-debug"), banner: page.visible("form-error") && page.$("form-error-title").textContent };
            }""",
            text=pasted.FULL,
        )
        self.assertEqual(got, {"field": False, "banner": "The server did not accept the request."})

    def test_a_paste_over_64_kb_is_stopped_in_the_page_next_to_its_field(self):
        got = run_page(
            """async (page, input) => {
              page.$("f-env").value = "EXECUTIONS_MODE=queue";
              page.type("f-debug", input.text + "\\n".repeat(input.room + 1));
              await page.submit();
              const over = { sent: page.sent.length, error: page.visible("e-debug") && page.$("e-debug").textContent,
                             other: page.visible("e-env"), focus: document.activeElement.id };
              page.type("f-debug", input.text + "\\n".repeat(input.room));
              await page.submit();
              return { over, sent: page.sent.length, bytes: new TextEncoder().encode(JSON.parse(page.sent[0]).debug_info_text).length };
            }""",
            text=pasted.FULL, room=debug_info.MAX_BYTES - len(pasted.FULL.encode()),
        )  # fmt: skip
        self.assertEqual((got["over"]["sent"], got["over"]["other"], got["over"]["focus"]), (0, False, "f-debug"))
        self.assertIn("the limit is 64 KB", got["over"]["error"])
        self.assertNotIn(pasted.MARK, got["over"]["error"])
        self.assertEqual((got["sent"], got["bytes"]), (1, debug_info.MAX_BYTES))

    def test_a_score_names_the_most_severe_finding_of_its_area_and_takes_its_colour(self):
        sent = cli.json_safe(web.run_check(web.parse_run_request(json.dumps({"debug_info_text": pasted.FULL}).encode())))
        finding = lambda area, severity: {"rule_id": "X-001", "severity": severity, "area": area, "title": "A title", "evidence": "e", "impact": "i", "recommendation": "r"}  # noqa: E731
        sent["findings"] = [finding("reliability", "critical"), finding("security", "warning"), finding("reliability", "info"), finding("performance", "info"), finding("other", "new")]
        sent["scores"] = [
            {"area": "reliability", "score": 55, "partial": False, "findings": 2},
            {"area": "security", "score": 85, "partial": True, "findings": 1},
            {"area": "performance", "score": 95, "partial": False, "findings": 1},
            {"area": "scalability", "score": 100, "partial": False, "findings": 0},
            {"area": "unread", "score": 100, "partial": True, "findings": 0},  # almost nothing was checked: not green
            {"area": "other", "score": 90, "partial": False, "findings": 1},  # a severity this page does not know
            {"area": "maintainability", "score": None, "partial": False, "findings": 0},
        ]
        got = run_page(
            """async (page, input) => {
              page.type("f-debug", input.text);
              await page.submit();
              await page.finishRun(input.response);
              const read = () => ({ scores: page.$("scores").children.map((card) => [card.className, card.textContent]), stats: page.$("stats").children.map((item) => item.className),
                                    findings: page.walk(page.$("findings")).filter((node) => node.tagName === "li").map((node) => [node.className, node.attributes["data-severity"]]) });
              const first = read();
              page.$("act-again").click();
              await page.submit();
              await page.finishRun(input.withoutInfo);
              return { ...first, withoutInfo: read().stats };
            }""",
            raw=PATHS[0], response=json.dumps(sent, allow_nan=False), text=pasted.FULL,
            withoutInfo=json.dumps({**sent, "findings": [f for f in sent["findings"] if f["severity"] != "info"]}, allow_nan=False),
        )  # fmt: skip
        self.assertEqual(got["scores"], [
            ["score score--critical", "reliability55/ 1002 findings · worst: critical"],
            ["score score--partial score--warning", "security85/ 100Partial1 finding · worst: warning"],
            ["score score--info", "performance95/ 1001 finding · worst: info"],
            ["score score--ok", "scalability100/ 100No findings"],
            ["score score--partial", "unread100/ 100PartialNo findings"],
            ["score", "other90/ 1001 finding"],
            ["score score--none", "maintainabilityNot scoredNo data was collected"],
        ])  # fmt: skip
        self.assertEqual(got["stats"][:3], ["stat stat--critical", "stat stat--warning", "stat stat--info"])
        self.assertEqual(got["withoutInfo"][:3], ["stat stat--critical", "stat stat--warning", "stat"])  # a zero has no colour
        # A severity this page does not know is drawn as info, and its title is not coloured: the
        # stylesheet colours a title by the severity the finding really has, for the three known ones only.
        self.assertIn(["finding finding--info", "new"], got["findings"])
        css = (web.STATIC_DIR / "app.css").read_text(encoding="utf-8")
        self.assertNotIn("color", re.search(r"\n\.finding \.finding__title \{[^}]*\}", css).group())
        self.assertIn('.finding[data-severity="critical"] .finding__title,\n.finding[data-severity="warning"] .finding__title,\n.finding[data-severity="info"] .finding__title { color: var(--sev); }', css)

    def test_a_run_on_the_debug_information_alone_is_a_partial_report_not_a_failed_one(self):
        sent = web.run_check(web.parse_run_request(json.dumps({"debug_info_text": pasted.FULL}).encode()))
        response = json.dumps(cli.json_safe(sent), allow_nan=False)
        for raw in PATHS:
            got = run_page(
                """async (page, input) => {
                  page.type("f-debug", input.text);
                  await page.submit();
                  await page.finishRun(input.response);
                  const saved = await page.download();
                  const first = { view: page.view(), label: page.$("status-label").textContent, partial: page.visible("status-partial"),
                           summary: page.$("summary").textContent, stats: page.$("stats").textContent,
                           points: page.visible("summary-points") && page.$("summary-points").textContent,
                           urgent: page.visible("urgent") && [page.$("urgent-title").textContent, page.$("urgent-text").textContent],
                           note: page.visible("status-note"), meta: page.$("report-meta").textContent,
                           scores: page.$("scores").textContent, scoresNote: page.$("scores-note").textContent,
                           assessed: page.$("not-assessed").textContent, share: page.$("share-body").textContent, saved,
                           report: page.$("view-report").textContent };
                  page.$("act-again").click();
                  const again = { view: page.view(), kept: page.$("f-debug").value === input.text };
                  const opened = await page.open(saved);
                  return { ...first, again, opened };
                }""",
                raw=raw, response=response, text=pasted.FULL,
            )  # fmt: skip
            with self.subTest(rawJSON=raw):
                self.assertEqual(got["view"], "report")
                self.assertNotIn("could not run", got["label"])
                self.assertTrue(got["partial"])
                # The Summary of the report in parts: the counts, the first fix, then every other
                # sentence as the API wrote it, with its lines about the debug information.
                notes, top = sent["summary_notes"], sent["findings"][0]
                self.assertEqual(got["summary"], notes[0])
                self.assertEqual(got["points"], "".join(notes[1:]))
                self.assertIn("Debug information generated on 2026-11-19 was read.", got["points"])
                self.assertIn("rests on the debug information only", got["points"])
                counts = [sum(f["severity"] == s for f in sent["findings"]) for s in ("critical", "warning", "info")]
                self.assertEqual(got["stats"], f"{counts[0]} critical{counts[1]} warning{'' if counts[1] == 1 else 's'}{counts[2]} info0/6 data sources readdebug information read")
                self.assertEqual(sent["sources"], {"assessed": 0, "total": 6, "debug_info": True})
                self.assertEqual(got["urgent"], [top["title"], top["recommendation"]])
                self.assertFalse(got["note"])
                self.assertIn("n8n 3.3.0", got["meta"])
                self.assertEqual(got["scores"].count("Partial"), len(sent["scores"]))
                self.assertNotIn("Not scored", got["scores"])
                self.assertIn("reflect only the checks that ran", got["scoresNote"])
                self.assertEqual(got["assessed"].count("Not provided"), len(report.SECTIONS))
                # The share step and the viewer name the source and show the facts filled from the text.
                for shown in (got["share"], got["opened"]["shown"]):
                    self.assertIn("n8n debug information (debug_info)collected", shown)
                    self.assertIn("main.concurrency_limitunlimitedmeasuredfrom debug_infodebug information generated on 2026-11-19", shown)
                self.assertEqual((got["opened"]["view"], got["opened"]["error"]), ("profile", ""))
                self.assert_saved(got["saved"], sent["profile"], raw)
                # "Run again" goes back to the form with the text still in its field, like the other sources.
                self.assertEqual(got["again"], {"view": "form", "kept": True})
                for text in (got["report"], got["saved"], got["opened"]["shown"], response):
                    self.assertNotIn(pasted.MARK, text)

    def test_a_check_that_could_not_run_shows_its_summary_whole_without_the_parts(self):
        response = run_response(Snapshot())
        sent = json.loads(response)
        self.assertEqual((sent["exit_code"], sent["summary_notes"]), (3, None))
        got = run_page(
            """async (page, input) => {
              await page.run(input.response);
              return { label: page.$("status-label").textContent, summary: page.$("summary").textContent,
                       parts: ["stats", "urgent", "summary-points"].map((id) => [page.visible(id), page.$(id).textContent]) };
            }""",
            response=response,
        )
        self.assertEqual(got["label"], "The check could not run")
        self.assertEqual(got["summary"], sent["summary"])
        # "urgent" is the title and the text the page fills; its static label is not in the harness.
        self.assertEqual(got["parts"], [[False, ""]] * 3)

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
                self.assertIn("1 of 67 facts, 2 of ", got["summary"])

    def test_answers_are_saved_as_profile_py_writes_them(self):
        answers = {"hosting.model": "private_cloud", "worker.autoscaling": "no", "n8n.image_modified": "yes", MULTI: ["grafana", "prometheus"]}
        response = run_response()
        sent = json.loads(response)["profile"]
        declared, dropped = profile.apply_answers(sent, answers)
        self.assertEqual(dropped, [])
        for raw in PATHS:
            got = run_page(
                """async (page, input) => {
                  await page.run(input.response);
                  const groups = page.questions();
                  const seen = { block: page.visible("questions-block"), empty: page.visible("questions-empty"), ids: Object.keys(groups),
                                 atStart: Object.values(groups).flatMap((group) => Object.values(group)).filter((input) => input.checked).map((input) => input.value),
                                 types: Object.fromEntries(Object.entries(groups).map(([id, group]) => [id, [...new Set(Object.values(group).map((input) => input.type))].join()])),
                                 values: Object.fromEntries(Object.entries(groups).map(([id, group]) => [id, Object.keys(group)])),
                                 legends: page.walk(page.$("questions-list")).filter((node) => node.tagName === "legend").map((node) => node.textContent) };
                  page.choose(groups["hosting.model"]["private_cloud"]);
                  page.choose(groups["worker.autoscaling"]["no"]);
                  page.choose(groups["n8n.image_modified"]["yes"]);
                  page.choose(groups["proxy.kind"]["nginx"]);
                  page.choose(groups["proxy.kind"][""]);  // back to "No answer"
                  const tools = groups["observability.tools"];
                  page.toggle(tools["none"]);
                  page.toggle(tools["grafana"]);  // "none" gives way
                  page.toggle(tools["prometheus"]);  // ticked after grafana, saved before it
                  seen.noneGaveWay = !tools["none"].checked;
                  seen.first = await page.download();
                  seen.table = page.$("share-body").textContent;
                  seen.summary = page.$("share-summary").textContent;
                  seen.boxes = Object.keys(page.boxes()).length;
                  const notes = () => page.walk(page.$("questions-list")).filter((node) => node.className === "question__note" && !node.hidden).length;
                  seen.notesBefore = notes();
                  page.toggle(page.boxes()["worker.autoscaling"]);  // an answer, then withheld
                  page.toggle(page.boxes()["n8n.version"]);
                  seen.notesAfter = notes();
                  seen.second = await page.download();
                  seen.tableAfter = page.$("share-body").textContent;
                  page.toggle(tools["none"]);  // and the other way round
                  seen.othersGaveWay = !tools["grafana"].checked && !tools["prometheus"].checked;
                  seen.third = await page.download();
                  page.toggle(tools["none"]);  // nothing ticked: no answer
                  seen.fourth = await page.download();
                  return seen;
                }""",
                raw=raw, response=response,
            )  # fmt: skip
            with self.subTest(rawJSON=raw):
                # Kubernetes was read, so the orchestrator is measured and not asked about.
                self.assertEqual((got["block"], got["empty"]), (True, False))
                self.assertEqual(got["ids"], [q["id"] for q in questions.OPERATOR if q["id"] != "orchestrator"])
                self.assertEqual(got["atStart"], [""] * 13)  # "No answer" of each radio group, and no checkbox
                for question in questions.OPERATOR[:2] + questions.OPERATOR[3:]:
                    multi = question["kind"] == "multi"
                    self.assertEqual(got["types"][question["id"]], "checkbox" if multi else "radio")
                    self.assertEqual(got["values"][question["id"]], ([] if multi else [""]) + [v for v, _ in question["values"]])
                    self.assertTrue(any(legend.startswith(question["question"]) for legend in got["legends"]))
                self.assertEqual(len(got["legends"]), 14)
                self.assertTrue(got["noneGaveWay"] and got["othersGaveWay"])
                self.assert_saved(got["first"], declared, raw)
                self.assertIn("hosting.modelprivate_clouddeclared", got["table"])
                self.assertIn("worker.autoscalingnodeclared", got["table"])
                self.assertIn("observability.toolsprometheus: yes, grafana: yesdeclared", got["table"])
                self.assertIn("proxy.kindunknownnot answered", got["table"])
                self.assertIn("orchestratorkubernetesmeasured", got["table"])
                self.assertIn("Answered: 4 of 14 questions. Withheld: 0 of 67 facts", got["summary"])
                self.assertEqual(got["boxes"], len(all_ids(sent)))
                self.assertEqual((got["notesBefore"], got["notesAfter"]), (0, 1))
                self.assert_saved(got["second"], profile.apply_withholding(declared, ["worker.autoscaling", "n8n.version"]), raw)
                self.assertIn("worker.autoscalingwithheld", got["tableAfter"])
                only_none = profile.apply_answers(sent, {**answers, MULTI: ["none"]})[0]
                self.assert_saved(got["third"], profile.apply_withholding(only_none, ["worker.autoscaling", "n8n.version"]), raw)
                unanswered = profile.apply_answers(sent, {k: v for k, v in answers.items() if k != MULTI})[0]
                self.assert_saved(got["fourth"], profile.apply_withholding(unanswered, ["worker.autoscaling", "n8n.version"]), raw)

    def test_every_question_can_be_answered_and_a_measured_fact_is_not_asked(self):
        without_kubernetes = dataclasses.replace(PLAIN, kubernetes=None)
        response = run_response(without_kubernetes)
        sent = json.loads(response)["profile"]
        scenario = """async (page, input) => {
          await page.run(input.response);
          const groups = page.questions();
          for (const [id, answer] of Object.entries(input.answers)) {
            if (!groups[id]) continue;
            if (Array.isArray(answer)) for (const value of answer) page.toggle(groups[id][value]);
            else page.choose(groups[id][answer]);
          }
          return { block: page.visible("questions-block"), empty: page.visible("questions-empty"), asked: Object.keys(groups).length,
                   summary: page.$("share-summary").textContent, saved: await page.download() };
        }"""
        got = run_page(scenario, raw=PATHS[0], response=response, answers=EVERY_ANSWER)
        self.assertEqual((got["block"], got["empty"], got["asked"]), (True, False, 15))
        self.assertIn("Answered: 15 of 15 questions.", got["summary"])
        self.assert_saved(got["saved"], profile.apply_answers(sent, EVERY_ANSWER)[0], PATHS[0])
        # Every one of them measured (no probe can, today): nothing is asked, and a line says so.
        measured = copy.deepcopy(sent)
        for fact in measured["facts"]:
            if fact["id"] in questions.OPERATOR_BY_ID:
                fact.update(value="x", provenance="measured", source="env", detail="")
        got = run_page(scenario, raw=PATHS[0], response=json.dumps({**json.loads(response), "profile": measured}), answers=EVERY_ANSWER)
        self.assertEqual((got["block"], got["empty"], got["asked"]), (True, True, 0))
        self.assertTrue(got["summary"].startswith("Withheld: "))
        self.assert_saved(got["saved"], measured, PATHS[0])
        # A server that sends no questions (an older one behind a newer page): the section is not shown, the rest works.
        got = run_page(scenario, raw=PATHS[0], response=response, answers=EVERY_ANSWER, no_questions=True)
        self.assertEqual((got["block"], got["asked"]), (False, 0))
        self.assert_saved(got["saved"], sent, PATHS[0])

    def test_the_copied_sheet_is_the_one_show_questions_prints(self):
        files = sheet_files()
        scenario = """async (page, input) => {
          const seen = [];
          for (const text of input.files) {
            const opened = await page.open(text);
            const status = page.walk(page.$("profile-body")).filter((node) => node.getAttribute("role") === "status")[1];  // after the call sheet's
            page.$("sheet-copy").click();
            await page.settle();
            const box = page.$("sheet-text");
            seen.push({ view: opened.view, shown: opened.shown, copied: page.copied.length, text: page.copied[page.copied.length - 1],
                        status: status.textContent, boxShown: !box.hidden, box: box.value });
            page.$("profile-close").click();
          }
          return seen;
        }"""
        for raw in PATHS:
            got = run_page(scenario, raw=raw, files=[profile.to_json(data) for _, data in files])
            for number, ((label, data), seen) in enumerate(zip(files, got), 1):
                with self.subTest(rawJSON=raw, file=label):
                    self.assertEqual((seen["view"], seen["copied"]), ("profile", number))
                    self.assertEqual(seen["text"], profile.render_questions(data))
                    self.assertEqual(seen["status"], "Question sheet copied to the clipboard as Markdown.")
                    self.assertFalse(seen["boxShown"])
                    # On the page: the same questions, the file's text as it is.
                    self.assertIn("Questions still open", seen["shown"])
                    facts = {}
                    for fact in data["facts"]:
                        facts.setdefault(fact["id"], fact)
                    for question in CATALOGUE["measured"] + CATALOGUE["operator"]:
                        is_open = question["id"] not in facts or facts[question["id"]]["provenance"] == "unknown"
                        self.assertEqual(f"{question['question']} {question['id']}" in seen["shown"], is_open, question["id"])
                    for question in CATALOGUE["conversation"]:
                        self.assertIn(question["question"] + question["why"], seen["shown"])
        # The browser refuses the clipboard: the same text is offered in a box, and a line says so.
        label, data = files[-2]
        got = run_page(scenario, raw=PATHS[0], files=[profile.to_json(data)], no_clipboard=True)[0]
        self.assertEqual((got["copied"], got["boxShown"]), (0, True))
        self.assertEqual(got["box"], profile.render_questions(data))
        self.assertIn("did not allow copying", got["status"])
        # Without the questions of the server there is no sheet, and the profile is shown all the same.
        got = run_page("async (page, input) => (await page.open(input.file))", raw=PATHS[0], file=profile.to_json(data), no_questions=True)
        self.assertEqual(got["view"], "profile")
        self.assertIn("The list of questions did not load", got["shown"])
        self.assertNotIn("Ask in conversation", got["shown"])

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
        self.assertEqual(len(facts), 67)
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
                  page.choose(page.questions()["hosting.model"]["hybrid"]);
                  page.toggle(page.questions()["observability.tools"]["grafana"]);
                  page.$("act-again").click();
                  await page.run(input.second);
                  const chosen = Object.values(page.questions()).flatMap((group) => Object.values(group)).filter((input) => input.checked && input.value);
                  return { ticked: Object.values(page.boxes()).filter((box) => box.checked).length + chosen.length, reason: page.$("f-reason").value,
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
        hostile["facts"].append({"id": mark + ".y", "value": None, "provenance": "withheld", "source": None, "detail": ""})  # named on the sheet
        hostile["facts"][0] = {"id": "n8n.version", "value": None, "provenance": "unknown", "source": None, "detail": mark}  # its detail too
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
        self.assertGreater(got["shown"].count(mark), 17)  # it is all there, as text
        self.assertIn(f"Which n8n version runs? n8n.version{mark}", got["shown"])
        self.assertIn(f"not questions to press on: {mark}.y.", got["shown"])
        self.assertIn(mark, got["meta"])
        # No element is named by the file, and no attribute holds text of the file.
        self.assertEqual(
            set(got["tags"]) - {"a", "option"},  # made by the page at start-up and on a download, not from a file
            {"section", "h3", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "th", "td", "p", "span", "ul", "li", "wbr", "svg", "path"}
            | {"h4", "strong", "code", "div", "button", "textarea"}  # the question sheet and its copy button
            | {"ol"},  # the call sheet's findings
        )
        self.assertEqual([entry for entry in got["attributes"] if "<" in entry or "alert" in entry], [])
        self.assertLessEqual({entry.split("=")[0] for entry in got["attributes"]}, {"scope", "colspan", "translate", "viewBox", "aria-hidden", "focusable", "d", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-dasharray"}
            | {"type", "id", "role", "aria-live", "rows", "readonly", "hidden", "aria-label"})  # of the sheet's own controls, fixed text


if __name__ == "__main__":
    unittest.main()
