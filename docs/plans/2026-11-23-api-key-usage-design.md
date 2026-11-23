# Usage numbers from the n8n API: design

Date: 2026-11-23. Status: approved by the user on 2026-11-23.
Builds on `2026-11-18-environment-profile-design.md` (piece 1),
`2026-11-19-profile-questions-design.md` (piece 2) and
`2026-11-19-debug-info-source-design.md` (piece 3), all merged. Tool version
today: 0.4.0.

## Intent

A profile says how an instance is built. It does not say how much it is used.
The person who files a profile next to a customer's record wants four numbers
to understand how the customer is doing and where they are heading: executions
per day, the failure rate, the number of active workflows and the number of
users.

These were planned to come from Postgres. Database access is the source people
give least, and asking for it is often not feasible. What an administrator can
always do, without involving whoever runs the database or the cluster, is stay
inside the product: create an n8n API key in the interface, hand it to the
tool, and revoke it afterwards.

This piece adds the n8n API key as a source and reads the four numbers through
n8n's public API. They describe; they do not judge.

## What the API offers

Observed on two running instances, n8n 3.3.0 and n8n 2.16.1 (community
licence, SQLite), and read in n8n's source at the tags 1.50.0, 1.70.0, 1.90.0
and 2.16.1. Base path `/api/v1`, key in the header `X-N8N-API-KEY`. Without a
key, or with one n8n does not know, every call answers 401. *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*

| Call | Gives | Key scope | Notes |
|---|---|---|---|
| `GET /insights/summary` | `total.value`, `failed.value`, `failureRate.value` (a ratio) for a date range; with no dates, the last 7 days | `insights:read` | 200 on 3.3.0, 404 on 2.16.1 (added in 2.17.0). 403 for a key without the scope, and for a member's key on 3.3.0. The community licence allows only the 7-day range: 14 days and more answer 403. Dates must be full ISO date-times. **It lags**: n8n compacts Insights once an hour by default, so the summary said 0 right after 24 executions and matched the executions list exactly (27 in total, 5 failed) once compacted. |
| `GET /executions` | a page of executions, newest first (`id DESC` at every tag), each with `startedAt`, `stoppedAt`, `waitTill`, `finished`; `limit` up to 250, `nextCursor` | `execution:list` (no scopes before 1.90.0) | Only executions that n8n saved and pruning has not deleted, never the ones still running, and only those of workflows the key's user may see. `status` is in each item on 2.16.1 and 3.3.0 (`success`, `error` observed) and **is not there at 1.50.0, 1.70.0 and 1.90.0**. `startedAfter` is an unknown parameter on 2.16.1 (400), so no filter is used. |
| `GET /workflows?active=true` | a page of workflows, each the full workflow; `nextCursor` | `workflow:list` | No count field: pages must be walked. Only the workflows the key's user may see: a member's key got 1 of 4. |
| `GET /users` | a page of users, each with e-mail, name and `isPending` | `user:list` | **A member's key gets the whole list too** (200, all 4 users) on 2.16.1 and 3.3.0: a member may hold `user:list`. 403 for a key without the scope and, in the source of every tag, when the licence limits the number of users. At 1.50.0 and 1.70.0 the path needs the global scopes `user:list` and `user:read`, which a member does not have. |
| `GET /users/{id}` | one user | `user:read` | Used only for its status: 200 for the owner's key, 403 for a member's, on both instances. Only an owner or an admin holds `user:read`. |

## Goals

- A run can take an n8n API key, next to the URL, on the command line and in
  the web interface.
- The profile and the report gain the four numbers, each with how it was
  obtained and over which period.
- Each number stands alone: a key that cannot read one of them still gives the
  others, and the output says which is missing and why.
- The key is used for reading only and never leaves the run.

## Non-goals

- Findings, scores or thresholds based on the numbers (a later piece, once
  numbers from real instances have been seen).
- The same numbers from Postgres, or from the metrics endpoint.
- Per-workflow or per-project numbers, trends, or a history of several runs.
- Any other use of the key: credentials, variables, the audit endpoint, tags,
  source control, workflow content.
- Creating, checking the scopes of, or revoking the key for the person.
- Reading the n8n version from the API.

