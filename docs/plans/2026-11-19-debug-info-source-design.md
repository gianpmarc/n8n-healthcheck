# Debug information as a source: design

Date: 2026-11-19. Status: approved by the user on 2026-11-20.
Builds on `2026-11-18-environment-profile-design.md` (piece 1) and
`2026-11-19-profile-questions-design.md` (piece 2), both merged.

## Intent

Every n8n instance can produce a short text called "Debug information"
(Help, About n8n, Debug information, Copy). It is the one thing any
administrator can hand over in seconds, without installing or running anything,
and today it is the only thing most of them hand over.

The tool cannot use it. Without Kubernetes or an env file, a profile is almost
empty: the version, the execution mode, the number of mains and workers and the
pruning settings stay unknown, although that text states all of them.

This piece makes the debug information a source like the others. It can be the
only source of a run. Most often the person who pastes it is not the instance's
administrator but the person who received it from them (in a ticket or a chat),
running their own copy of the tool; sometimes the administrator pastes it next
to the other sources.

This replaces what was planned as piece 3 (usage counts). Usage counts need
database access, which is the source people give least; they move to a later
piece.

## What the text is

Generated in the browser by n8n (`useDebugInfo.ts` in n8n's editor). Markdown:
a title `# Debug info`, sections as `## name`, items as `- key: value`, and a
last line `Generated at: <ISO time>`. A variant used inside bug reports has one
more `#` on every heading.

| Section | Keys | Present |
|---|---|---|
| `core` | `n8nVersion`, `platform` (`docker (cloud)`, `docker (self-hosted)`, `npm`), `nodeJsVersion`, `nodeEnv`, `database` (`sqlite`, `postgres`), `executionMode` (`regular`, `scaling (single-main)`, `scaling (multi-main)`), `concurrency` (number, `-1` is no limit), `license` (`community`, `enterprise (production)`, `enterprise (sandbox)`), `consumerId` | always |
| `storage` | `success`, `error` (`all`, `none`), `progress`, `manual` (booleans), `binaryMode` (`memory`, `filesystem`, `s3`, `azure`, `database`) | always |
| `pruning` | `enabled`; when true also `maxAge` (`N hours`) and `maxCount` (`N executions`) | always |
| `security` | `secureCookie: false`, `blockFileAccessToN8nFiles: false` | only when one of them is insecure |
| `client` | `userAgent`, `isTouchDevice` | always |
| `cluster` | `instanceCount`, `versions` (comma separated), `instances` (one line each: `instanceKey`, `hostId`, `instanceType` in `main`, `worker`, `webhook`, `engine`, `instanceRole` in `leader`, `follower`, `unset`, `version`), `checks` (one line each: `check`, `status` in `succeeded`, `failed`, `warnings`) | only on n8n versions from May 2026 on, and only when the instance registry is loaded |

`concurrency` is the limit on concurrent production executions of the main
process. It is not the worker concurrency.

n8n 1.x writes two values differently (`useDebugInfo.ts` at the tags 1.50.0,
1.70.0 and 1.90.0): `executionMode` is `regular` or a bare `scaling`, which
does not say how many mains there are, and `database` is `sqlite`, `postgres`,
`mysql` or `mariadb`. Both are read: `scaling` is queue mode with the number of
mains unknown, `mysql` is `mysqldb`. No other value of a key that is read
differs in 1.x.

The shape of the two list items, with made-up ids (the parser must be built on
this shape, taken from n8n's source and from a real 3.3.0 instance):

```
## cluster

- instanceCount: 2
- versions: 3.3.0
- instances:
  - instanceKey: 00000000-0000-4000-8000-000000000001, hostId: main-aaaaaaaaaaaa, instanceType: main, instanceRole: leader, version: 3.3.0
  - instanceKey: 00000000-0000-4000-8000-000000000002, hostId: worker-bbbbbbbbbbbb, instanceType: worker, instanceRole: unset, version: 3.3.0
- checks:
  - check: hostid-clash, status: succeeded, warnings: -
  - check: split-brain, status: failed, warnings: some-code; another-code

Generated at: 2026-11-19T16:50:44.057Z
```

`status` is only `succeeded` or `failed`; `warnings` is a third key on the same
line, `-` when there are none.

## Goals

- A run can take the debug information as a source, alone or with the others,
  on the command line and in the web interface.
- It fills profile facts that are otherwise unknown, and lets existing rules
  fire without Kubernetes or an env file.
- Nothing that identifies the instance, its hosts or the person's browser
  leaves the parser.
- A text that went through a ticket or a chat still reads.

## Non-goals

- Usage or activity counts (a later piece).
- Fetching the same data from the instance by URL.
- Cross-checking the debug information against what a probe measured.
- Pasting a debug information into the viewer to complete a received profile.
- Filling the new facts from other sources (an env file could fill some; later).
- Keeping the text itself, in a snapshot or anywhere else.

## Anti-goals

It is a failure, even if everything works, when:

- a `consumerId`, `instanceKey`, `hostId` or user agent, or any part of one,
  appears in a snapshot, a report, a profile, a log line or an error message;
- any text from the paste that is not one of the known values, a number or a
  version reaches an output;
- a value from the debug information overrides one a probe measured;
- an old debug information is presented as the current state without saying
  when it was generated;
- a run with the six existing sources and no debug information is scored or
  labelled differently from today;
- a file written by this version is refused by tool version 0.2.0 or 0.3.0;
- anything in the anti-goals of pieces 1 and 2 stops holding.

## Approaches considered

1. **A seventh source of the normal run** (chosen). One field, one flag, feeds
   both the report and the profile, combines with the other sources. Touches
   collection, rules, profile and page.
2. **A separate converter** from the text to a profile. Smaller, but no
   findings, no combination with other sources, and a second path to maintain.
3. **Paste it in the viewer to complete a received profile.** Rejected: the
   archived file would no longer be what its sender reviewed (already ruled out
   in piece 2).

Chosen by the user: 1.

## Decisions made by the user

- The debug information is, today, the only thing most administrators provide.
- Both people paste it, but most often the one who received it.
- Approach 1.
- The list of facts, findings and warnings below, as proposed.

## Reading the text

A new module parses the text into a small dictionary and nothing else survives.

- **Allowlist, not blocklist.** Only the keys in the table above are read, each
  with its own strict reader: a value from the fixed list, a whole number, a
  boolean, or a version (`digits.digits.digits`, optional suffix as the tool
  already accepts for versions). A key that is not known, or a value that does
  not fit, is dropped; a dropped value makes that one fact unknown, it does not
  fail the run.
- **Identifiers.** `consumerId`, `instanceKey`, `hostId`, `userAgent` and
  `isTouchDevice` are never stored. Instances are only counted by
  `instanceType`.
- **Checks.** For each check: its name only when it is one of `hostid-clash`,
  `lifecycle`, `split-brain`, `version-mismatch`, otherwise it is counted as
  "other"; and its status. The `warnings` text is dropped. *(My call: warning
  codes are free strings from n8n; the name of the failed check is enough to
  act on.)*
- **Tolerant of transport.** The text may arrive with Windows line ends, with
  blank lines added or removed, with headings that lost their `#` or gained one,
  with bullets as `-`, `*` or `•`, with leading spaces or a `>` quote mark on
  every line, and wrapped in a code fence. Sections are recognised by their
  names. *(My call: it usually arrives through a ticket or a chat.)*
- **What counts as a debug information.** A `core` section with a readable
  `n8nVersion`. Anything else is refused with one sentence that does not echo
  the text. Size limit 64 KB.
- **Generated at.** Kept as a date. Missing, unreadable or in the future means
  "date unknown".
- **One text only.** A text that holds more than one debug information (a
  second title, a section that comes twice, or a second `Generated at`) is
  refused with a third fixed sentence. Two texts are never merged: the values
  of the older one would stand under the date of the newer, and instances
  would be counted twice. *(Added after the review of the first build.)*
- **Hard-wrapped list items.** An instance or check line that a mail client
  broke is joined again before it is read. A continuation is a line that
  follows such an item with no blank line between, has no bullet and no `#`,
  and does not start a section, an item or the date line. Anywhere else a line
  without a colon is a heading, and one that is not known ends the section.
  *(Added after the review.)*
- **UTF-16.** A file in UTF-16 with a byte order mark, which Windows PowerShell
  writes, is read. *(Added after the review.)*

### What is kept (the snapshot section)

`Snapshot` gains `debug_info`, `None` when not given:

```
{"generated_on": "2026-11-19" | None,
 "n8n_version": str, "node_version": str | None,
 "platform": "docker_self_hosted" | "docker_cloud" | "npm" | None,
 "database": "sqlite" | "postgresdb" | "mysqldb" | "mariadb" | None,
 "execution_mode": "regular" | "queue" | None, "multi_main": bool | None,
 "concurrency_limit": int | "unlimited" | None,
 "license": "community" | "enterprise_production" | "enterprise_sandbox" | None,
 "save_success": "all" | "none" | None, "save_error": "all" | "none" | None,
 "save_progress": bool | None, "save_manual": bool | None,
 "binary_mode": str | None,
 "pruning": {"enabled": bool, "max_age_hours": int | None, "max_count": int | None} | None,
 "insecure": {"secure_cookie": bool, "file_access_blocked": bool},
 "cluster": None | {"instances": {"main": int, "worker": int, "webhook": int, "engine": int},
                    "versions": [str], "checks_failed": [str]}}
```

`insecure` holds `True` for a key only when the text flagged it. `binary_mode`
uses the tool's existing values (`memory` in the text is the tool's `default`).
A saved snapshot (`--save-snapshot`) therefore holds no identifier, and an older
snapshot without the field still loads. *(My call: the exact shape; the builder
may adjust it as long as no identifier and no free text is in it.)*

## Precedence

A probe wins, **value by value**. The debug information fills a fact, or gives
a rule one of the values it reads, only where the other sources given in the
same run do not state that value. No comparison is made and no disagreement is
reported.

- "Does not state" includes a variable that is simply not set in an env file or
  a container: there the tool today assumes n8n's default (for example regular
  mode), and the debug information, which reports the effective value, is the
  better evidence. A variable that is set, readable or not (a Secret), is
  stated by the probe and the debug information is not used for it.
