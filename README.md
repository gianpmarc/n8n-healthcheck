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

Then open http://localhost:8080, fill in what you have, and press Run. The first field of the form takes the [debug information](#debug-information) of n8n (Help > About n8n > Debug information > Copy), pasted as it is; it can be the only field you fill. The image includes `psql` and `kubectl`, so there is nothing else to install. Details, Linux notes and how to reach services on your machine: [docker/README.md](docker/README.md).

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
| `--api-key-file` | with `--url`: [usage numbers](#usage-numbers) through n8n's public API |
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

In the web interface, paste the text in the "Debug information" field, the first of the form. The server reads it in memory and keeps nothing of it; a text it refuses is reported next to the field, and nothing is collected.

The tool reads known values, numbers and versions from the text and discards the rest. The instance id, the host ids and the browser's user agent in it are never stored or shown, in the report, a saved snapshot or the profile. A text that went through a ticket or a chat (quote marks, changed bullets, a code fence, lost `#`) still reads. A line that a mail client broke in two is joined again, and a UTF-16 file (what Windows PowerShell writes with `>`) is read. A file that is larger than 64 KB, is not a debug information, or holds more than one of them stops the run with exit code 3: give a single text, the most recent. Texts of n8n 1.x are read too.

### Usage numbers

The sources above say how an instance is built, not how much it is used. Give an n8n API key next to the URL and the tool reads four numbers through n8n's public API: executions per day and the failure rate over the last 7 days, the active workflows and the users.

```bash
python3 -m n8n_healthcheck --url https://n8n.example.com --api-key-file n8n-api-key.txt
```

The key is read from a file (or from standard input with `-`), never from a flag, so that it does not stay in the shell history. In the web interface it is the "n8n API key" field under the URL. It is sent only to the instance of that URL, in `GET` requests that never go through a proxy (the proxy settings of the machine are ignored for them), and is never written to the report, a snapshot, the profile or a log. The calls are `GET /api/v1/insights/summary` (or, where n8n does not have it, `GET /api/v1/executions`), `GET /api/v1/workflows?active=true`, `GET /api/v1/users` and `GET /api/v1/users/<id>`; of the answers only counts are kept. Where n8n lets you choose the scopes of a key, these are enough: `insights:read`, `execution:list`, `workflow:list`, `user:list`, `user:read`. Create a key for the run and delete it in n8n afterwards.

Use the key of the owner or of an admin. Any other user's key sees only that user's workflows and executions; the tool notices (the last call is refused) and marks those numbers as partial. The numbers appear in a "Usage" section of the report and as `usage.*` facts in the [environment profile](#environment-profile). They describe the instance: no finding and no score rests on them. Without the Insights summary (n8n before 2.17.0, or a key that may not read it) the executions are counted from the executions list, which holds only what n8n saved and has not pruned: the report then says "about" and why. A number that cannot be read is listed as not read with its reason, and the others are still given.

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
| `--withhold ID` | leave an entry out; repeatable. A fact id (`worker.replicas`), `setting:<scope>:<NAME>`, `check:<RULE_ID>` for one check, or `check` for the whole outcome |
| `--show-profile FILE` | print a saved profile as Markdown and exit; works offline, takes only `-o` |
| `--show-sheet FILE` | print the [call sheet](#call-sheet) of a saved profile as Markdown and exit; works offline, takes only `-o` |

Read the file before you share it: `--show-profile` prints exactly what is in it. A withheld entry stays in the file marked `withheld`, so the reader knows it was removed rather than unknown.

The profile never contains a secret, a host name, a URL, an IP address, a file path, an e-mail address, or a Deployment, namespace or registry name. A setting's value is written only when n8n documents the variable as a boolean, a number or one word of a fixed list; every other documented variable is listed as "set" without its value, and variables n8n does not document are counted, not named. Anything the tool cannot tell for certain is `unknown`, never guessed. The tool does not send the file anywhere.

From version 0.6.0 the profile also holds the outcome of every rule: a finding with its impact and its remedy, a check that passed, one that does not apply, or one that could not be assessed and why. Every such text is a fixed sentence of the rule, never a value of your instance; [docs/rules.md](docs/rules.md) lists what each rule reads and every sentence it can write. The evidence of a finding, which quotes what was read, stays in the report.

Withholding a fact does not remove the setting it was read from, or a check that rests on it: `--withhold executions.mode` leaves the `EXECUTIONS_MODE` setting and any check about queue mode in the file. Nothing is withheld for you: when the file is saved, a note on stderr names the checks that rest on a withheld entry, and `--show-profile` lists them, the checks that "do not apply" because of it included. Withhold those too (`setting:instance:EXECUTIONS_MODE`, `check:REL-001`, or `check` for the whole outcome) if you want the datum gone. A withheld check stays in the file with its rule id only, and the score of its area is withheld with it, whatever the check said: a score left in the file would tell whether the check was a finding.

Withholding applies to the profile only. The report is a separate document and quotes raw values as evidence.

### In the web interface

After a run, the page shows a step under the report, "Share the environment profile". It lists every fact and every setting exactly as it will be written, each with a box to withhold it, plus one box for the check outcome and one for each check, the reason and the reference. "Download profile" saves `n8n-profile-YYYY-MM-DD.json` on your computer; the page sends it nowhere. The same rule as on the command line applies: each row is its own entry, so withhold the fact, the setting and the check separately if you want a datum gone. A withheld fact or setting names the checks that still rest on it.

Above the tables the step asks "A few questions": the [profile questions](#profile-questions) the tool could not answer itself, as radio buttons and checkboxes. Every one is optional and starts on "No answer". An answer shows in the facts table as `declared` and can be withheld there like any fact; a fact the tool measured is not asked about. The downloaded file is the one `--answer` and `--withhold` write for the same choices.

To read a profile someone sent you, use "Open a profile" on the start page. The file is read in the browser, not uploaded. It opens on the [call sheet](#call-sheet), with "Copy as Markdown" (the same text as `--show-sheet`), followed by its run details, sources, facts, settings and check outcome: the same content as `--show-profile`. Under them, "Questions still open" lists what the profile leaves unknown, and "Copy as Markdown" copies that sheet: the same text as `--show-questions`. A file that is not a profile, was written by a newer version, or is incomplete is refused with a message; nothing of it is shown.

### Call sheet

The person who supports an instance often receives only the profile. `--show-sheet FILE` turns it into an outline for the call with whoever runs the instance:

1. the run and the environment in brief (version, mode, replicas, database, usage numbers);
2. **what works well**: the checks that read what they need and found it good ("by n8n's default" when the variable is simply not set). Each sentence states the condition the rule checked and nothing stronger, and a check never passes when two sources given in the same run disagree on what it reads: it is then listed as not assessed;
3. **what does not**: the findings, most severe first, with their impact and the values of the profile they rest on;
4. **what to improve**: the remedy of each finding;
5. **not assessed**: the checks that could not decide, grouped by what would have to be provided ("With Postgres: ..."), and those that do not apply;
6. the questions still open.

The sheet is made from the file alone and changes nothing. A profile written before 0.6.0 opens too: its sheet lists the findings by title and says that the rest was not recorded. A 0.6.0 profile opens in 0.5.0, which ignores what it does not know.

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
