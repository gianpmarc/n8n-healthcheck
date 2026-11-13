# n8n-healthcheck

**TL;DR** — Point it at a self-hosted n8n instance. It reads health endpoints, metrics, config, Postgres, Redis and Kubernetes, and returns a report of what will break at scale and how to fix it. Read-only. No dependencies.

> Status: early development. Not an official n8n project.

## Why

Deploying n8n is well documented. Knowing whether an existing deployment is healthy is not. This tool answers one question: *is this instance ready for production load, and if not, what do I change first?*

## What you get

A Markdown report with:

- a one-paragraph summary for non-technical readers
- a score per area: scalability, reliability, performance, maintainability, security
- for each finding: the evidence, the impact, the fix

## Usage

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
| `--kube-namespace` | replicas, resources, probes |
| `--snapshot` | a previously saved snapshot (offline run) |

## Try it

`lab/` contains a deliberately misconfigured queue-mode n8n stack and the fixed version, so you can see a report before and after.

## Development

```bash
python3 -m unittest discover -s tests
```

Python 3.10+, standard library only.
