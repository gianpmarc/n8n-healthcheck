# What n8n-healthcheck does

This page is for someone who has to decide whether to allow this tool to run against
an n8n instance. It says what the tool reads, what it computes, what it writes, and
what it never does. The last section names the test that holds each statement, and how
to run them.

It does not repeat what other pages already say in full: the rules and every sentence
they can write are in [rules.md](rules.md), and the file format is in
[profile-format.md](profile-format.md).

## What it is

n8n-healthcheck is a small program, written in Python with the standard library only,
that you start yourself on a machine of yours. It inspects a self-hosted n8n instance
and produces two things:

- a **report**, in Markdown, of what will break under load and how to fix it;
- an **environment profile**, one JSON file of facts about the instance, which you
  review and send yourself to the person who asked you for it.

It is read-only. It runs where you start it and has no service of its own on the
internet: there is no account, no upload and no telemetry. Not an official n8n project.

You can use it from the command line (`python3 -m n8n_healthcheck`) or through a local
web page (`python3 -m n8n_healthcheck serve`, or the Docker image). The page is a guide
in six steps; it does the same work as the command line.

## What you give it, what it asks, what it keeps

Every source except the debug information is optional. A source you do not give is
reported as not assessed, never guessed.

### Debug information (required for a profile)

**You give:** the text n8n shows under Help > About n8n > Debug information > Copy.

**What is asked of the instance:** nothing. The text is read where the tool runs.

**What is kept:** the n8n and Node.js versions, the platform, the database type, the
execution mode, the concurrency limit, the licence type, what is saved of executions,
the binary data mode, the pruning settings, two security flags, and from the `cluster`
section the number of instances by type, their versions and the names of failed checks.
Each value is read by a strict reader that accepts a known word, a whole number, a
boolean or a version and drops anything else. The instance id, the host ids and the
browser's user agent in the text are not taken from it: nothing of them is kept. A text larger than 64 KB, one that is
not a debug information, or one that holds more than one, is refused with a fixed
sentence that quotes nothing of it.

The profile needs it because it is what states the n8n version. Without it no profile
is written: the page does not open the later steps, the server answers a run with the
report and no profile, and `--save-profile` without `--debug-info` ends with exit code 3
before anything is collected. The report alone needs none.

### n8n URL

**You give:** the base URL of the instance.

**What is asked:** three `GET` requests without a body and without credentials:
`/healthz`, `/healthz/readiness` and `/metrics`. Each waits 5 seconds at most.

**What is kept:** whether the two health endpoints answered 200, the metrics the
instance publishes, and the n8n version read from them.

These three requests use the proxy settings of the machine, if it has any, and follow a
redirect to another `http` or `https` address; a redirect to anything else is refused.

### n8n API key (optional, with the URL)

**You give:** an API key, in a file on the command line or in a masked field on the page.

**What is asked:** `GET` requests only, to the instance of that URL:
`/api/v1/insights/summary` (or `/api/v1/executions` where n8n does not have it),
`/api/v1/workflows?active=true`, `/api/v1/users` and `/api/v1/users/<id>`. At most 130
requests and 60 seconds in all.

**What is kept:** four counts: executions per day, failure rate, active workflows, users.

The key is sent in one header, to that URL only. It never goes through a proxy and never
to another host: a redirect that leaves the URL's origin is not followed. It is held in
memory for the run and is not written to the report, a snapshot, the profile, a log or
an error message.

### Environment file

**You give:** the path of the n8n env file (`KEY=VALUE` lines), or its content pasted on
the page.

**What is asked:** nothing of the instance. The file is opened to be read.

**What is kept:** the variables. The value of a variable whose name holds `PASS`,
`SECRET`, `TOKEN`, `KEY`, `CREDENTIAL` or `CERT`, and any value that embeds credentials
in a URL, is replaced by a marker as it is read, so it never reaches the report, a
snapshot or the profile.

On the page the pasted text is written to a temporary file that only the tool's user can
read, in a private temporary folder, for the length of the run, and removed when the
run ends, whether it succeeded or not.

### Postgres

**You give:** the connection string of the n8n database.

