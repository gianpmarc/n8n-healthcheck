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
- a setting's value is written only when both its name and its shape pass a
  test (see "Settings");
- variables that are not n8n settings are counted, not named.

The only text a person types is `run.reference`.

## Example

```json
{
  "format": "n8n-healthcheck-profile",
  "format_version": 1,
  "tool_version": "0.2.0",
  "created_at": "2026-11-18T10:22:00Z",
  "run": {"reason": "pre_upgrade", "reference": "upgrade to 2.50, change 1234"},
  "sources": {"health": "collected", "metrics": "disabled", "env": "not_provided",
              "postgres": "collected", "redis": "failed", "kubernetes": "collected"},
  "facts": [
    {"id": "executions.mode", "value": "queue", "provenance": "measured",
     "source": "kubernetes", "detail": ""},
    {"id": "database.version", "value": null, "provenance": "unknown",
     "source": null, "detail": "Postgres was not provided"},
    {"id": "worker.replicas", "value": null, "provenance": "withheld",
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

(The example shows three facts. A real file always has the full catalogue.)

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
| `settings_not_listed` | How many other variables were set and are not listed, not even by name. |
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
| `source` | Which source the value came from (a key of `sources`); `null` when there is no value. |
| `detail` | One short fixed sentence when it helps, for example why a fact is unknown. May be empty. Never quotes what was observed. |

Provenance:

- `measured`: the tool read it from the environment.
- `unknown`: the tool could not tell for certain. `detail` says why. Unknown
  never means "no" or "zero".
- `withheld`: the person who ran the tool chose to leave it out. It was not
  necessarily known.
- `declared`: reserved for answers given by a person. Version 0.2.0 of the tool
  never writes it, but a reader should accept it.

A fact is `unknown` rather than guessed when: its source was not read; the
variable it depends on is set but its value could not be read (it comes from a
Kubernetes Secret, or it was redacted); part of the container environment could
not be read, so "not set" cannot be proven; the containers of the role disagree
on it; or the value does not have the shape the fact allows.

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
(`sqlite`) and `multi_main.enabled` (`false`). For every other fact, a variable
that is not set gives `unknown` with the detail "not set, the n8n default for
this version applies": look the default up for the n8n version in `n8n.version`.

### Catalogue, version 1

| Id | Value | From |
|---|---|---|
| `n8n.version` | version | The version n8n reports on its metrics endpoint; otherwise the image tag, when every main container has the same one. |
| `n8n.image_official` | boolean | Kubernetes: `true` when every n8n container runs the image published by n8n, pulled from its usual location; `false` when none does (a custom build, or a copy in another registry). The image name is never written. |
| `orchestrator` | `kubernetes` | `kubernetes` when Kubernetes was read. Otherwise unknown: the tool cannot tell Docker from a bare process. |
| `executions.mode` | `regular`, `queue` | `EXECUTIONS_MODE`. |
| `main.replicas` | integer | Kubernetes: replicas of the Deployments that run the main n8n process, summed. |
| `worker.replicas` | integer | Same, for the worker process. |
| `webhook.replicas` | integer | Same, for the webhook process. |
| `worker.concurrency` | integer | Executions one worker runs at a time: `N8N_CONCURRENCY_PRODUCTION_LIMIT` unless it is not set or `-1`, otherwise the worker's `--concurrency` flag, otherwise 10. Needs the worker containers from Kubernetes. |
| `multi_main.enabled` | boolean | `N8N_MULTI_MAIN_SETUP_ENABLED`. |
| `task_runners.enabled` | boolean | `N8N_RUNNERS_ENABLED`. |
| `task_runners.mode` | `internal`, `external` | `N8N_RUNNERS_MODE`. |
| `binary_data.mode` | `default`, `filesystem`, `s3`, `database` | `N8N_DEFAULT_BINARY_DATA_MODE`. |
| `metrics.enabled` | boolean | `true` when the metrics endpoint answered, `false` when it answered "not found". |
| `pruning.enabled` | boolean | `EXECUTIONS_DATA_PRUNE`. |
| `pruning.max_age_hours` | integer | `EXECUTIONS_DATA_MAX_AGE`. |
| `database.engine` | `sqlite`, `postgresdb`, `mysqldb`, `mariadb` | `DB_TYPE`. |
| `database.version` | version | Postgres server version. |
| `database.pool_size` | integer | `DB_POSTGRESDB_POOL_SIZE`. |
| `database.max_connections` | integer | Postgres `max_connections`. |
| `database.execution_count` | integer | Rows in n8n's execution table. An estimate above 100,000 rows. |
| `database.execution_table_bytes` | integer | Size on disk of the execution tables, in bytes. |
| `database.oldest_execution_age_days` | number | Age of the oldest stored execution, in days. |
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
  what the software reports (`16.4` for "16.4 (Debian ...)").
- **integer**, **number**: never negative.
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

## Settings

The n8n settings that were set, one entry each:

| Field | Meaning |
|---|---|
| `scope` | `instance` for an environment file; `main`, `worker` or `webhook` for the Kubernetes containers of that role. When both were read, both are listed. |
| `name` | The variable name. |
| `state` | `value`, `set` or `withheld`. |
| `value` | The value as a string when `state` is `value`; otherwise `null`. |

**Which names are listed.** Names made of capital letters, digits and `_` that
start with `N8N_`, `EXECUTIONS_`, `QUEUE_`, `DB_`, `WEBHOOK_`, `OFFLOAD_` or
`NODE_FUNCTION_`, plus `NODE_ENV` and `NODE_OPTIONS`. Any other variable is
counted in `settings_not_listed` (once per scope it appears in) and is not
named, because a custom variable name can identify a company or a product.

**When the value is written** (`state` is `value`). Only when all of these hold:

1. the tool could read it: it is not a secret (secrets are removed when the
   environment is read, before the profile is built) and does not come from a
   Kubernetes Secret;
2. the name does not contain `HOST`, `URL`, `URI`, `ENDPOINT`, `DOMAIN`,
   `ADDRESS`, `PROXY`, `PATH`, `FOLDER`, `DIR`, `FILE`, `BUCKET`, `EMAIL`,
   `USER`, `SENDER`, `NAME`, `OPTIONS`, `PREFIX`, `DATABASE`, `SCHEMA` or
   `TABLE`;
3. the value is at most 64 characters of letters, digits, `_`, `-` and `,`,
   and nothing else: no dot, slash, colon, `@` or space;
4. for a Kubernetes scope, every container of the role has the variable with
   the same value.

Otherwise the entry says `set`: the variable is there and its value is not in
the file. The two tests, by name and by shape, are deliberately strict. Some
harmless values (a cron expression, a dotted list, a version number) show as
`set`. That is the intended trade: a missing value can be asked for, a leaked
host cannot be taken back.

`withheld` means the person who ran the tool removed the value.

A fact and the setting it is read from are separate entries. Withholding
`executions.mode` does not withhold the `EXECUTIONS_MODE` setting.

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
  should ignore fact ids it does not know rather than fail.
- `n8n-healthcheck` refuses to open a file with a higher `format_version` than
  it knows and says to update the tool, instead of showing part of it.
