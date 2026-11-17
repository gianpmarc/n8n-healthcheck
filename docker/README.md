# Run n8n-healthcheck with Docker

**TL;DR**

1. Install and start Docker Desktop 4.27 or later (or Docker Engine 25 or later with Compose v2). Older versions do not support the `start_interval` setting of the health check in `docker-compose.yml`.
2. Double-click `Start Health Check.command` in the repository root (macOS). On Linux: `bash "Start Health Check.command"`. On Windows: `docker compose up -d --build`, then open the URL yourself; there is no Windows launcher.
3. Your browser opens http://localhost:8080. To stop: double-click `Stop Health Check.command`, or run `docker compose down`.

If you downloaded the repository as a ZIP, macOS may refuse to open the launcher (not tested; this is our understanding of Gatekeeper). Run `bash "Start Health Check.command"` in Terminal from the repository root instead. To keep double-clicking, allow the file once: on macOS 15 and later in System Settings → Privacy & Security → Open Anyway, on older versions with right-click → Open. A `git clone` is not affected.

## What is in the image, and why

| Content | Why |
|---|---|
| Python 3.13 (official `python:3.13-slim-trixie`) | Runs the tool. No pip packages: the tool is standard library only. |
| `psql` 17 (Debian `postgresql-client`) | `--postgres-dsn` shells out to `psql`. Many machines do not have it. |
| `kubectl` v1.37.2 (official binary, SHA-256 verified at build time) | `--kube-namespace` shells out to `kubectl`. |

The image builds for `amd64` and `arm64`, and runs as a non-root user (uid 10001). Compose runs it with a read-only root filesystem, all capabilities dropped and `no-new-privileges`.

## Point it at services on your machine

Inside a container, `localhost` is the container itself. To reach something that listens on your machine, write `host.docker.internal` wherever you would write `localhost`, in URLs and in DSNs.

On macOS and Windows (Docker Desktop) this reaches anything listening on your machine, including ports bound to `127.0.0.1`. On Linux it does not: see "Linux" below.

Worked example with the lab on Docker Desktop (see `lab/README.md`). Start the bad stack and load it:

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

### Linux

Not tested on Linux; this section follows from how Docker Engine networking works.

On Linux, `host.docker.internal` is the address of the Docker bridge, not the host's loopback. A service is reachable only if it listens on that address (or on all interfaces). A service bound to `127.0.0.1` only, like every port of the lab, refuses the connection.

For a target that runs in Docker, such as the lab, attach the health check to the target's network and use the service names with their container ports. With the UI running and `lab/bad` started:

```bash
docker network connect n8n-hc-lab-bad_default "$(docker compose ps -q healthcheck)"
```

| Field | Value |
|---|---|
| n8n URL | `http://n8n:5678` |
| Postgres DSN | `postgresql://n8n:lab-postgres-password@postgres:5432/n8n` |
| Redis URL | `redis://:lab-redis-password@redis:6379` |

Before tearing the lab down, run `docker compose down` here (or `docker network disconnect` with the same arguments), otherwise the lab network cannot be removed. The good stack's network is `n8n-hc-lab-good_default`.

For a target that runs directly on the host, either make it listen on the bridge address, or run the tool natively.

## Run the CLI through the image

The image's entrypoint is the tool, so everything after the image name is passed to it. Build once with `docker compose build`; the image is named `n8n-healthcheck:local`.

```bash
docker run --rm n8n-healthcheck:local --help
```

Files must be mounted. The report goes to stdout, so redirect it on your side. The same lab example, from the repository root:

```bash
docker run --rm --add-host host.docker.internal:host-gateway -v "$PWD/lab/bad/n8n.env:/data/n8n.env:ro" n8n-healthcheck:local --url http://host.docker.internal:5678 --env-file /data/n8n.env --postgres-dsn postgresql://n8n:lab-postgres-password@host.docker.internal:15432/n8n --redis-url redis://:lab-redis-password@host.docker.internal:16379 > report-bad.md
```

On Linux, replace `--add-host host.docker.internal:host-gateway` with `--network n8n-hc-lab-bad_default` and use the service-name values from the Linux table above.

A saved snapshot works the same way: `-v "$PWD/snapshot.json:/data/s.json:ro"` and `--snapshot /data/s.json`.

The exit code is the tool's own: 0 clean, 1 warnings, 2 critical findings, 3 the check could not run.

On Linux a mounted file must be readable by uid 10001. If your env file is private (mode 600), add `--user "$(id -u):$(id -g)"`.

To run `psql` or `kubectl` directly: `docker run --rm --entrypoint psql n8n-healthcheck:local --version`.

## Kubernetes

Off by default: the container has no kubeconfig, so it cannot reach any cluster. To turn it on, uncomment the `environment` and `volumes` block at the end of `docker-compose.yml`. It mounts `~/.kube/config` read-only, and fails with a clear error if that file does not exist. Then restart with the launcher.

On Linux, `~/.kube/config` is normally readable only by you (mode 600), and the container runs as uid 10001. Also uncomment the `user:` line and set it to the output of `id -u` and `id -g`, for example `user: "1000:1000"`. Docker Desktop on macOS and Windows does not need this.

Two things do not work from inside a container:

- **Local clusters (kind, minikube, Docker Desktop Kubernetes).** Their kubeconfig points at `https://127.0.0.1:<port>`. Inside the container that address is the container, not your machine. Changing the address to `host.docker.internal` is not enough either, because the cluster's certificate is not valid for that name. For a local cluster, run the tool natively: `python3 -m n8n_healthcheck --kube-namespace ...`.
- **Login helpers.** If your kubeconfig calls a program to get a token (`aws`, `gcloud`, `gke-gcloud-auth-plugin`, `kubelogin`, `az`), that program is not in the image. Run the tool natively, or use a kubeconfig with a static token.

Remote clusters with certificates or tokens stored in the kubeconfig work.

## Security notes

- The UI is published on `127.0.0.1:8080` only. Other machines on your network cannot reach it. Do not change that to `0.0.0.0`: the UI has **no authentication**.
- The server rejects requests whose `Host` header is not `localhost:8080` or `127.0.0.1:8080`. Keep port 8080 on both sides of the mapping in `docker-compose.yml`. With a different published port the server rejects the browser's requests unless you allow that host and port. Under Compose that means replacing the whole command, for example for port 9000:

  ```yaml
      ports:
        - 127.0.0.1:9000:8080
      command: [serve, --host, 0.0.0.0, --port, "8080", --no-browser, --allow-host, "localhost:9000", --allow-host, "127.0.0.1:9000"]
  ```

  The launcher still opens port 8080; open the new address yourself.
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
