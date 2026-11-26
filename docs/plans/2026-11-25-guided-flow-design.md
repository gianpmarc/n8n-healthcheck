# Guided flow for the person who runs the tool: design

Status: approved by the owner on 2026-11-25.

Target version: 0.7.0.

## Intent, and why

The tool is run by someone at the organisation that operates n8n, usually without the
person who asked for it in the room. That person then receives one file, the environment
profile, and must get the best possible picture of the instance from it.

Today the page is one form with six optional sources. Nothing tells the operator which
source matters most, how much the result will be worth, or what to do with the file. An
end-to-end trial on 2026-11-25 showed the cost: a run without the debug information gave
a file with "n8n version: unknown", the one fact every later conversation needs.

Operators are careful about security and about what they share. They must be able to
see, before running anything, what the tool reads, what it computes and what leaves
their machine.

So this piece makes the tool guide the operator from the first screen to the file they
send, and makes the public repository state plainly what the tool does.

## Goals

1. The page is a guided flow of six steps, from "what this tool does" to "download and
   send". The operator never has to read anything outside the page to complete it.
2. The debug information is required for a profile. Without it no profile is written,
   on the page or on the command line.
3. Before downloading, the operator sees how much the file lets the reader assess and
   what each source they did not give would add.
4. One document in the public repository says what the tool reads, computes, writes and
   never writes, for a reader who has to decide whether to trust it.

## Non-goals

- No change to the rules, findings, scores, report, collectors or the profile format
  (`format_version` stays 1).
- No upgrade-risk content. That is the next piece.
- No translation: the page and the document stay in English.
- No way for the tool to send the file. It still sends nothing anywhere.
- No change to "Open a profile" and the call sheet beyond where the entry point sits.
- No interactive guide in the terminal. The command line keeps its flags.

## Anti-goals

The piece has failed, even if it works, if:

- the operator learns that the debug information was required only after running
  everything;
- any text on the page or in the document promises more than the code does (for
  example "never contains X" for something a test does not hold);
- a value of the instance, or the pasted debug information, reaches a log, an error, a
  URL or browser storage because of the new steps;
- a profile written by 0.7.0 cannot be opened by 0.6.0, or the other way round;
- the page stops working with a keyboard, with a screen reader, or at 375 px.

## Constraints

- Python 3.10+, standard library only. The page is plain HTML, CSS and JavaScript with
  no build step and no external request. Tests: `python3 -m unittest discover -s tests`.
- Collectors stay read-only. The zero-leak tests are never weakened.
- The server keeps nothing of a request and the page stores nothing in the browser.
- The repository is public: no customer data, no name of an internal system. The reader
  of the file is "the person who asked you for it".
- The existing visual language of the page (tokens, components, states) is reused.

## Approaches considered

- **A. The page becomes a flow of steps (chosen by the owner).** The existing form,
  report and share step are reordered into six numbered steps, with a new first and a
  new last one. The requirement is visible at step 2. Cost: the whole page is touched
  and must be verified again in full.
- **B. The page stays as it is, with a gate on the download, a completeness summary and
  a closing text.** Little work, but the operator is not guided and meets the
  requirement at the worst moment, after the run.
- **C. An interactive guide in the terminal.** Dropped: the operator's path is the page,
  and reviewing what is shared does not work well in a terminal.

The owner chose A.

## Decisions made by the owner

1. The guide lives inside the tool: launch it and be guided from start to end.
2. The documentation in the public GitHub repository is what shows that the tool is
   safe and what it extracts. No separate document is sent beforehand.
3. The debug information, and with it the n8n version, is always in the file.
4. Without the debug information the profile cannot be downloaded. There is no
   override, also when the n8n interface cannot be reached.
5. The file reaches the reader by e-mail or attached to a support ticket.
6. The six steps and what each one says, as described under "The flow". The owner saw
   them as a mockup of the content only (no file of it exists): the layout and the look
   were not decided by it and are the builder's, within the page's visual language.
