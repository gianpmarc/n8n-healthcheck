#!/usr/bin/env python3
"""Load generator for the n8n-healthcheck lab. Standard library only.

Imports lab/workflow.json into the running stack and publishes it (skipped when
the webhook is already live), then fires N webhook requests so the queue and the
executions table hold real data.

    python3 lab/load.py bad          # 200 requests against the bad stack
    python3 lab/load.py good -n 500
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAB = Path(__file__).resolve().parent
WORKFLOW = LAB / "workflow.json"
WORKFLOW_ID = json.loads(WORKFLOW.read_text())["id"]
WEBHOOK_PATH = "/webhook/healthcheck-lab"


def compose(stack, *args, **kwargs):
    cmd = ["docker", "compose", "-f", str(LAB / stack / "docker-compose.yml"), *args]
    return subprocess.run(cmd, capture_output=True, **kwargs)


def n8n_cli(stack, *args):
    return compose(stack, "exec", "-T", "n8n", "n8n", *args)


def post(url):
    """POST one request; return the HTTP status, or 0 if n8n is unreachable."""
    req = urllib.request.Request(
        url, data=b'{"source":"lab"}', headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError:
        return 0


def ready(base_url):
    """True once n8n reports the database connected and migrated."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/healthz/readiness", timeout=5) as resp:
            return resp.status == 200
    except OSError:
        return False


def fail(message, *procs):
    for proc in procs:
        message += "\n" + (proc.stdout + proc.stderr).decode(errors="replace").strip()
    sys.exit("error: " + message)


def publish(stack, webhook):
    print(f"Importing workflow {WORKFLOW_ID} into the {stack} stack ...")
    copied = compose(
        stack, "exec", "-T", "n8n", "sh", "-c", "cat > /tmp/workflow.json",
        input=WORKFLOW.read_bytes(),
    )
    if copied.returncode != 0:
        fail("cannot write into the n8n container", copied)
    imported = n8n_cli(stack, "import:workflow", "--input=/tmp/workflow.json")
    if imported.returncode != 0:
        fail("import:workflow failed", imported)

    print("Publishing it ...")
    published = n8n_cli(stack, "publish:workflow", f"--id={WORKFLOW_ID}")
    if published.returncode != 0:
        # n8n 1.x has no publish:workflow; this is its equivalent.
        fallback = n8n_cli(stack, "update:workflow", f"--id={WORKFLOW_ID}", "--active=true")
        if fallback.returncode != 0:
            fail("publish:workflow failed, and so did update:workflow", published, fallback)

    # The running instance picks up CLI publishes on a timer, not instantly.
    print("Waiting for the webhook to go live ...")
    deadline = time.time() + 90
    while post(webhook) != 200:
        if time.time() > deadline:
            fail(
                f"{webhook} did not return 200 within 90s. Restart the main with "
                f"`docker compose -f lab/{stack}/docker-compose.yml restart n8n` "
                "and run this script again."
            )
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stack", choices=["bad", "good"])
    parser.add_argument("-n", "--requests", type=int, default=200)
    parser.add_argument("--url", default="http://localhost:5678")
    args = parser.parse_args()
    webhook = args.url.rstrip("/") + WEBHOOK_PATH

    # No n8n container at all: fail now instead of waiting out the timeout.
    # "created" and "restarting" count as up, so a starting stack still waits.
    try:
        container = compose(
            args.stack, "ps", "-a", "-q", "--status", "created",
            "--status", "restarting", "--status", "running", "n8n",
        )
    except OSError as exc:
        fail(f"cannot run docker compose: {exc}")
    if container.returncode != 0:
        fail("docker compose failed. Is the Docker daemon running?", container)
    if not container.stdout.strip():
        fail(f"no n8n container is running. Is the {args.stack} stack up?", container)

    print("Waiting for n8n to be ready ...")
    deadline = time.time() + 120
    while not ready(args.url):
        if time.time() > deadline:
            fail(f"{args.url}/healthz/readiness did not return 200 within 120s. "
                 f"Is the {args.stack} stack up?")
        time.sleep(3)

    # Importing unpublishes a live workflow, so a second run would hit 404s
    # mid-swap. If the webhook already answers, leave it alone.
    if post(webhook) == 200:
        print("Workflow is already published; skipping import.")
    else:
        publish(args.stack, webhook)

    print(f"Firing {args.requests} requests at {webhook} ...")
    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(post, [webhook] * args.requests))
    ok = statuses.count(200)
    print(f"{ok}/{args.requests} accepted (HTTP 200).")

    count = compose(
        args.stack, "exec", "-T", "postgres",
        "psql", "-U", "n8n", "-d", "n8n", "-tAc", "SELECT count(*) FROM execution_entity",
    )
    if count.returncode == 0:
        print(f"Rows in execution_entity: {count.stdout.decode().strip()}")
    print("Each execution waits 2s on a worker, so the queue drains over the next minute.")
    if ok != args.requests:
        sys.exit(1)


if __name__ == "__main__":
    main()
