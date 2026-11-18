# Environment profile: design

Date: 2026-11-18. Status: draft, waiting for approval.

## Intent

Today the tool answers "what will break". The person helping a self-hosted
customer also needs "what exactly is this environment": without it, a problem
report cannot be told apart from a consequence of the setup, and upgrade advice
is a guess.

The customer runs the tool on their own infrastructure. This piece adds a second
output next to the report: an **environment profile**, one JSON file of facts
that the customer reviews, downloads and attaches to a ticket. The person who
receives it opens it with the same tool, offline, and files it in whatever
customer-records system they use. That filing happens outside this repository.

It must be practical for three people: the customer who provides it (one file,
nothing surprising in it), the account manager who reads it (facts, where each
came from, what is still unknown), and whoever files it (structured, stable).

This is piece 1 of 5. The later pieces (a questionnaire for what no probe can
see, real usage counts, comparing two profiles, an ECS source) build on the
format defined here and are out of scope.

## Goals

- A documented, versioned JSON format for the profile.
- Every fact carries its provenance: measured, unknown or withheld. `declared`
  is reserved in the format for the questionnaire (piece 2) and never produced
  here.
- The customer sees every entry before the file exists and can withhold any of
  them.
- The file can be opened later, without access to the instance, and shown
  readably (web interface and command line).
- A saved profile can be printed as readable Markdown.

## Non-goals

- The questionnaire, usage counts, profile comparison, ECS (pieces 2 to 5).
- Sending the file anywhere. The tool never uploads; the customer does.
- Any knowledge of the receiving system. No field names, endpoints or mappings
  of another product in this repository.
- Identifying the customer. The file has no customer name field.
- New collectors. Piece 1 uses only what the six existing snapshot sections hold.
- Changing the health report. It stays a separate document (see "Withholding").

## Anti-goals

It is a failure, even if everything works, when:

- a profile contains a secret, a host name, a URL, an IP address, a file path,
  an e-mail address, or a Deployment, namespace or image registry name;
- a profile looks complete when it is not: something not collected must read
  `unknown`, something removed must read `withheld`, never be silently absent;
- a default is reported as a fact when the environment was only partly readable;
- the existing report, exit codes, API fields or snapshot format change
  behaviour for someone who ignores the profile;
- a dependency or a build step is added.

## Constraints

- Python 3.10+, standard library only. Web UI stays plain HTML, CSS and JS,
  under the existing Content-Security-Policy, with no storage and no logging of
  request data.
- Read-only towards the instance. Nothing new is collected.
- The repository is public. Test data is synthetic.
- The existing rule for uncertainty applies: `<redacted>`, `<valueFrom>` and an
  incomplete Kubernetes environment (`env_complete` false) mean "cannot tell".
  Use `effective_env`, `unknown`, `n8n_containers` in `rules/_helpers.py` rather
  than re-deriving them.

## Approaches considered

1. **One JSON file** (chosen). What the customer reviewed and what gets filed
   are the same object. Not readable by eye, which the tool's own viewer and its
   Markdown rendering of a saved profile make up for.
2. **One Markdown file with an embedded data block.** Readable anywhere, but
   mail clients and ticket systems alter text, and a hand-edited line breaks the
   import without anyone noticing.
3. **Two files, report and data.** Each does one job well, but customers will
   send one of the two, or two from different days.

Chosen by the user: 1.

## Decisions made by the user

- The customer runs the tool; the profile is something they choose to send.
- The tool stays stateless. History per customer lives in the receiving system.
- Runs repeat over time (first contact, before each upgrade, during incidents),
  so each profile records when and why it was taken.
- One JSON file is the profile.
- Environment variables: facts derived from them, plus the n8n settings
  themselves with their value, except those that hold hosts, URLs or paths,
  which are recorded only as "set".

## The file