## Anti-goals

It is a failure, even if everything works, when:

- the key, or any part of it, appears in a snapshot, a profile, a report, a log
  line, an error message, the page or a saved file;
- the key is sent to any host other than the one of the given URL, including
  through a redirect;
- the tool sends anything but `GET` with the key;
- a workflow name or id, a node, a user's e-mail, name or id, a project name, an
  execution id, or any text of an API response other than a count, a known
  status word or a date used for arithmetic reaches an output;
- an estimate is shown as an exact number, or a number is shown without its
  period;
- a run without a key gives a report, a score or a profile fact that differs
  from what version 0.4.0 gives, other than the new facts being `unknown`;
- a file written by this version is refused by tool versions 0.2.0 to 0.4.0;
- a slow or huge instance makes the run hang or issue requests without a bound;
- anything in the anti-goals of pieces 1 to 3 stops holding.

## Approaches considered

1. **API key, the Insights summary first, counting pages where it is missing**
   (chosen). One request on recent n8n, independent of pruning; an estimate,
   said to be one, on older n8n.
2. **API key, counting pages only.** One code path for every version, but
   hundreds of requests on a large instance and blind to pruned executions:
   worst exactly where the numbers matter most.
3. **No key: questions in the question sheet.** No secret involved, but the
   numbers are typed by a person, less reliable and not repeatable.
4. **Postgres**, as first planned. Rejected by the user: access is often not
   feasible.

Chosen by the user: 1.

## Decisions made by the user

- Staying inside the product matters more than what database access could add.
- The four numbers: executions per day, failure rate, active workflows, users.
- Approach 1.
- The numbers only describe. No finding and no score comes from them in this
  piece.

Everything below is my proposal unless marked otherwise.

## Collecting

A new collector, enabled when a key is given. It needs the URL: a key without a
URL is a usage error. The key goes in the `X-N8N-API-KEY` header of requests to
`<url>/api/v1/...` and nowhere else.

- **Redirects.** A request that carries the key does not follow a redirect to
  another origin (scheme, host and port). A refused redirect makes that number
  unknown.
- **Plain HTTP.** Allowed, because instances on a private network or on the same
  machine are common. When the URL is `http` and the host is not a loopback
  address, the Summary says the key was sent without encryption.
- **Bounds.** Every request uses the tool's existing timeout. A list is walked
  for at most 40 pages of 250 items (10 000 items). The whole collector issues
  at most 130 requests (1 + 40 for the executions, 40 + 1 for the users, 40 for
  the workflows: 122, so the fifth call fits the bound), reads at most 32 MB for one response, and has 60
  seconds in all, measured on the clock and not per request. A number that is
  not read when the time is over, or whose response is too large, is unknown
  ("the instance answered too slowly" or "the answer was too large"). The
  numbers are read in this order: executions, users, active workflows (the
  heaviest answers last). *(Added after the builder check: the page gives up
  after 3 minutes and the server runs one check at a time.)*
- **Failures.** The four numbers are read one after the other; a failure of one
  never stops the others. Only the HTTP status of a failed call is reported,
  never the response body. 401 on the first call stops the collector with "the
  API key was refused".
- **Response bodies** are read for the fields named below and dropped. Nothing
  else is kept in memory beyond the call.

### Executions per day and failure rate

Period: the 7 days before the run. Fixed, not an option. *(It is the API's
default, so the range most likely to be licensed everywhere, and one week
smooths the difference between working days and weekends.)*

1. `GET /insights/summary` with no dates. On 200: executions per day is
   `total.value / 7`, the failure rate is `failed.value / total.value`. Method
   `insights`. What counts as an execution is whatever n8n's Insights counts;
   the detail says so and the tool does not second-guess it.
