# What the tool checks

`n8n-healthcheck` runs 24 rules on what it collected. This page is the statement of what
each one computes: what it reads, when it passes, when it does not apply, when it could
not decide, and every sentence it can write.

It is also the list of **every text a rule can put in an environment profile**
(`check.checks`, see [profile-format.md](profile-format.md)). Those texts are fixed
sentences of the code: none is built from a value of your instance. Which sentence is
chosen can tell a state (a title about queue mode tells the mode), as a finding's title
always did. The evidence of a finding, which quotes the values that were read, is in the
report only and is never written to a profile.

A test keeps this page in step with the code: every rule id and every fixed text below
is checked against the rules themselves.

## The four outcomes

Every rule states exactly one outcome per run.

| Outcome | Meaning |
|---|---|
| `finding` | The rule found a problem. It has a severity, a title, an impact and a remedy. |
| `passed` | The rule read the values it needs and they are good. The sentence it writes states the condition it checked, and nothing stronger. |
| `not_applicable` | The rule does not apply to this setup, or stays silent on purpose. |
| `not_assessed` | The rule could not decide. It says why. |

A rule never passes on something it did not read, and its passed sentence says what
was read: "fewer than 100 jobs wait", not "the queue has no backlog"; "the variable is
set to", not "every process does". Where a rule compares against a short list or reads
a setting whose effect depends on something it does not read, the sentence says so. One case needs a word: a variable
that is **not set**, in an environment that was read in full, has the n8n default. The
rules already raise findings on that basis, and they pass on it too; the outcome then says
`"basis": "default"` instead of `"read"`, and the call sheet prints "by n8n's default, not
set explicitly".

### Why a rule could not decide

| Reason | Meaning | `needs` |
|---|---|---|
| `source_missing` | None of the sources where the rule looks for this was given. | The sources of which any one would let it decide: `url`, `env`, `postgres`, `redis`, `kubernetes`, `debug_info`. |
| `source_failed` | Such a source was asked for and could not be collected. | Those sources. |
| `not_stated` | A source where the rule looks was given and read, and does not say it. | empty |
| `value_unreadable` | A source holds the value, which could not be read: a Kubernetes Secret, a `_FILE` variable, a redacted value, an environment read in part. | empty |
| `sources_disagree` | Two sources given in the run state different things about what the rule reads. See below. | empty |
| `rule_error` | The rule itself failed. That is a bug in the tool; the report lists it under "Not assessed". | empty |

`url` stands for the n8n URL (health endpoints and `/metrics`), `env` for the env file.

### When two sources disagree

Several sources can state the same thing: an env file and the containers of Kubernetes,
a probe and the debug information, a variable and the answer of an endpoint. The rules
have an order between them (the env file describes every process, a probe wins over the
debug information), and that order decides when a **finding** is raised: it is unchanged.
It never decides a pass. When a rule has no finding and two sources given in the run
disagree on what it reads, the rule is `not_assessed` for the reason `sources_disagree`:
it neither passes nor "does not apply" against a source that says otherwise.

For the variables a rule declares (the settings under "Rests on"), three comparisons are
made, each between sources that describe the same thing, on what each source **states**
for the variable:

- its value, when it is set and readable;
- its n8n default, when it is not set in an environment read in full: `EXECUTIONS_MODE`
  regular, `DB_TYPE` sqlite, `N8N_MULTI_MAIN_SETUP_ENABLED` false,
  `N8N_CONCURRENCY_PRODUCTION_LIMIT` -1, `EXECUTIONS_DATA_PRUNE` true,
  `EXECUTIONS_DATA_SAVE_ON_PROGRESS` false, `N8N_SECURE_COOKIE` true,
  `N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES` true, `N8N_RUNNERS_MODE` internal, `N8N_METRICS`
  false, `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS` false. `N8N_DEFAULT_BINARY_DATA_MODE` and
  `N8N_RUNNERS_ENABLED` have no default that holds on every version: not set, they state
  only that they are not set, which differs from any value;
- nothing, when it cannot be told: a Secret, a `_FILE` variable, a redacted value, or a
  variable that is not among the ones read of an environment read in part.

The comparisons:

- **The env file and a Kubernetes container** of a role the rule judges. The file
  describes every process, so it disagrees with a container that states something else,
  one that leaves the variable to another default included. Next to an env file, a
  container that cannot be told for the variable cannot be said to agree: the rule is then
  `not_assessed` for the reason `value_unreadable`, never a pass.
- **The execution mode and the database type across every n8n container.** These two are
  one for the instance: every process is in the same mode and on the same database,
  whatever its role. So `EXECUTIONS_MODE` and `DB_TYPE` are compared between the env file
  and every container (main, worker, webhook), also for a rule that judges the main
  process only, and between the containers of Kubernetes themselves when no env file was
  given: a main container in regular mode next to a worker set to queue mode, or workers
  on another `DB_TYPE` than main, are one instance stated in two ways, and the reason
  written is `sources_disagree` there too. For any other variable two containers are two
  processes, each with its own value, and are not compared. One exception: a worker or a
  webhook processor that does **not set** `EXECUTIONS_MODE`, in an environment read in
  full, says nothing about the mode. Such a process runs in queue mode only, by its start
  command, and what n8n makes of the missing variable there was not checked: it is not
  read as "regular". Read in part, the same container may set the mode in what was not
  read: it cannot be told, as for any other variable. Without an env file, a container
  that cannot be told is compared with nothing.
- **The debug information and a probe** (the env file, or a main container), for a
  variable the rule reads on the main process: the text reports effective values of that
  process. For the execution mode and the database type, which are one for the instance,
  the text is compared with every n8n container (main, worker, webhook) as well: workers
  set to queue mode next to a text in regular mode disagree, also when no main container
  was read. The binary data mode is compared as well. The two security settings are in the
  text only when they are insecure. A variable that cannot be told, or is not set and has
  no default, is compared with nothing here: the text then fills what the probe left open.

The mode is compared in this way also for a rule that does not apply because of the mode
without declaring it (REL-002, REL-003, REL-004). The comparison is on every variable
the rule declares, also one the outcome did not turn on: the rule then says less than it
could, never more. Six rules also compare what is not a variable, or compare a variable
in a way of their own; their sections say so (SCAL-002, SCAL-004, MAINT-001, MAINT-003,
PERF-003, SEC-002).

