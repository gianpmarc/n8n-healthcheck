# n8n-healthcheck

Point it at a self-hosted n8n instance. It reads health endpoints, metrics, config, Postgres, Redis and Kubernetes, and returns a report of what will break at scale and how to fix it. Read-only. No dependencies.

> Status: working, tested end to end against n8n 2.47 with Postgres 16, Redis 7 and Kubernetes. Not an official n8n project.

## Why

Deploying n8n is well documented. Knowing whether an existing deployment is healthy is not. This tool answers one question: *is this instance ready for production load, and if not, what do I change first?*

## What you get

A Markdown report with:

- a one-paragraph summary for non-technical readers
- a score per area: scalability, reliability, performance, maintainability, security
- for each finding: the evidence, the impact, the fix

## Quick start: web interface

No command line needed. With Docker running, double-click `Start Health Check.command` (macOS), or run:

```bash
docker compose up -d --build
```

Then open http://localhost:8080, fill in what you have, and press Run. The image includes `psql` and `kubectl`, so there is nothing else to install. Details, Linux notes and how to reach services on your machine: [docker/README.md](docker/README.md).

Without Docker:

```bash
python3 -m n8n_healthcheck serve
```

The interface listens on `localhost` only, has no login, and stores nothing. After a run it also offers the [environment profile](#environment-profile) to review and download, and the start page can open a saved one.

## Usage: command line

```bash
python3 -m n8n_healthcheck --url https://n8n.example.com --env-file n8n.env -o report.md
```

Every source is optional. A source you do not provide is reported as "not assessed", never guessed.

| Flag | Source |
|---|---|
| `--url` | `/healthz`, `/healthz/readiness`, `/metrics` |
| `--env-file` | n8n environment variables |
| `--postgres-dsn` | execution table size, pruning, connections |
| `--redis-url` | queue depth, memory policy |
| `--kube-namespace` | replicas, resources, probes, container environment |
| `--debug-info` | the "Debug information" text of n8n: version, mode, database, pruning, instances |
| `--snapshot` | a previously saved snapshot (offline run) |

### Kubernetes

`--kube-namespace` uses your current `kubectl` context (or `--kube-context`) and needs permission to `get` Deployments and ConfigMaps in that namespace. Without `--env-file`, the n8n settings are read from the main n8n Deployment, including values that come from ConfigMaps. Secrets are never read: a setting that comes from a Secret is reported as "set, value unknown" and no conclusion is drawn from it.

### Debug information

Every n8n instance can produce a short text under Help > About n8n > Debug information > Copy. It is the one thing anyone can hand over in seconds, without installing or running anything, so the tool takes it as a source. Save it in a file, or pipe it in:

```bash
python3 -m n8n_healthcheck --debug-info debug-info.txt
pbpaste | python3 -m n8n_healthcheck --debug-info -
```

It can be the only source: the rules that can read it run, and the scores are marked partial. It also combines with the others. A probe wins, value by value: the text is used only for what the other sources of the run do not state, and the evidence of a finding says which values came from it and when it was generated. The report's summary says that it was read, warns when it is more than 30 days old or carries no date, and says so when the text has no `cluster` section (older n8n versions), because replicas and version alignment cannot be told without it.

The tool reads known values, numbers and versions from the text and discards the rest. The instance id, the host ids and the browser's user agent in it are never stored or shown, in the report, a saved snapshot or the profile. A text that went through a ticket or a chat (quote marks, changed bullets, a code fence, lost `#`) still reads. A line that a mail client broke in two is joined again, and a UTF-16 file (what Windows PowerShell writes with `>`) is read. A file that is larger than 64 KB, is not a debug information, or holds more than one of them stops the run with exit code 3: give a single text, the most recent. Texts of n8n 1.x are read too.

## Environment profile

The report says what will break. Whoever helps you with the instance also needs to know what the environment is. For that the tool can write a second output: one JSON file of facts (n8n version, execution mode, replicas, database and queue figures, the n8n settings that are set), each marked as measured, unknown or withheld.

```bash
python3 -m n8n_healthcheck --url https://n8n.example.com --kube-namespace n8n \
  --save-profile n8n-profile-2026-11-18.json --reason pre_upgrade --reference "upgrade to 2.50"
```

| Flag | Meaning |
|---|---|
| `--save-profile FILE` | write the profile as JSON, next to the usual report |
| `--reason REASON` | `baseline` (default), `pre_upgrade`, `post_upgrade`, `incident` or `other` |
| `--reference TEXT` | one line of free text stored in the profile, at most 200 characters; do not put secrets in it |
| `--withhold ID` | leave an entry out; repeatable. A fact id (`worker.replicas`), `setting:<scope>:<NAME>`, or `check` |
| `--show-profile FILE` | print a saved profile as Markdown and exit; works offline, takes only `-o` |

Read the file before you share it: `--show-profile` prints exactly what is in it. A withheld entry stays in the file marked `withheld`, so the reader knows it was removed rather than unknown.

The profile never contains a secret, a host name, a URL, an IP address, a file path, an e-mail address, or a Deployment, namespace or registry name. A setting's value is written only when n8n documents the variable as a boolean, a number or one word of a fixed list; every other documented variable is listed as "set" without its value, and variables n8n does not document are counted, not named. Anything the tool cannot tell for certain is `unknown`, never guessed. The tool does not send the file anywhere.

Withholding a fact does not remove the setting it was read from, or a finding that mentions it: `--withhold executions.mode` leaves the `EXECUTIONS_MODE` setting and any finding about queue mode in the file. Withhold those too (`setting:instance:EXECUTIONS_MODE`, `check`) if you want the datum gone.

Withholding applies to the profile only. The report is a separate document and quotes raw values as evidence.

### In the web interface

After a run, the page shows a step under the report, "Share the environment profile". It lists every fact and every setting exactly as it will be written, each with a box to withhold it, plus one box for the check outcome, the reason and the reference. "Download profile" saves `n8n-profile-YYYY-MM-DD.json` on your computer; the page sends it nowhere. The same rule as on the command line applies: each row is its own entry, so withhold the fact, the setting and the check outcome separately if you want a datum gone.

Above the tables the step asks "A few questions": the [profile questions](#profile-questions) the tool could not answer itself, as radio buttons and checkboxes. Every one is optional and starts on "No answer". An answer shows in the facts table as `declared` and can be withheld there like any fact; a fact the tool measured is not asked about. The downloaded file is the one `--answer` and `--withhold` write for the same choices.

To read a profile someone sent you, use "Open a profile" on the start page. The file is read in the browser, not uploaded, and shown with its run details, sources, facts, settings and check outcome: the same content as `--show-profile`. Under them, "Questions still open" lists what the profile leaves unknown, and "Copy as Markdown" copies that sheet: the same text as `--show-questions`. A file that is not a profile, was written by a newer version, or is incomplete is refused with a message; nothing of it is shown.

`--save-profile` needs a live run: it cannot be combined with `--snapshot`. The exit code is the one of the health check, with or without a profile. The file format is described in [docs/profile-format.md](docs/profile-format.md).

## Profile questions

Some of what matters cannot be read by any probe: where n8n runs, what sits in front of it, how the database is backed up. The tool has a short list of closed questions for that, 15 at most. Every one is optional, and an answer is a value from a fixed list, never text you type.

```bash
python3 -m n8n_healthcheck --url https://n8n.example.com --kube-namespace n8n \
  --save-profile n8n-profile-2026-11-19.json \
  --answer hosting.model=public_cloud --answer database.backups=point_in_time \
  --answer observability.tools=prometheus,grafana
```

| Flag | Meaning |
|---|---|
| `--list-questions` | print the questions with the values each one takes, and exit |
| `--answer ID=VALUE` | answer one question; repeatable, needs `--save-profile`. Several values as `ID=a,b`, `yes` or `no` for a yes or no question |
| `--show-questions FILE` | print what a saved profile leaves open, as Markdown, and exit; works offline, takes only `-o` |

An answer is written in the profile as `declared`, so the reader can tell it from what the tool measured. What was measured wins: when the tool read the fact itself (it can tell Kubernetes, for example), your answer for it is dropped and a line on stderr says so. A question you do not answer stays `unknown`. A declared fact can be withheld like any other.

`--show-questions` turns a profile into a sheet for the next call, in three parts: what the tool could measure if it were given the source, the questions above that are still unanswered, and a fixed list of questions that are only ever asked in conversation (how users sign in, how upgrades are done, who is on call). The tool never stores answers to that last group. Withheld facts are listed in one line at the end: they were left out on purpose.

## Try it

`lab/` contains a deliberately misconfigured queue-mode n8n stack and the fixed version, so you can see a report before and after.

## Development

```bash
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only.
