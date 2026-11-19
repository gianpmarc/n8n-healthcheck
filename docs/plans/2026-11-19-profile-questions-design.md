# Profile questions: design

Date: 2026-11-19. Status: approved by the user on 2026-11-19.
Builds on `2026-11-18-environment-profile-design.md` (piece 1, merged).

## Intent

A profile today holds what the probes could read. Much of what decides whether
a setup is risky cannot be read by any probe: where it runs, what sits in front
of it, how the database is protected, how upgrades are done.

Those answers do not live in one head. The person who runs the tool knows the
technical ones (hosting, proxy, database service). The organisational ones
(single sign-on, upgrade process) come out of conversations with several people,
over time, and the account manager records them in their own customer-records
system, which keeps the history.

So this piece does two things:

1. lets the person running the tool answer a short set of closed questions,
   which become `declared` facts in the profile;
2. turns any profile into a **question sheet**: everything still unknown, plus
   the questions that are only ever asked in conversation, ready for a call.

## Goals

- A fixed catalogue of closed questions, in two groups: **operator** (asked in
  the tool) and **conversation** (never asked or stored by the tool, only
  printed on the sheet).
- Every operator question is optional. An unanswered one stays `unknown`.
- A measured fact always wins over an answer.
- The question sheet, from a saved profile: command line and web viewer.
- The profile file stays exactly what the person who ran the tool saw and sent.

## Non-goals

- Storing answers gathered in conversations. They are recorded outside this
  tool, by whoever gathers them.
- Editing or annotating a received profile.
- Free-text answers of any kind.
- Branching logic between questions.
- Translations; the catalogue is in English like the rest of the tool.
- Any mapping to another product's fields.

## Anti-goals

It is a failure, even if everything works, when:

- an answer can carry a name, a host, a URL or any text the person typed;
- a `declared` value is shown or filed as if it had been measured;
- an answer overrides or contradicts a measured fact in the file;
- the questionnaire feels mandatory, or long: more than 15 operator questions;
- a file written by this version is refused by tool version 0.2.0;
- anything in piece 1's anti-goals stops holding.

## Approaches considered

1. **The file carries what the operator knows; the rest goes to the records
   system** (chosen). Answers are taken where they are cheapest, and the file
   stays what the customer reviewed.
2. **The account manager completes the received profile.** One file with
   everything, but it needs a second level of provenance, the archived file is
   no longer the one the customer reviewed, and three calls give three annotated
   versions: the tool would start keeping the history the records system keeps.
3. **No answers in the tool at all.** Simplest, but loses the answers the
   operator already has while running the tool.

Chosen by the user: 1.

## Decisions made by the user

- Answers emerge from conversations with several people at the customer, so the
  tool cannot be where all of them are collected.
- Approach 1, with the catalogue split in two groups.

## The catalogue

One entry per question: a fact id, the question as a sentence, the kind, the
allowed values with a label each, and the group. Kinds: `choice` (one value),
`yes_no`, `multi` (several values, at least one).
There is no "other, please specify": where the list cannot be complete it has an
`other` value.

The list below is a proposal for the user to correct before building. *(My
call: content, order and wording.)*

### Operator group (asked in the tool, stored as `declared`)

