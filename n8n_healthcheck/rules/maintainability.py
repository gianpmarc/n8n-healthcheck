"""Maintainability rules: can the instance be observed and upgraded safely."""

from __future__ import annotations

from typing import Optional

from ..models import Finding, Snapshot
from ._helpers import env_flag, image_tag, image_version, is_queue_mode, n8n_containers, shown

# Tags that are moved to a new release by their publisher, so two pulls of the same
# reference can return different n8n versions. An untagged image means `latest`.
MUTABLE_IMAGE_TAGS = ("latest", "stable", "beta", "next", "nightly")


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/monitor-n8n
def metrics_disabled(snapshot: Snapshot) -> Optional[Finding]:
    env = snapshot.env
    if env is None or env_flag(env, "N8N_METRICS") is True:
        return None
    # Samples were scraped, so /metrics is on whatever the env file says.
    if snapshot.metrics:
        return None
    # n8n's config marks queue metrics as "Not supported in multi-main setup".
    queue_metrics = ""
    if is_queue_mode(env) and env_flag(env, "N8N_MULTI_MAIN_SETUP_ENABLED") is not True:
        queue_metrics = (
            " Also set N8N_METRICS_INCLUDE_QUEUE_METRICS=true on the main process for "
            "waiting/active/failed job gauges (not supported in a multi-main setup)."
        )
    return Finding(
        rule_id="MAINT-001",
        area="maintainability",
        severity="warning",
        title="Prometheus metrics endpoint is disabled",
        evidence=f"{shown(env, 'N8N_METRICS')} (default: false).",
        impact=(
            "There is no /metrics endpoint to scrape, so memory growth, event-loop lag and "
            "queue depth are invisible until users report failed or slow executions."
        ),
        recommendation=(
            "Set N8N_METRICS=true on the main process and workers and scrape /metrics with "
            f"Prometheus.{queue_metrics} Keep /metrics off the public internet."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/update-n8n#run-the-same-n8n-version-on-every-process
def mutable_image_tag(snapshot: Snapshot) -> Optional[Finding]:
    if snapshot.kubernetes is None:
        return None
    mutable = []
    for deployment, container, _role in n8n_containers(snapshot.kubernetes):
        tag = image_tag(container.get("image"))
        if tag is None or tag in MUTABLE_IMAGE_TAGS:
            mutable.append(f"{deployment['name']}/{container['name']}: {container.get('image')}")
    if not mutable:
        return None
    return Finding(
        rule_id="MAINT-002",
        area="maintainability",
        severity="warning",
        title="n8n image uses a mutable tag",
        evidence="; ".join(mutable) + ".",
        impact=(
            "Any pod restart or scale-out can pull a newer n8n than the one already running, "
            "so main and workers end up on different versions and an unplanned upgrade "
            "applies database migrations that a rollback of the tag does not undo."
        ),
        recommendation=(
            "Pin every n8n process to the same specific version, for example "
            "n8nio/n8n:<n8n-version> (or an image digest), and change it only as a planned "
            "upgrade. Keep the n8nio/runners image on the matching version."
        ),
    )


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/update-n8n#run-the-same-n8n-version-on-every-process
def version_skew(snapshot: Snapshot) -> Optional[Finding]:
    if snapshot.kubernetes is None:
        return None
    by_tag: dict = {}
    for deployment, container, role in n8n_containers(snapshot.kubernetes):
        # Compared on the leading x.y.z, so 2.40.1 and 2.40.1-build7 are one release.
        # Digests, mutable tags and custom tags name no version and are skipped: a
        # digest may well be the same release as a tag next to it.
        version = image_version(container.get("image"))
        if version is None:
            continue
        by_tag.setdefault(version, []).append(f"{deployment['name']} ({role})")
    if len(by_tag) < 2:
        return None
    return Finding(
        rule_id="MAINT-003",
        area="maintainability",
        severity="warning",
        title="n8n processes run different versions",
        evidence="; ".join(f"{tag}: {', '.join(names)}" for tag, names in sorted(by_tag.items()))
        + ".",
        impact=(
            "Main, workers and webhook processors share one database, so a process on an "
            "older version runs against a schema migrated by a newer one and executions "
            "fail in ways that depend on which pod picks them up."
        ),
        recommendation=(
            "Set the same image tag on every n8n Deployment (main, worker, webhook "
            "processor) and update them together as one coordinated change."
        ),
    )


RULES = [metrics_disabled, mutable_image_tag, version_skew]
