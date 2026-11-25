"""Properties held over generated instances, for the cases nobody listed.

Two classes of error came back twice in review, each time in inputs that no table of cases
named: a check that passes, or "does not apply", while another source of the same run says
otherwise; and an entry of the file that tells something about a setting without naming
it. So these are asserted over snapshots made by a seeded generator, and not case by case:

1. No check is `passed` or `not_applicable` when two sources given in the run state
   different things about what its rule reads, a variable that is not set stating its
   n8n default. The oracle is written here, on the snapshot, and calls none of the code
   that decides it (rules/_helpers.py, against_sources()).
2. Every setting, and every fact of the table below, whose value an entry depended on is
   in the `rests_on` of that entry. Dependence is measured, not declared: the value is
   changed in the snapshot, and an entry that comes out different depended on it.

What the generator covers and what it does not is in docs/rules.md, "When two sources
disagree". All values are made up.
"""

import copy
import dataclasses
import random
import unittest

from n8n_healthcheck import engine, profile
from n8n_healthcheck.models import Snapshot
from n8n_healthcheck.rules import ALL_RULES

SECRET, REDACTED = "<valueFrom>", "<redacted>"
SEED, COUNT = 20261125, 700

# The values each variable takes: good, bad, written another way, and unknown to n8n.
VALUES = {
    "EXECUTIONS_MODE": ["queue", "regular", "Queue ", "weird"],
    "DB_TYPE": ["sqlite", "postgresdb", "mysqldb", "weird"],
    "N8N_CONCURRENCY_PRODUCTION_LIMIT": ["-1", "0", "3", "5", "20", "abc"],
    "N8N_METRICS": ["true", "false", "1"],
    "N8N_MULTI_MAIN_SETUP_ENABLED": ["true", "false"],
    "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": ["true", "false", "maybe"],
    "N8N_RUNNERS_MODE": ["internal", "external", "weird"],
    "N8N_RUNNERS_ENABLED": ["true", "false"],
    "N8N_SECURE_COOKIE": ["true", "false"],
    "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": ["true", "false"],
    "EXECUTIONS_DATA_PRUNE": ["true", "false"],
    "EXECUTIONS_DATA_SAVE_ON_PROGRESS": ["true", "false"],
    "N8N_DEFAULT_BINARY_DATA_MODE": ["default", "filesystem", "s3", "weird"],
}
# What n8n uses when the variable is not set. Written here a second time on purpose.
DEFAULT = {
    "EXECUTIONS_MODE": "regular",
    "DB_TYPE": "sqlite",
    "N8N_CONCURRENCY_PRODUCTION_LIMIT": "-1",
    "N8N_METRICS": "false",
    "N8N_MULTI_MAIN_SETUP_ENABLED": "false",
    "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS": "false",
    "N8N_RUNNERS_MODE": "internal",
    "N8N_SECURE_COOKIE": "true",
    "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": "true",
    "EXECUTIONS_DATA_PRUNE": "true",
    "EXECUTIONS_DATA_SAVE_ON_PROGRESS": "false",
}
UNSET = "<unset>"
IMAGES = ["n8nio/n8n:1.80.0", "n8nio/n8n:2.1.0", "n8nio/n8n:2.1.0", "n8nio/n8n:2.1.0", "n8nio/n8n:1", "n8nio/n8n:1.80", "n8nio/n8n:latest", "n8nio/n8n@sha256:abc", "registry.example/team/n8n:prod", "redis:7"]  # fmt: skip
COMMANDS = [[], [], ["n8n", "worker"], ["n8n", "worker", "--concurrency=3"], ["sh", "-c", "n8n worker --concurrency 12"], ["n8n", "webhook"]]
VERSIONS = [None, "1.80.0", "2.1.0", "v2.1.0"]


