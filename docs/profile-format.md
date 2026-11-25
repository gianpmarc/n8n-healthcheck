# Environment profile format

An environment profile is one JSON file of facts about a self-hosted n8n
environment. `n8n-healthcheck` writes it next to the health report, the person
who ran the tool reviews it, and they decide whether to send it to whoever is
helping them. The tool never sends it anywhere.

This page describes the file for anyone who reads one: by eye, with
`n8n-healthcheck --show-profile FILE`, or from their own code.

- Format name: `n8n-healthcheck-profile`
- Format version: `1`
- Encoding: UTF-8, strict JSON (no `NaN`, no `Infinity`)
- Suggested file name: `n8n-profile-YYYY-MM-DD.json`

## What a profile never contains

No secret, host name, URL, IP address, file path, e-mail address, and no
Deployment, namespace, container or image registry name. No customer name
either: the file does not say whose environment it is.

This is enforced by how the file is built, not by search and replace:

- no fact holds free text; every fact value has a fixed shape (see below), and
  a value that does not fit is not written;
- a setting's value is written only for a variable n8n documents as a boolean,
  a number or one word of a fixed list, and only when the value is one (see
  "Settings");
- variables n8n does not document are counted, not named.

The only text a person types is `run.reference`.

## Example

```json
{
  "format": "n8n-healthcheck-profile",
  "format_version": 1,
  "tool_version": "0.3.0",
  "created_at": "2026-11-19T10:22:00Z",
  "run": {"reason": "pre_upgrade", "reference": "upgrade to 2.50, change 1234"},
  "sources": {"health": "collected", "metrics": "disabled", "env": "not_provided",
              "postgres": "collected", "redis": "failed", "kubernetes": "collected"},
  "facts": [
    {"id": "executions.mode", "value": "queue", "provenance": "measured",
     "source": "kubernetes", "detail": ""},
    {"id": "database.version", "value": null, "provenance": "unknown",
     "source": null, "detail": "Postgres was not provided"},
    {"id": "worker.replicas", "value": null, "provenance": "withheld",
     "source": null, "detail": ""},
    {"id": "hosting.model", "value": "public_cloud", "provenance": "declared",
     "source": null, "detail": ""}
  ],
  "settings": [
    {"scope": "main", "name": "EXECUTIONS_DATA_PRUNE", "state": "value", "value": "true"},
    {"scope": "main", "name": "N8N_EDITOR_BASE_URL", "state": "set", "value": null}
  ],
  "settings_not_listed": 7,
  "check": {
    "partial": true,
    "scores": [{"area": "reliability", "score": 60, "partial": true}],
    "findings": [{"rule_id": "REL-002", "severity": "warning",
                  "area": "reliability", "title": "..."}]
  }
}
```

(The example shows four facts. A real file always has the full catalogue, and
from tool version 0.6.0 `check` also holds `checks`: see "Check".)

## Top-level fields

| Field | Meaning |
|---|---|
| `format` | Always `n8n-healthcheck-profile`. |
| `format_version` | Integer. See "Versioning". |
| `tool_version` | Version of `n8n-healthcheck` that wrote the file. |
| `created_at` | When the run happened, UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `run.reason` | Why the profile was taken: `baseline`, `pre_upgrade`, `post_upgrade`, `incident` or `other`. |
| `run.reference` | Free text typed by the person who ran the tool, one line, at most 200 characters. May be empty. |
| `sources` | What the tool was able to read. See "Sources". |
| `facts` | The fact catalogue, always complete and in the same order. See "Facts". |
| `settings` | The n8n settings that were set. See "Settings". |
| `settings_not_listed` | How many other distinct variables were set and are not listed, not even by name. |
| `check` | The outcome of the health check on that day. See "Check". |

## Sources

One state for each thing the tool can read: `health`, `metrics`, `env`,
`postgres`, `redis`, `kubernetes`.

| State | Meaning |
|---|---|
| `collected` | It was read. |
| `disabled` | Only for `metrics`: the metrics endpoint is switched off on the instance. |
| `failed` | It was asked for and could not be read. |
| `not_provided` | It was not asked for. |

`env` is the environment given as a file. When the environment was read from
Kubernetes instead, `env` stays `not_provided` and each fact says where it came
from in its own `source`.