- A rule and the fact about the same thing must agree: when the profile says
  pruning is off because the text says so, `PERF-001` fires.
- One finding may therefore rest on two sources (the mode from Kubernetes, the
  database from the text); its evidence names both.

*(My call: per value and not per source, so the profile and the report cannot
contradict each other in the same run.)*

## Profile

`sources` gains `debug_info` with the states `collected` and `not_provided`.
It is never `failed`: a refused text stops the run before anything is collected.
The list of sources a file **must** hold stays the six of today, frozen like
the fact ids, so files written by 0.2.0 and 0.3.0 still open; the viewer shows
a seventh row only when the file has it.

A fact filled from it has `provenance: "measured"`, `source: "debug_info"`, and
a `detail` that says when the text was generated: "debug information generated
on 2026-11-19", or "debug information, date unknown". *(My call: n8n generates
the text, no person types the values, so it is a measurement; the source says
where it came from and the detail says how old it is. A new provenance value
would be refused by versions 0.2.0 and 0.3.0.)*

No change to `format_version`: versions 0.2.0 and 0.3.0 accept an extra source
name, unknown fact ids and any string as a fact's source. `FORMAT_1_FACT_IDS`
does not grow. Tool version becomes `0.4.0`.

### Existing facts it can fill

| Fact | From |
|---|---|
| `n8n.version` | `n8nVersion` |
| `executions.mode` | `executionMode`: `regular`, or `queue` for both scaling values |
| `multi_main.enabled` | `executionMode`; stays unknown for the bare `scaling` of n8n 1.x, and `REL-006` then stays silent from the text |
| `database.engine` | `database` |
| `pruning.enabled`, `pruning.max_age_hours` | `pruning` |
| `binary_data.mode` | `binaryMode` |
| `main.replicas`, `worker.replicas`, `webhook.replicas` | instances counted by type, only when `cluster` is present |