def generate(seed: int = SEED, count: int = COUNT) -> list:
    """`count` snapshots. Half of them start from one environment that every source repeats, so that sources agree often enough for checks to pass."""
    rng = random.Random(seed)

    def environment(kubernetes=False, like=None):
        env = {}
        for name, values in VALUES.items():
            if like is not None and rng.random() < 0.85:
                if name in like:
                    env[name] = like[name]
                continue
            draw = rng.random()
            if draw < 0.5:
                continue
            if draw < 0.54:
                env[name + "_FILE"] = "/run/made-up"
            elif draw < 0.58:
                env[name] = SECRET if kubernetes else REDACTED
            else:
                env[name] = rng.choice(values)
        return env

    def kubernetes(like):
        deployments = []
        for number in range(rng.randint(0, 4)):
            container = {
                "name": "n8n",
                "image": rng.choice(IMAGES),
                "command": rng.choice(COMMANDS),
                "env": environment(True, like),
                "requests": rng.choice([{}, {"cpu": "1", "memory": "1Gi"}]),
                "limits": rng.choice([{}, {"memory": "1Gi"}]),
                "liveness_probe": rng.choice([True, False]),
                "readiness_probe": rng.choice([True, False]),
                "env_complete": rng.choice([True, True, True, False]),
            }
            deployments.append({"name": f"deployment-{number}", "replicas": rng.choice([0, 1, 2, 3]), "ready_replicas": 1, "containers": [container]})
        return {"deployments": deployments}

    def text(like):
        def stated(name, read, otherwise):
            if like is not None and rng.random() < 0.85:
                return read(like[name]) if name in like else None
            return rng.choice(otherwise)

        word = lambda allowed: lambda raw: raw if raw in allowed else None
        flag = lambda raw: {"true": True, "false": False}.get(raw)
        # A limit of 0 is not one the tool keeps from a text (debug_info.checked()).
        limit = lambda raw: "unlimited" if raw == "-1" else int(raw) if raw.isdigit() and raw != "0" else None
        pruning = stated("EXECUTIONS_DATA_PRUNE", flag, [None, True, False])
        cluster = {
            "instances": {"main": rng.randint(0, 2), "worker": rng.randint(0, 3), "webhook": 0, "engine": 0},
            "versions": rng.choice([[], ["1.80.0"], ["2.1.0"], ["2.1.0"], ["1.80.0", "2.1.0"]]),
            "checks_failed": rng.choice([None, [], ["split-brain"]]),
        }
        return {
            "generated_on": rng.choice([None, "2026-11-01"]),
            "n8n_version": rng.choice(VERSIONS[1:3]),  # a text keeps a plain x.y.z only (debug_info.checked())
            "node_version": None,
            "platform": None,
            "database": stated("DB_TYPE", word(("sqlite", "postgresdb", "mysqldb")), [None, "sqlite", "postgresdb"]),
            "execution_mode": stated("EXECUTIONS_MODE", word(("regular", "queue")), [None, "regular", "queue"]),
            "multi_main": stated("N8N_MULTI_MAIN_SETUP_ENABLED", flag, [None, True, False]),
            "concurrency_limit": stated("N8N_CONCURRENCY_PRODUCTION_LIMIT", limit, [None, "unlimited", 3, 20]),
            "license": rng.choice([None, "community", "enterprise_production", "enterprise_sandbox"]),
            "save_success": None,
            "save_error": None,
            "save_progress": stated("EXECUTIONS_DATA_SAVE_ON_PROGRESS", flag, [None, True, False]),
            "save_manual": None,
            "binary_mode": stated("N8N_DEFAULT_BINARY_DATA_MODE", word(("default", "filesystem", "s3")), [None, "default", "filesystem"]),
            "pruning": None if pruning is None else {"enabled": pruning, "max_age_hours": 336, "max_count": 10000},
            "insecure": {"secure_cookie": rng.random() < 0.2, "file_access_blocked": rng.random() < 0.2},
            "cluster": rng.choice([None, cluster]),
        }

    snapshots = []
    for _ in range(count):
        like = environment() if rng.random() < 0.5 else None
        if like is not None:
            like = {name: value for name, value in like.items() if not name.endswith("_FILE") and value != REDACTED}
        given = lambda chance: rng.random() < chance
        snapshots.append(
            Snapshot(
                health={"healthz": True, "readiness": rng.choice([True, False]), "metrics_disabled": rng.choice([True, False])} if given(0.4) else None,
                metrics=rng.choice([None, None, {}, {"n8n_made_up_total": 1.0}]),
                env=(dict(like) if like is not None and given(0.8) else environment()) if given(0.6) else None,
                postgres={"version": "16", "execution_count": 1000, "execution_table_bytes": 5000, "oldest_execution_age_days": 3,
                          "connections": rng.choice([1, 79, 80]), "max_connections": rng.choice([0, 100])} if given(0.3) else None,  # fmt: skip
                redis={"version": "7", "waiting_jobs": rng.choice([None, 0, 9, 10, 100]), "active_jobs": rng.choice([None, 0, 1]), "failed_jobs": 0,
                       "used_memory_bytes": rng.choice([10, 80]), "maxmemory_bytes": rng.choice([0, 100]),
                       "maxmemory_policy": rng.choice(["noeviction", "allkeys-lru"])} if given(0.4) else None,  # fmt: skip
                kubernetes=kubernetes(like) if given(0.6) else None,
                n8n_version=rng.choice(VERSIONS),
                debug_info=text(like) if given(0.5) else None,
            )
        )
    return snapshots