From tool version 0.4.0 a file also lists a seventh source, `debug_info`: the
"Debug information" text n8n offers under Help > About n8n (see "Facts from the
debug information"). Its state is `collected` or `not_provided`, never
`failed`: a text that cannot be read stops the run before anything is written.
A file written by version 0.2.0 or 0.3.0 does not have it, and a reader must
not require it.

From tool version 0.5.0 a file lists an eighth source, `api`: n8n's public API,
read with an API key for the usage numbers (see "Usage numbers from the n8n
API"). Its state is `collected` when at least one of the numbers was read,
`failed` when a key was given and none could be read, and `not_provided`
without a key. A file written by an earlier version does not have it, and a
reader must not require it.

## Facts

Every fact has the same five fields:

| Field | Meaning |
|---|---|
| `id` | The fact, from the catalogue below. |
| `provenance` | `measured`, `unknown`, `withheld`, or `declared` (see below). |
| `value` | The value when `provenance` is `measured` or `declared`; otherwise `null`. |
| `source` | Which source a measured value came from (a key of `sources`); `null` for every other provenance. |
| `detail` | One short fixed sentence when it helps, for example why a fact is unknown. May be empty. Never quotes what was observed. For a fact read from a debug information it carries the date that text was generated: a date, never text that was observed. |

Provenance:

- `measured`: the tool read it from the environment. This includes a value
  read from a debug information, which n8n generates itself: its `source` is
  `debug_info` and its `detail` says how old the text is.
- `unknown`: the tool could not tell for certain. `detail` says why. Unknown
  never means "no" or "zero".
- `withheld`: the person who ran the tool chose to leave it out. It was not
  necessarily known.
- `declared`: the person who ran the tool answered a closed question (see
  "Declared facts"). The value is one of a fixed list, never text they typed.
  It was not checked by the tool: do not read it as measured. Version 0.2.0 of
  the tool never writes it.

A fact is `unknown` rather than guessed when: its source was not read; the
variable it depends on is set but its value could not be read (it comes from a
Kubernetes Secret, or it was redacted); part of the container environment could
not be read, so "not set" cannot be proven; the variable is given through a file
(`<NAME>_FILE`); the containers of the role disagree on it; or the value does
not have the shape the fact allows.

### Which environment a fact is read from

The environment file when one was given. Otherwise the Kubernetes containers of
the role the fact is about: the main n8n containers for every fact below read
from a variable, the worker containers for `worker.concurrency`. Containers are
compared on the one variable the fact needs, so two Deployments that differ in
something else still give an answer.

### Defaults

Three facts are reported as measured, with the detail "n8n default, variable
not set", when the variable is provably not set, because their default is the
same in every n8n version: `executions.mode` (`regular`), `database.engine`
(`sqlite`) and `multi_main.enabled` (`false`). When Kubernetes was read and the
main containers do not run the official image, even these are `unknown`: an
image built by someone else may set the variable itself. For every other fact, a variable
that is not set gives `unknown` with the detail "not set, the n8n default for
this version applies": look the default up for the n8n version in `n8n.version`.

### Catalogue, version 1

| Id | Value | From |
|---|---|---|
| `n8n.version` | version | The version n8n reports on its metrics endpoint; otherwise the image tag, when every main container runs the official image with the same tag. The tag of any other image is not taken as a version. |
| `n8n.image_official` | boolean | Kubernetes: `true` when every n8n container runs `n8nio/n8n`, `docker.io/n8nio/n8n` or `docker.n8n.io/n8nio/n8n`; `false` when none does (a custom build, or a copy in another registry); unknown when they are mixed. The image name actually used is never written. |
| `orchestrator` | `kubernetes` | `kubernetes` when Kubernetes was read. Otherwise unknown: the tool cannot tell Docker from a bare process. From tool version 0.3.0 the person running the tool can then declare it, with more values: see "Declared facts". |
| `executions.mode` | `regular`, `queue` | `EXECUTIONS_MODE`. |
| `main.replicas` | integer | Kubernetes: replicas of the Deployments that run the main n8n process, summed. |
| `worker.replicas` | integer | Same, for the worker process. |
| `webhook.replicas` | integer | Same, for the webhook process. |
| `worker.concurrency` | integer | Executions one worker runs at a time: `N8N_CONCURRENCY_PRODUCTION_LIMIT` unless it is not set or `-1`, otherwise the worker's `--concurrency` flag, otherwise 10. Needs the worker containers from Kubernetes. Unknown when the flag is there but is not a plain number, and when the limit is given through `N8N_CONCURRENCY_PRODUCTION_LIMIT_FILE`. The default of 10 is stated only for the official image; for any other image with neither set, the fact is unknown. |
| `multi_main.enabled` | boolean | `N8N_MULTI_MAIN_SETUP_ENABLED`. |
| `task_runners.enabled` | boolean | `N8N_RUNNERS_ENABLED`. |
| `task_runners.mode` | `internal`, `external` | `N8N_RUNNERS_MODE`. |
| `binary_data.mode` | `default`, `filesystem`, `s3`, `azure`, `database` | `N8N_DEFAULT_BINARY_DATA_MODE`: the value of the variable. When the fact is read from a debug information instead, it is the effective mode, whatever set it (n8n's `memory` is written as `default`). |
| `metrics.enabled` | boolean | `true` when the metrics endpoint answered, `false` when it answered "not found". |
| `pruning.enabled` | boolean | `EXECUTIONS_DATA_PRUNE`. |
| `pruning.max_age_hours` | integer | `EXECUTIONS_DATA_MAX_AGE`. |
| `database.engine` | `sqlite`, `postgresdb`, `mysqldb`, `mariadb` | `DB_TYPE`. |
| `database.version` | version | Postgres server version. |
| `database.pool_size` | integer | `DB_POSTGRESDB_POOL_SIZE`. |
| `database.max_connections` | integer | Postgres `max_connections`. |
| `database.execution_count` | integer | Rows in n8n's execution table. An estimate above 100,000 rows. |
| `database.execution_table_bytes` | integer | Size on disk of the execution tables, in bytes. |
| `database.oldest_execution_age_days` | number | Age of the oldest stored execution, in days. Unknown, with the detail "there are no stored executions", when the table is empty. |
| `queue.redis_version` | version | Redis server version. |
| `queue.maxmemory_policy` | a Redis eviction policy, for example `noeviction` | Redis `maxmemory-policy`. |
| `queue.maxmemory_bytes` | integer | Redis `maxmemory`, in bytes. `0` means no limit. |
| `queue.waiting_jobs` | integer | Jobs waiting in n8n's queue when the tool ran. |
| `queue.active_jobs` | integer | Jobs being executed when the tool ran. |
| `queue.failed_jobs` | integer | Failed jobs kept in the queue. |
| `resources.main` | object | Kubernetes requests and limits of the main containers. |
| `resources.worker` | object | Same, for the worker containers. |
| `probes.main` | object | Whether the main containers have a liveness and a readiness probe. |
| `probes.worker` | object | Same, for the worker containers. |

Value shapes:

- **version**: `digits.digits` or `digits.digits.digits`, the leading part of
  what the software reports (`16.4` for "16.4 (Debian ...)"). A longer dotted
  number is not a version and gives `unknown`.
- **integer**, **number**: never negative. Read only from plain ASCII digits; a
  value written any other way (`1_000`, digits of another script) is unknown.
  One fact, `main.concurrency_limit`, may be the word `unlimited` instead of a
  number (see "Facts from the debug information").
- **resources object**:
  `{"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"cpu": null, "memory": "1Gi"}}`.
  Each quantity is a Kubernetes quantity as a string, or `null` when not set.
  Only `cpu` and `memory` are written.
- **probes object**: `{"liveness": true, "readiness": false}`.

Replica facts: only Deployments are read. A role for which no Deployment was
found is `unknown`, never `0`, because it may run as another kind of workload.
A measured `0` means a Deployment exists and is scaled to zero.

`resources.*` and `probes.*` are `unknown` when the containers of the role are
not all configured the same.

**Which containers are n8n.** The tool takes a container for n8n when its image
name contains "n8n", and tells the role by the start command (`worker`,
`webhook`, anything else is main). An exporter or another server whose image
name merely contains "n8n" would therefore look like a main container. So:

- An image called exactly `n8n`, in any registry, is n8n.
- In the main role, when at least one container is called exactly `n8n`, the
  others are left out of every fact and of the settings: they are never
  counted. The facts a left-out container could change if it were a main
  process after all (`main.replicas`, `resources.main`, `probes.main`,
  `n8n.image_official`, and `n8n.version` when it comes from the image tag)
  then say so in their `detail`, with the number only and never a name: "1
  other container with n8n in its image name was left out".
- A role in which no container is called exactly `n8n` (a build under another
  name, or only an unrelated image) gives `unknown` for its replica, resources
  and probes facts, and no version is read from its tag.
- A worker or webhook container under another name carries the role's own
  command, so it may well be n8n: it is neither counted nor left out, and the
  replica, resources, probes and concurrency facts of that role are `unknown`.

One case cannot be told apart and is resolved as the common one: a main process
built under another name (`example/n8n-custom`) running in the same namespace
as a main container called `n8n` is treated like an exporter and left out, so
`main.replicas` would not include it.

`n8n.image_official` is `false` for a copy of the official image kept in
another registry: it says where the image is pulled from, not what is in it.

## Facts from the debug information

Every n8n instance can produce a short text under Help > About n8n > Debug
information. From tool version 0.4.0 that text is a source (`--debug-info`), and
it can be the only one. The tool reads known values, numbers and versions from
it and nothing else: the instance id, the host ids, the browser's user agent
and the warning codes in it are never stored, in a profile or anywhere else.

A fact read from it has `provenance: "measured"`, `source: "debug_info"` and a
`detail` that says when the text was generated: "debug information generated on
2026-11-19", or "debug information, date unknown" when the text carries no
readable date or one in the future. The text may be old: read the date.

**A probe wins, value by value.** The debug information fills a fact only where
no other source of the same run states that value. A variable that is set in
the env file or in a main container, readable or not, is stated, and the debug
information is not used for it. A variable that is simply not set is not
stated: the debug information reports the effective value, which is better
evidence than a default, so it is used, also for the three facts under
"Defaults". No comparison is made and no disagreement is reported.

It can fill these facts of the catalogue above: `n8n.version`,
`executions.mode` (`queue` for every "scaling" value n8n writes),
`multi_main.enabled` (not from a text of n8n 1.x, which says "scaling" without
the number of mains), `database.engine`, `pruning.enabled`,
`pruning.max_age_hours`, `binary_data.mode` (the effective mode), and
`main.replicas`, `worker.replicas` and `webhook.replicas`. `worker.concurrency`
is never read from it.

The three replica facts are counted from the instances n8n lists in the text's
`cluster` section, when Kubernetes was not read or shows no container of that
role; the detail then adds "counted from the instances n8n listed". A type with
no instance listed is `unknown`, never `0`. A text without a `cluster` section
leaves them `unknown` with the detail "this debug information has no cluster
section (older n8n, or the instance registry was not loaded)".

### Added in tool version 0.4.0

| Id | Value | From |
|---|---|---|
| `n8n.node_version` | version | The Node.js version n8n runs on. |
| `n8n.platform` | `docker_self_hosted`, `docker_cloud`, `npm` | How n8n says it is installed. |
| `license.type` | `community`, `enterprise_production`, `enterprise_sandbox` | The kind of license that is active. |
| `main.concurrency_limit` | integer of at least 1, or the word `unlimited` | The limit on concurrent production executions of the main process (`N8N_CONCURRENCY_PRODUCTION_LIMIT`); `unlimited` when n8n reports `-1`. It is not the worker concurrency. |
| `executions.save_success` | `all`, `none` | Whether the data of successful executions is saved. |
| `executions.save_error` | `all`, `none` | Whether the data of failed executions is saved. |
| `executions.save_progress` | boolean | Whether execution progress is saved after every node. |
| `executions.save_manual` | boolean | Whether manual executions are saved. |
| `pruning.max_count` | integer | How many executions are kept before the oldest are pruned. |
| `cluster.versions_aligned` | boolean | `true` when the instances n8n lists all run one version. Unknown, with a detail that says another source states the versions, when Kubernetes was read and its image tags name a version: the check outcome then judges the versions from Kubernetes. |
| `cluster.checks_failed` | integer | How many of n8n's own cluster checks have the status "failed". A check that succeeded with warnings does not count. |
| `security.secure_cookie` | `false` only | `false` when n8n flags the session cookie as not restricted to HTTPS. |
| `security.file_access_blocked` | `false` only | `false` when n8n flags that access to its own files is not blocked. |

These ids are in every file written by tool version 0.4.0 or later, after the
ids of "Declared facts", in this order. A file written by version 0.2.0 or
0.3.0 has none of them.

- **No debug information.** Each is `unknown` with the detail "The debug
  information was not provided". In this version no other source fills them.
- **Stated by a probe.** The eight that correspond to an n8n variable
  (`main.concurrency_limit`, the four `executions.save_*`, `pruning.max_count`
  and the two `security.*`) are `unknown`, with a detail that says another
  source states the setting, when that variable is set in the env file or in a
  main container: the variable is then in "Settings".
- **The two `security` facts** are written only as `false`. n8n reports these
  settings only when they are insecure, and older n8n versions do not report
  them at all, so silence is `unknown` with the detail "n8n reports this only
  when it is insecure", never `true`.
- **The two `cluster` facts** are `unknown` without a `cluster` section.

## Usage numbers from the n8n API

From tool version 0.5.0 a run can be given an n8n API key next to the URL
(`--api-key-file`, or the field in the web interface). The tool then reads four
numbers through n8n's public API, with `GET` requests only: executions per
day, the failure rate, the active workflows and the users. The numbers
describe the instance. No finding and no score rests on them. The key itself
is never written, in a profile or anywhere else, and neither is a workflow, a
user, an execution or any text of an answer: only counts.

A fact read this way has `provenance: "measured"`, `source: "api"` and a
`detail` that says how it was obtained.

**Executions** are counted over the 7 days before the run, in one of two ways,
which `usage.method` names:

- `insights`: n8n's own Insights summary of the last 7 days (n8n 2.17.0 and
  later, with a key that may read it). An execution is whatever Insights
  counts. Insights can lag behind by up to an hour, and a count of zero says
  so in its `detail`.
- `listed`: counted from the executions list, newest first, when the Insights
  summary is not available. This is **always an estimate**, and
  `usage.estimate` is `true`: the list holds only executions n8n saved and has
  not pruned. Executions with the status `success`, `error` or `crashed` are
  counted, and `error` and `crashed` are the failed ones. `usage.period_days`
  is the time the list actually covers, which is shorter than 7 when the list
  ends earlier or the tool's bound of 10 000 executions is reached. When less
  than one day is covered, the numbers are `unknown`. The list is in the order
  of the ids: executions started earlier than the 7 days are skipped, and the
  reading ends after the first page that holds only such executions. The list
  is taken to cover the whole 7 days only when it holds an older execution
  that n8n's pruning could have removed: one that stopped before the 7 days
  and is not new, running or waiting.

  n8n 1.x does not say in the list how an execution ended. When no execution
  of the period has a status, the executions that have stopped and are not waiting are counted, and
  `usage.failure_rate_percent` is `unknown`.

**A key that does not see everything.** For a user who is not the owner or an
admin, n8n's lists return only that user's workflows and executions. The tool
asks n8n for the detail of one user, which only an owner or an admin may read,
and looks at nothing but whether the request was allowed. When it was not,
`usage.active_workflows_partial` is `true`, the detail of
`usage.active_workflows` says "as far as this key's user can see", and a
listed count of executions says the same. Numbers from the Insights summary
are for the whole instance.

### Added in tool version 0.5.0

| Id | Value | Meaning |
|---|---|---|
| `usage.executions_per_day` | number | Executions per day over the period. |
| `usage.failure_rate_percent` | number, 0 to 100 | Failed executions out of 100 counted ones. Unknown when no execution was counted. |
| `usage.period_days` | number, 1 to 7 | The days the two numbers above cover. |
| `usage.method` | `insights`, `listed` | How the two numbers were obtained. |
| `usage.estimate` | boolean | `true` when they are an estimate: always for `listed`, never for `insights`. |
| `usage.active_workflows` | integer | Workflows that are active. |
| `usage.active_workflows_partial` | boolean | `true` when the key may not see every workflow. |
| `usage.users` | integer | Users, without invitations that were not accepted. |

These ids are in every file written by tool version 0.5.0 or later, after the
ids of "Facts from the debug information", in this order. A file written by an
earlier version has none of them.

- **No API key.** Each is `unknown` with the detail "The n8n API key was not
  provided".
- **A number that could not be read** is `unknown` with the reason: the key
  was refused or is not allowed to read it, the instance answered too slowly,
  the answer was too large, or there are more than 10 000 items. One number
  failing leaves the others.
- **Executions that are not saved.** When another source of the same run
  states that successful executions are not saved, a listed count cannot count
  them and both execution numbers are `unknown`; when it states that failed
  ones are not saved, the failure rate is `unknown`.
- `usage.period_days`, `usage.method` and `usage.estimate` are `unknown`
  whenever `usage.executions_per_day` is.

`database.execution_count`, from Postgres, is a different thing: how many
executions are stored, not how many run.

## Declared facts

Some of what decides whether a setup is risky cannot be read by any probe:
where it runs, what sits in front of it, how the database is protected. For
those the tool asks the person who runs it a short list of closed questions.
Every question is optional.

### Added in tool version 0.3.0

| Id | Value | Question |
|---|---|---|
| `hosting.model` | `public_cloud`, `private_cloud`, `on_premises`, `hybrid` | Where does n8n run? |
| `hosting.cloud_provider` | `aws`, `azure`, `gcp`, `other`, `none` | Which cloud provider? |
| `orchestrator` | `kubernetes`, `ecs`, `docker_compose`, `docker_swarm`, `nomad`, `virtual_machine`, `other` | What runs the n8n containers or processes? |
| `install.method` | `helm_chart`, `kubernetes_manifests`, `compose_file`, `task_definition`, `npm`, `other` | How is n8n installed and updated? |
| `n8n.image_modified` | boolean | Is the n8n image built or modified by you (extra packages, patches, a different base)? |
| `proxy.kind` | `nginx`, `traefik`, `haproxy`, `envoy`, `cloud_load_balancer`, `other`, `none` | What sits in front of n8n? |
| `proxy.tls_termination` | `at_proxy`, `at_n8n`, `none` | Where does TLS end? |
| `proxy.sticky_sessions` | boolean | Are sticky sessions enabled in front of the main instances? |
| `database.service` | `managed_service`, `self_managed` | Who operates the database? |
| `database.high_availability` | boolean | Does the database have a standby that takes over? |
| `database.backups` | `point_in_time`, `scheduled`, `none` | How is the database backed up? |
| `queue.redis_topology` | `single`, `sentinel`, `cluster`, `managed_service` | How is Redis deployed? |
| `secrets.source` | `environment`, `orchestrator_secrets`, `external_store`, `files` | Where do n8n's own secrets come from at start? |
| `worker.autoscaling` | boolean | Do workers scale automatically? |
| `observability.tools` | object of the chosen values, each `true`: `prometheus`, `grafana`, `datadog`, `opentelemetry`, `cloudwatch`, `splunk`, `elastic`, `other`, `none` | What do you use to watch n8n? |

These ids are in every file written by tool version 0.3.0 or later, after the
catalogue of version 1, in this order (`orchestrator` keeps its place in the
catalogue above). A file written by version 0.2.0 has `orchestrator` and none
of the other fourteen.

- **Not answered.** The fact is `unknown` with the detail "not answered".
- **Answered.** `provenance` is `declared`, `value` is the chosen value,
  `source` is `null` and `detail` is empty. A yes or no question gives a
  boolean. `observability.tools` takes several values and is stored as an
  object holding only the chosen ones, in the order of the list:
  `{"prometheus": true, "datadog": true}`. `none` cannot be chosen together
  with another value.
- **Measured wins.** An answer is taken only for a fact the tool could not
  measure. `orchestrator` is the one question a probe can also answer: when
  Kubernetes was read it is measured, and an answer given for it is dropped, so
  a file never holds an answer that contradicts a measurement.
- A declared fact can be withheld like any other.
- No answer is free text. Where a list cannot be complete it has an `other`
  value, and nothing more is said.

`n8n.image_modified` is not `n8n.image_official`: the second is measured from
where the image is pulled, the first is what the person says is in it. The two
can legitimately differ.

On the command line the questions are listed by `--list-questions` and answered
with `--answer ID=VALUE` next to `--save-profile` (`--answer ID=a,b` for several
values, `yes` or `no` for a yes or no question).

## The question sheet

`n8n-healthcheck --show-questions FILE` prints, from a saved profile, what is
still open, as Markdown in three parts:

1. **Could be measured.** Unknown facts a probe can fill, each with the
   profile's own `detail`: which source was missing, or why it could not be
   told.
2. **Ask whoever runs the platform.** The questions above that are still
   `unknown`, with the values an answer may hold. A file written by version
   0.2.0 has only `orchestrator` among them, so the other fourteen are always
   listed for it, and `orchestrator` too unless it was measured or withheld.
3. **Ask in conversation.** A fixed list of questions the tool never asks or
   stores (how users sign in, how upgrades are done, who is on call), each with
   a line on why it matters. Their answers belong wherever the reader keeps
   their own notes, not in the profile.

Withheld facts are listed in one line at the end, by id: the sender chose to
leave them out, so they are not questions to press on. The sheet is derived
from the file and changes nothing in it.

## Settings

The n8n settings that were set, one entry each:

| Field | Meaning |
|---|---|
| `scope` | `instance` for an environment file; `main`, `worker` or `webhook` for the Kubernetes containers of that role. When both were read, both are listed. |
| `name` | The variable name. |
| `state` | `value`, `set` or `withheld`. |
| `value` | The value as a string when `state` is `value`; otherwise `null`. |

**Which names are listed.** Only the variables n8n documents in its environment
variable reference (the tool carries the list; version 0.2.0 has the reference
as published on 2026-11-18, plus `NODE_ENV`), and `<NAME>_FILE` of one of them.
Any other variable is counted in `settings_not_listed` and is not named, even when its name starts with `N8N_`: a custom
variable name can identify a company or a product. A variable n8n adds after
the list was made is counted too, until the tool is updated. The count is of
distinct variables: one that is set in three roles counts once. So "not listed"
does not mean "not an n8n setting".

**When the value is written** (`state` is `value`). Only when all of these hold:

1. the tool could read it: it is not a secret (secrets are removed when the
   environment is read, before the profile is built) and does not come from a
   Kubernetes Secret;
2. n8n documents the variable as a boolean, a number, or one word of a fixed
   list. Variables that hold a host, URL, path, name, identifier, list of
   packages or nodes, or a credential are never written, and neither is the
   licence tenant id, although it is a number;
3. the value is one: `true` or `false`; ASCII digits with an optional minus
   sign and decimal part; exactly one of the documented words;
4. for a Kubernetes scope, every container of the role has the variable with
   the same value.

Otherwise the entry says `set`: the variable is there and its value is not in
the file. This is deliberately strict. A setting n8n documents as free text
never shows its value, even when the value is harmless. That is the intended
trade: a missing value can be asked for, a leaked host cannot be taken back.

`withheld` means the person who ran the tool removed the value.

A fact and the setting it is read from are separate entries, and a finding in
`check` can say the same thing again. Withholding `executions.mode` does not
withhold the `EXECUTIONS_MODE` setting or a finding about queue mode: withhold
those too.

## Check

The outcome of the health check that ran with the profile:

```json
{
  "partial": true,
  "scores": [{"area": "reliability", "score": 60, "partial": true}],
  "findings": [{"rule_id": "REL-002", "severity": "warning",
                "area": "reliability", "title": "..."}]
}
```

- `partial`: not every source was read or not every rule ran, so the scores
  cover only part of the instance.
- `scores`: one per area (`scalability`, `reliability`, `performance`,
  `maintainability`, `security`), 0 to 100, or `null` when nothing was read. A row with
  `"withheld": true` and a `null` score is an area whose score was withheld
  with one of its checks: see "Withholding".
- `findings`: most severe first. `severity` is `critical`, `warning` or
  `info`. The title is fixed text of the rule. The evidence, impact and
  recommendation of a finding are in the health report, not here: evidence
  quotes raw values.

When the whole block was withheld it is `{"withheld": true}`.

### The outcome of every rule

Added in tool version 0.6.0. `check.checks` is a list with one entry per rule,
in rule order. A file written by an earlier version has no such key and still
opens. Every rule states exactly one outcome per run:

```json
{"rule_id": "SCAL-005", "area": "scalability", "outcome": "passed",
 "basis": "read", "text": "Postgres connections in use are below 80% of max_connections.",
 "rests_on": ["database.max_connections"]}

{"rule_id": "REL-001", "area": "reliability", "outcome": "finding",
 "severity": "critical", "text": "Queue mode on SQLite", "impact": "...",
 "remedy": "...", "rests_on": ["executions.mode", "database.engine",
                               "setting:instance:EXECUTIONS_MODE"]}

{"rule_id": "REL-003", "area": "reliability", "outcome": "not_assessed",
 "text": "Redis memory in use against its limit", "reason": "source_missing",
 "needs": ["redis"], "rests_on": []}

{"rule_id": "REL-006", "area": "reliability", "outcome": "not_applicable",
 "text": "Multi-main setup in queue mode",
 "rests_on": ["executions.mode", "setting:instance:EXECUTIONS_MODE"]}

{"rule_id": "SEC-002", "area": "security", "outcome": "withheld", "text": ""}
```

| Key | In | Meaning |
|---|---|---|
| `rule_id`, `area` | every entry | The rule and its area. |
| `outcome` | every entry | `finding`, `passed`, `not_applicable`, `not_assessed`, or `withheld` when the sender left this check out. |
| `text` | every entry | For a finding its title, for a passed check the sentence that states the condition the rule checked and nothing stronger, otherwise the subject of the rule. Empty for a withheld check. |
| `severity`, `impact`, `remedy` | `finding` | The severity, what goes wrong if left as is, and the fix. |
| `basis` | `passed` | `read`: the rule read the good value. `default`: the variable is not set in an environment that was read in full, and the n8n default is the good value. |
| `reason`, `needs` | `not_assessed` | Why the rule could not decide, and the sources of which any one would let it. See below. |
| `rests_on` | all but `withheld` | The ids of the facts and settings of this file that hold what the rule read. Ids only, the ones `--withhold` takes: their values are the ones already in the file, withheld or not. A fact is listed only when the file holds a value for it. A check that does not apply lists what made it so (the execution mode, for a queue-mode check on an instance in regular mode), and a check that could not decide after reading a value (`not_stated`, `value_unreadable`, `sources_disagree`) lists what it read. Empty when the rule read nothing, or reads something the profile does not hold; the values are then in the health report. |

| `reason` | Meaning | `needs` |
|---|---|---|
| `source_missing` | None of the sources where the rule looks was given. | Any one of these would let it decide: `url`, `env`, `postgres`, `redis`, `kubernetes`, `debug_info`. |
| `source_failed` | Such a source was asked for and could not be collected. | Those sources. |
| `not_stated` | A source where the rule looks was given and read, and does not say it. | empty |
| `value_unreadable` | A source holds the value, which could not be read (a Secret, a `_FILE` variable, a redacted value, an environment read in part). | empty |
| `sources_disagree` | Two sources given in the run state different things about what the rule reads: an env file and a Kubernetes container, a probe and the debug information, a variable and the answer of an endpoint. The rule has no finding, and it does not pass or "not apply" against a source that says otherwise. `docs/rules.md` says what is compared. | empty |
| `rule_error` | The rule itself failed to run. | empty |

The ids in `needs` name what the operator gives, not the keys of `sources`:
`url` covers the health and metrics endpoints, `env` is the env file.

**Every text in `checks` is fixed.** `text`, `impact` and `remedy` are each
one of a closed list of sentences written in the code of the rules, all of
them printed in [rules.md](rules.md). None is built from a value read from the
instance, so none can hold a host, a URL, a path or a name. Which sentence was
chosen can tell a state of the instance (a title about queue mode tells the
mode), as the title of a finding always did. The evidence of a finding is
still never written.

`checks` and `findings` are built from one list and always agree: a finding
is in both, and a rule that failed to run is in neither `findings` nor the
scores, and is `not_assessed` with the reason `rule_error` in `checks`.

A reader must accept what it does not know here, so that a later version can
add to it: `checks` must be a list of objects that each have a string
`rule_id`, `area`, `outcome` and `text`, or the file is refused; any other key
is read when it has the shape above and ignored when it has not; an entry with
an `outcome` or a `reason` the reader does not know is shown as "not known to
this version", not refused.

## The call sheet

`n8n-healthcheck --show-sheet FILE` prints, from a saved profile, an outline
for a call between the person who supports an instance and the person who
runs it, as Markdown. The web page shows the same sheet when a profile is
opened, with "Copy as Markdown". It is a rendering of the file: it changes
nothing and holds nothing the file does not.

1. **Run.** Date, reason, reference, tool version, the sources and their
   state, and a line when the profile is more than 30 days old.
2. **The environment in brief.** n8n version, execution mode, main and worker
   replicas, database type, and the four usage numbers, each as measured,
   declared, unknown or withheld. "about" marks a usage number the profile
   records as an estimate.
3. **What works well.** The `passed` checks, by area.
4. **What does not.** The findings, most severe first: title, impact, and what
   the rule rests on with the values the file holds.
5. **What to improve.** The remedies of those findings, in the same order.
6. **Not assessed.** The `not_assessed` checks, grouped by what would have to
   be given, then those a source did not state, those whose value could not be
   read, those on which two sources disagree, and rules that failed.
7. **Does not apply.** One line, and a count of the checks that were withheld.
8. **Questions still open.** The question sheet above.

When `check` was withheld, parts 3 to 7 are one line saying so. For a file
written before 0.6.0, part 4 lists the findings by title and one line says
that the file does not record the rest. A withheld fact shows as withheld,
never as its value.

## Withholding

Before the file is written, any fact, any setting, any single check, or the
whole `check` block can be withheld. A withheld entry stays in the file, without its value, so a
reader can tell "removed" from "unknown". `run`, `sources`, `format`,
`format_version`, `tool_version` and `created_at` cannot be withheld.

On the command line an entry is named by its fact id (`worker.replicas`), by
`setting:<scope>:<NAME>` (`setting:main:EXECUTIONS_MODE`), by
`check:<RULE_ID>` (`check:SCAL-002`) for one check, or `check` for the whole
block.

A withheld check keeps its `rule_id` and its `area` in `checks`, with
`"outcome": "withheld"` and an empty text, and its finding, if it had one,
leaves `findings`.

The score of that check's area is withheld with it: the row stays in `scores`
as `{"area": "security", "score": null, "partial": ..., "withheld": true}`.
This happens whatever the check had stated, a finding or not. A score that was
kept would still count the finding, and one computed again without it would
differ from the first, so either would tell that the withheld check was a
finding and how severe. The scores of the other areas are written as before.
A reader shows such a row as withheld, not as "n/a": a `null` score without
`"withheld": true` still means that nothing was read. A reader that does not
know the key (tool version 0.5.0) shows "n/a".

Nothing is withheld automatically. The text of a check can say something about
a fact or a setting that was withheld: "workers run with at least 2 replicas"
bounds `worker.replicas`. So the review names, for every withheld fact and
setting, the checks in the file that rest on it (`--show-profile`, a note on
stderr when the file is saved, and the share step of the web page), and the
sender decides whether to withhold those checks as well. A check that does
not apply is named too: eight queue-mode checks marked "does not apply" tell
that the mode is regular, so withholding `executions.mode` names each of them.

Withholding applies to the profile only. The health report is a separate
document that quotes raw values as evidence; share it or not as before.

## Versioning

- Removing or renaming a field or a fact id raises `format_version`.
- Adding a fact id, or a value to one of the lists above, does not. A reader
  should ignore fact ids it does not know rather than fail, and must not
  require an id that was added after version 0.2.0 of the tool.
- `n8n-healthcheck` refuses to open a file with a higher `format_version` than
  it knows and says to update the tool, instead of showing part of it.
- A version 1 file always has the six sources and the 32 fact ids listed in
  the catalogue above, which are the ids of tool version 0.2.0. A file where
  one of those is missing is refused as invalid, so nothing can be silently
  absent. That list is frozen: ids a later tool version adds are written by
  that version but are not required of a file, so a newer tool still opens a
  file written by an older one. Version 0.3.0 added the ids under "Declared
  facts" this way, and version 0.4.0 the ids under "Facts from the debug
  information" and the source `debug_info`, which is not required of a file
  either. Version 0.5.0 added the ids under "Usage numbers from the n8n API"
  and the source `api` in the same way.
- Version 0.6.0 added `check.checks` without raising the format version: a
  file that has it opens in version 0.5.0, which ignores the key, and a file
  without it opens in 0.6.0.
- When a file is opened, `run.reference` must obey the rules it is typed
  under: one line, at most 200 characters, no control characters, no
  surrounding spaces. A file that is not valid is refused with a message that
  names the faulty entry by its position, never by text taken from the file.
