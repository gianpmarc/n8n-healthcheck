"""The questions about an environment that no probe can answer, as data.

Two groups. The operator group is asked by the tool and stored in the profile as
`declared` facts. The conversation group is never asked or stored: it is only printed
on the question sheet. Every question is closed: an answer is a value of its list, never
text a person typed. This module holds data and fixed text only; profile.py applies it.
"""

from __future__ import annotations

YES_NO = (("yes", "Yes"), ("no", "No"))


def _question(fact_id: str, sentence: str, kind: str, values: tuple) -> dict:
    return {"id": fact_id, "question": sentence, "kind": kind, "values": values}


# Kinds: `choice` (one value), `yes_no`, `multi` (several values, at least one).
# A value is (what is stored and typed, its label). At most 15 entries.
OPERATOR = (
    _question(
        "hosting.model",
        "Where does n8n run?",
        "choice",
        (
            ("public_cloud", "Public cloud"),
            ("private_cloud", "Private cloud"),
            ("on_premises", "On premises"),
            ("hybrid", "Hybrid"),
        ),
    ),
    _question(
        "hosting.cloud_provider",
        "Which cloud provider?",
        "choice",
        (("aws", "AWS"), ("azure", "Azure"), ("gcp", "Google Cloud"), ("other", "Another provider"), ("none", "None")),
    ),
    _question(
        "orchestrator",
        "What runs the n8n containers or processes?",
        "choice",
        (
            ("kubernetes", "Kubernetes"),
            ("ecs", "Amazon ECS"),
            ("docker_compose", "Docker Compose"),
            ("docker_swarm", "Docker Swarm"),
            ("nomad", "Nomad"),
            ("virtual_machine", "A virtual machine, without containers"),
            ("other", "Something else"),
        ),
    ),
    _question(
        "install.method",
        "How is n8n installed and updated?",
        "choice",
        (
            ("helm_chart", "Helm chart"),
            ("kubernetes_manifests", "Kubernetes manifests"),
            ("compose_file", "Compose file"),
            ("task_definition", "Task definition"),
            ("npm", "npm"),
            ("other", "Something else"),
        ),
    ),
    _question(
        "n8n.image_modified",
        "Is the n8n image built or modified by you (extra packages, patches, a different base)?",
        "yes_no",
        YES_NO,
    ),
    _question(
        "proxy.kind",
        "What sits in front of n8n?",
        "choice",
        (
            ("nginx", "nginx"),
            ("traefik", "Traefik"),
            ("haproxy", "HAProxy"),
            ("envoy", "Envoy"),
            ("cloud_load_balancer", "A cloud load balancer"),
            ("other", "Something else"),
            ("none", "Nothing"),
        ),
    ),
    _question(
        "proxy.tls_termination",
        "Where does TLS end?",
        "choice",
        (("at_proxy", "At the proxy or load balancer"), ("at_n8n", "At n8n itself"), ("none", "No TLS")),
    ),
    _question(
        "proxy.sticky_sessions",
        "Are sticky sessions enabled in front of the main instances?",
        "yes_no",
        YES_NO,
    ),
    _question(
        "database.service",
        "Who operates the database?",
        "choice",
        (("managed_service", "A managed service"), ("self_managed", "Self-managed")),
    ),
    _question(
        "database.high_availability",
        "Does the database have a standby that takes over?",
        "yes_no",
        YES_NO,
    ),
    _question(
        "database.backups",
        "How is the database backed up?",
        "choice",
        (("point_in_time", "Point-in-time recovery"), ("scheduled", "Scheduled backups"), ("none", "No backups")),
    ),
    _question(
        "queue.redis_topology",
        "How is Redis deployed?",
        "choice",
        (
            ("single", "A single instance"),
            ("sentinel", "Sentinel"),
            ("cluster", "Cluster"),
            ("managed_service", "A managed service"),
        ),
    ),
    _question(
        "secrets.source",
        "Where do n8n's own secrets come from at start?",
        "choice",
        (
            ("environment", "Plain environment variables"),
            ("orchestrator_secrets", "The orchestrator's secrets"),
            ("external_store", "An external secret store"),
            ("files", "Files"),
        ),
    ),
    _question("worker.autoscaling", "Do workers scale automatically?", "yes_no", YES_NO),
    _question(
        "observability.tools",
        "What do you use to watch n8n?",
        "multi",
        (
            ("prometheus", "Prometheus"),
            ("grafana", "Grafana"),
            ("datadog", "Datadog"),
            ("opentelemetry", "OpenTelemetry"),
            ("cloudwatch", "CloudWatch"),
            ("splunk", "Splunk"),
            ("elastic", "Elastic"),
            ("other", "Something else"),
            ("none", "Nothing"),
        ),
    ),
)
OPERATOR_BY_ID = {question["id"]: question for question in OPERATOR}