# --- the oracle of property 1: read on the snapshot, with none of the code under test ---


def role_of(container: dict):
    name = (container.get("image") or "").split("@")[0].rsplit("/", 1)[-1].split(":")[0]
    if "n8n" not in name:
        return None
    words = " ".join(container.get("command") or []).split()
    return "worker" if "worker" in words else "webhook" if "webhook" in words else "main"


def containers(snapshot: Snapshot) -> list:
    """(role, environment, read in full, container, replicas of its Deployment) of every n8n container."""
    found = []
    for deployment in (snapshot.kubernetes or {}).get("deployments", []):
        for container in deployment["containers"]:
            if role_of(container):
                found.append((role_of(container), container["env"], container["env_complete"] is True, container, deployment["replicas"]))
    return found


def says(env: dict, name: str, complete: bool = True):
    """What an environment states for a variable; None when it cannot be told."""
    if name in env:
        return None if env[name] in (SECRET, REDACTED) else env[name].strip().lower()
    if name + "_FILE" in env or not complete:
        return None
    return DEFAULT.get(name, UNSET)


def same(one: str, other: str) -> bool:
    flags = {"true": "1", "false": "0"}
    return flags.get(one, one) == flags.get(other, other)


NO_TEXT = {"pruning": None, "insecure": {"secure_cookie": False, "file_access_blocked": False}, "concurrency_limit": None, "execution_mode": None,
           "database": None, "multi_main": None, "save_progress": None, "binary_mode": None}  # fmt: skip


def text_says(section: dict) -> dict:
    """What a debug information states, as the variables are written."""
    pruning, insecure, limit = section["pruning"], section["insecure"], section["concurrency_limit"]
    values = {
        "EXECUTIONS_MODE": section["execution_mode"],
        "DB_TYPE": section["database"],
        "N8N_MULTI_MAIN_SETUP_ENABLED": section["multi_main"],
        "N8N_CONCURRENCY_PRODUCTION_LIMIT": "-1" if limit == "unlimited" else limit,
        "EXECUTIONS_DATA_PRUNE": pruning and pruning["enabled"],
        "EXECUTIONS_DATA_SAVE_ON_PROGRESS": section["save_progress"],
        "N8N_DEFAULT_BINARY_DATA_MODE": section["binary_mode"],
        "N8N_SECURE_COOKIE": False if insecure["secure_cookie"] else None,
        "N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES": False if insecure["file_access_blocked"] else None,
    }
    return {name: str(value).lower() for name, value in values.items() if value is not None}