7. The n8n version is the one entry the operator cannot withhold (decided on
   2026-11-25, after the builder check). Every other fact, setting and check stays
   withholdable as today.

## The flow

One step is shown at a time, with a visible list of the six steps and the current one
marked. "Back" never loses what was entered. Moving forward needs the step to be
complete where the step has a requirement.

### Step 1. What this tool does

No field. Three statements, each true of the code and held by an existing or new test:

- it only reads: nothing is changed on the instance, the database or the cluster;
- it runs on the machine where n8n-healthcheck was started, and talks only to what the
  operator points it at: the n8n URL (with the API key, when one is given), the
  database, Redis, and the cluster of the chosen kubectl context. The report and the
  file are sent nowhere;
- at the end there is one file, which the operator reviews and sends themself. They can
  leave out any entry of it except the n8n version, and the step says so here, before
  anything is given.

Then what the file never contains (the list already in the README: secret, host name,
URL, IP address, file path, e-mail address, Deployment, namespace or registry name),
with the one exception the README already states (the optional one-line reference the
operator types is written as typed), and a link to the trust document in the repository
(see "The trust document").

### Step 2. Debug information (required)

How to copy it from n8n (Help > About n8n > Debug information > Copy), then the field.
The text is checked when the operator asks to continue, by the server, with the parser
that exists. On success the step shows what was read: n8n version, execution mode,
number of instances; and says that the instance and host ids were read and discarded.
A refused text is reported next to the field with the parser's message, and the
operator stays on the step.

A text of an older n8n without a `cluster` section is accepted, and the step says that
replicas and version alignment cannot be told from it. A text more than 30 days old or
without a date is accepted with the warning the report already gives (that warning is
made in `report.py`, not in the parser, so the step has to produce it from the same
rule).

### Step 3. Other sources (optional)

The five other sources and the API key, as today, each with one line on what it reads.

Nothing has been collected yet, so the step predicts nothing. It reports what is
already true: the server runs the rules on the debug information of step 2 alone, and
the step shows the result of that run. "The debug information decides N of 24 checks"
(outcomes `finding`, `passed` and `not_applicable`). Then, per source, how many checks
are waiting for it: the checks whose outcome is `not_assessed` with reason
`source_missing` and that source among their `needs`. A check that lists several
sources is counted under each, and the wording says "can be decided with" so that the
numbers are not read as a sum. The other not-assessed checks (`not_stated`,
`value_unreadable`) are shown as one line: "M checks need a source that states a
setting the debug information does not show", without naming a source, because no
rule names one. No rule and no rule declaration changes for this. The API key line says
that it adds four usage numbers and no check.

### Step 4. Run and report

The run, then the report as today. One sentence says that the report stays with the
operator and quotes raw values as evidence, unlike the file.

### Step 5. Review what you share

The share step as today (questions, facts, settings, checks, each with its box, reason
and reference). New at the top, computed the same way as in step 3 but from the real
run: how many of the 24 checks were decided, and for the others what would be needed,
grouped by source, with a way back to step 3.

The fact `n8n.version` is listed like the others but has no box: the row says that the
version is always part of the file. On the command line `--withhold n8n.version` is
refused with exit code 3 and a message that says the same. The whole-check and
per-check withholding are unchanged. A profile of an earlier version in which the
version is withheld still opens.

Going back and running again keeps what the operator chose in this step: an entry that
was withheld stays withheld when the new run still has it, and answers, reason and
reference are kept. Today a new run clears them; a withheld entry coming back into the
file unnoticed is the failure to avoid.

Amended on 2026-11-26, after the review of 3148e1f (approved by the owner): "the new run
still has it" is not enough for a setting, whose entry is named by its scope, so the
same variable read from another source is another entry. For the entries that are new
in a run: a setting whose variable the operator withheld is withheld under the new
scope too; "Withhold all" of a group is remembered, so the new entries of that group
arrive withheld; and the step names, with their number and their ids, the entries that
are new in this run and not withheld. An entry the operator has already seen keeps what
they chose for it. Facts and checks have the same ids in every run and keep their
choice by id.