**What is asked:** one `psql` command (`psql -X`, so no start-up file of yours is run)
with one transaction. It starts with `SET LOCAL transaction_read_only = on`, so the
server itself refuses any write, and a statement timeout; then one `SELECT` reads
`server_version`, the row count and size of `execution_entity`, the age of the oldest
execution, the number of client connections in `pg_stat_activity`, and
`max_connections`. No row of your workflows, credentials or execution data is read.

**What is kept:** those six numbers.

The password is taken out of the connection string and passed to `psql` in its
environment, so it does not show in the process list.

### Redis

**You give:** the connection URL of the Redis that holds the n8n queue.

**What is asked:** `AUTH` and `SELECT` when the URL has a password or a database, then
`INFO`, `EXISTS` on the queue's id counter, `LLEN` on the waiting and active lists and
`ZCARD` on the failed set. No key is written, moved or deleted, and no job is read.

**What is kept:** the Redis version, memory figures and policy from `INFO`, and three
counts of jobs.

### Kubernetes

**You give:** the namespace n8n runs in, and optionally a kubectl context. Without one
the current context of `kubectl` is used.

**What is asked:** two commands: `kubectl get deployments` and `kubectl get configmaps`
in that namespace, as JSON. `kubectl` authenticates as your kubeconfig says.

**What is kept:** for each Deployment its name, replicas, and for each container the
image, resources, probes and environment. Secrets are never read: a variable that comes
from a Secret is recorded as set with an unknown value, and no conclusion is drawn from
it. The ConfigMaps are read to resolve variables that point to them.

## What it computes

The tool runs 24 checks on what was collected. Each one ends as a finding, as passed,
as not applicable, or as not assessed with the reason. A check that lacks a source is
not assessed: it is never passed by default. Each area (scalability, reliability,
performance, maintainability, security) gets a score from 0 to 100 computed from its
findings. What each check reads and every sentence it can write: [rules.md](rules.md).

On the page, step 3 shows how many of the 24 checks the debug information decides by
itself and, for each source, at least how many more can be decided with it; step 5
shows what the run decided. These numbers are counted from what the checks stated;
they are not estimates. The number under a source is a lower bound: it counts the checks
that name that source as one they lack, and a source can decide others too.

## What it writes

### The report

A Markdown text: a summary, a score per area, and for each finding the evidence, the
impact and the fix. **The evidence quotes values read from your instance.** The report
is written to the file you name with `-o`, or to the terminal, or shown on the page,
where you can download or copy it. The tool sends it nowhere: it stays with you unless
you send it.

### The environment profile

One JSON file. It holds when it was taken and by which version, which sources were
read, a fixed catalogue of facts (each marked measured, declared, unknown or withheld),
the n8n settings that are set, the scores, and the outcome of every check. `format_version` is 1.

You review it before it leaves your machine. On the page, step 5 lists every fact, every
setting and every check exactly as it will be written, each with a box to withhold it;
on the command line, `--show-profile` prints the file and `--withhold` leaves an entry
out. A withheld entry stays in the file marked withheld, without its value, so the
reader knows it was removed and not unknown.

**One entry cannot be withheld: the n8n version.** It is what every later conversation
needs first. Its row on the page has no box, and `--withhold n8n.version` ends with exit
code 3. Everything else can be withheld, the whole check outcome included.

The tool does not send the file. The page saves it through your browser, and its last
step says to send it yourself, by e-mail or attached to your support ticket.

## What the profile never contains

No secret, host name, URL, IP address, file path, e-mail address, or Deployment,
namespace or registry name.

- A setting's value is written only when n8n documents the variable as a boolean, a
  number or one word of a fixed list. Every other documented variable is listed as
  "set" without its value, and variables n8n does not document are counted, not named.
- Every text of a check is a fixed sentence of its rule, never a value of your instance.
  The evidence of a finding is not in the profile.
- An answer to one of the optional questions is a value from a fixed list, never text
  you type.

**One exception:** the optional one-line reference (`--reference`, or the field in step
5), for example a ticket or change number. It is written to the file as you type it, so
do not put a secret in it.

## What it never does

- **It never writes to the instance, the database, Redis or the cluster.** Every
  request to n8n is a `GET`; the database is read in a transaction the server holds
  read-only; Redis gets commands that read; `kubectl` gets `get`.