# Which rules read a variable that is one for the instance. Written by hand from
# docs/rules.md, not taken from what the rules declare: the oracle must not learn from the
# code which containers a variable is compared on.
READS_THE_MODE = {"SCAL-001", "SCAL-002", "SCAL-003", "SCAL-004", "REL-001", "REL-006", "PERF-002", "PERF-003", "MAINT-001", "SEC-002"}
READS_THE_DATABASE = {"SCAL-001", "REL-001", "SEC-002"}


def one_for_the_instance(snapshot: Snapshot, name: str) -> str:
    """Why `name` is not stated one way by the env file, the debug information and every n8n container, whatever its role; "" when it is."""
    stated = [("env file", says(snapshot.env, name))] if snapshot.env is not None else []
    if name in text_says(snapshot.debug_info or NO_TEXT):
        stated.append(("debug information", text_says(snapshot.debug_info)[name]))
    for role, env, complete, _container, _replicas in containers(snapshot):
        # A worker or a webhook processor that does not set the mode says nothing about it,
        # when all of its environment was read: read in part, it may set it out of sight.
        if name == "EXECUTIONS_MODE" and role != "main" and complete and name not in env and name + "_FILE" not in env:
            continue
        stated.append((f"{role} container", says(env, name, complete)))
    known = [(who, value) for who, value in stated if value is not None]
    for who, value in known[1:]:
        if not same(value, known[0][1]):
            return f"{name}: {known[0][0]} {known[0][1]!r}, {who} {value!r}"
    if snapshot.env is not None and says(snapshot.env, name) is not None and len(known) < len(stated):
        return f"{name}: the env file states it, a container cannot be told"
    return ""


def major(version) -> int:
    return int(version.lstrip("v").split(".")[0])


def versions_read(snapshot: Snapshot) -> dict:
    """Every n8n version read in the run, by where it was read: a tag that starts with a number is one, on any n8n container."""
    section = snapshot.debug_info or {}
    tags = [(container["image"].rsplit(":", 1) + [""])[1] for *_rest, container, _replicas in containers(snapshot) if "@" not in container["image"]]
    return {
        "image tag": [tag for tag in tags if tag[:1].isdigit()],
        "listed instance": list((section.get("cluster") or {}).get("versions") or []),
        "instance": [version for version in (snapshot.n8n_version, section.get("n8n_version")) if version],
    }