For the same reason the report, and with it the file, belongs to the inputs it was made
from. When the form no longer builds the request of the last run (the debug information
or a source was changed, or a later run did not end with a report), steps 5 and 6 close
and the report on screen is marked as the one of an earlier run.

### Step 6. Download and send

The file name, the download button, and: send it to the person who asked you for it,
by e-mail or attached to your support ticket; this page does not send it.

### "Open a profile"

It is for the reader of a file, not for the operator. It stays reachable from the first
screen as a secondary entry, outside the six steps, and behaves as in 0.6.0.

## The requirement on the command line

`--save-profile` without `--debug-info` is refused before anything is collected, with a
message that says how to get the text, exit code 3 like the other refused inputs. The
report alone (`-o`, no `--save-profile`) still needs no debug information.

The server follows the same rule: a run without the debug information returns the
report and no profile, so the page cannot build a file from it.

A profile already written without the debug information (any earlier version) still opens
with `--show-profile`, `--show-sheet`, `--show-questions` and "Open a profile".

## The trust document

One new file, `docs/what-it-does.md`, linked from the top of the README and from step 1.
Written for someone deciding whether to allow the tool, not for a developer. It says:

- what the tool is and is not (read-only, local, not an official n8n project);
- for each source: what is asked of the operator, the exact calls or queries made, and
  what is kept of the answers;
- what the report contains and that it stays with the operator;
- what the profile contains, what it never contains, and how the operator reviews and
  withholds entries;
- what is never done: no write to the instance, the database, Redis or the cluster; no
  Kubernetes Secret read; nothing kept after a run (the env text is held in a temporary
  file readable by the tool's user only for the length of the run, then removed; the
  API key is held in memory only); no network call other than to the n8n URL, the
  database and Redis the operator gave and, through `kubectl`, the cluster of the
  context they chose;
- how to check these claims: where the tests that hold them are, and how to run them.

It repeats nothing it can link to (`docs/rules.md`, `docs/profile-format.md`), and it
makes no claim that a test does not hold. The README's existing sections stay; where
the new document now says the same thing better, the README links to it.

## My calls (marked, not decided by the owner)

- The requirement holds on the command line and in the server too, not only on the
  page.
- Step 2 validates on "Continue", not while typing.
- "Open a profile" stays on the first screen as a secondary entry.
- The numbers of steps 3 and 5 come from running the rules (on the debug information
  alone, then on the real run), not from a table and not from a prediction.
- A new run keeps the operator's choices of step 5.
- The "never contains" claim keeps its existing exception for the typed reference; the
  field is not restricted.
- The trust document is one file under `docs/`, not a longer README.
- The run reason keeps its place in the share step, as today.
- Version 0.7.0.

## Left to the builder

- How the six steps are laid out and marked, within the page's existing visual
  language; the exact English wording, kept plain and true of the code.
- Whether the steps are separate views or sections revealed in turn, as long as focus
  moves to the new step's heading and the browser's Back does not lose input.
- How the per-source numbers are exposed by the server to the page.
- The tests: the gate on the page, the server and the command line; the per-source
  numbers against the rules; no value of the instance in any new text; the page tests
  that pin the old layout updated to the new one.

## Done means

- All tests pass; findings, scores, the report and a profile built from the same
  sources are identical to 0.6.0.
- On the page, with a keyboard only and at 375 px: the six steps can be completed; step
  3 cannot be reached without a valid debug information; the file downloads only then.
- `--save-profile` without `--debug-info` exits 3 with the message, and so does
  `--withhold n8n.version`. On the page the version has no box.
- A 0.6.0 profile opens in 0.7.0 and a 0.7.0 profile opens in 0.6.0.
- Every claim of `docs/what-it-does.md` and of step 1 names the test that holds it, in
  the builder's report.