`binary_data.mode` from the text is the effective mode, while from an env file
it is the variable's value; `docs/profile-format.md` is amended to say so.

A replica count from the text adds to its detail "counted from the instances
n8n listed". `engine` instances are not part of any of the three counts. A type
with no instance listed stays `unknown`, never `0`, as the format promises.
Without
a `cluster` section the three stay unknown with the detail "this debug
information has no cluster section (older n8n, or the instance registry was not
loaded)". `worker.concurrency` is never filled from it.

### New facts (added in 0.4.0, after the existing ones)

| Fact id | Value | From |
|---|---|---|
| `n8n.node_version` | version | `nodeJsVersion` |
| `n8n.platform` | `docker_self_hosted`, `docker_cloud`, `npm` | `platform` |
| `license.type` | `community`, `enterprise_production`, `enterprise_sandbox` | `license` |
| `main.concurrency_limit` | a positive whole number, or the word `unlimited` for `-1` | `concurrency` |
| `executions.save_success` | `all`, `none` | `storage.success` |
| `executions.save_error` | `all`, `none` | `storage.error` |
| `executions.save_progress` | boolean | `storage.progress` |
| `executions.save_manual` | boolean | `storage.manual` |
| `pruning.max_count` | whole number | `pruning.maxCount` |
| `cluster.versions_aligned` | boolean | one version among the instances; gives way like `MAINT-003`: unknown when Kubernetes names the versions |
| `cluster.checks_failed` | whole number | checks whose status is `failed`; a succeeded check with warnings does not count |
| `security.secure_cookie` | `false` only | `security.secureCookie` |
| `security.file_access_blocked` | `false` only | `security.blockFileAccessToN8nFiles` |