- **It never reads a Kubernetes Secret.**
- **It keeps nothing after a run.** The command line writes only the files you name.
  The local server writes nothing to disk except the pasted env file described above,
  logs the method, path and status of a request and nothing of its content, and holds
  the API key and the debug information in memory for the run only. The page stores
  nothing in the browser: no cookie, no local storage; reloading it clears everything.
  Its address never changes and never holds a value you typed.
- **It contacts nothing but what you gave it.** The only modules that can open a
  connection or start a command are the four collectors: the n8n URL (with the proxy
  and redirect behaviour stated above), the n8n API with your key, Redis, and the
  `psql` and `kubectl` commands. A source you did not give is not contacted. The local
  server listens and answers; it never connects out. The page loads its own three files
  from that server and nothing from anywhere else; its one link to an outside address
  is the link to this document.
- **It never sends the report or the profile anywhere.**

What the tool does not control: `psql` and `kubectl` are programs of your machine, and
they connect and authenticate the way their own configuration says (a kubeconfig can
name a credential helper, for example). Names are resolved by your system's resolver.

## The local web page

`serve` listens on `127.0.0.1` by default and warns when told to listen elsewhere. It
has no login, so it checks every request instead: the `Host` header must be a name it
was started with, and a request that carries data must come from its own page. A text
you paste is sent to that local server only.

## How to check these statements

The tests are in [`tests/`](../tests). They need Python 3.10 or later and nothing else;
the tests that drive the page also need Node.js and are skipped, with a message that
says what was then not checked, where it is missing.

```bash
python3 -m unittest discover -s tests
```

`tests/test_claims.py` holds the statements of this page that no earlier test held, and
checks that every test named below exists. The tests pass with or without a proxy configured
on the machine: they reach their own stand-in servers directly.