A consequence to know: an env file given together with Kubernetes must match the
containers variable by variable for a check to pass. A container that gets part of its
environment from a Secret (`envFrom`) leaves every check that reads a variable it does not
show as `value_unreadable`.

### Known limits

- **Kubernetes alone, workers that do not set the mode.** A main container in regular
  mode next to worker Deployments whose containers do not set `EXECUTIONS_MODE` is not a
  disagreement: SCAL-002 passes on two worker replicas, SCAL-003, REL-001 and REL-006
  "do not apply", PERF-002 passes by default. The reason: a worker that leaves the
  variable out says nothing about the mode, and read as "regular" every cluster with a
  main in queue mode and workers that leave it out would disagree with itself. Nobody
  checked what n8n makes of a worker without `EXECUTIONS_MODE`.
- **The same choice next to an env file.** An env file and a main container in queue
  mode, with workers that set `DB_TYPE` but not the mode: REL-001 and SCAL-002 pass.
- **Kubernetes alone, a container that cannot be told.** Main in queue mode on
  `postgresdb`, workers whose `DB_TYPE` is in a Secret or whose environment was read in
  part: REL-001 passes with its sentence about "every process". Without an env file there
  is nothing that says how every process is set, so such a container is compared with
  nothing; next to an env file the same containers give `value_unreadable`. The same
  holds for the mode, also against the debug information: workers read in part or with
  `EXECUTIONS_MODE` in a Secret are not a disagreement on the mode. Next to a main
  container in regular mode, SCAL-002 passes while SCAL-003, REL-001 and REL-006 "do not
  apply". Next to a text in regular mode and no main container, REL-006 "does not
  apply", and so does REL-001 unless the database type disagrees; SCAL-002 is
  `sources_disagree` (worker replicas against a text in regular mode) and SCAL-003 is
  `not_stated`.
- **SCAL-004 and the debug information.** The limit the text states is compared with the
  limit of the env file only when Kubernetes shows no worker container. An env file with
  a limit of 10, a worker container with a limit of 10 and a text that states 3:
  SCAL-004 passes, on what the worker container says.
- **Several main containers that differ** are read by no rule, as in 0.5.0: the checks
  that read the main process are `not_stated`.
- **Image tags.** For SEC-002 a tag is a version read in two cases only. A tag that
  starts with a full x.y.z, with or without a leading `v` (`2.1.0`, `v2.1.0-exp.0`,
  `1.80.0`), is that version: it can give the pass or hold it back. A tag whose leading
  number is 1 or 0, followed by the end of the tag or by `.`, `-` or `_` (`1`, `1.80`,
  `1.x`, `1.80.x`, `1.`, `v1`, `1-debian`, `1_custom`), is read as a 1.x: it holds the
  pass back and cannot give it. Any other tag is not a version read, and neither gives
  nor holds back: a number alone or x.y (`2`, `42`, `24.04`, `2.1`, which may be a build
  number or a date), `v2-debian`, a word that starts with a digit (`1a2b3c`), a tag where
  the number is not first (`custom-1.80.0`), `latest`, a digest. So a pass on "every n8n
  version read is 2.x or later" always has a full x.y.z behind it. A full x.y.z that is
  a date (`24.11.5`) is read as a version, as in the rounds before. MAINT-003 compares
  full x.y.z tags only: a tag `1` cannot be said to differ from `1.80.0`.

### What the property test covers

`tests/test_sources_property.py` asserts the above, and what follows about `rests_on`, on
snapshots made by a seeded generator and not on listed cases. It generates an env file,
Kubernetes Deployments of the three roles (variables set, not set, in a Secret, given by
`_FILE`, environments read in part, images on n8n 1.x and 2.x, on the tags `1`, `1.80`, `1.x`, `1_custom`, `2` and `42`, and without a version,
replicas, the `--concurrency` flag), a debug information (with and without a cluster
section), the answers of the URL, the n8n version, Redis and Postgres; half of the
snapshots repeat one environment across their sources so that sources also agree.

Its oracle reads the snapshot with its own table of defaults and none of the code it
checks. Which rules read the mode and the database type is a list written by hand in
the test, and for those the oracle asks that the env file, the debug information and
every n8n container state them one way: it does not take from the rules which containers
a variable is compared on. A worker or a webhook processor is left out for the mode only
when it does not set it in an environment read in full. It also asks that SEC-002 does
not pass on a process that leaves `N8N_RUNNERS_ENABLED` unset when any version read in
the run is 1.x or none was read. It reads an image tag, on any n8n container, with a
function of its own: a full x.y.z is a version, a part of a 0.x or 1.x holds the pass
back, any other tag is no version read (see "Known limits"). The test fails when no snapshot has a 1.x stated by an
image tag alone, by a listed instance alone, and by `/metrics` or the text alone. And it
asks that SCAL-004 does not pass on the limit of the env file alone against another limit
in the debug information.

It does not cover:

- a sentence that says more than the rule checked, and a wrong default: the two tables of
  defaults were written by hand and none was checked against n8n, so the same wrong
  value in both is not seen;
- for a variable other than the mode and the database type, a comparison the oracle
  defines as the code does (the roles the rule declares);
- values outside its lists: a version written `2.1.0-exp.0`, a tag that is a date or a
  word that starts with a digit, a `--concurrency` flag in other forms,
  `N8N_RUNNERS_ENABLED` set to a word;
- the choices under "Known limits", which the oracle holds as the code does: without an
  env file a container that cannot be told is compared with nothing, and SCAL-004 is
  compared with the text only when Kubernetes shows no worker;
- several main containers that differ: no rule reads them, as in 0.5.0, and that
  dependence on every variable is not named by the entries;
- a Deployment with two n8n containers, the `source_failed` reason, usage numbers, and
  the facts other than the eight it moves (`queue.waiting_jobs`, `queue.active_jobs`,
  `queue.maxmemory_policy`, `queue.maxmemory_bytes`, `database.max_connections`,
  `license.type`, `worker.replicas`, `n8n.version`);
- a main container in regular mode next to worker Deployments whose containers do not set
  the mode: nothing states queue mode there, and the queue-mode checks still "do not
  apply";
- the page, the withheld score, leaks and the output of 0.5.0, which other tests hold.

## What a check rests on