Thirteen new ids. Without a debug information each is `unknown` with the detail
"The debug information was not provided". The two `security` facts are written
only as `false`, when the text flags them; otherwise they are `unknown` with the
detail "n8n reports this only when it is insecure". *(My call: an n8n version
older than that section would look secure by silence.)* The two `cluster` facts
are unknown without a `cluster` section, with the detail given above.

Each new fact gets a sentence for part 1 of the question sheet, as piece 2
requires for every fact.

`docs/profile-format.md` is amended in two places: a `detail` may carry the date
a debug information was generated (a date, never text that was observed), and
`main.concurrency_limit` may be the word `unlimited`. Numbers stay never
negative.

## Report

The debug information is not one of the six data sources the report counts. It
is a weaker alternative to the probes, not a seventh thing every run should
have. *(My call.)* Concretely:

- **Not given.** The report and the page are exactly as today, word for word,
  including "Every data source was collected" for a full run. It is not
  mentioned.
- **Given.** One line in the Summary, in the report and on the results page:
  "Debug information generated on <date> was read." (or "date unknown"),
  followed when they apply by the staleness line and the no-cluster line below.
  The count of data sources stays "N of 6".
- **Given alone.** The run is assessed, not "could not run": the rules that can
  read it run, scores are shown and marked partial as for any run with missing
  probes, and the Summary says the check rests on the debug information only
  and none of the six data sources was given.
- **Given next to a probe that failed.** The run counts as having collected
  something; it does not exit as "nothing collected".

The test that ties `Snapshot`'s fields to `report.SECTIONS` changes to allow
this one field outside the six.

### Existing rules that also read it

Each takes from the debug information the values the other sources do not
state (see Precedence). Evidence then says "from the debug information
generated on <date>" for those values.