# (question, why it matters). No fact id and no value list: the tool never stores these.
CONVERSATION = (
    (
        "How do users sign in: local accounts, SAML, OIDC, LDAP?",
        "Single sign-on changes how accounts are recovered and what an upgrade can break.",
    ),
    (
        "Is there a staging environment that mirrors production?",
        "Without one, every upgrade and every change is first tried on production.",
    ),
    (
        "How often is n8n upgraded, and who decides?",
        "An instance many versions behind needs a staged upgrade and misses fixes.",
    ),
    (
        "Is there a tested rollback for an upgrade?",
        "Database migrations run on start, so going back means restoring a backup that was tested.",
    ),
    (
        "Does the image or the start-up carry local patches or preloaded code?",
        "Local changes can break on an upgrade and make a problem hard to reproduce elsewhere.",
    ),
    (
        "How do workflows move from development to production?",
        "Editing in production, or copying by hand, is where untested changes come from.",
    ),
    (
        "Which workflows would hurt most if they stopped, and what volume do they carry?",
        "Sizing and priorities follow the critical workflows, not the average one.",
    ),
    (
        "Who is the technical owner, and who is on call?",
        "During an incident the right person must be reachable without a search.",
    ),
    (
        "Which region or regions does it run in, and are there data-residency rules?",
        "Residency rules limit where data, backups and logs may be kept.",
    ),
    (
        "What is the change-approval process for the platform?",
        "It decides how fast a recommended fix can actually be applied.",
    ),
)

# One sentence for each fact a probe can fill, for part 1 of the question sheet.
# `orchestrator` is not here: its sentence is the operator question.
MEASURED = {
    "n8n.version": "Which n8n version runs?",
    "n8n.image_official": "Is the n8n image pulled from an official n8n registry?",
    "executions.mode": "Does n8n run in regular or in queue mode?",
    "main.replicas": "How many main replicas run?",
    "worker.replicas": "How many worker replicas run?",
    "webhook.replicas": "How many webhook replicas run?",
    "worker.concurrency": "How many executions does one worker run at a time?",
    "multi_main.enabled": "Is multi-main mode enabled?",
    "task_runners.enabled": "Are task runners enabled?",
    "task_runners.mode": "Do task runners run in internal or in external mode?",
    "binary_data.mode": "Where is binary data stored?",
    "metrics.enabled": "Is the metrics endpoint enabled?",
    "pruning.enabled": "Is execution pruning enabled?",
    "pruning.max_age_hours": "After how many hours are executions pruned?",
    "database.engine": "Which database engine does n8n use?",
    "database.version": "Which Postgres version runs?",
    "database.pool_size": "How large is n8n's database connection pool?",
    "database.max_connections": "How many connections does Postgres allow?",
    "database.execution_count": "How many executions are stored?",
    "database.execution_table_bytes": "How large are the execution tables on disk?",
    "database.oldest_execution_age_days": "How old is the oldest stored execution?",
    "queue.redis_version": "Which Redis version runs?",
    "queue.maxmemory_policy": "Which eviction policy does Redis use?",
    "queue.maxmemory_bytes": "What is the Redis memory limit?",
    "queue.waiting_jobs": "How many jobs wait in the queue?",
    "queue.active_jobs": "How many jobs are being executed?",
    "queue.failed_jobs": "How many failed jobs are kept in the queue?",
    "resources.main": "Which CPU and memory requests and limits do the main containers have?",
    "resources.worker": "Which CPU and memory requests and limits do the worker containers have?",
    "probes.main": "Do the main containers have a liveness and a readiness probe?",
    "probes.worker": "Do the worker containers have a liveness and a readiness probe?",
}


def sentence(fact_id: str) -> str:
    """The question a catalogue fact answers. KeyError for an id that is not in the catalogue."""
    if fact_id in OPERATOR_BY_ID:
        return OPERATOR_BY_ID[fact_id]["question"]
    return MEASURED[fact_id]


def line(question: dict) -> str:
    """One operator question as a Markdown list item, with the values an answer may hold."""
    if question["kind"] == "yes_no":
        allowed = "`yes` or `no`"
    else:
        lead = "one or more of " if question["kind"] == "multi" else "one of "
        allowed = lead + ", ".join(f"`{value}` ({label})" for value, label in question["values"])
    return f"- **{question['question']}** (`{question['id']}`): {allowed}"


def render_list() -> str:
    """The operator questions as Markdown, for `--list-questions`."""
    lines = [
        "# Questions you can answer",
        "",
        "Every one is optional. Answer with `--answer ID=VALUE` next to `--save-profile`;",
        "for a question that takes several values, `--answer ID=a,b`. An answer is written",
        'in the profile as "declared". When the tool measured the fact itself, the answer',
        "is dropped: what was measured wins.",
        "",
    ]
    lines += [line(question) for question in OPERATOR]
    lines.append("")
    return "\n".join(lines)