| Fact id | Question | Values |
|---|---|---|
| `hosting.model` | Where does n8n run? | `public_cloud`, `private_cloud`, `on_premises`, `hybrid` |
| `hosting.cloud_provider` | Which cloud provider? | `aws`, `azure`, `gcp`, `other`, `none` |
| `orchestrator` | What runs the n8n containers or processes? | `kubernetes`, `ecs`, `docker_compose`, `docker_swarm`, `nomad`, `virtual_machine`, `other` |
| `install.method` | How is n8n installed and updated? | `helm_chart`, `kubernetes_manifests`, `compose_file`, `task_definition`, `npm`, `other` |
| `n8n.image_modified` | Is the n8n image built or modified by you (extra packages, patches, a different base)? | yes / no |
| `proxy.kind` | What sits in front of n8n? | `nginx`, `traefik`, `haproxy`, `envoy`, `cloud_load_balancer`, `other`, `none` |
| `proxy.tls_termination` | Where does TLS end? | `at_proxy`, `at_n8n`, `none` |
| `proxy.sticky_sessions` | Are sticky sessions enabled in front of the main instances? | yes / no |
| `database.service` | Who operates the database? | `managed_service`, `self_managed` |
| `database.high_availability` | Does the database have a standby that takes over? | yes / no |
| `database.backups` | How is the database backed up? | `point_in_time`, `scheduled`, `none` |
| `queue.redis_topology` | How is Redis deployed? | `single`, `sentinel`, `cluster`, `managed_service` |
| `secrets.source` | Where do n8n's own secrets come from at start? | `environment`, `orchestrator_secrets`, `external_store`, `files` |
| `worker.autoscaling` | Do workers scale automatically? | yes / no |
| `observability.tools` | What do you use to watch n8n? (several) | `prometheus`, `grafana`, `datadog`, `opentelemetry`, `cloudwatch`, `splunk`, `elastic`, `other`, `none` |

Fifteen entries. One id, `orchestrator`, already exists as a measured fact (a
probe can only ever measure `kubernetes`); the other fourteen are new.
`n8n.image_modified` is deliberately not the existing `n8n.image_official`: that
one is measured from the registry name and says where the image is pulled from,
not what is in it, so the two can legitimately differ.

### Conversation group (only on the sheet, never stored)

Each has a short "why it matters" line on the sheet. No fact id, no value list.

- How do users sign in: local accounts, SAML, OIDC, LDAP?
- Is there a staging environment that mirrors production?
- How often is n8n upgraded, and who decides?
- Is there a tested rollback for an upgrade?
- Does the image or the start-up carry local patches or preloaded code?
- How do workflows move from development to production?
- Which workflows would hurt most if they stopped, and what volume do they carry?
- Who is the technical owner, and who is on call?
- Which region or regions does it run in, and are there data-residency rules?
- What is the change-approval process for the platform?

## How answers enter the profile

- An answer is accepted only for an operator question, only with a value from
  that question's list, and only when the fact is `unknown` after the probes
  ran. It then becomes `provenance: "declared"`, `value` the chosen value,
  `source: null`, `detail: ""`.
- **Measured wins.** When the probes measured the fact, an answer for it is
  dropped. On the command line a line on stderr says so; in the web interface
  the question is simply not asked.
- The fourteen new facts exist in every profile written from now on. Unanswered,
  they are `unknown` with the detail "not answered".
- **How a `multi` answer is stored.** As an object holding only the chosen
  values, each `true`, in catalogue order: `{"prometheus": true, "datadog":
  true}`. A list is not used because tool version 0.2.0 refuses a list as a
  fact value, while it accepts and displays an object ("prometheus: yes,
  datadog: yes"). On the command line it is typed `ID=a,b`. *(My call.)*
- `multi` with `none` chosen together with another value is refused.
- Order of operations: probes, then answers, then withholding. A declared fact
  can be withheld like any other.
- **Format.** No change to `format_version`: tool version 0.2.0 already accepts
  unknown fact ids, the `declared` provenance and a null source (see `validate`
  in `profile.py`), and its viewer groups facts by the part of the id before
  the first dot. A test proves it by validating a new file, every question
  answered, with a copy of the 0.2.0 `validate` rules kept in the test. The frozen list of required ids does not grow.
- Tool version becomes `0.3.0`.

## The question sheet

Markdown, rendered from a saved profile, in three parts:

1. **Could be measured.** Unknown facts that a probe can fill, each with its
   question as a sentence and the profile's own `detail` (which source was
   missing, or why it could not be told).
2. **Ask whoever runs the platform.** Operator questions still `unknown`, with
   their allowed values. `orchestrator` is listed here, once, not in part 1.
3. **Ask in conversation.** The whole conversation group, always, each with its
   "why it matters" line.

