# Lab: see a report before and after

**TL;DR** — Start a deliberately misconfigured queue-mode n8n stack, run the health check and read the findings. Start the fixed stack, run it again and watch them go.

Demo only. Every password and key in this directory is a throwaway value, and every port binds to `127.0.0.1`.

## Prerequisites

- Docker with Compose v2 (`docker compose version`)
- Python 3.10+
- Free local ports 5678 (n8n), 15432 (Postgres) and 16379 (Redis)

Run every command from the repository root. Run one stack at a time, because both use the same ports.

## Bad run

```bash
docker compose -f lab/bad/docker-compose.yml up -d
```

Wait until n8n answers (about a minute on first start):

```bash
curl -fsS http://localhost:5678/healthz
```

Create some load. This imports and publishes `lab/workflow.json`, then sends 200 webhook requests:

```bash
python3 lab/load.py bad
```

Run the health check:

```bash
python3 -m n8n_healthcheck --url http://localhost:5678 --env-file lab/bad/n8n.env --postgres-dsn postgresql://n8n:lab-postgres-password@localhost:15432/n8n --redis-url redis://:lab-redis-password@localhost:16379 -o report-bad.md
```

Tear it down before starting the other stack:

```bash
docker compose -f lab/bad/docker-compose.yml down -v
```

## Good run

```bash
docker compose -f lab/good/docker-compose.yml up -d
```

```bash
curl -fsS http://localhost:5678/healthz
```

```bash
python3 lab/load.py good
```

```bash
python3 -m n8n_healthcheck --url http://localhost:5678 --env-file lab/good/n8n.env --postgres-dsn postgresql://n8n:lab-postgres-password@localhost:15432/n8n --redis-url redis://:lab-redis-password@localhost:16379 -o report-good.md
```

Compare `report-bad.md` with `report-good.md`.

## What is wrong in the bad stack

| Misconfiguration | What it causes | Fix in the good stack |
|---|---|---|
| One worker | A worker crash or restart stops every execution. No headroom for bursts. | Two workers (`deploy.replicas: 2`) |
| `EXECUTIONS_DATA_PRUNE=false` with `EXECUTIONS_DATA_SAVE_ON_SUCCESS=all` | The executions table grows without bound. Queries slow down, then the disk fills. | `EXECUTIONS_DATA_PRUNE=true`, 7 days or 10,000 executions |
| `N8N_METRICS=false` | `/metrics` returns 404. No dashboards, no alerts, no view of the queue. | `N8N_METRICS=true` plus queue metrics |
| Redis `maxmemory 64mb`, `maxmemory-policy allkeys-lru` | Under memory pressure Redis evicts queue keys. Queued executions disappear without an error. | `noeviction`, 512 MB |
| Postgres `max_connections=20` | Adding workers exhausts connections. Executions fail with "too many clients". | `max_connections=200` |
| Image tag `latest` | Each pull can change the n8n version. Main and workers can drift apart. | Pinned to `2.47.7` |

## What the good stack still gets wrong

The good stack runs a single main process. If it goes down, the editor, webhooks and triggers go down with it. The fix is multi-main, which needs a self-hosted Enterprise licence, so the lab cannot show it. Expect that finding to remain. The good stack also has no TLS and no backups.

## How the load script works

`lab/load.py` needs no owner account and no API key. It copies `lab/workflow.json` into the n8n container, runs `n8n import:workflow` and `n8n publish:workflow`, waits for the webhook to return 200, then sends the requests. Each execution waits two seconds on a worker, so jobs stay visible in the queue for a short while. Run the health check straight after the load to catch them.

Options: `python3 lab/load.py good -n 500` sends 500 requests.

If the script reports that the webhook did not go live:

1. Restart the main: `docker compose -f lab/good/docker-compose.yml restart n8n` (or `lab/bad/...`).
2. Run `python3 lab/load.py good` again.

To open the editor at http://localhost:5678 you must create the owner account by hand on first visit. The lab does not need it.

## Teardown

```bash
docker compose -f lab/bad/docker-compose.yml down -v
```

```bash
docker compose -f lab/good/docker-compose.yml down -v
```

`-v` removes the Postgres volume, so the next start is clean.
