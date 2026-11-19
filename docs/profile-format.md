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

(The example shows four facts. A real file always has the full catalogue.)

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

## Facts

Every fact has the same five fields:

| Field | Meaning |
|---|---|
| `id` | The fact, from the catalogue below. |
| `provenance` | `measured`, `unknown`, `withheld`, or `declared` (see below). |
| `value` | The value when `provenance` is `measured` or `declared`; otherwise `null`. |
| `source` | Which source a measured value came from (a key of `sources`); `null` for every other provenance. |
| `detail` | One short fixed sentence when it helps, for example why a fact is unknown. May be empty. Never quotes what was observed. |

Provenance:

- `measured`: the tool read it from the environment.
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
| `binary_data.mode` | `default`, `filesystem`, `s3`, `azure`, `database` | `N8N_DEFAULT_BINARY_DATA_MODE`. |
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
  `maintainability`, `security`), 0 to 100, or `null` when nothing was read.
- `findings`: most severe first. `severity` is `critical`, `warning` or
  `info`. The title is fixed text of the rule. The evidence, impact and
  recommendation of a finding are in the health report, not here: evidence
  quotes raw values.

When the whole block was withheld it is `{"withheld": true}`.

## Withholding

Before the file is written, any fact, any setting, or the whole `check` block
can be withheld. A withheld entry stays in the file, without its value, so a
reader can tell "removed" from "unknown". `run`, `sources`, `format`,
`format_version`, `tool_version` and `created_at` cannot be withheld.

On the command line an entry is named by its fact id (`worker.replicas`), by
`setting:<scope>:<NAME>` (`setting:main:EXECUTIONS_MODE`), or `check`.

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
  facts" this way.
- When a file is opened, `run.reference` must obey the rules it is typed
  under: one line, at most 200 characters, no control characters, no
  surrounding spaces. A file that is not valid is refused with a message that
  names the faulty entry by its position, never by text taken from the file.
