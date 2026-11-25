# Call sheet: what works, what does not, what to improve

Status: approved by the owner on 2026-11-24.
Target version: 0.6.0.

## Intent

The tool is run by the person who operates the instance (the operator). The
person who supports them (the reader) usually has no access to the instance and
receives one file: the environment profile. From that file alone the reader
must be able to talk through three things with the operator: what works well
today, what does not, and what to improve for the stability of the system.

Today the profile cannot carry that conversation:

- A rule returns a finding or nothing, and "nothing" means three different
  things: the check passed, the check does not apply to this setup, or the data
  to run it was missing. The reader cannot tell them apart.
- A finding reaches the profile as a title only. What goes wrong and how to fix
  it stay in the report, which quotes raw values and is not meant to leave the
  operator's company.

The operator is attentive to security and to what they share. Everything this
piece adds to the file must be visible to them before they send it, and must
hold no value of their instance.

## Goals

1. Every rule states one outcome per run: `finding`, `passed`, `not_assessed`
   or `not_applicable`.
2. The profile carries the outcome of every rule, with fixed texts.
3. From a profile, the tool produces a call sheet for the reader: what works
   well, what does not, what to improve, what was not assessed and what the
   operator would have to provide for it.
4. The operator sees, in the existing review of the profile, everything that
   was added.

## Non-goals

- Findings about capacity from the usage numbers. A later piece; until then
  the "future" part of the sheet is only the existing rules.
- What could break in an upgrade. The next piece; it will be one more section
  of this sheet. (Decided for it already: its catalogue of changes starts at
  n8n 2.30.)
- A page for the operator that states what the tool does, source by source.
  A separate small piece.
- Any change to the report, to the scores or to the penalty of a finding.
- Sending the profile anywhere, or any mapping to a system of customer records.

## Anti-goals

- "Works well" said of something the tool did not positively verify.
- A text in the new part of the file that holds a value read from the
  instance, or anything the profile already forbids (host, URL, IP, path,
  e-mail, namespace, Deployment or registry name, secret).
- Something in the file that `--show-profile` and the page's review step do
  not show.
- A profile of 0.6.0 that 0.5.0 refuses to open.
- A weaker zero-leak test.

## Approaches considered

- **A. The profile carries the outcome of every rule (chosen by the owner).**
  One file, computed on the operator's machine; the reader's sheet is a
  rendering of it. Touches every rule and adds a part to the format.
- **B. The reader's side derives everything from the facts.** No format
  change, but the logic would exist twice and drift apart: the operator would
  see one outcome and the reader another. Rejected.
- **C. The operator also sends a cleaned report.** Two files to review, and
  free text is far harder to keep free of raw values than closed facts.
  Rejected: it works against what the operator cares about.

## Decisions made by the owner

- The operator runs the tool; the reader works from the file received.
- The sheet is the reader's own outline during a call, not a document sent to
  the operator.
- It must serve three subjects: what works well, what does not, what to
  improve for future stability.
- It must be clear to the operator what the tool does and computes, and what
  they share.
- Approach A.
- A check passes on an n8n default and says so (`basis`).
- One check can be withheld on its own.

## Design (the calls below are the designer's unless marked as the owner's)

### 1. Four outcomes

Each rule id has exactly one outcome per run.

| Outcome | Meaning |
|---|---|
| `finding` | The rule found a problem. As today. |
| `passed` | The rule read the values it needs and positively verified the good condition. |
| `not_applicable` | The rule does not apply to this setup (a queue rule in regular mode), or the rule stays silent on purpose (a documented default on a stock trial install). |
| `not_assessed` | The rule could not decide: a source was not given, a value was not readable, or the rule raised. |

The line between `passed` and `not_applicable` is the anti-goal: a deliberate
silence is never `passed`. When in doubt, `not_applicable` or `not_assessed`.

A `not_assessed` outcome has a reason and, where a source would help, the
sources that would let the rule decide:

