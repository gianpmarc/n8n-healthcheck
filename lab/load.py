#!/usr/bin/env python3
"""Load generator for the n8n-healthcheck lab. Standard library only.

Imports lab/workflow.json into the running stack, publishes it, then fires N
webhook requests so the queue and the executions table hold real data.

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


def fail(message, proc=None):
    if proc is not None:
        message += "\n" + (proc.stdout + proc.stderr).decode(errors="replace").strip()
    sys.exit("error: " + message)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stack", choices=["bad", "good"])
    parser.add_argument("-n", "--requests", type=int, default=200)
    parser.add_argument("--url", default="http://localhost:5678")
    args = parser.parse_args()
    webhook = args.url.rstrip("/") + WEBHOOK_PATH

    print(f"Importing workflow {WORKFLOW_ID} into the {args.stack} stack ...")
    copied = compose(
        args.stack, "exec", "-T", "n8n", "sh", "-c", "cat > /tmp/workflow.json",
        input=WORKFLOW.read_bytes(),
    )
    if copied.returncode != 0:
        fail(f"cannot reach the n8n container. Is the {args.stack} stack up?", copied)
    imported = n8n_cli(args.stack, "import:workflow", "--input=/tmp/workflow.json")
    if imported.returncode != 0:
        fail("import:workflow failed", imported)

    print("Publishing it ...")
    published = n8n_cli(args.stack, "publish:workflow", f"--id={WORKFLOW_ID}")
    if published.returncode != 0:
        # n8n 1.x has no publish:workflow; this is its equivalent.
        published = n8n_cli(
            args.stack, "update:workflow", f"--id={WORKFLOW_ID}", "--active=true"
        )
    if published.returncode != 0:
        fail("could not publish the workflow", published)

    # The running instance picks up CLI publishes on a timer, not instantly.
    print("Waiting for the webhook to go live ...")
    deadline = time.time() + 90
    while post(webhook) != 200:
        if time.time() > deadline:
            fail(
                f"{webhook} did not return 200 within 90s. Restart the main with "
                f"`docker compose -f lab/{args.stack}/docker-compose.yml restart n8n` "
                "and run this script again."
            )
        time.sleep(3)

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