| Statement | File | Tests |
|---|---|---|
| The n8n URL gets three `GET` requests, no body, no credentials | `test_claims.py` | `test_the_n8n_url_gets_three_get_requests_without_a_body` |
| A redirect to something that is not http(s) is refused | `test_collectors.py` | `test_redirect_to_a_non_http_url_is_not_followed` |
| The API key: `GET` only, one header | `test_usage.py` | `test_every_request_is_a_get_with_the_key_in_one_header` |
| The API key never goes to another host or through a proxy | `test_usage.py` | `test_a_redirect_to_another_host_is_not_followed`, `test_a_proxy_of_the_machine_never_gets_the_key` |
| The API key calls are bounded in number and time | `test_usage.py` | `test_the_request_bound_holds_whatever_the_lists_do`, `test_the_bounds_are_the_ones_of_the_design` |
| The API key is in no output | `test_usage.py` | `test_nothing_of_the_key_or_of_an_answer_reaches_an_output_of_the_command_line`, `test_the_web_interface_takes_the_key_and_lets_nothing_out`, `test_collect_keeps_the_key_out_of_the_snapshot` |
| Postgres: one read-only transaction of `SELECT`s | `test_claims.py` | `test_postgres_gets_one_read_only_transaction_of_selects` |
| The database password is not on the command line | `test_collectors.py` | `test_postgres_password_only_in_environment` |
| Redis: only commands that read | `test_claims.py` | `test_redis_gets_only_commands_that_read` |
| Kubernetes: two `get` commands, never a Secret | `test_claims.py` | `test_kubectl_gets_two_get_commands_and_never_asks_for_a_secret` |
| A variable from a Secret stays unknown | `test_collectors.py` | `test_secrets_and_field_refs_stay_unknown_but_complete` |
| The env file is only read; secret values are redacted | `test_claims.py` | `test_the_env_file_is_opened_to_be_read_and_left_as_it_was` |
| Which names count as secrets | `test_collectors.py` | `test_redaction_allowlist_is_exact` |
| The pasted env text: a private file, removed after the run | `test_web.py` | `test_env_text_goes_through_a_private_file_that_is_removed` |
| The debug information: ids not taken from it, nothing of it in any output | `test_debug_info.py` | `test_cluster_instances_are_counted_by_type_and_nothing_else_is_kept`, `test_no_marker_in_any_output_of_a_run`, `test_no_marker_in_an_error_message` |
| The debug information is never written to disk by the server | `test_web.py` | `test_a_debug_information_is_never_written_to_disk` |
| A refused text is answered with a fixed sentence | `test_web.py` | `test_a_refused_debug_information_is_a_400_with_the_fixed_sentence` |
| No profile without the debug information, on the command line | `test_guided_flow.py` | `test_a_profile_without_the_debug_information_is_refused_before_anything_is_collected`, `test_the_report_alone_needs_no_debug_information` |
| No profile without it, from the server | `test_guided_flow.py` | `test_a_run_without_the_debug_information_answers_with_the_report_and_no_profile` |
| No profile without it, on the page | `test_web_profile.py` | `test_the_debug_information_is_checked_before_step_three_opens`, `test_a_text_edited_after_the_check_closes_the_later_steps_again` |
| Reading the pasted text collects nothing | `test_guided_flow.py` | `test_an_accepted_text_says_what_was_read_and_collects_nothing` |
| The numbers of steps 3 and 5 are counted from the checks | `test_guided_flow.py` | `test_the_numbers_of_the_debug_information_alone_are_what_the_rules_state`, `test_it_is_the_result_of_a_run_given_nothing_else` |
| A run connects only to the hosts given | `test_claims.py` | `test_a_run_with_every_source_connects_to_the_hosts_given_and_starts_psql_and_kubectl`, `test_a_source_that_was_not_given_is_not_contacted` |
| Only the collectors and the local server can connect or start a command | `test_claims.py` | `test_only_the_collectors_and_the_local_server_can_open_a_connection_or_start_a_command` |
| The page stores nothing and asks only its own server | `test_web_profile.py` | `test_page_stores_nothing_and_loads_nothing_else` |
| The page's one outside address is the link to this page | `test_claims.py` | `test_the_readme_and_the_first_step_link_to_the_document` |
| The server logs no content of a request | `test_web.py` | `test_log_has_no_query_string_and_no_traceback`, `test_bad_request_does_not_echo_values` |
| The server checks `Host` and the origin of a request | `test_web.py` | `test_host_header_guard`, `test_origin_must_match_the_host` |
| The server listens on a loopback address by default | `test_web.py` | `test_defaults_open_the_browser_on_loopback`, `test_non_loopback_host_warns_and_no_browser` |
| The profile never contains a secret, host, URL, IP address, path or e-mail address | `test_profile.py` | `test_nothing_seeded_reaches_the_profile`, `test_nothing_seeded_reaches_the_profile_from_kubernetes_alone` |
| Nothing typed on the page reaches the profile | `test_web.py` | `test_profile_holds_nothing_from_the_request_and_no_host_or_secret` |
| A setting's value is written only for a boolean, a number or a fixed word | `test_profile.py` | `test_value_written_only_for_a_boolean_a_number_or_a_fixed_word`, `test_other_documented_variables_are_never_written_whatever_they_hold` |
| The texts of the checks are fixed sentences | `test_call_sheet.py` | `test_every_text_in_checks_is_a_literal_of_the_rules_whatever_the_instance`, `test_no_literal_can_hold_what_a_profile_forbids` |
| The evidence of a finding is not in the profile | `test_profile.py` | `test_check_keeps_the_outcome_without_the_evidence` |
| The reference is written as typed (the one exception) | `test_claims.py` | `test_what_the_file_never_contains_is_the_same_list_everywhere` |
| The n8n version cannot be withheld, on the command line | `test_guided_flow.py` | `test_the_n8n_version_cannot_be_withheld`, `test_every_other_entry_can_still_be_withheld` |
| The n8n version has no box on the page | `test_web_profile.py` | `test_the_n8n_version_has_no_box_and_no_button_withholds_it`, `test_download_saves_exactly_what_is_ticked` |
| A withheld entry stays withheld after a new run | `test_web_profile.py` | `test_a_new_run_keeps_what_the_operator_chose` |
| The page does not send the file | `test_web_profile.py` | `test_step_six_says_how_the_file_travels_and_that_the_page_does_not_send_it` |