Each rule lists the profile entries that hold what it read: fact ids, and settings by the
name of the variable. In a profile they are written as ids only (`rests_on`), the same ids
`--withhold` takes; the values are the ones already in the file. An entry is listed only
when the rule and the entry read the same thing: a fact is named only when the file
holds a value for it, so a fact that only a debug information fills
(`security.secure_cookie`, `security.file_access_blocked`, `main.concurrency_limit`) is
not named when the rule read the environment. What a rule reads that no entry of the
profile holds (connections in use, memory in use, image tags, the readiness answer, the
probes and resources of webhook containers) is not listed: for those, the values are in
the report.

A setting is named in every scope the rule read it in: `instance` with an env file, and
the Kubernetes roles given below. Next to an env file those are the containers the file
is compared with (see "When two sources disagree"), so a withheld `setting:main:…` or
`setting:worker:…` names the checks whose outcome that comparison decided, and a pass
that says the container agrees. The execution mode and the database type are named for
every role (`main`, `worker`, `webhook`), since they are compared across every container;
the lists below write that as "every scope". A variable that is not set has no entry.

Every outcome carries the list, whatever it is: a finding, a pass, a rule that does not
apply, and a rule that could not decide. A rule that lacks a source read values on the way
there: Redis was not given, and REL-002 is "not assessed" instead of "not applicable"
because the mode is not regular, so the entry names `executions.mode` and
`EXECUTIONS_MODE`, whether or not they are in the rule's own list (REL-002, REL-003 and
REL-004 read the mode only when Redis was not given). So a withheld fact or setting names
every check that tells something about it. Only a rule that failed (`rule_error`) read
nothing and rests on nothing, and a rule none of whose sources was given has no entry of
the file to name.

## Every rule