2. On 403 or 404 (older n8n, Insights not available, scope missing, range not
   licensed): walk `GET /executions`, newest first, without execution data,
   until an execution started more than 7 days ago is met, the list ends, or the
   page bound is reached. Method `listed`.
   - Counted: executions whose status is `success`, `error` or `crashed`.
     Failed: `error` and `crashed`. Running, waiting, new, canceled and unknown
     ones are in neither number.
   - The period is the time actually covered: 7 days when an execution older
     than that was met; otherwise from the oldest execution read to the run,
     whether the page bound was reached or the list simply ended. Executions
     per day is the count divided by that period. *(Changed after the builder
     check: n8n's default pruning keeps 10 000 executions, the same number as
     the page bound, so dividing by 7 when the list ends would be wrong many
     times over on a busy instance.)*
   - When the period covered is shorter than one day, both numbers are unknown
     ("less than one day of executions is stored"): a rate per day from a few
     hours, or from three executions, would mislead.
   - A listed result is **always an estimate**: the list holds only executions
     that n8n saved and has not pruned, and only those the key's user may see.
     The reason says what is known: `page_limit` (the bound was reached),
     `short_history` (the list ended before 7 days: pruning or a young
     instance, the run cannot tell which), `partial_view` (see "A key that does
     not see everything"), otherwise `saved_only`.
   - When another source of the same run states that successful executions are
     not saved, both numbers are unknown on this path ("successful executions
     are not saved, so the list cannot count them"). When it states that failed
     ones are not saved, the failure rate is unknown. This is decided where the
     facts and the report are built, with the precedence of piece 3; the
     collector stores what it counted and does not read other sources.
   - **n8n versions whose list has no `status`** (1.x). *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*
     Executions per day is still given, method `listed`, always an estimate. An
     execution is counted there when it has stopped (`stoppedAt` is set) and is
     not waiting (`waitTill` is empty); the list never holds the ones still
     running. This also counts canceled executions, which the list cannot tell
     apart. The failure rate is unknown, with the detail "this n8n version does
     not report the outcome of an execution in the list": no failure is derived
     from `finished`. The snapshot stores `failed: None` for it.
   - An empty list covers no time at all, so it is "less than one day", not
     zero executions per day: an instance that saves nothing would look idle.
     *(Builder's call.)*
3. A total of zero gives `0` executions per day and an unknown failure rate
   ("no execution in the period").

The builder must check, in n8n's source at the tags 1.50.0, 1.70.0, 1.90.0 and
a 2.x tag below 2.17.0, how the executions list is ordered, which fields and
filters it has, and how the status is written, as piece 3 did for the debug
information. If newest-first cannot be relied on for a version, the numbers are
unknown for it rather than guessed. Anything that contradicts this section is
brought back before it is built.

### A key that does not see everything

*(Changed after the check against real n8n, decided by the owner on 2026-11-23.)* For a user who is not the owner or an admin, n8n's lists
return only what that user may see, so the same calls give part of the instance
and nothing in the answer says so. A 403 on `GET /users` is not the signal: a
member's key lists the users (see the table). The signal is a fifth call,
`GET /users/<id>` with the id of the first user of the list just read. Only an
owner or an admin may read a user's detail. The tool looks at the status and
nothing else: the answer is not read, and the id is used in the request path
only and never kept. An id that is not letters, digits and hyphens is not sent.

When that call does not answer 200 (or could not be made, because the users
list was refused), the key may not see the whole instance. Then the listed
executions and the active workflows are marked as partial: the facts' details
and the Usage section say "as far as this key's user can see", `usage.estimate`
is true with the reason `partial_view`, and `usage.active_workflows_partial` is
true. Numbers from the Insights summary are not marked. A key of the owner
whose scopes leave out `user:list` or `user:read` is marked too; the form's
help line names both scopes. A 403 on the users list only makes the users
number unknown.

The users number is not partial for a member's key: on both instances it got
the same list as the owner's.

### Active workflows

Walk `GET /workflows?active=true`, count the items, keep nothing else. When the
page bound is reached the fact is unknown with the detail "more than 10 000
active workflows".

### Users

Walk `GET /users`, count the users that are not pending (an invitation not yet
accepted is not a user). On 403: unknown, "the API key cannot list users: its
scopes leave that out, or the licence limits the number of users". *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*
More than 10 000 users: unknown, like the active workflows.

### What is kept (the snapshot section)

`Target` gains `api_key`. `Snapshot` gains `usage`, `None` when no key was
given. *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)* With a key the section is always there, also when
no number could be read, so that the report can say why:

```
{"executions": None | {"method": "insights" | "listed", "total": int, "failed": int | None,
                       "period_days": number, "estimate": bool,
                       "estimate_reason": "page_limit" | "short_history" | "saved_only" | None},
 "active_workflows": int | None, "users": int | None,
 "refused": {"executions": R, "users": R, "active_workflows": R},
 "whole_instance": bool, "key_unencrypted": bool}
```

`failed` is `None` when the list has no `status`. `refused` holds what made a
number unknown (`R`): the HTTP status, or one of `"limit"` (the page bound on
workflows or users), `"slow"`, `"large"`, `"short"` (less than one day
covered), `"unreachable"`, `"invalid"` (an answer that is not what n8n writes),
`"redirect"` (a redirect to another origin, not followed); `None` when the
number was read. `whole_instance` is true only when the detail of a user
answered 200; `partial_view` is not stored, it follows from
`whole_instance` being false. The key is not a field of `Snapshot`. As with the
debug information, the section is read back through strict readers (whole
numbers that are not negative, the listed words), because a snapshot can be
loaded from a file. The exact shape is the builder's, as long as no key, no
identifier and no free text is in it.

## Profile

`sources` gains `api` with the states `collected`, `failed` and
`not_provided`. `collected` means at least one number was read. The list of
sources a file must hold stays the six of today.

New facts, added in 0.5.0 after the existing ones (eight):

| Fact id | Value | Meaning |
|---|---|---|
| `usage.executions_per_day` | number | Executions per day over the period. |
| `usage.failure_rate_percent` | number, 0 to 100 | Failed executions over counted executions. |
| `usage.period_days` | number | The period the two numbers above cover. |
| `usage.method` | `insights`, `listed` | How the two numbers were obtained. |
| `usage.estimate` | boolean | True when they are an estimate: always for `listed`, never for `insights`. |
| `usage.active_workflows` | integer | Workflows that are active. |
| `usage.active_workflows_partial` | boolean | True when the key may not see every workflow. |
| `usage.users` | integer | Users, without pending invitations. |

Each has `provenance: "measured"`, `source: "api"`, and a `detail` that states
the method and, for an estimate, why. Without a key each is `unknown` with the
detail "The n8n API key was not provided". A number that could not be read is
`unknown` with its reason. Each fact gets a sentence for part 1 of the question
sheet, as piece 2 requires; none of them becomes a question a person answers in
this piece.

No change to `format_version`: versions 0.2.0 to 0.4.0 accept an extra source
name and unknown fact ids. `FORMAT_1_FACT_IDS` does not grow.
`docs/profile-format.md` gains the source and the eight facts. The existing
fact `database.execution_count` (from Postgres) is untouched: it is how many
executions are stored, not how many run. Tool version becomes `0.5.0`.

## Report

The API key is not one of the six data sources the report counts, like the
debug information.

- **No key.** The report and the page are exactly as today, word for word.
- **Key given.** A section "Usage" after the Summary: the period and method on
  one line, then the four numbers. When the method is `insights` that line also
  says that Insights can lag behind by up to an hour. *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)* An estimate is written with "about" and its
  reason; a number that could not be read is listed with its reason. One line
  in the Summary: "Usage numbers were read through the n8n API." (or that the
  key was refused), followed when it applies by the line about the unencrypted
  key.
- **Key and URL only.** The run is assessed as today for a URL alone; the Usage
  section adds to it and changes no score.
- **Key given, URL unreachable.** The source is `failed`; the run ends as it
  does today when the URL cannot be reached.

No rule reads the section. The test that ties `Snapshot`'s fields to
`report.SECTIONS` allows this second field outside the six.

## Command line

```
--api-key-file FILE   file holding an n8n API key, used only to read usage
                      numbers; "-" reads it from standard input. Needs --url.
```

There is no flag that takes the key itself: it would stay in the shell history
and in the process list. There is no environment variable either *(dropped
after the builder check: one left set in a shell would turn runs that work
today into usage errors; in the web interface the key is typed in the form)*. A file that cannot be read, is empty, or holds
more than one line is a usage error (exit 3) with no part of its content in the
message. The key with `--snapshot`, or without `--url`, is a usage error.
`--save-snapshot` stores the `usage` section and never the key. `"-"` cannot be
used for both `--debug-info` and `--api-key-file`.

## Web interface

- `POST /api/run` accepts `api_key`; `GET /api/meta` lists the new source. A
  key without a URL is a 400.
- The form gains an "n8n API key" field in the "n8n URL" source: a password
  field, not filled in again after a run, with a line that says where to create
  the key in n8n, that it is used only to read four numbers, which scopes are
  enough where n8n lets them be chosen (with `user:read`), that the key should
  be the owner's or an admin's because any other sees only its own user's
  workflows and executions, and that the key can be revoked after the run.
  *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*
- The results show the Usage section in the page's own structure, after the
  summary card and before the scores. It is not a score card and has no
  severity colour.
- The share step, the questions and the viewer need no new behaviour: the new
  source and facts appear through the existing tables, and each fact can be
  withheld like any other.
- The server never logs the request body and never echoes the key back.

## README

One paragraph: what the key is used for, the four `GET` calls, the scopes, and
the advice to create a key for the run and revoke it afterwards.

## Tests

- A fake n8n API served by the standard library on a local port, with made-up
  data: no real instance, no real key, no real name or e-mail.
- Each number: the Insights path; the listed path after 404 and after 403; zero
  executions; the page bound; a list that ends before 7 days; less than one
  day covered; statuses that are not counted; successful or failed executions
  known not to be saved; pending users; a refused users list, which leaves only
  the users unknown; the refused detail of a user (a member's key that lists
  the users all the same) and the partial marking that follows from it; a list
  without `status` (n8n 1.x): executions per day given, failure rate unknown,
  running and waiting ones not counted; more than 10 000 active workflows.
  *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*
- One number failing leaves the other three; 401 stops the collector.
- Zero leak: a key, workflow names, e-mails, user names and execution ids that
  are marker strings; no marker in the snapshot, the saved snapshot, the report,
  the profile, any error or warning, the JSON of the web interface or the page.
- The key goes only to the given origin: a redirect to another host is not
  followed and that host never receives the header.
- Read only: every request the collector issues is a `GET`.
- The bounds hold against a server that never stops paging, one that never
  answers, one that answers one byte at a time, and one that sends an endless
  body: the collector returns within its 60 seconds.
- A run without a key gives the same report text, scores and existing facts as
  0.4.0.
- Old files: 0.2.0, 0.3.0 and 0.4.0 profiles still open; a 0.5.0 file passes
  the 0.2.0 validation rules kept in `tests/validate_0_2_0.py`.
- Command line: the flag, standard input, each usage error.
- Web: the field with and without a URL, the refused key, a partial result,
  keyboard, phone width, a real browser.

## Not verified by this design

- A running n8n 1.x: the table above comes from two running instances (3.3.0
  and 2.16.1) and, for 1.x, from n8n's source only. *(Changed after the check against real n8n, decided by the owner on 2026-11-23.)*
- Which date ranges of the Insights summary the paid licences allow (the
  community licence allows 7 days and no more).
- A licence that limits the number of users (403 on `GET /users`): source only.
- What n8n's Insights counts as an execution (for example whether manual
  executions are in it).
- How the executions list behaves on n8n 1.x (left to the builder, see above).

## Checking against a real n8n

The builder starts two throwaway n8n containers of its own, before writing the
collector and again at the end: one recent image and one below 2.17.0. They
have their own Compose project name and ports (never 5678 or 8080), a made-up
owner and a key created for the test, and they are removed with their volumes
when the work ends. No container that already exists on the machine is
stopped, restarted or rebuilt. What the two instances answer replaces the
table in "What the API offers" wherever it differs, and a difference that
changes this design is brought back before it is built. The made-up owner and
key are never committed.

## Left to the builder

- The collector's module name and internal structure, and where the request
  helper lives next to the existing one.
- Wording of details, labels and the Usage section, within the tables above.
- Layout of the field and of the Usage section within the existing visual
  language.
- Whether the work is one session or two (collector, profile, report and
  command line; then web), as in the earlier pieces.