def against_a_source(snapshot: Snapshot, check) -> str:
    """Why this check may not be `passed` or `not_applicable`: two sources that differ, named; "" when none does."""
    info = check.info
    for name, readers in (("EXECUTIONS_MODE", READS_THE_MODE), ("DB_TYPE", READS_THE_DATABASE)):
        if info.rule_id in readers or (name == "EXECUTIONS_MODE" and check.by_mode):
            why = one_for_the_instance(snapshot, name)
            if why:
                return why
    names = set(info.settings) | ({"EXECUTIONS_MODE"} if check.by_mode else set())
    stated_in_text = text_says(snapshot.debug_info) if snapshot.debug_info is not None else {}
    for name in sorted(names):
        one = name in ("EXECUTIONS_MODE", "DB_TYPE")
        roles = set(info.scopes) | ({"main"} if one else set())
        file = says(snapshot.env, name) if snapshot.env is not None else None
        for role, env, complete, _container, _replicas in containers(snapshot):
            there = says(env, name, complete)
            if file is not None and role in roles and not one:
                if there is None:
                    return f"{name}: the env file states it, a {role} container cannot be told"
                if not same(file, there):
                    return f"{name}: env file {file!r}, {role} container {there!r}"
            if role == "main" and "main" in roles and name in stated_in_text and there not in (None, UNSET) and not same(there, stated_in_text[name]):
                return f"{name}: main container {there!r}, debug information {stated_in_text[name]!r}"
        if "main" in roles and name in stated_in_text and file not in (None, UNSET) and not same(file, stated_in_text[name]):
            return f"{name}: env file {file!r}, debug information {stated_in_text[name]!r}"
    # What is not a variable.
    section = snapshot.debug_info or {}
    cluster = section.get("cluster") or {}
    workers = [(env, complete, container, replicas) for role, env, complete, container, replicas in containers(snapshot) if role == "worker"]
    if info.rule_id == "MAINT-001":
        if snapshot.metrics and snapshot.env is not None and says(snapshot.env, "N8N_METRICS") in ("false", "0"):
            return "metrics were scraped, the env file says they are off"
        if not snapshot.metrics and (snapshot.health or {}).get("metrics_disabled") and snapshot.env is not None and says(snapshot.env, "N8N_METRICS") in ("true", "1"):
            return "the env file says metrics are on, the URL answered 404"
    if info.rule_id == "MAINT-003":
        tags = [(container["image"].rsplit(":", 1) + [""])[1] for _role, _env, _complete, container, _replicas in containers(snapshot) if "@" not in container["image"]]
        # A tag that names a part of a version (1, 1.80) cannot be said to differ from a release.
        versions = {tag for tag in tags if tag[:1].isdigit() and tag.count(".") == 2} | set(cluster.get("versions") or [])
        versions |= {version for version in (snapshot.n8n_version, section.get("n8n_version")) if version}
        versions = {version.lstrip("v") for version in versions}  # v2.1.0 is the release 2.1.0
        if len(versions) > 1:
            return f"versions {sorted(versions)}"
    if info.rule_id == "SCAL-002" and sum(replicas for *_rest, replicas in workers) >= 2:
        if snapshot.env is not None and says(snapshot.env, "EXECUTIONS_MODE") not in (None, "queue"):
            return "worker Deployments, and an env file that is not in queue mode"
        if section.get("execution_mode") == "regular":
            return "worker Deployments, and a debug information in regular mode"
        if (cluster.get("instances") or {}).get("worker") == 1:
            return "two worker replicas asked for, one worker listed by n8n"
    if info.rule_id == "SCAL-004" and snapshot.env is not None and (says(snapshot.env, "N8N_CONCURRENCY_PRODUCTION_LIMIT") or "").isdigit():
        for env, complete, container, _replicas in workers:
            own = says(env, "N8N_CONCURRENCY_PRODUCTION_LIMIT", complete)
            words = " ".join(container["command"]).replace("=", " ").split()
            flag = words[words.index("--concurrency") + 1] if "--concurrency" in words else ""
            if (own or "").isdigit() and 1 <= int(own) < 5 or (own == "-1" and flag.isdigit() and int(flag) < 5):
                return "the env file gives every worker a limit, a worker is set below 5 by itself"
        # The limit of the file is all that was read for the workers, and the text states another one.
        text_limit = {"unlimited": "-1", None: None}.get(section.get("concurrency_limit"), str(section.get("concurrency_limit")))
        if not workers and says(snapshot.env, "EXECUTIONS_MODE") == "queue" and text_limit is not None and int(says(snapshot.env, "N8N_CONCURRENCY_PRODUCTION_LIMIT")) != int(text_limit):
            return "the env file gives the workers a limit, the debug information states another one"
    if info.rule_id == "SEC-002":
        # A pass that leans on the version: a process judged does not set N8N_RUNNERS_ENABLED to true.
        everyone = [(role, env) for role, env, *_rest in containers(snapshot) if role != "webhook"]
        mains = [env for role, env in everyone if role == "main"]
        offloads = bool(mains) and says(mains[0], "EXECUTIONS_MODE") == "queue" and says(mains[0], "OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS") in ("true", "1")
        if snapshot.env is not None:
            judged = [snapshot.env]
        elif mains and says(mains[0], "EXECUTIONS_MODE") == "queue":
            judged = [env for role, env in everyone if role == "worker" or not offloads]
        else:
            judged = mains
        if any(says(env, "N8N_RUNNERS_ENABLED") not in ("true", "1") for env in judged):
            read = [version for versions in versions_read(snapshot).values() for version in versions]
            if not read or any(major(version) < 2 for version in read):
                return f"N8N_RUNNERS_ENABLED is not set to true everywhere, and the versions read are {sorted(read)}"
    return ""