Default name `n8n-profile-YYYY-MM-DD.json`. UTF-8, indented, no NaN.

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
    "findings": [{"rule_id": "REL-002", "severity": "warning", "title": "..."}]
  }
}
```

- `run.reason`: one of `baseline`, `pre_upgrade`, `post_upgrade`, `incident`,
  `other`. Default `baseline`.
- `run.reference`: free text, at most 200 characters, single line. It is the
  only text the customer types, and the interface says not to put secrets in it.
- `sources`: one state per snapshot section. `collected` when the section is
  present; `disabled` for metrics when `/metrics` answered 404; `failed` when it
  was requested and came back empty (the `failed` list the CLI and the web API
  already compute); otherwise `not_provided`. An environment read from
  Kubernetes leaves `env` as `not_provided`: the facts say where they came from.
- `facts`: always the full catalogue, in catalogue order. `provenance` is
  `measured`, `declared` (reserved), `unknown` or `withheld`. `value` is null
  unless measured. `detail` is one short sentence when it helps, for example
  "n8n default, variable not set".
- `settings`: see "Settings" below. `state` is `value`, `set` or `withheld`.
- `check`: the outcome on that day, without evidence text (evidence quotes raw
  values and would need its own review). Findings keep id, severity, area and
  title only.

The format is described for outside readers in a new `docs/profile-format.md`.
A change that removes or renames a field or a fact id raises `format_version`.
Adding a fact id does not.

## Fact catalogue, version 1

Ids are this project's own vocabulary. One line each is enough for a reader of
the file to understand it.

| Id | From |
|---|---|
| `n8n.version` | `Snapshot.n8n_version`; else the version in the image tag when every main container agrees (`image_version`) |
| `n8n.image_official` | Kubernetes: true when the image is `n8nio/n8n` or `docker.n8n.io/n8nio/n8n`; the image name itself is never written |
| `orchestrator` | `kubernetes` when that source was collected, else unknown |
| `executions.mode` | `EXECUTIONS_MODE`, default `regular` |
| `main.replicas`, `worker.replicas`, `webhook.replicas` | Kubernetes Deployments, summed per role. A role with no container found is unknown, never 0: only Deployments are read, so a StatefulSet would be missed |
| `worker.concurrency` | Same precedence as rule SCAL in `rules/scalability.py`: `N8N_CONCURRENCY_PRODUCTION_LIMIT` on the workers unless unset or -1, else the `--concurrency` flag, else 10. Needs worker containers; with an env file only it is unknown |
| `multi_main.enabled` | `N8N_MULTI_MAIN_SETUP_ENABLED` |
| `task_runners.enabled`, `task_runners.mode` | `N8N_RUNNERS_ENABLED`, `N8N_RUNNERS_MODE` |
| `binary_data.mode` | `N8N_DEFAULT_BINARY_DATA_MODE` |
| `metrics.enabled` | metrics collected: true; `metrics_disabled`: false; else unknown |
| `pruning.enabled`, `pruning.max_age_hours` | `EXECUTIONS_DATA_PRUNE`, `EXECUTIONS_DATA_MAX_AGE` |
| `database.engine` | `DB_TYPE`, default `sqlite` |
| `database.version` | Postgres |
| `database.pool_size` | `DB_POSTGRESDB_POOL_SIZE` |
| `database.max_connections` | Postgres |
| `database.execution_count`, `database.execution_table_bytes`, `database.oldest_execution_age_days` | Postgres |
| `queue.redis_version`, `queue.maxmemory_policy`, `queue.maxmemory_bytes` | Redis |
| `queue.waiting_jobs`, `queue.active_jobs`, `queue.failed_jobs` | Redis |
| `resources.main`, `resources.worker` | Kubernetes requests and limits; value is an object `{requests, limits}`; unknown when containers of the role disagree |
| `probes.main`, `probes.worker` | Kubernetes; value `{liveness, readiness}` |

Rules for every fact:

- **Which environment.** The env file when one was given, as the rules do;
  otherwise the Kubernetes containers of the role the fact is about.
- **Disagreement is per variable.** `effective_env` gives up when main
  containers differ in anything; the profile compares the one variable a fact
  needs across the containers of the role (with `container_env` and `unknown`).
  If they disagree on it, that fact is unknown and `detail` says so.
- **Defaults.** Only three are stable across n8n versions and are reported as
  measured when the variable is provably unset (environment read and complete):
  `executions.mode` = `regular`, `database.engine` = `sqlite`,
  `multi_main.enabled` = false. For every other fact an unset variable gives
  unknown with the detail "not set, the n8n default for this version applies".
  *(My call: the other defaults changed between releases, and a wrong default
  stated as a fact is worse than a gap.)*
- **Each fact has its own value shape**, and a value that does not fit is
  unknown, not written: an enumeration for modes and policies, an integer for
  counts, a boolean, a version (`digits.digits[.digits]`, the leading match of
  the raw string), a Kubernetes quantity (`^[0-9.]+[a-zA-Z]{0,2}$`) inside the
  `resources.*` objects. No fact holds free text.

Catalogue entries that no probe can fill (hosting, cloud provider, region,
reverse proxy, TLS, SSO, database high availability, backups, upgrade process,
observability) are **not** added in piece 1. They belong to the questionnaire.
*(My call: listing them now as permanently unknown would make every profile look
mostly empty for no gain.)*

## Settings

The n8n settings themselves, from the same environment the rules use.

- **Which names**: those starting with `N8N_`, `EXECUTIONS_`, `QUEUE_`, `DB_`,
  `WEBHOOK_`, `OFFLOAD_`, `NODE_FUNCTION_`, plus `NODE_ENV` and `NODE_OPTIONS`.
  Any other variable is not listed, not even by name: `settings_not_listed`
  carries only how many there were. *(My call: custom variable names can
  identify a company or a product.)*
- **Scope**: `instance` for an env file; for Kubernetes one entry per n8n role
  (`main`, `worker`, `webhook`) that has the variable. With both sources, both
  are listed. Containers of one role that disagree on a variable give
  `state: "set"` with no value.
- **When the value is written** (`state: "value"`): only when all of these hold,
  otherwise `state: "set"`:
  - the stored value is not `<redacted>` or `<valueFrom>`;
  - the name does not contain `HOST`, `URL`, `URI`, `ENDPOINT`, `DOMAIN`,
    `ADDRESS`, `PROXY`, `PATH`, `FOLDER`, `DIR`, `FILE`, `BUCKET`, `EMAIL`,
    `USER`, `SENDER`, `NAME`, `OPTIONS`, `PREFIX`, `DATABASE`, `SCHEMA`,
    `TABLE` (the last four are free identifiers such as `acme_prod`);
  - the value has a safe shape: at most 64 characters of letters, digits, `_`,
    `-` and `,` only. No dots, slashes, colons, `@` or spaces, so no host, URL,
    path, address or e-mail can pass.

*(My call: two independent tests, by name and by shape, because either alone has
obvious holes. The cost is that a few harmless values, a cron expression or a
dotted list, show as "set". A missing value is acceptable; a leaked host is
not.)*

## Withholding

Any fact or setting can be withheld by its id (`worker.replicas`) or, for a
setting, `setting:<scope>:<NAME>`. A withheld entry stays in the file with
`provenance` or `state` set to `withheld` and no value, so the reader knows it
was removed rather than unknown. `run`, `sources`, `format*`, `tool_version` and
`created_at` cannot be withheld. `check` can be withheld as a whole (`check`
becomes `{"withheld": true}`).

Withholding covers the profile only. The health report is a different document:
its findings quote raw values as evidence, it existed before, and the customer
shares it or not as today. The share step says so in one line. For that reason
the report does **not** gain an Environment section; the readable form of the
facts is always rendered from the profile file, after withholding. *(My call,
and a change from what was first discussed: a section in the report would show
what the customer had just withheld.)*

## Where the code goes

- A new module `n8n_healthcheck/profile.py`: builds the profile from a
  `Snapshot`, the findings and the failed sections; applies withholding; loads
  and validates a file; renders one as Markdown. It reads, never collects.
- `report.py` is not changed.
- Version becomes `0.2.0`.

### Command line

```
--save-profile FILE     write the environment profile as JSON
--reason REASON         baseline | pre_upgrade | post_upgrade | incident | other
--reference TEXT        free text stored in the profile
--withhold ID           repeatable; an id that does not exist is a usage error
--show-profile FILE     print a saved profile as Markdown, then exit 0
```

`--save-profile` cannot be combined with `--snapshot`: a saved snapshot does
not record which sources failed or when it was taken. `--reason`, `--reference` and
`--withhold` without `--save-profile` are usage errors. `--show-profile` takes
no other flag except `-o`; an unreadable or invalid file is exit code 3. Exit
codes of a normal run do not change.

### Web interface

- `POST /api/run` returns one more field, `profile`, built with no reason,
  no reference and nothing withheld. Existing fields are untouched.
- After a run, a new step under the report: **"Share the environment profile"**.
  A table of facts and a table of settings, each row with what will be written
  and a control to withhold it; a reason selector; a reference field; a line
  saying what the file never contains and that nothing is sent anywhere; a
  Download button. Reason, reference and withholding are applied in the browser
  to the object the server returned, then saved as a file. *(My call: the server
  stores nothing, so sending the profile back just to edit three fields would
  add an endpoint for no benefit. The browser never computes a fact or decides
  what is safe; it only blanks entries.)*
- On the start page, **"Open a profile"**: pick a file, see it readably (run
  details, facts grouped by the part before the first dot, settings, check
  outcome). The file is read in the browser and not uploaded. A file with a
  different `format`, a higher `format_version` or a broken structure gets a
  clear message instead of a partial view.
- Every state is designed: no measured facts, everything withheld, long
  reference, phone width, keyboard only.

## Tests

- **Zero leak.** A snapshot seeded with marker strings in every place a secret,
  host, URL, path, IP, e-mail, namespace, Deployment or registry name can appear
  (env values, Kubernetes env, image, command, names). The serialised profile
  and its Markdown rendering must contain none of them. This test is
  the point of the piece and must not be weakened later.
- Every fact: measured, default applied, unknown because the source is missing,
  unknown because the environment is incomplete, unknown because roles disagree.
- Safe-shape and name tests, with the awkward cases (a host without dots is
  still blocked by name; a URL in an innocently named variable is blocked by
  shape).
- Withholding: fact, setting, check, unknown id.
- Round trip: build, save, load, render. Invalid and newer-version files.
- Catalogue: every id emitted is documented in `docs/profile-format.md`.
- Existing 200 tests pass unchanged.
- Web: the share step and the viewer checked in a real browser.

## Left to the builder

- Internal structure of `profile.py` and how the catalogue is declared.
- Exact wording and layout of the share step and the viewer, within the existing
  visual language.
- Saving the file in the browser: reuse the Blob link the page already uses for
  the report download.
- README wording for the new flags and the share step.
