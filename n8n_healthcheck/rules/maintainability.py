"""Maintainability rules: can the instance be observed and upgraded safely."""

from __future__ import annotations

from .. import debug_info
from ..models import Finding, Outcome, Snapshot, Variant
from ._helpers import (
    NOT_STATED,
    PASSED,
    SOURCES_DISAGREE,
    effective_env,
    env_flag,
    image_tag,
    image_version,
    image_versions,
    is_queue_mode,
    n8n_containers,
    release,
    rule,
    shown,
    source_missing,
    undecided,
    unknown,
    versions_stated,
)

# Tags that are moved to a new release by their publisher, so two pulls of the same
# reference can return different n8n versions. An untagged image means `latest`.
MUTABLE_IMAGE_TAGS = ("latest", "stable", "beta", "next", "nightly")


def _no_metrics(queue_metrics: str) -> Variant:
    return Variant(
        title="Prometheus metrics endpoint is disabled",
        impact=(
            "There is no /metrics endpoint to scrape, so memory growth, event-loop lag and "
            "queue depth are invisible until users report failed or slow executions."
        ),
        remedy=(
            "Set N8N_METRICS=true on the main process and workers and scrape /metrics with "
            f"Prometheus.{queue_metrics} Keep /metrics off the public internet."
        ),
    )


NO_METRICS = _no_metrics("")
# In queue mode with a single main, where n8n supports the queue gauges.
NO_METRICS_QUEUE = _no_metrics(
    " Also set N8N_METRICS_INCLUDE_QUEUE_METRICS=true on the main process for "
    "waiting/active/failed job gauges (not supported in a multi-main setup)."
)


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/monitor-n8n
@rule(
    "MAINT-001",
    "maintainability",
    subject="Prometheus metrics endpoint",
    passed="The Prometheus metrics endpoint is enabled.",
    variants=(NO_METRICS, NO_METRICS_QUEUE),
    settings=("N8N_METRICS", "EXECUTIONS_MODE", "N8N_MULTI_MAIN_SETUP_ENABLED"),
)
def metrics_disabled(snapshot: Snapshot) -> Finding | Outcome:
    env = effective_env(snapshot)
    if env is None or env_flag(env, "N8N_METRICS") is True or unknown(env, "N8N_METRICS"):
        # Samples were scraped, so /metrics is on whatever else is known.
        if snapshot.metrics or (env is not None and env_flag(env, "N8N_METRICS") is True):
            # The variable says on, and the URL answered 404 on /metrics.
            if not snapshot.metrics and (snapshot.health or {}).get("metrics_disabled") is True:
                return SOURCES_DISAGREE
            return PASSED
        if env is not None:
            return undecided(env, "N8N_METRICS") or NOT_STATED
        if snapshot.kubernetes is not None:
            return NOT_STATED  # no main container, or mains that disagree
        # The rule reads the variable; a URL alone decides only when the scrape answers.
        return source_missing("env", "kubernetes", *(("url",) if snapshot.health is None else ()))
    # Samples were scraped, so /metrics is on and there is no finding, while the
    # environment says it is off: the two do not describe the same thing.
    if snapshot.metrics:
        return SOURCES_DISAGREE
    # n8n's config marks queue metrics as "Not supported in multi-main setup".
    multi_main = "N8N_MULTI_MAIN_SETUP_ENABLED"
    single_main = env_flag(env, multi_main) is not True and not unknown(env, multi_main)
    return metrics_disabled.info.finding(
        NO_METRICS_QUEUE if is_queue_mode(env) and single_main else NO_METRICS,
        "warning",
        f"{shown(env, 'N8N_METRICS')} (default: false).{env.note}",
    )


