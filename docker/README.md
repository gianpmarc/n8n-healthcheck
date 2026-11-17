# Run n8n-healthcheck with Docker

**TL;DR**

1. Install and start Docker Desktop (or Docker Engine with Compose v2).
2. Double-click `Start Health Check.command` in the repository root (macOS). On Linux: `bash "Start Health Check.command"`. On Windows: `docker compose up -d --build`, then open the URL yourself; there is no Windows launcher.
3. Your browser opens http://localhost:8080. To stop: double-click `Stop Health Check.command`, or run `docker compose down`.

## What is in the image, and why

| Content | Why |
|---|---|
| Python 3.13 (official `python:3.13-slim-trixie`) | Runs the tool. No pip packages: the tool is standard library only. |
| `psql` 17 (Debian `postgresql-client`) | `--postgres-dsn` shells out to `psql`. Many machines do not have it. |
| `kubectl` v1.37.2 (official binary, SHA-256 verified at build time) | `--kube-namespace` shells out to `kubectl`. |

The image builds for `amd64` and `arm64`, and runs as a non-root user (uid 10001). Compose runs it with a read-only root filesystem, all capabilities dropped and `no-new-privileges`.

## Point it at services on your machine

Inside a container, `localhost` is the container itself. To reach something that listens on your machine, write `host.docker.internal` wherever you would write `localhost`, in URLs and in DSNs. This works on macOS, Windows and Linux (the Compose file adds the name on Linux).

Worked example with the lab (see `lab/README.md`). Start the bad stack and load it:

```bash
docker compose -f lab/bad/docker-compose.yml up -d
```

```bash
python3 lab/load.py bad
```

Then use these values in the UI:

| Field | Value |
|---|---|
| n8n URL | `http://host.docker.internal:5678` |
| Postgres DSN | `postgresql://n8n:lab-postgres-password@host.docker.internal:15432/n8n` |
| Redis URL | `redis://:lab-redis-password@host.docker.internal:16379` |

Remote services (a hostname or an IP address that is not your machine) need no change.

## Run the CLI through the image

The image's entrypoint is the tool, so everything after the image name is passed to it. Build once with `docker compose build`; the image is named `n8n-healthcheck:local`.

```bash
docker run --rm n8n-healthcheck:local --help
```

Files must be mounted. The report goes to stdout, so redirect it on your side. The same lab example, from the repository root:

```bash
docker run --rm --add-host host.docker.internal:host-gateway -v "$PWD/lab/bad/n8n.env:/data/n8n.env:ro" n8n-healthcheck:local --url http://host.docker.internal:5678 --env-file /data/n8n.env --postgres-dsn postgresql://n8n:lab-postgres-password@host.docker.internal:15432/n8n --redis-url redis://:lab-redis-password@host.docker.internal:16379 > report-bad.md
```

A saved snapshot works the same way: `-v "$PWD/snapshot.json:/data/s.json:ro"` and `--snapshot /data/s.json`.

The exit code is the tool's own: 0 clean, 1 warnings, 2 critical findings, 3 the check could not run.

On Linux a mounted file must be readable by uid 10001. If your env file is private (mode 600), add `--user "$(id -u):$(id -g)"`.

To run `psql` or `kubectl` directly: `docker run --rm --entrypoint psql n8n-healthcheck:local --version`.

## Kubernetes

Off by default: the container has no kubeconfig, so it cannot reach any cluster. To turn it on, uncomment the `environment` and `volumes` block at the end of `docker-compose.yml`. It mounts `~/.kube/config` read-only. Then restart with the launcher.

Two things do not work from inside a container:

- **Local clusters (kind, minikube, Docker Desktop Kubernetes).** Their kubeconfig points at `https://127.0.0.1:<port>`. Inside the container that address is the container, not your machine. Changing the address to `host.docker.internal` is not enough either, because the cluster's certificate is not valid for that name. For a local cluster, run the tool natively: `python3 -m n8n_healthcheck --kube-namespace ...`.
- **Login helpers.** If your kubeconfig calls a program to get a token (`aws`, `gcloud`, `gke-gcloud-auth-plugin`, `kubelogin`, `az`), that program is not in the image. Run the tool natively, or use a kubeconfig with a static token.

Remote clusters with certificates or tokens stored in the kubeconfig work.

## Security notes

- The UI is published on `127.0.0.1:8080` only. Other machines on your network cannot reach it. Do not change that to `0.0.0.0`: the UI has **no authentication**.
- The server rejects requests whose `Host` header is not `localhost:8080` or `127.0.0.1:8080`. Keep port 8080 on both sides of the mapping in `docker-compose.yml`: with a different published port the server rejects the browser's requests unless you allow that host with `--allow-host`.
- Credentials you type (DSN, Redis URL) are used for that check and are not stored. The container has a read-only filesystem and no volume; nothing survives `docker compose down`.
- The tool only reads from your n8n, Postgres, Redis and Kubernetes.

## Stop and remove

```bash
docker compose down
```

To also delete the image:

```bash
docker compose down --rmi local
```