| Reason | Meaning | `needs` |
|---|---|---|
| `source_missing` | No given source states what the rule reads. | The ids of the sources of which any one would let it decide, from: `url`, `env`, `postgres`, `redis`, `kubernetes`, `debug_info`. |
| `value_unreadable` | A source was given but the value is not readable (a Secret, a redacted value, an incomplete environment). | empty |
| `source_failed` | A source that would state it was asked for and could not be collected. | The ids of those sources. |
| `not_stated` | A source was given and read, and does not say what the rule needs: a debug information without a cluster section, Redis without a queue key, an image pinned by digest, Kubernetes mains that disagree. | empty |
| `rule_error` | The rule raised (today's `ENGINE-SKIPPED`). | empty |

The ids in `needs` name what the operator gives, not the profile's `sources`
(`url` covers the health and metrics endpoints).

**Decided by the owner: a good condition that rests on an n8n default.** Today's rules take a variable that is not set, in an environment
read in full, as the n8n default, and raise findings on that basis. The facts
of the profile are stricter and call the same value unknown. A
rule passes on a default as it already fails on one, and the entry says so
with `"basis": "default"` (otherwise `"read"`); the sheet prints "by n8n's
default, not set explicitly" next to it. Where a source only ever reports the
bad case (the debug information lists the two security settings only when
they are insecure), silence is `not_stated`, never `passed`.

How a rule states its outcome (return type, helper, decorator) is left to the
builder. What is fixed: every `return None` of today's rules is classified, no
finding of today changes (same rule id, severity, area, title, evidence,
impact, recommendation for the same snapshot), and the scores stay the same.

### 2. Fixed texts

Each rule has, as literals in the code with no interpolation:

- `passed`: one sentence that states the good condition ("Workers run with at
  least 2 replicas").
- `subject`: a few words naming what the rule checks, for the other outcomes
  ("Worker replicas").

Each finding variant has, also as literals: `title`, `impact` and `remedy`,
which are today's title, impact and recommendation. A variant is one distinct
triple of those texts: a rule has several when it has several titles
(`SCAL-001`, `REL-004`) or when it words the impact or the recommendation by
the state of the instance (`REL-003` by Redis policy, `PERF-003` by execution
mode, `MAINT-001` by mode and multi-main).

(Corrected after the builder check: none of today's impacts and
recommendations holds a value read from the instance; they interpolate only
constants of the code. Only `evidence` holds instance values, and it stays out
of the profile.)

So "fixed" means: every text in the file is one of a closed set of literals
listed in `docs/rules.md`. Which literal is chosen can reveal a state (the
execution mode), as a finding's title already does today.

Each rule also lists what it rests on (`rests_on`), so that the sheet can show
the measured values next to a finding without the evidence text: fact ids and
setting ids (`setting:<scope>:<NAME>`), the same ids `--withhold` takes. Only
ids are written; the values are those already in the file, withheld or not as
the operator chose. An id is listed only when the rule and that entry read the
same thing: where they can disagree (a rule that also judges webhook
containers, next to facts about main and worker only) it is left out. What a
rule reads that is in no part of the profile (connections in use, memory used,
image tags, readiness) is not added in this piece: the list is then empty, and
the sheet says that the values are in the operator's report.

### 3. In the profile

`check` gains one key, `checks`: a list with one entry per rule, in rule
order.

```json
{"rule_id": "SCAL-004", "area": "scalability", "outcome": "passed",
 "basis": "read", "text": "...the passed sentence...", "rests_on": ["worker.replicas"]}

{"rule_id": "REL-002", "area": "reliability", "outcome": "finding",
 "severity": "warning", "text": "...title...", "impact": "...", "remedy": "...",
 "rests_on": ["setting:instance:EXECUTIONS_DATA_PRUNE"]}

{"rule_id": "PERF-001", "area": "performance", "outcome": "not_assessed",
 "text": "...subject...", "reason": "source_missing", "needs": ["postgres"],
 "rests_on": []}
```

The three entries show the shape only: the rule ids and texts are not those of
the real rules.


- `check.partial`, `check.scores` and `check.findings` stay exactly as they
  are, so that an older version reads the file as before.
- `format_version` stays 1, on the condition that 0.5.0 opens a 0.6.0 profile
  and shows it as it shows a 0.5.0 one. The builder verifies this with the
  released 0.5.0 code; if it does not hold, stop and report.
- `--withhold check` removes the whole of `check`, `checks` included, as today.
- **Decided by the owner: a withheld fact that a check restates.**
  A passed sentence ("workers run with at least 2 replicas") bounds a fact the
  operator may have withheld. Today's stance for findings is "withhold the
  check too", which would now empty most of the sheet. So an
  operator can also withhold one check (`--withhold check:<RULE_ID>`, and a
  box per check on the page); the entry stays with its rule id and
  `"outcome": "withheld"`, and its finding leaves `check.findings`. In the
  review, a withheld fact or setting names the checks that rest on it, so the
  operator can decide. Nothing is withheld automatically.
- **Decided by the owner after the build, 2026-11-24: the score of the area of
  a withheld check.** The design said that the finding leaves `check.findings`
  and was silent on `check.scores`; the build left the scores as they were, so
  an area score still told that a finding was withheld and how severe. When a
  check is withheld on its own, the score of that check's area is not written:
  the row stays in `check.scores` with `"score": null` and `"withheld": true`.
  The score is never computed again without the finding. It applies whatever
  the outcome of the withheld check was, so that the hidden score does not
  tell that the check was a finding. The other areas keep their scores.
  `--show-profile`, the review step, an opened profile and the call sheet show
  such a score as withheld, not as missing data. 0.5.0 opens the file and
  shows that score as "n/a".
- For the same findings, `checks` and `check.findings` always agree: the
  writer builds both from one list.
- A profile written before 0.6.0 has no `checks`. It still opens.
- The reader follows the format's rule for what it does not know: `checks`
  must be a list of objects with a string `rule_id`, `area`, `outcome` and
  `text`, else the file is refused; an unknown key is ignored, and an entry
  with an outcome or a reason this version does not know is shown as "not
  known to this version", not refused. A later version can then add to it.
- `docs/profile-format.md` describes the key, the outcomes, the reasons and
  the rule that the texts are fixed.

### 4. What the operator sees

- `--show-profile` and the page's step "Share the environment profile" list
  every entry of `checks` with all of its texts, under the check outcome that
  the single box withholds.
- `docs/rules.md` (new): one table of every rule, with what it reads, when it
  is `passed`, `not_applicable` and `not_assessed`, and its fixed texts. It is
  the statement of what the tool computes. A test keeps it in step with the
  code (every rule id and every fixed text of the code is in the document).

### 5. The call sheet

A rendering of one profile, offline, that changes nothing.

- Command line: `--show-sheet FILE` prints Markdown and exits; it takes only
  `-o`, like `--show-profile` and `--show-questions`.
- Page: in "Open a profile", a section "Call sheet" with "Copy as Markdown",
  the same text as the command.

Contents, in this order:

1. **Run.** Date, reason, reference, tool version, the sources and their
   state. A line when the profile is more than 30 days old.
2. **The environment in brief.** A fixed short list of facts, each as
   measured, declared, unknown or withheld: n8n version, execution mode, main
   and worker replicas, database type, and the four usage numbers of the
   report (executions per day, failure rate, active workflows, users), with
   "about" when the profile marks them as an estimate.
3. **What works well.** The `passed` checks, by area.
4. **What does not.** The `finding` checks, most severe first: title, impact,
   and what the rule rests on with its values.
5. **What to improve.** The remedies of those findings, in the same order,
   each naming its finding.
6. **Not assessed.** The `not_assessed` checks, grouped by what the operator
   would have to provide ("with Postgres: ..."), then those whose value was
   unreadable, then rule errors.
7. **Does not apply.** One line listing the subjects of the `not_applicable`
   checks.
   Withheld checks are counted in one line.
8. **Questions still open.** The existing questions sheet, unchanged.

Edge cases:

- `check` withheld: sections 3 to 7 are replaced by one line saying the
  operator withheld the check outcome.
- A profile without `checks` (before 0.6.0): section 4 lists the findings by
  title only, and sections 3, 5, 6 and 7 are replaced by one line saying the
  file was written by a version that does not record them.
- A fact withheld by the operator shows as withheld, never as its value.
- No finding: section 4 says so, and says whether the check was partial.

The sheet is dense and plain: it is an outline for the reader, not a document
for the operator.

### 6. Safety

- The zero-leak tests cover `checks`, the sheet in Markdown and the page's
  sheet with the same markers as the rest of the profile.
- A test asserts that every text in `checks` is one of the literals of the
  rules, for snapshots with different values: no text depends on the instance.
- The collectors do not change. Nothing new is read from the instance.

## Amendments after review

Decided by the owner on 2026-11-25, after the review of the build. They follow from the
first anti-goal: "works well" is never said of something the tool did not positively
verify.

- **A passed sentence states the condition the rule checked, and nothing stronger.**
  "Fewer than 100 jobs wait in the queue", not "the queue has no backlog"; "the variable
  is set to", not "every process does". Where a rule compares against a short list, or
  reads a setting whose effect depends on something it does not read, the sentence says
  so. SEC-002 does not pass on external runners when `N8N_RUNNERS_ENABLED` is false.
- **Two sources that disagree never give a pass.** The order between sources (the env
  file describes every process, a probe wins over the debug information) decides when a
  finding is raised, and is unchanged. When a rule has no finding and two sources given
  in the run disagree on what it reads, the outcome is `not_assessed` with the new reason
  `sources_disagree`. `not_stated` was not reused: it says that a source does not say,
  which is not true here. The same holds for "does not apply": a queue-mode check is
  not left out as not applying when one source says regular and another says queue.
  What is compared is in `docs/rules.md`, "When two sources disagree".
- **`rests_on` on more outcomes.** A check that does not apply lists what made it so,
  and a check that could not decide after reading a value lists what it read, so that
  a withheld fact or setting names every check that tells something about it.
- **`rests_on` names a fact only when the file holds a value for it**, which is when the
  rule and the fact read the same source.

Findings, scores and the health report are unchanged by these.

### After the second review

Decided by the owner on 2026-11-25. The second amendment above was held only where both
sources set the variable; these close it.

- **Not set states the default, on either side.** Between an env file and a Kubernetes
  container read in full, a variable that is not set counts as its n8n default, as it
  already did against the debug information. A container that cannot be told for the
  variable (a Secret, a `_FILE` variable, an environment read in part) cannot be said to
  agree with the file: the check is then `not_assessed` for the reason
  `value_unreadable`. That reason was chosen over `sources_disagree` because nothing was
  read that differs: a value could not be read, which is what the reason says.
- **Two more comparisons.** PERF-003 against the binary mode the debug information
  states. MAINT-003 against the n8n version read from `/metrics` or stated by a debug
  information, when it differs from the image tags.
- **SEC-002 and the version.** With `N8N_RUNNERS_MODE=external` and `N8N_RUNNERS_ENABLED`
  not set, the check passes only when the n8n version read is 2.x or later; on 1.x, or
  with no version, it is `not_stated`. The sentence says "not set to false". Nobody
  checked n8n itself for its default on 1.x, nor whether 2.x still reads the variable.
- **`rests_on` names the container settings that were compared**, and is written for
  every outcome: a check that lacks a source read values on the way (the mode, for a
  queue check without Redis), and the entry tells something about them.
- **SCAL-002** does not pass on worker Deployments when the env file or the debug
  information says the instance is in regular mode.
- **A property test** (`tests/test_sources_property.py`) holds the two rules above over
  generated snapshots, since the same class of error came back twice in cases nobody had
  listed. What it covers is in `docs/rules.md`.

Findings, scores and the health report are unchanged by these too.

### After the third review

Decided by the owner on 2026-11-25.

- **The execution mode and the database type are one for the instance.** They are
  compared between the env file and every n8n container, whatever its role, and between
  the containers of Kubernetes themselves when no env file was given. A worker or a
  webhook processor that does not set the mode says nothing about it. REL-001 keeps its
  sentence about "every process" and reads `DB_TYPE` on every container before saying it.
- **SEC-002 passes on the version only when every version read is 2.x or later**:
  `/metrics`, the debug information and the instances it lists, and the image tag of each
  main and worker container. Versions on both sides of 2.0 are `sources_disagree`.
- **SCAL-004 is compared with the limit the debug information states** when the limit of
  the env file is all that was read for the workers.
- **The oracle of the property test** defines the comparison of the mode and of the
  database type by itself, and holds the two points above.

Findings, scores and the health report are unchanged by these as well.

### After the fourth review

Decided by the owner on 2026-11-25, as a last small round.

- **The debug information is compared with every n8n container** for the execution mode
  and the database type, not with the main ones only.
- **A worker or a webhook processor read in part cannot be told for the mode**
  (`value_unreadable` next to an env file). It says nothing about the mode only when
  its environment was read in full and does not set it.
- **SEC-002 reads a version from any image tag that starts with a version number**
  (`1`, `1.80`), and from the webhook image too.
- **Not changed, and a known limit:** in Kubernetes alone, a main container in regular
  mode next to workers that do not set `EXECUTIONS_MODE` is not a disagreement. A worker
  that leaves the variable out says nothing about the mode; read as "regular", every
  cluster with a main in queue mode and such workers would disagree with itself. Nobody
  checked what n8n makes of a worker without `EXECUTIONS_MODE`. The limits are listed
  in `docs/rules.md`, "Known limits".

Findings, scores and the health report are unchanged by these as well.

### After the fifth review

Decided by the owner on 2026-11-25, as the last round.

- **For SEC-002 a tag that names a part of a version can hold the pass back, never give
  it.** "Every n8n version read is 2.x or later" needs a full x.y.z: from `/metrics`, the
  debug information, an instance it lists, or a full x.y.z tag. A tag whose leading
  number is 1 or 0, followed by the end of the tag or by `.`, `-` or `_` (`1`, `1.80`,
  `1.x`, `v1`, `1-debian`, `1_custom`), is read as a 1.x. Any other tag (`2`, `42`,
  `24.04`, a word, a digest) is not a version read.
- **Known limits, written out:** with Kubernetes alone, a worker read in part or with
  the mode in a Secret is compared with nothing for the mode too, also against the debug
  information; and SCAL-004 compares the limit of the text only when Kubernetes shows no
  worker container. Both are in `docs/rules.md`, "Known limits".

Findings, scores and the health report are unchanged by these as well.

## Left to the builder

- How rules state their outcome, and the internal names.
- The wording of every `passed`, `subject`, fixed `impact` and `remedy`, in
  the style of the existing texts. The owner reads them in `docs/rules.md`
  with the result.
- The classification of each `return None`, following section 1. Where a case
  does not fit any of the four outcomes, stop and ask.
- The layout of the sheet on the page, with the existing components and
  tokens, at phone width and with a keyboard.

## Done means

- Every rule gives one of the four outcomes; for the existing test snapshots
  the findings, scores and reports are identical to 0.5.0 apart from the
  version number.
- A 0.6.0 profile opens in 0.5.0; a 0.5.0 profile opens in 0.6.0.
- `--show-sheet` and the page give the same text.
- The tests pass, with new ones for each outcome of each rule, the format, the
  sheet's edge cases and the leaks.
- README, `docs/profile-format.md` and `docs/rules.md` say what changed.