MUTABLE_TAG = Variant(
    title="n8n image uses a mutable tag",
    impact=(
        "Any pod restart or scale-out can pull a newer n8n than the one already running, "
        "so main and workers end up on different versions and an unplanned upgrade "
        "applies database migrations that a rollback of the tag does not undo."
    ),
    remedy=(
        "Pin every n8n process to the same specific version, for example "
        "n8nio/n8n:<n8n-version> (or an image digest), and change it only as a planned "
        "upgrade. Keep the n8nio/runners image on the matching version."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/update-n8n#run-the-same-n8n-version-on-every-process
@rule(
    "MAINT-002",
    "maintainability",
    subject="Tag of the n8n image",
    passed=(
        f"No n8n image is untagged or uses one of the tags {', '.join(MUTABLE_IMAGE_TAGS)}. Only these "
        "are compared: another tag may move as well."
    ),
    variants=(MUTABLE_TAG,),
)
def mutable_image_tag(snapshot: Snapshot) -> Finding | Outcome:
    if snapshot.kubernetes is None:
        return source_missing("kubernetes")
    mutable = []
    for deployment, container, _role in n8n_containers(snapshot.kubernetes):
        tag = image_tag(container.get("image"))
        if tag is None or tag in MUTABLE_IMAGE_TAGS:
            mutable.append(f"{deployment['name']}/{container['name']}: {container.get('image')}")
    if not mutable:
        return PASSED if any(n8n_containers(snapshot.kubernetes)) else NOT_STATED
    return mutable_image_tag.info.finding(MUTABLE_TAG, "warning", "; ".join(mutable) + ".")


VERSION_SKEW = Variant(
    title="n8n processes run different versions",
    impact=(
        "Main, workers and webhook processors share one database, so a process on an "
        "older version runs against a schema migrated by a newer one and executions "
        "fail in ways that depend on which pod picks them up."
    ),
    remedy=(
        "Set the same image tag on every n8n Deployment (main, worker, webhook "
        "processor) and update them together as one coordinated change."
    ),
)


# https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/update-n8n#run-the-same-n8n-version-on-every-process
@rule(
    "MAINT-003",
    "maintainability",
    subject="n8n version of every process",
    passed=(
        "Every n8n process that was read is on the same version: the image tags in Kubernetes, "
        "or the instances that n8n lists. No other version was read for the instance."
    ),
    variants=(VERSION_SKEW,),
    facts=("cluster.versions_aligned", "n8n.version"),
)
def version_skew(snapshot: Snapshot) -> Finding | Outcome:
    by_tag = image_versions(snapshot.kubernetes)
    from_images = bool(by_tag)
    evidence = "; ".join(f"{tag}: {', '.join(names)}" for tag, names in sorted(by_tag.items())) + "."
    if not by_tag and debug_info.of(snapshot) is not None:
        # Kubernetes names no version (or was not given): the versions of the instances
        # n8n itself listed in the debug information.
        versions = (debug_info.of(snapshot).get("cluster") or {}).get("versions") or []
        by_tag = dict.fromkeys(versions)
        evidence = (
            f"The {debug_info.phrase(debug_info.of(snapshot))} lists instances on "
            f"{len(versions)} versions: {', '.join(versions)}."
        )
    if len(by_tag) < 2:
        if not by_tag:
            if snapshot.kubernetes is None and debug_info.of(snapshot) is None:
                return source_missing("kubernetes", "debug_info")
            return NOT_STATED  # no image tag names a version, and the text lists none
        # One version among the images: the same on every process only when each names one
        # (a digest or a custom tag may well be another release).
        named = [image_version(c.get("image")) is not None for _, c, _ in n8n_containers(snapshot.kubernetes or {})]
        if from_images and not all(named):
            return NOT_STATED
        # One version among the images, or among the instances n8n lists, while /metrics
        # or the debug information states another one for the instance.
        (version,) = by_tag
        return SOURCES_DISAGREE if versions_stated(snapshot) - {release(version)} else PASSED
    return version_skew.info.finding(VERSION_SKEW, "warning", evidence)


SANDBOX_LICENSE = Variant(
    title="Enterprise sandbox license",
    impact=(
        "This matters only if this instance serves production: a sandbox license is "
        "meant for development and testing, not for production use."
    ),
    remedy=(
        "If this is a development or test instance, nothing needs to change. If it "
        "serves production, activate the production license key on it instead."
    ),
)


# Only the debug information states the licence, so there is no probe to give way to.
@rule(
    "MAINT-004",
    "maintainability",
    subject="Kind of license",
    passed="The license is not a sandbox license.",
    variants=(SANDBOX_LICENSE,),
    facts=("license.type",),
)
def sandbox_license(snapshot: Snapshot) -> Finding | Outcome:
    if debug_info.of(snapshot) is None:
        return source_missing("debug_info")
    license_type = debug_info.of(snapshot).get("license")
    if license_type != "enterprise_sandbox":
        return PASSED if license_type is not None else NOT_STATED
    return sandbox_license.info.finding(
        SANDBOX_LICENSE,
        "info",
        f"The {debug_info.phrase(debug_info.of(snapshot))} reports the license as enterprise (sandbox).",
    )


RULES = [metrics_disabled, mutable_image_tag, version_skew, sandbox_license]