Withheld facts are listed in one line at the end, by id, as "withheld by the
sender": they are not questions to press on. The sheet has a header with the
profile's date, reason and reference.

Every fact of the piece 1 catalogue needs a question sentence for part 1
("How many worker replicas run?"). *(My call: these live next to the catalogue
entry, one sentence each.)*

## Where the code goes

- `n8n_healthcheck/questions.py`: the two groups as data, and the sentence for
  each measured fact. `profile.py` gains `apply_answers` and
  `render_questions`; the fourteen new ids join `CATALOGUE` (not
  `FORMAT_1_FACT_IDS`).
- `docs/profile-format.md`: the new ids, `declared`, the rule that measured
  wins, and a short section on the sheet.

### Command line

```
--answer ID=VALUE       repeatable; for `multi`, ID=a,b
--list-questions        print the operator questions with their values, exit 0
--show-questions FILE   print the question sheet of a saved profile, exit 0
```

`--answer` needs `--save-profile`. An unknown id, a conversation question, a
value outside the list, or the same id twice is a usage error (exit 3) before
anything is collected. An answer dropped because the fact was measured is not an
error. `--show-questions` follows the rules of `--show-profile`.

### Web interface

- `GET /api/meta` gains `questions`: the operator group (id, sentence, kind,
  values with labels). The browser holds no copy of the catalogue.
- In the share step, above the tables: **"A few questions"**, one control per
  operator question whose fact came back `unknown`; radio buttons for `choice`
  and `yes_no` with a "no answer" option selected by default, checkboxes for
  `multi`. A line says they are optional, that answers are written as "declared"
  and that the list of what the tool measured is below.
- An answered question shows in the facts table as `DECLARED` with its value,
  and can be withheld there like any fact.
- The browser applies answers the way it applies withholding: it sets the three
  fields on an entry the server produced, with a value taken from the list the
  server sent. The file must be byte-identical to what `apply_answers` followed
  by `apply_withholding` writes for the same choices, and the existing harness
  tests that.
- A new run clears the answers, like the other choices.
- In the viewer ("Open a profile"), a **"Questions still open"** section with
  the sheet, and a button to copy it as Markdown. The file is never uploaded, so
  the page renders the sheet itself from the opened file plus catalogue data in
  `/api/meta` (conversation questions and the sentences for measured facts are
  therefore served there too). The copied Markdown must equal what
  `render_questions` prints for the same file, and the page harness tests that.
  *(My call: the person reading profiles works in the web interface, so the
  sheet belongs there, and two renderings that drift apart would be worse than
  the cost of testing them against each other.)*

## Tests

- Answers: accepted, each refusal, measured wins, `multi` ordering and `none`,
  withheld after declared.
- Zero leak still holds with every question answered: the only new text in a
  file is values from the catalogue.
- A file with all fourteen new facts declared is accepted by the 0.2.0
  validation rules, and a 0.2.0 file (without them) still opens and gets a
  sheet in which they appear as open questions.
- Sheet: the three parts, withheld line, a profile with nothing unknown.
- Every catalogue id has a sentence; every operator question is documented in
  `docs/profile-format.md`; operator group has at most 15 entries.
- Web: browser output equals Python's for answers plus withholding; the
  questions section, the viewer's sheet, keyboard, phone width, real browser.
- Existing tests pass. Four of them count or enumerate the catalogue and are
  updated for the new ids, nothing else in them changes:
  `tests/test_profile.py` (the table covering `FACT_IDS`, and the check that
  every id is documented, which should accept a new "added in 0.3.0" section of
  `docs/profile-format.md`), `tests/test_web_profile.py` (the count of 32
  facts), `tests/page_harness.js` (the `/api/meta` stub).

## Left to the builder

- Internal structure of `questions.py`.
- Wording of labels and "why it matters" lines, within the catalogue above.
- Layout of the questions section and the sheet in the page, within the existing
  visual language.
- Whether the work is one session or two (core and command line, then web), as
  in piece 1.