# --- property 2: what an entry depended on ---


def stated_by(check) -> tuple:
    """What the file writes for a check, apart from `rests_on`."""
    found = check.finding
    return (check.outcome, check.basis, check.reason, check.needs, found and (found.title, found.severity))


def with_setting(snapshot: Snapshot, scope: str, name: str, value) -> Snapshot:
    """A copy with one setting of the file changed in its scope; `value` None removes it."""
    changed = copy.deepcopy(snapshot)
    envs = [changed.env] if scope == "instance" else [env for role, env, *_ in containers(changed) if role == scope]
    for env in envs:
        env.pop(name, None)
        if value is not None:
            env[name] = value
    return changed


def _put(section: str, key: str, value):
    def change(snapshot: Snapshot) -> None:
        if getattr(snapshot, section) is not None:
            getattr(snapshot, section)[key] = value

    return change


def _replicas(snapshot: Snapshot) -> None:
    for deployment in (snapshot.kubernetes or {}).get("deployments", []):
        if any(role_of(container) == "worker" for container in deployment["containers"]):
            deployment["replicas"] = 1 if deployment["replicas"] != 1 else 2


def _version(snapshot: Snapshot) -> None:
    snapshot.n8n_version = "1.80.0" if snapshot.n8n_version != "1.80.0" else "2.1.0"


# fact id -> changes to the snapshot that move that fact.
FACT_CHANGES = {
    "queue.waiting_jobs": [_put("redis", "waiting_jobs", 500), _put("redis", "waiting_jobs", 0)],
    "queue.active_jobs": [_put("redis", "active_jobs", 0), _put("redis", "active_jobs", 5)],
    "queue.maxmemory_policy": [_put("redis", "maxmemory_policy", "noeviction"), _put("redis", "maxmemory_policy", "allkeys-lru")],
    "queue.maxmemory_bytes": [_put("redis", "maxmemory_bytes", 0), _put("redis", "maxmemory_bytes", 100)],
    "database.max_connections": [_put("postgres", "max_connections", 1000), _put("postgres", "max_connections", 80)],
    "license.type": [_put("debug_info", "license", "enterprise_sandbox"), _put("debug_info", "license", "community")],
    "worker.replicas": [_replicas],
    "n8n.version": [_version],
}


def assessed(snapshot: Snapshot):
    checks = engine.assess(snapshot, ALL_RULES)
    return checks, profile.build_profile(snapshot, [], (), checks=checks)


class SourcesPropertyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshots = generate()
        cls.assessed = [assessed(snapshot) for snapshot in cls.snapshots]

    def test_the_generator_gives_the_same_snapshots_for_the_same_seed(self):
        self.assertEqual(generate(count=20), generate(count=20))
        self.assertNotEqual(generate(seed=1, count=20), generate(seed=2, count=20))

    def test_no_check_passes_or_does_not_apply_against_a_source_of_the_same_run(self):
        decided, turned, only_1x_through = set(), set(), set()
        for number, (snapshot, (checks, _made)) in enumerate(zip(self.snapshots, self.assessed)):
            for check in checks:
                why = against_a_source(snapshot, check)
                if why.startswith("N8N_RUNNERS_ENABLED"):
                    old = [where for where, versions in versions_read(snapshot).items() if any(major(version) < 2 for version in versions)]
                    only_1x_through.update(old if len(old) == 1 else [])
                if check.outcome in ("passed", "not_applicable"):
                    self.assertEqual(why, "", f"snapshot {number}, {check.info.rule_id} is {check.outcome}")
                    decided.add((check.info.rule_id, check.outcome))
                elif check.reason in ("sources_disagree", "value_unreadable") and (why or against_a_source(snapshot, dataclasses.replace(check, by_mode=True))):
                    turned.add(check.info.rule_id)
        # The property is not held by a generator whose sources never agree, or never differ.
        settings_rules = {rule.info.rule_id for rule in ALL_RULES if rule.info.settings}
        self.assertLessEqual(settings_rules, {rule_id for rule_id, outcome in decided if outcome == "passed"} | {"SCAL-001"})
        self.assertIn(("SCAL-001", "not_applicable"), decided)
        self.assertLessEqual(settings_rules | {"REL-002", "REL-003", "REL-004", "MAINT-003"}, turned)
        # SEC-002 was held back by a 1.x that each kind of source was alone to state.
        self.assertEqual(only_1x_through, {"image tag", "listed instance", "instance"})

    def test_an_entry_names_every_setting_it_depended_on(self):
        depended = set()
        for number, (snapshot, (checks, made)) in enumerate(zip(self.snapshots, self.assessed)):
            before = [stated_by(check) for check in checks]
            rests_on = [entry["rests_on"] for entry in made["check"]["checks"]]
            for setting in made["settings"]:
                scope, name = setting["scope"], setting["name"]
                if name not in VALUES:
                    continue  # a `<NAME>_FILE`
                # Main containers whose environments differ are read by no rule (as in 0.5.0),
                # whatever the variable that differs: that dependence on every variable is not named.
                mains = [(env, complete) for role, env, complete, *_ in containers(snapshot) if role == "main"]
                if scope == "main" and any(main != mains[0] for main in mains):
                    continue
                for value in [None, *VALUES[name]]:
                    after = engine.assess(with_setting(snapshot, scope, name, value), ALL_RULES)
                    for rule, was, now, named in zip(ALL_RULES, before, after, rests_on):
                        if stated_by(now) != was:
                            self.assertIn(
                                profile.setting_id(setting), named, f"snapshot {number}, {rule.info.rule_id}: {was} became {stated_by(now)} with {name}={value!r} in {scope}"
                            )
                            depended.add((rule.info.rule_id, scope))
        # Each scope was seen to matter, the containers next to an env file included.
        self.assertLessEqual({"instance", "main", "worker"}, {scope for _rule_id, scope in depended})
        self.assertGreater(len({rule_id for rule_id, _scope in depended}), 12)

    def test_an_entry_names_every_fact_it_depended_on(self):
        depended = set()
        for number, (snapshot, (checks, made)) in enumerate(zip(self.snapshots, self.assessed)):
            before = [stated_by(check) for check in checks]
            rests_on = [entry["rests_on"] for entry in made["check"]["checks"]]
            facts = {fact["id"]: fact for fact in made["facts"]}
            for fact_id, changes in FACT_CHANGES.items():
                for change in changes:
                    changed = copy.deepcopy(snapshot)
                    change(changed)
                    after, made_after = assessed(changed)
                    moved = {fact["id"]: fact for fact in made_after["facts"]}[fact_id]["value"] != facts[fact_id]["value"]
                    if not moved or facts[fact_id]["provenance"] == "unknown":
                        continue  # the change did not reach the fact, or the file holds no value for it
                    for rule, was, now, named in zip(ALL_RULES, before, after, rests_on):
                        if stated_by(now) != was:
                            self.assertIn(fact_id, named, f"snapshot {number}, {rule.info.rule_id}: {was} became {stated_by(now)}")
                            depended.add(fact_id)
        self.assertEqual(depended, set(FACT_CHANGES))


if __name__ == "__main__":
    unittest.main()