| Rule | Area | What it checks |
|---|---|---|
| [SCAL-001](#scal-001) | scalability | Limit on concurrent production executions in regular mode |
| [SCAL-002](#scal-002) | scalability | Number of queue-mode workers |
| [SCAL-003](#scal-003) | scalability | Where manual executions run in queue mode |
| [SCAL-004](#scal-004) | scalability | Worker concurrency |
| [SCAL-005](#scal-005) | scalability | Postgres connections in use |
| [REL-001](#rel-001) | reliability | Database used in queue mode |
| [REL-002](#rel-002) | reliability | Redis eviction policy |
| [REL-003](#rel-003) | reliability | Redis memory in use against its limit |
| [REL-004](#rel-004) | reliability | Execution queue backlog |
| [REL-005](#rel-005) | reliability | Readiness of the instance |
| [REL-006](#rel-006) | reliability | Multi-main setup in queue mode |
| [REL-007](#rel-007) | reliability | Liveness and readiness probes of the n8n containers |
| [REL-008](#rel-008) | reliability | Resource requests and memory limit of the n8n containers |
| [REL-009](#rel-009) | reliability | Cluster checks that n8n runs itself |
| [PERF-001](#perf-001) | performance | Pruning of execution data |
| [PERF-002](#perf-002) | performance | Saving of execution progress after every node |
| [PERF-003](#perf-003) | performance | Where binary data is kept |
| [MAINT-001](#maint-001) | maintainability | Prometheus metrics endpoint |
| [MAINT-002](#maint-002) | maintainability | Tag of the n8n image |
| [MAINT-003](#maint-003) | maintainability | n8n version of every process |
| [MAINT-004](#maint-004) | maintainability | Kind of license |
| [SEC-001](#sec-001) | security | Session cookie restricted to HTTPS |
| [SEC-002](#sec-002) | security | Task runners for the Code node |
| [SEC-003](#sec-003) | security | Access to n8n's own files from the nodes |

## Scalability

### SCAL-001

- **Subject** (written when the rule does not apply or could not decide): Limit on concurrent production executions in regular mode
- **Reads:** The execution mode, the database type and `N8N_CONCURRENCY_PRODUCTION_LIMIT` of the main process: env file, else the main container in Kubernetes, else the debug information.
- **Passed when:** Regular mode, on an install that looks like production (Postgres), with a limit above 0 that was read.
- **Passed text:** Regular mode has a limit on concurrent production executions.
- **Not applicable when:** Queue mode (unless the limit is 0, which is a finding in any mode). A stock install, regular mode not on Postgres: no limit is the documented default there and the rule stays silent on purpose.
- **Not assessed when:** None of the three sources was given; a value is in a Secret or a file, or the environment was read in part; the debug information does not state the mode or the database.
- **Rests on:** `executions.mode`, `database.engine`, `main.concurrency_limit`, `EXECUTIONS_MODE` (every scope), `DB_TYPE` (every scope), `N8N_CONCURRENCY_PRODUCTION_LIMIT` (instance, main)

**Finding, variant 1**

- Title: Invalid production concurrency limit
- Impact: n8n raises an error when it sets up concurrency control with a limit of 0, so the setting does not limit anything and can stop the instance starting.
- Remedy: Set N8N_CONCURRENCY_PRODUCTION_LIMIT to a positive number (for example 20) to cap concurrent production executions, or to -1 to disable the limit.

**Finding, variant 2**

- Title: Regular mode with no limit on concurrent production executions
- Impact: In regular mode every execution runs inside the single main process, so a burst of webhooks or triggers can saturate its event loop and make the editor, API and webhooks unresponsive.
- Remedy: Set N8N_CONCURRENCY_PRODUCTION_LIMIT (for example 20) so excess production executions queue instead of running all at once. To scale beyond one process, move to queue mode: EXECUTIONS_MODE=queue with Redis and worker processes.

### SCAL-002

- **Subject** (written when the rule does not apply or could not decide): Number of queue-mode workers
- **Reads:** The replicas of the worker Deployments in Kubernetes; when Kubernetes shows no worker, the worker instances and the execution mode of the debug information. The execution mode of the env file, to see that it does not say otherwise.
- **Passed when:** Kubernetes shows worker Deployments that ask for 2 replicas or more in total (what they ask for, not what is running); or the debug information lists 2 workers or more and the mode is known to be queue.
- **Passed text:** There are at least 2 workers: replicas that the worker Deployments ask for, or worker instances that n8n lists as registered.
- **Not applicable when:** The mode is known to be regular and Kubernetes shows no worker.
- **Not assessed when:** Neither Kubernetes nor a debug information was given; the workers are scaled to zero (they may run elsewhere, so nothing is said); no worker is listed; the mode cannot be told. Sources disagree: Kubernetes asks for 2 replicas or more, and the debug information lists 1 worker, or the env file (set to regular, or leaving the mode to its default) or the debug information says the instance is in regular mode, where no worker runs. With 1 replica the finding is raised as before, whatever the mode.
- **Rests on:** `worker.replicas`, `executions.mode`, `EXECUTIONS_MODE` (every scope)

**Finding**

- Title: Only one queue-mode worker
- Impact: A single worker runs every production execution, so its restart, eviction or an out-of-memory kill pauses all workflows, and throughput is capped at one process.
- Remedy: Run at least 2 worker replicas (Helm chart: queueMode.workerReplicaCount), and scale on queue depth if load varies.

### SCAL-003

- **Subject** (written when the rule does not apply or could not decide): Where manual executions run in queue mode
- **Reads:** `EXECUTIONS_MODE` and `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS` of the main process: env file, else the main container in Kubernetes.
- **Passed when:** Queue mode, with `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS` read as true.
- **Passed text:** Manual executions are set to be offloaded to the workers (OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS is true).
- **Not applicable when:** Regular mode.
- **Not assessed when:** No env file and no Kubernetes; Kubernetes has no main container or mains that disagree; a value is in a Secret or a file, or the environment was read in part.
- **Rests on:** `executions.mode`, `EXECUTIONS_MODE` (every scope), `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS` (instance, main)

**Finding**

- Title: Manual executions run on the main process
- Impact: Executions started from the editor run inside the main process, so one heavy manual test run competes with the UI, the API, webhook intake and triggers for the same CPU and memory.
- Remedy: Set OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true on the main process so manual executions go through the queue like production ones. n8n does not recommend running production with offloading disabled.

### SCAL-004

- **Subject** (written when the rule does not apply or could not decide): Worker concurrency
- **Reads:** `N8N_CONCURRENCY_PRODUCTION_LIMIT` of an env file in queue mode; else, for each worker container in Kubernetes, that variable and the `--concurrency` flag.
- **Passed when:** The limit of the env file, or the limit or the flag of every worker that was read, is 5 or higher; by default when a worker sets neither (the worker default is 10).
- **Passed text:** The concurrency set for every worker that was read is 5 or higher.
- **Not applicable when:** The mode is known to be regular and there is no worker to read.
- **Not assessed when:** No env file and no Kubernetes; an env file in queue mode without a limit and no Kubernetes (the flag is only visible there); Kubernetes shows no worker; a limit in a Secret or a file; a flag that is not a plain number. Sources disagree: the env file gives a limit of 5 or higher and a worker container of Kubernetes states another limit (its own value, or no limit when it does not set the variable), or is set below 5 by its flag; the env file gives a limit, Kubernetes shows no worker, and the debug information states another limit (with workers in Kubernetes the check rests on them, and the text, which describes the main process, is not compared); the mode is stated in two ways. A worker container read in part next to an env file: `value_unreadable`.
- **Rests on:** `worker.concurrency`, `executions.mode`, `EXECUTIONS_MODE` (every scope), `N8N_CONCURRENCY_PRODUCTION_LIMIT` (instance, worker)

**Finding**

- Title: Worker concurrency below n8n's recommended minimum
- Impact: Reaching a given throughput with low-concurrency workers takes many more worker processes, each holding its own database connections, which can exhaust the Postgres connection pool and cause delays and failed executions.
- Remedy: Raise worker concurrency to 5 or higher and run fewer, larger workers. It is set by the worker's --concurrency flag (Helm chart: queueMode.workerConcurrency; default 10), unless N8N_CONCURRENCY_PRODUCTION_LIMIT is set, which overrides the flag.

### SCAL-005

- **Subject** (written when the rule does not apply or could not decide): Postgres connections in use
- **Reads:** The connections in use and `max_connections` of Postgres.
- **Passed when:** The connections in use are below 80% of `max_connections`.
- **Passed text:** Postgres connections in use are below 80% of max_connections.
- **Not applicable when:** Never.
- **Not assessed when:** Postgres was not given, or did not report one of the two numbers.
- **Rests on:** `database.max_connections`

**Finding**

- Title: Postgres connections close to max_connections
- Impact: Once max_connections is reached Postgres rejects new connections, so adding a worker or doing a rolling restart makes n8n processes fail to start or executions fail to save.
- Remedy: Budget connections as DB_POSTGRESDB_POOL_SIZE (default 2) times the number of n8n processes (main, workers, webhook processors) and check what else shares the server. Then either raise Postgres max_connections, or lower DB_POSTGRESDB_POOL_SIZE / run fewer workers with higher concurrency.

## Reliability

### REL-001

- **Subject** (written when the rule does not apply or could not decide): Database used in queue mode
- **Reads:** The execution mode and the database type of the main process: env file, else the main container in Kubernetes, else the debug information. Both on every other n8n container too, to see that none states otherwise before the sentence says "every process".
- **Passed when:** Queue mode, with a database type read as `postgresdb`, `mysqldb` or `mariadb`.
- **Passed text:** Queue mode runs on a database that every process can share, not on SQLite.
- **Not applicable when:** Regular mode.
- **Not assessed when:** None of the three sources was given; the mode or the database type is in a Secret or a file, was read in part, or is a word n8n does not know. Sources disagree: the env file, the debug information or an n8n container of any role states another mode or another database type.
- **Rests on:** `executions.mode`, `database.engine`, `EXECUTIONS_MODE` (every scope), `DB_TYPE` (every scope)

**Finding**

- Title: Queue mode on SQLite
- Impact: SQLite in queue mode is unsupported by n8n: main and workers must all use one database, which a local SQLite file cannot provide (for example, workers on another host cannot open it at all), so executions fail or are lost.
- Remedy: Move to PostgreSQL: set DB_TYPE=postgresdb with DB_POSTGRESDB_HOST, DB_POSTGRESDB_DATABASE, DB_POSTGRESDB_USER and DB_POSTGRESDB_PASSWORD on the main process and every worker, migrating data with the n8n export/import CLI.

### REL-002

- **Subject** (written when the rule does not apply or could not decide): Redis eviction policy
- **Reads:** The `maxmemory-policy` and `maxmemory` of Redis.
- **Passed when:** The policy is `noeviction`, or `maxmemory` is 0 (no limit, so nothing is evicted).
- **Passed text:** Redis does not evict queue keys: its policy is noeviction, or it has no memory limit.
- **Not applicable when:** Redis was not given and the mode is known to be regular.
- **Not assessed when:** Redis was not given; Redis did not report the policy or the limit.
- **Rests on:** `queue.maxmemory_policy`, `queue.maxmemory_bytes`; without Redis, `executions.mode` and `EXECUTIONS_MODE` (every scope), which then decide between "not applicable" and "not assessed"

**Finding**

- Title: Redis may evict queue keys under memory pressure
- Impact: When Redis reaches maxmemory it silently deletes keys, which can include queued jobs and job locks, so executions disappear from the queue or are marked as crashed with no error pointing at Redis.
- Remedy: Set maxmemory-policy noeviction on the Redis used by n8n (CONFIG SET maxmemory-policy noeviction, or the parameter group on a managed service, whose default is often volatile-lru), so a full Redis fails writes visibly instead.

### REL-003

- **Subject** (written when the rule does not apply or could not decide): Redis memory in use against its limit
- **Reads:** The memory in use, `maxmemory` and `maxmemory-policy` of Redis.
- **Passed when:** The memory in use is below 80% of `maxmemory`.
- **Passed text:** Redis memory in use is below 80% of maxmemory.
- **Not applicable when:** `maxmemory` is 0, so there is no limit to come close to; or Redis was not given and the mode is known to be regular.
- **Not assessed when:** Redis was not given; Redis did not report the memory in use or the limit.
- **Rests on:** `queue.maxmemory_bytes`, `queue.maxmemory_policy`; without Redis, `executions.mode` and `EXECUTIONS_MODE` (every scope), which then decide between "not applicable" and "not assessed"

**Finding, variant 1**

- Title: Redis memory close to maxmemory
- Impact: At maxmemory, with this policy, Redis rejects new writes, so n8n can no longer enqueue executions or renew job locks.
- Remedy: Raise maxmemory (or the node size of a managed Redis), sizing it for the queue and n8n's cache together, and do not share this Redis with other applications. If large webhook responses are the cause, lower N8N_WEBHOOK_RESPONSE_RELAY_SIZE_MAX or offload them with N8N_WEBHOOK_RESPONSE_RELAY_OFFLOAD_ENABLED=true.

**Finding, variant 2**

- Title: Redis memory close to maxmemory
- Impact: At maxmemory, with this policy, Redis evicts keys, which can include queued jobs and job locks.
- Remedy: Raise maxmemory (or the node size of a managed Redis), sizing it for the queue and n8n's cache together, and do not share this Redis with other applications. If large webhook responses are the cause, lower N8N_WEBHOOK_RESPONSE_RELAY_SIZE_MAX or offload them with N8N_WEBHOOK_RESPONSE_RELAY_OFFLOAD_ENABLED=true.

### REL-004

- **Subject** (written when the rule does not apply or could not decide): Execution queue backlog
- **Reads:** The waiting and active jobs of the Bull queue in Redis.
- **Passed when:** Fewer than 100 jobs wait, and not 10 or more with none active. Nine jobs waiting with none active pass: the sentence says only this.
- **Passed text:** Fewer than 100 jobs wait in the queue, and it is not the case that 10 or more wait while none is active.
- **Not applicable when:** Redis was not given and the mode is known to be regular.
- **Not assessed when:** Redis was not given; the queue key was not found in Redis.
- **Rests on:** `queue.waiting_jobs`, `queue.active_jobs`; without Redis, `executions.mode` and `EXECUTIONS_MODE` (every scope), which then decide between "not applicable" and "not assessed"

**Finding, variant 1**

- Title: Queued executions are not being picked up
- Impact: No worker is processing the queue, so production executions (webhooks, schedules, triggers) are accepted but never run.
- Remedy: Check that worker processes (`n8n worker`) are running and healthy, that they use the same QUEUE_BULL_REDIS_HOST, QUEUE_BULL_REDIS_PORT, QUEUE_BULL_REDIS_DB and QUEUE_BULL_PREFIX as the main process, and that EXECUTIONS_MODE=queue is set on them.

**Finding, variant 2**

- Title: Execution queue backlog
- Impact: Workers are saturated, so every new execution waits behind the backlog and webhook callers and scheduled workflows see growing delays or timeouts.
- Remedy: Add worker replicas, or raise worker concurrency (--concurrency flag or N8N_CONCURRENCY_PRODUCTION_LIMIT) if workers have spare CPU and memory. To track the backlog over time, set N8N_METRICS_INCLUDE_QUEUE_METRICS=true (n8n_scaling_mode_queue_jobs_waiting); n8n does not support that setting in a multi-main setup, where the Bull queue length in Redis is the signal to watch and autoscale on.

### REL-005

- **Subject** (written when the rule does not apply or could not decide): Readiness of the instance
- **Reads:** The answer of `/healthz/readiness`.
- **Passed when:** The readiness endpoint answered that the instance is ready.
- **Passed text:** The readiness check answers: the database is connected and migrated.
- **Not applicable when:** Never.
- **Not assessed when:** The n8n URL was not given; the endpoint gave no answer that could be read.
- **Rests on:** nothing the profile holds; the values are in the report.

**Finding**

- Title: Readiness check is failing
- Impact: /healthz/readiness only returns 200 when the database is connected and migrated, so the instance cannot currently load or save workflows and executions.
- Remedy: Check the n8n logs for database connection or migration errors and verify the DB_POSTGRESDB_* settings and that Postgres is reachable from the n8n process. If N8N_ENDPOINT_HEALTH was changed, probe that path instead of /healthz.

### REL-006

- **Subject** (written when the rule does not apply or could not decide): Multi-main setup in queue mode
- **Reads:** The execution mode and `N8N_MULTI_MAIN_SETUP_ENABLED` of the main process: env file, else the main container in Kubernetes, else the debug information.
- **Passed when:** Queue mode, with the multi-main setup read as enabled. The number of main processes is not read.
- **Passed text:** The multi-main setup is switched on (N8N_MULTI_MAIN_SETUP_ENABLED); how many main processes run is not checked.
- **Not applicable when:** Regular mode.
- **Not assessed when:** None of the three sources was given; a value is in a Secret or a file, was read in part, or is not stated by the debug information.
- **Rests on:** `executions.mode`, `multi_main.enabled`, `EXECUTIONS_MODE` (every scope), `N8N_MULTI_MAIN_SETUP_ENABLED` (instance, main)

**Finding**

- Title: Single main process (no multi-main setup)
- Impact: The one main process is a single point of failure: while it is down or being upgraded there is no editor or API, and schedules, polling triggers and webhooks (unless served by webhook processors) do not fire.
- Remedy: If that downtime is not acceptable, run two or more main processes with N8N_MULTI_MAIN_SETUP_ENABLED=true on each, behind a load balancer with sticky sessions. Multi-main requires an Enterprise licence; without one, plan maintenance windows for upgrades.

### REL-007

- **Subject** (written when the rule does not apply or could not decide): Liveness and readiness probes of the n8n containers
- **Reads:** The liveness and readiness probes of every n8n container in Kubernetes (main, worker, webhook).
- **Passed when:** Every n8n container has both probes.
- **Passed text:** Every n8n container has a liveness and a readiness probe.
- **Not applicable when:** Never.
- **Not assessed when:** Kubernetes was not given, or shows no n8n container.
- **Rests on:** nothing the profile holds; the values are in the report.

**Finding**

- Title: n8n containers without liveness or readiness probes
- Impact: Kubernetes cannot tell a hung n8n process from a healthy one: it is never restarted, and during rollouts traffic reaches pods before their database connection and migrations are ready.
- Remedy: Add an HTTP livenessProbe on /healthz and a readinessProbe on /healthz/readiness (port 5678). Workers only serve these endpoints when QUEUE_HEALTH_CHECK_ACTIVE=true is set (port: QUEUE_HEALTH_CHECK_PORT).

### REL-008

- **Subject** (written when the rule does not apply or could not decide): Resource requests and memory limit of the n8n containers
- **Reads:** The CPU and memory requests and the memory limit of every n8n container in Kubernetes (main, worker, webhook).
- **Passed when:** Every n8n container has the two requests and the memory limit.
- **Passed text:** Every n8n container has CPU and memory requests and a memory limit.
- **Not applicable when:** Never.
- **Not assessed when:** Kubernetes was not given, or shows no n8n container.
- **Rests on:** nothing the profile holds; the values are in the report.

**Finding**

- Title: n8n containers without resource requests or a memory limit
- Impact: Without requests the scheduler can pack n8n onto an overcommitted node, where it is among the first pods evicted under memory pressure; without a memory limit one runaway execution can starve every other pod on that node.
- Remedy: Set resources.requests.cpu, resources.requests.memory and resources.limits.memory on every n8n container (the official Helm chart defaults main and worker pods to requests of 200m CPU / 512Mi and a 1Gi memory limit), then size them from observed usage.

### REL-009

- **Subject** (written when the rule does not apply or could not decide): Cluster checks that n8n runs itself
- **Reads:** The cluster checks listed in the debug information.
- **Passed when:** The debug information lists the cluster checks and none failed.
- **Passed text:** None of the cluster checks that n8n runs itself failed.
- **Not applicable when:** Never.
- **Not assessed when:** The debug information was not given, has no cluster section, or lists no check.
- **Rests on:** `cluster.checks_failed`

**Finding**

- Title: A cluster check failed
- Impact: n8n runs these checks on the processes that registered with the instance. A failed one means they do not form one consistent cluster: for example two processes sharing a host id (hostid-clash), more than one leader (split-brain) or processes on different versions (version-mismatch). Scheduled work can then run twice or not at all.
- Remedy: Open Help > About n8n > Debug information on the instance again to see whether the check still fails, then read the logs of every main, worker and webhook process around that time. Give every process its own host id and the same n8n version, and make sure only one main is the leader.

## Performance

### PERF-001

- **Subject** (written when the rule does not apply or could not decide): Pruning of execution data
- **Reads:** `EXECUTIONS_DATA_PRUNE` of the main process: env file, else the main container in Kubernetes, else the debug information. With Postgres, the evidence adds the size of the execution table.
- **Passed when:** Pruning read as enabled; by default when the variable is not set in an environment read in full.
- **Passed text:** Execution data pruning is enabled.
- **Not applicable when:** Never.
- **Not assessed when:** None of the three sources was given; the value is in a Secret or a file, was read in part, or is not a boolean.
- **Rests on:** `pruning.enabled`, `database.execution_count`, `database.execution_table_bytes`, `database.oldest_execution_age_days`, `EXECUTIONS_DATA_PRUNE` (instance, main)

**Finding**

- Title: Execution data pruning is disabled
- Impact: Execution history and its binary data are never deleted, so the database grows without bound until queries and the executions list slow down and the disk fills.
- Remedy: Set EXECUTIONS_DATA_PRUNE=true and choose retention with EXECUTIONS_DATA_MAX_AGE (hours, default 336) and EXECUTIONS_DATA_PRUNE_MAX_COUNT (default 10000). The first pruning passes on a large table take time; run them off-peak.

### PERF-002

- **Subject** (written when the rule does not apply or could not decide): Saving of execution progress after every node
- **Reads:** `EXECUTIONS_DATA_SAVE_ON_PROGRESS`: of the env file; else of the main container in regular mode and of every worker container in queue mode; in regular mode, else the debug information.
- **Passed when:** Read as false on every process judged; by default when it is not set there. This is the instance-wide setting: the setting of a single workflow is not read.
- **Passed text:** Saving execution progress after every node is off instance-wide (EXECUTIONS_DATA_SAVE_ON_PROGRESS); a workflow can still turn it on for itself.
- **Not applicable when:** Never.
- **Not assessed when:** No source was given; queue mode with no worker in Kubernetes; queue mode known from the debug information alone (the text describes the main process, not the workers); a value in a Secret or a file, or read in part.
- **Rests on:** `executions.mode`, `EXECUTIONS_MODE` (every scope), `EXECUTIONS_DATA_SAVE_ON_PROGRESS` (instance, main, worker)

**Finding**

- Title: Execution progress is saved after every node
- Impact: n8n writes the full execution data to the database after each node instead of once at the end, so every execution costs one database write per node and throughput drops as workflows and payloads grow.
- Remedy: Set EXECUTIONS_DATA_SAVE_ON_PROGRESS=false instance-wide, and turn on 'Save execution progress' in the workflow settings of the few workflows that need to resume from the failed node.

### PERF-003

- **Subject** (written when the rule does not apply or could not decide): Where binary data is kept
- **Reads:** `N8N_DEFAULT_BINARY_DATA_MODE` and `EXECUTIONS_MODE` of the main process: env file, else the main container in Kubernetes.
- **Passed when:** The mode is read as `filesystem`, `s3`, `azure` or `database`. Whether every process can reach that storage (in queue mode, a volume shared by main, workers and webhook processors) is not read.
- **Passed text:** N8N_DEFAULT_BINARY_DATA_MODE is set to one of filesystem, s3, azure, database, not to the in-memory mode; whether every process can reach that storage is not checked.
- **Not applicable when:** Never.
- **Not assessed when:** No env file and no Kubernetes; the variable is not set (the mode n8n then uses depends on its version and on the execution mode, so nothing is said); the value is in a Secret or a file, or was read in part. Sources disagree: the variable names a mode and the debug information states another one as the binary mode in effect (`memory` there is `default` here).
- **Rests on:** `binary_data.mode`, `executions.mode`, `N8N_DEFAULT_BINARY_DATA_MODE` (instance, main), `EXECUTIONS_MODE` (every scope)

**Finding, variant 1**

- Title: Binary data is kept in memory
- Impact: Every file a workflow handles is held in the n8n process memory, so large or concurrent files cause out-of-memory crashes that take down all running executions in that process.
- Remedy: Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem (on a persistent volume) if this instance runs in regular mode. In queue mode use database, or s3 / azure if your licence includes external storage; filesystem only works there when main, workers and webhook processors mount the same shared volume.

**Finding, variant 2**

- Title: Binary data is kept in memory
- Impact: Every file a workflow handles is held in the n8n process memory, so large or concurrent files cause out-of-memory crashes that take down all running executions in that process.
- Remedy: Set N8N_DEFAULT_BINARY_DATA_MODE=database, or s3 / azure if your licence includes external storage (preferred in queue mode, as database mode grows Postgres). filesystem only works in queue mode when main, workers and webhook processors mount the same shared volume.

**Finding, variant 3**

- Title: Binary data is kept in memory
- Impact: Every file a workflow handles is held in the n8n process memory, so large or concurrent files cause out-of-memory crashes that take down all running executions in that process.
- Remedy: Set N8N_DEFAULT_BINARY_DATA_MODE=filesystem so files are written to N8N_BINARY_DATA_STORAGE_PATH on a persistent volume instead of held in memory.

## Maintainability

### MAINT-001

- **Subject** (written when the rule does not apply or could not decide): Prometheus metrics endpoint
- **Reads:** `N8N_METRICS` of the main process (env file, else the main container in Kubernetes) and whether `/metrics` answered with samples.
- **Passed when:** `/metrics` was scraped and no environment says otherwise, or `N8N_METRICS` is read as true and the URL did not answer 404 on `/metrics`.
- **Passed text:** The Prometheus metrics endpoint is enabled.
- **Not applicable when:** Never.
- **Not assessed when:** No env file and no Kubernetes (the rule raises its finding on the variable, so a URL whose `/metrics` does not answer is not enough); the value is in a Secret or a file, or was read in part. Sources disagree: `N8N_METRICS` is true and the URL answered 404 on `/metrics`; or `/metrics` was scraped and the environment does not have `N8N_METRICS` as true (no finding is raised then, as before).
- **Rests on:** `N8N_METRICS` (instance, main), `EXECUTIONS_MODE` (every scope), `N8N_MULTI_MAIN_SETUP_ENABLED` (instance, main)

**Finding, variant 1**

- Title: Prometheus metrics endpoint is disabled
- Impact: There is no /metrics endpoint to scrape, so memory growth, event-loop lag and queue depth are invisible until users report failed or slow executions.
- Remedy: Set N8N_METRICS=true on the main process and workers and scrape /metrics with Prometheus. Keep /metrics off the public internet.

**Finding, variant 2**

- Title: Prometheus metrics endpoint is disabled
- Impact: There is no /metrics endpoint to scrape, so memory growth, event-loop lag and queue depth are invisible until users report failed or slow executions.
- Remedy: Set N8N_METRICS=true on the main process and workers and scrape /metrics with Prometheus. Also set N8N_METRICS_INCLUDE_QUEUE_METRICS=true on the main process for waiting/active/failed job gauges (not supported in a multi-main setup). Keep /metrics off the public internet.

### MAINT-002

- **Subject** (written when the rule does not apply or could not decide): Tag of the n8n image
- **Reads:** The image tag of every n8n container in Kubernetes.
- **Passed when:** No n8n image is untagged or tagged `latest`, `stable`, `beta`, `next` or `nightly`. Only these five are compared: a tag such as `prod`, `main` or `1` passes, and may move as well.
- **Passed text:** No n8n image is untagged or uses one of the tags latest, stable, beta, next, nightly. Only these are compared: another tag may move as well.
- **Not applicable when:** Never.
- **Not assessed when:** Kubernetes was not given, or shows no n8n container.
- **Rests on:** nothing the profile holds; the values are in the report.

**Finding**

- Title: n8n image uses a mutable tag
- Impact: Any pod restart or scale-out can pull a newer n8n than the one already running, so main and workers end up on different versions and an unplanned upgrade applies database migrations that a rollback of the tag does not undo.
- Remedy: Pin every n8n process to the same specific version, for example n8nio/n8n:<n8n-version> (or an image digest), and change it only as a planned upgrade. Keep the n8nio/runners image on the matching version.

### MAINT-003

- **Subject** (written when the rule does not apply or could not decide): n8n version of every process
- **Reads:** The versions named by the image tags of the n8n containers in Kubernetes; when no tag names a version, the versions of the instances listed in the debug information. The version n8n reports on `/metrics` and the one the debug information states, to see that they are not another one.
- **Passed when:** Every n8n container names a version in its tag and they are one version (the tags, not what is running); or the debug information lists the instances on one version. In both cases no other version was read: from `/metrics`, as the version of the debug information, or among the instances it lists.
- **Passed text:** Every n8n process that was read is on the same version: the image tags in Kubernetes, or the instances that n8n lists. No other version was read for the instance.
- **Not applicable when:** Never.
- **Not assessed when:** Neither source was given; an image is pinned by digest or carries a tag that names no version; the debug information has no cluster section or lists no version. Sources disagree: the images, or the instances n8n lists, are on one version, and `/metrics`, the debug information as its own version, or the instances it lists state another one (compared on the leading x.y.z).
- **Rests on:** `cluster.versions_aligned`, `n8n.version`

**Finding**

- Title: n8n processes run different versions
- Impact: Main, workers and webhook processors share one database, so a process on an older version runs against a schema migrated by a newer one and executions fail in ways that depend on which pod picks them up.
- Remedy: Set the same image tag on every n8n Deployment (main, worker, webhook processor) and update them together as one coordinated change.

### MAINT-004

- **Subject** (written when the rule does not apply or could not decide): Kind of license
- **Reads:** The license reported by the debug information.
- **Passed when:** The debug information reports a license that is not the sandbox one.
- **Passed text:** The license is not a sandbox license.
- **Not applicable when:** Never.
- **Not assessed when:** The debug information was not given, or does not state the license.
- **Rests on:** `license.type`

**Finding**

- Title: Enterprise sandbox license
- Impact: This matters only if this instance serves production: a sandbox license is meant for development and testing, not for production use.
- Remedy: If this is a development or test instance, nothing needs to change. If it serves production, activate the production license key on it instead.

## Security

### SEC-001

- **Subject** (written when the rule does not apply or could not decide): Session cookie restricted to HTTPS
- **Reads:** `N8N_SECURE_COOKIE` of the main process: env file, else the main container in Kubernetes; else the debug information, which reports it only when it is insecure.
- **Passed when:** Read as true; by default when the variable is not set in an environment read in full.
- **Passed text:** The session cookie is restricted to HTTPS.
- **Not applicable when:** Never.
- **Not assessed when:** No env file and no Kubernetes (a debug information that does not flag the setting proves nothing, so it is never a pass); the value is in a Secret or a file, or was read in part. Sources disagree: a probe states the variable as true and the debug information flags the setting.
- **Rests on:** `security.secure_cookie`, `N8N_SECURE_COOKIE` (instance, main)

**Finding**

- Title: Session cookie is not restricted to HTTPS
- Impact: The browser sends the n8n session cookie over plain HTTP, so anyone on the network path can capture it and act as that user, including the instance owner.
- Remedy: Serve n8n over HTTPS (terminate TLS at a reverse proxy or load balancer and set N8N_PROXY_HOPS accordingly), then remove N8N_SECURE_COOKIE=false so the default of true applies.

### SEC-002

- **Subject** (written when the rule does not apply or could not decide): Task runners for the Code node
- **Reads:** `N8N_RUNNERS_MODE` and `N8N_RUNNERS_ENABLED` of every process that runs Code nodes: the env file; else, in Kubernetes, the main container (unless it offloads manual executions) and every worker container. When `N8N_RUNNERS_ENABLED` is not set, every n8n version read in the run: from `/metrics`, from the debug information and the instances it lists, and from the image tag of each n8n container, whatever its role: a full x.y.z is that version, a tag that names a part of a 0.x or 1.x (`1`, `1.80`, `1-debian`) is read as 1.x and can only hold the pass back, any other tag (`2`, `42`, `latest`) is not a version read. `DB_TYPE` and the mode for the severity of the finding.
- **Passed when:** `N8N_RUNNERS_MODE` read as `external` on every process judged, and `N8N_RUNNERS_ENABLED` there either read as true, or not set while every n8n version read is 2.x or later (the first source that answers does not decide for the others: the process judged may be another one than the one that reported its version). Never by default: the default is `internal`. The pass says what the variables are, not that the runners run. The rule's own remedy says that n8n 1.x also needs `N8N_RUNNERS_ENABLED=true`, which is why a variable that is not set does not pass on 1.x, nor when the version is not known. Nobody checked n8n itself for its default on 1.x, nor whether n8n 2.x still reads the variable: `false` never passes, on any version.
- **Passed text:** N8N_RUNNERS_MODE is external on every process that was read and runs Code nodes, and N8N_RUNNERS_ENABLED is not set to false there: it is set to true, or it is not set and every n8n version read is 2.x or later.
- **Not applicable when:** Never.
- **Not assessed when:** No env file and no Kubernetes; the mode, the offloading or the runners mode is in a Secret or a file, or was read in part; queue mode with no process to judge; `external` with `N8N_RUNNERS_ENABLED` false or set to a word n8n does not know, or not set while every version read is 1.x or no version was read (not stated), or possibly set where it could not be read. Sources disagree: not set, and the versions read are on both sides of 2.0.
- **Rests on:** `executions.mode`, `n8n.version`, `N8N_RUNNERS_MODE` (instance, main, worker), `N8N_RUNNERS_ENABLED` (instance, main, worker), `EXECUTIONS_MODE` (every scope), `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS` (instance, main, worker), `DB_TYPE` (every scope)

**Finding**

- Title: Code node runs without external task runners
- Impact: Code node scripts run in a child process of n8n (or, on n8n 1.x with runners disabled, inside n8n itself) as the same user on the same host, so code that escapes the sandbox can read the encryption key, stored credentials and environment variables.
- Remedy: Set N8N_RUNNERS_MODE=external and N8N_RUNNERS_AUTH_TOKEN, and run the n8nio/runners image (same version as n8n) as a sidecar next to every worker, and next to main unless OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true. On n8n 1.x also set N8N_RUNNERS_ENABLED=true. Internal mode is deprecated from n8n 3.0.

### SEC-003

- **Subject** (written when the rule does not apply or could not decide): Access to n8n's own files from the nodes
- **Reads:** What the debug information flags as insecure, then `N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES` of the main process, which wins when a probe states it.
- **Passed when:** A probe (env file or Kubernetes) states the variable as true and no debug information flags the setting. Never by default, and never from a debug information that merely does not flag it.
- **Passed text:** Access to n8n's own files is set to be blocked.
- **Not applicable when:** Never.
- **Not assessed when:** No probe states the variable as true, and the debug information was not given, or was given and does not flag the setting; it flags it and the variable is in a Secret. Sources disagree: it flags it and a probe states the variable as true.
- **Rests on:** `security.file_access_blocked`, `N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES` (instance, main)

**Finding**

- Title: Access to n8n's own files is not blocked
- Impact: Nodes that read files from disk can open n8n's own directory and configuration files, which hold the instance's encryption key, so anyone who can build a workflow can read the key that protects every stored credential.
- Remedy: Remove N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=false so the default of true applies. If workflows need files on disk, keep them outside n8n's directory and limit the nodes to that place with N8N_RESTRICT_FILE_ACCESS_TO.
