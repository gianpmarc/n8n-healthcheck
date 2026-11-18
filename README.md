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
| `--snapshot` | a previously saved snapshot (offline run) |

### Kubernetes

`--kube-namespace` uses your current `kubectl` context (or `--kube-context`) and needs permission to `get` Deployments and ConfigMaps in that namespace. Without `--env-file`, the n8n settings are read from the main n8n Deployment, including values that come from ConfigMaps. Secrets are never read: a setting that comes from a Secret is reported as "set, value unknown" and no conclusion is drawn from it.

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

To read a profile someone sent you, use "Open a profile" on the start page. The file is read in the browser, not uploaded, and shown with its run details, sources, facts, settings and check outcome: the same content as `--show-profile`. A file that is not a profile, was written by a newer version, or is incomplete is refused with a message; nothing of it is shown.

`--save-profile` needs a live run: it cannot be combined with `--snapshot`. The exit code is the one of the health check, with or without a profile. The file format is described in [docs/profile-format.md](docs/profile-format.md).

## Try it

`lab/` contains a deliberately misconfigured queue-mode n8n stack and the fixed version, so you can see a report before and after.

## Development

```bash
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only.