| Rule | Reads |
|---|---|
| `REL-001` Queue mode on SQLite | `database`, `executionMode` |
| `REL-006` Single main process | `executionMode` |
| `SCAL-001` Regular mode with no limit on production executions | `executionMode`, `concurrency`, `database` (the rule's existing production gate stays) |
| `SCAL-002` Only one queue-mode worker | queue mode and one `worker` instance (needs `cluster`) |
| `PERF-001` Execution data pruning is disabled | `pruning.enabled` |
| `PERF-002` Execution progress is saved after every node | `storage.progress`, **in regular mode only** |
| `SEC-001` Session cookie is not restricted to HTTPS | `security.secureCookie` |
| `MAINT-003` n8n processes run different versions | more than one version in `cluster` |

A rule whose existing condition needs something the text does not state stays
silent. The other rules are unchanged. Two deliberate limits *(my calls)*:

- `PERF-002` in queue mode judges the workers, and the text holds the main
  process's settings, so from the text it stays silent in queue mode.
- `PERF-003` (binary data in memory) does not read the text. Today it fires
  only when the variable is explicitly set, because the effective mode of an
  unset variable depends on the version; the text gives the effective mode, and
  firing on it would flag every stock install of older n8n versions.

### New rules

| Rule | Severity | Fires when |
|---|---|---|
| `REL-009` A cluster check failed | warning | `cluster` lists a failed check; evidence names the known ones |
| `SEC-003` Access to n8n's own files is not blocked | warning | the text flags `blockFileAccessToN8nFiles: false` |
| `MAINT-004` Enterprise sandbox license | info | `license` is `enterprise (sandbox)`; the text says it matters only if this instance serves production |

*(My call: ids, severities and wording, in the style of the existing rules.)*

### Warnings to the person

All three go in the Summary, after the line that says the text was read:

- Generated more than 30 days before the run: "generated N days ago, the
  instance may have changed". Not refused.
- Date unknown: said there.
- No `cluster` section: one line saying replicas and version alignment cannot
  be told, and why.

## Command line

```
--debug-info FILE    the text copied from Help > About n8n > Debug information;
                     "-" reads it from standard input
```

It is a source flag like the others: it may be the only one, it combines with
them, and `--save-snapshot` stores the section. A file that cannot be read, is
larger than 64 KB or is not a debug information is a usage error (exit 3)
before anything is collected, with no part of the text in the message. Existing
flags and their abbreviations keep their meaning.

## Web interface

- `POST /api/run` accepts `debug_info_text`; `GET /api/meta` lists the new
  source. A refused text is a 400 with the same sentence as the command line.
- The form gains a "Debug information" field: a text area to paste into, with a
  line saying where to copy it from in n8n and that instance ids, host ids and
  the browser's user agent are read and discarded. *(My call: it is the first
  source in the form, because it is the cheapest to provide; the builder may
  argue otherwise.)*
- It can be the only field filled. The existing "nothing to inspect" message
  names it too.
- The results, the share step, the questions and the viewer need no new
  behaviour: the new source and facts appear through the existing tables. The
  Summary lines above appear in the page's summary.
- When the person pasting is not the administrator, they are the one who
  reviews and withholds in the share step. No new control for that.

## Tests

- Parser: the full text; without `cluster`; with `security`; regular mode;
  pruning off; the bug-report heading variant; each transport mangling listed
  above; unknown keys and sections; values that do not fit; a text that is not
  a debug information; the size limit.
- Zero leak: a text whose identifiers, user agent, warnings and unknown keys
  hold marker strings; no marker appears in the snapshot, the report, the
  profile, the JSON, any error message or the page.
- Precedence: a fact and a rule with both a probe and the debug information
  disagreeing, the probe's value stands; an env file that does not set a
  variable, the text's value is used and the rule and the fact agree; a finding
  resting on two sources names both.
- Old files: a 0.2.0 and a 0.3.0 profile (six sources) still open.
- Each rule in the two tables: fires from the text alone; stays silent when the
  text does not state its condition.
- Profile: the new ids are documented and have sentences; a file with them is
  accepted by the 0.2.0 validation rules kept in `tests/validate_0_2_0.py`; a
  0.3.0 file still opens.
- A run with the six existing sources and no debug information gives the same
  report text and scores as before this change.
- Command line: the flag alone, with others, from standard input, each refusal.
- Web: the field alone and with others, the refusal, the warnings, keyboard,
  phone width, a real browser.
- All fixtures are synthetic: no identifier from a real instance.

## Left to the builder

- The parser's internal structure and the module's name.
- How rules get the values (a helper next to the existing ones), as long as the
  precedence rule holds.
- Wording of evidence, details and labels, within the tables above.
- Layout of the new field within the existing visual language.
- Whether the work is one session or two (core, rules and command line; then
  web), as in pieces 1 and 2.
