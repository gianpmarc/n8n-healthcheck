"""Usage numbers through an n8n API key: the collector, the facts, the report, the command
line and the web interface, against a fake n8n API served by the standard library.

Everything here is made up: the key, the workflows, the users and the executions. Their
names, e-mails and ids are marker strings, so that a leak into any output is caught.
"""

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import n8n_healthcheck
import test_debug_info as pasted  # a synthetic debug information; imported as a module so its tests run once
import test_web_profile as page_tests  # the page harness; as a module, for the same reason
import validate_0_2_0
from n8n_healthcheck import cli, debug_info, engine, profile, questions, report, usage
from n8n_healthcheck.collectors import api, collect
from n8n_healthcheck.models import Snapshot, Target
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.web import server as web

MARK = "ZZMARKERZZ"
KEY = f"n8n_api_{MARK}_key.made-up"
USAGE_FACTS = (
    "usage.executions_per_day",
    "usage.failure_rate_percent",
    "usage.period_days",
    "usage.method",
    "usage.estimate",
    "usage.active_workflows",
    "usage.active_workflows_partial",
    "usage.users",
)


def ago(**delta) -> str:
    """A date as n8n writes it, that long before now."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def execution(number: int, status="success", **delta) -> dict:
    """A listed execution of n8n 2.x. Its ids are markers."""
    started = ago(**delta)
    return {
        "id": f"{MARK}{number}",
        "workflowId": f"{MARK}workflow{number}",
        "mode": "webhook",
        "finished": status == "success",
        "startedAt": started,
        "stoppedAt": started,
        "waitTill": None,
        "retryOf": None,
        "retrySuccessId": None,
        "status": status,
    }


def spread(count: int, days: float, status="success", first: int = 0) -> list:
    """`count` executions, newest first, evenly over the last `days` days."""
    return [execution(first + i, status, seconds=(i + 1) * days * 86400 / count) for i in range(count)]


def user(number: int, pending=False) -> dict:
    return {
        "id": f"{MARK}-user-{number}",
        "email": f"{MARK}{number}@example.test",
        "firstName": f"{MARK}first",
        "lastName": f"{MARK}last",
        "isPending": pending,
    }


def workflow(number: int) -> dict:
    return {"id": f"{MARK}wf{number}", "name": f"{MARK} workflow", "active": True, "nodes": [{"name": f"{MARK}node"}]}


def summary(total: int, failed: int) -> dict:
    return {
        "total": {"value": total, "deviation": None, "unit": "count"},
        "failed": {"value": failed, "deviation": None, "unit": "count"},
        "failureRate": {"value": failed / total if total else 0, "deviation": None, "unit": "ratio"},
    }


class FakeN8n:
    """A made-up n8n: the health endpoints and the five calls of the public API.

    `insights` is (status, body); `executions`, `users` and `workflows` are a list of
    items, a status that refuses the call, or a function(offset) -> items for a list
    that never ends. `detail` is the status of GET /users/<id>. `odd` maps a path to a
    behaviour: "hang", "drip", "drip_headers", "endless", or ("redirect", url).
    """

    def __init__(self, insights=(404, None), executions=(), users=None, workflows=3, detail=200, odd=None, key=KEY):
        self.insights = insights
        self.lists = {
            "/api/v1/executions": executions,
            "/api/v1/users": [user(1), user(2), user(3, pending=True)] if users is None else users,
            "/api/v1/workflows": [workflow(i) for i in range(workflows)] if isinstance(workflows, int) else workflows,
        }
        self.detail, self.odd, self.key = detail, odd or {}, key
        self.requests = []  # (method, path with query, the key header or None)
        self.stop = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def answer(self, status, body=None, headers=()):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def handle_one(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                fake.requests.append((self.command, self.path, self.headers.get("X-N8N-API-KEY")))
                if self.command != "GET":
                    return self.answer(405, {"message": "made-up: only GET"})
                if parts.path in ("/healthz", "/healthz/readiness"):
                    return self.answer(200, {"status": "ok"})
                if not parts.path.startswith("/api/v1/"):
                    return self.answer(404, {"message": "not found"})
                odd = fake.odd.get(parts.path)
                if odd == "hang":
                    fake.stop.wait(30)
                    return None
                if odd == "drip_headers":
                    for byte in b"HTTP/1.0 200 OK\r\nX-Made-Up: " + b"a" * 100000:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if fake.stop.wait(0.05):
                            break
                    return None
                if odd in ("drip", "endless"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"data": [')
                    while not fake.stop.is_set():
                        self.wfile.write(b" " if odd == "drip" else b" " * 65536)
                        self.wfile.flush()
                        if odd == "drip":
                            fake.stop.wait(0.05)
                    return None
                if isinstance(odd, tuple):
                    return self.answer(302, {}, [("Location", odd[1])])
                if self.headers.get("X-N8N-API-KEY") != fake.key:
                    return self.answer(401, {"message": f"unauthorized {MARK}"})
                if parts.path == "/api/v1/insights/summary":
                    return self.answer(fake.insights[0], fake.insights[1] or {"message": f"not found {MARK}"})
                if parts.path.startswith("/api/v1/users/"):
                    return self.answer(fake.detail, user(1) if fake.detail == 200 else {"message": f"Forbidden {MARK}"})
                items = fake.lists.get(parts.path)
                if items is None:
                    return self.answer(404, {"message": "not found"})
                if isinstance(items, int):
                    return self.answer(items, {"message": f"Forbidden {MARK}"})
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("cursor", ["c0"])[0][1:])
                if callable(items):
                    page, more = items(offset)[:limit], True
                else:
                    page, more = items[offset : offset + limit], offset + limit < len(items)
                return self.answer(200, {"data": page, "nextCursor": f"c{offset + limit}" if more else None})

            def do_GET(self):
                with contextlib.suppress(OSError):
                    self.handle_one()

            do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_GET

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def api_paths(self):
        return [path for _, path, _ in self.requests if path.startswith("/api/")]


class Base(unittest.TestCase):
    def fake(self, **config) -> FakeN8n:
        fake = FakeN8n(**config)
        self.addCleanup(fake.close)
        return fake

    def read(self, fake=None, key=KEY, **config) -> dict:
        """The usage section the collector writes against a fake n8n."""
        self.n8n = fake or self.fake(**config)
        return api.collect(Target(url=self.n8n.url, api_key=key))["usage"]

    def numbers(self, section, **snapshot) -> dict:
        return usage.numbers(Snapshot(usage=section, **snapshot))


def facts_of(snapshot: Snapshot, failed=()) -> dict:
    return {fact["id"]: fact for fact in profile.build_profile(snapshot, [], failed)["facts"]}


class CollectorTest(Base):
    # --- executions -----------------------------------------------------------------

    def test_the_insights_summary_is_one_request_and_no_estimate(self):
        section = self.read(insights=(200, summary(70, 7)), executions=spread(5, 3))
        self.assertEqual(
            section["executions"],
            {"method": "insights", "total": 70, "failed": 7, "period_days": 7, "estimate": False, "estimate_reason": None},
        )
        self.assertNotIn("/api/v1/executions", " ".join(self.n8n.api_paths()))
        found = self.numbers(section)
        self.assertEqual((found["executions_per_day"][0], found["failure_rate_percent"][0]), (10, 10))
        self.assertEqual((found["period_days"][0], found["method"][0], found["estimate"][0]), (7, "insights", False))
        self.assertEqual(section["refused"], {"executions": None, "users": None, "active_workflows": None})

    def test_without_insights_the_list_is_counted(self):
        for status in (404, 403):
            with self.subTest(insights=status):
                # 30 in the last 7 days, 3 of them failed; older ones are not counted.
                listed = sorted(
                    spread(27, 6) + spread(3, 6.5, "error", first=100) + [execution(900 + i, days=8 + i) for i in range(20)],
                    key=lambda item: item["startedAt"],
                    reverse=True,
                )
                section = self.read(insights=(status, None), executions=listed)
                self.assertEqual(
                    section["executions"],
                    {"method": "listed", "total": 30, "failed": 3, "period_days": 7, "estimate": True, "estimate_reason": "saved_only"},
                )
                found = self.numbers(section)
                self.assertEqual(found["executions_per_day"][0], 4.29)
                self.assertEqual(found["failure_rate_percent"][0], 10)
                self.assertEqual((found["method"][0], found["estimate"][0]), ("listed", True))
                self.assertIn("estimate", found["executions_per_day"][1])
                self.assertIn("saved and has not pruned", found["executions_per_day"][1])

    def test_another_status_from_insights_is_not_a_reason_to_count_the_list(self):
        section = self.read(insights=(500, None), executions=spread(5, 3))
        self.assertIsNone(section["executions"])
        self.assertEqual(section["refused"], {"executions": 500, "users": None, "active_workflows": None})
        self.assertEqual((section["users"], section["active_workflows"]), (2, 3))  # the other numbers are still read
        self.assertEqual(self.numbers(section)["executions_per_day"], (None, "the n8n API answered with HTTP status 500"))

    def test_an_answer_that_is_not_the_summary_is_not_read(self):
        for body in ({"total": {"value": -1}, "failed": {"value": 0}}, {"total": {"value": 3}, "failed": {"value": 4}}, {"total": {"value": True}, "failed": {"value": 0}}, {"total": f"{MARK}"}, [1]):
            with self.subTest(body=body):
                section = self.read(insights=(200, body))
                self.assertIsNone(section["executions"])
                self.assertEqual(section["refused"]["executions"], "invalid")

    def test_zero_executions(self):
        found = self.numbers(self.read(insights=(200, summary(0, 0))))
        # Insights lags: its zero is given with that said, not as a bare exact number.
        zero = "n8n's Insights counted no execution in the last 7 days; it can lag behind by up to an hour"
        self.assertEqual(found["executions_per_day"], (0, zero))
        self.assertEqual(found["failure_rate_percent"], (None, zero))
        self.assertEqual(found["period_days"][0], 7)
        self.assertNotEqual(self.numbers(self.read(insights=(200, summary(3, 0))))["executions_per_day"][1], zero)
        # Counted from the list, no execution in the period is what the list says.
        listed = self.numbers(self.read(executions=[execution(1, days=9)]))
        self.assertEqual(listed["executions_per_day"][0], 0)
        self.assertEqual(listed["failure_rate_percent"], (None, "no execution in the period"))

    def test_the_page_bound_ends_the_walk_and_the_period_is_what_was_covered(self):
        # A list that never ends: 250 a page, 20 seconds apart, so 10 000 cover 2.3 days.
        def endless(offset):
            return [execution(offset + i, seconds=(offset + i + 1) * 20) for i in range(api.PAGE_SIZE)]

        section = self.read(executions=endless)
        found = section["executions"]
        self.assertEqual((found["total"], found["estimate_reason"]), (10000, "page_limit"))
        self.assertAlmostEqual(found["period_days"], 10000 * 20 / 86400, delta=0.02)
        self.assertEqual(sum("/executions" in path for path in self.n8n.api_paths()), api.MAX_PAGES)
        numbers = self.numbers(section)
        self.assertAlmostEqual(numbers["executions_per_day"][0], 4320, delta=40)  # not 10 000 / 7
        self.assertIn("up to 10 000", numbers["executions_per_day"][1])

    def test_a_list_that_ends_before_seven_days(self):
        section = self.read(executions=spread(40, 2))
        found = section["executions"]
        self.assertEqual((found["total"], found["estimate_reason"]), (40, "short_history"))
        self.assertAlmostEqual(found["period_days"], 2, delta=0.02)
        self.assertAlmostEqual(self.numbers(section)["executions_per_day"][0], 20, delta=0.3)

    def test_less_than_one_day_is_no_rate(self):
        for listed in (spread(50, 0.5), []):
            with self.subTest(executions=len(listed)):
                section = self.read(executions=listed)
                self.assertIsNone(section["executions"])
                self.assertEqual(section["refused"]["executions"], "short")
                found = self.numbers(section)
                for name in ("executions_per_day", "failure_rate_percent", "period_days", "method", "estimate"):
                    self.assertEqual(found[name], (None, "less than one day of executions is stored"))

    def test_more_than_the_page_bound_in_under_a_day_says_so(self):
        # 250 a page, one second apart: the 10 000 read cover under three hours.
        def endless(offset):
            return [execution(offset + i, seconds=offset + i + 1) for i in range(api.PAGE_SIZE)]

        section = self.read(executions=endless)
        self.assertIsNone(section["executions"])
        self.assertEqual(section["refused"]["executions"], "busy")
        found = self.numbers(section)
        for name in ("executions_per_day", "failure_rate_percent", "period_days", "method", "estimate"):
            self.assertEqual(found[name], (None, "more than 10 000 executions in less than one day, the bound of this tool"))
        self.assertEqual(sum("/executions" in path for path in self.n8n.api_paths()), api.MAX_PAGES)

    def test_one_execution_without_a_status_does_not_change_the_rule(self):
        listed = spread(99, 5, first=1)
        listed[10] = {**listed[10], "status": "error"}
        listed.insert(40, {**execution(0, days=2), "status": None})
        found = self.read(executions=listed)["executions"]
        self.assertEqual((found["total"], found["failed"]), (99, 1))  # the one without a status is not counted

    def test_one_older_execution_does_not_end_the_walk(self):
        # The list is in the order of the ids: an execution started long ago can come first.
        found = self.read(executions=[execution(0, days=8), *spread(200, 4, first=1)])["executions"]
        self.assertEqual((found["total"], found["period_days"], found["estimate_reason"]), (200, 7, "saved_only"))
        # Over the pages too; the walk ends after the first page that is older throughout.
        listed = [execution(0, days=8), *spread(600, 6, first=1), *[execution(1000 + i, days=9) for i in range(600)]]
        found = self.read(executions=listed)["executions"]
        self.assertEqual((found["total"], found["period_days"], found["estimate_reason"]), (600, 7, "saved_only"))
        self.assertEqual(sum("/executions" in path for path in self.n8n.api_paths()), 4)  # 1 201 items would be 5

    def test_only_finished_statuses_are_counted(self):
        listed = [
            execution(1, "success", hours=1),
            execution(2, "error", hours=2),
            execution(3, "crashed", hours=3),
            execution(4, "canceled", hours=4),
            execution(5, "waiting", hours=5),
            execution(6, "new", hours=6),
            execution(7, "running", hours=7),
            execution(8, "unknown", hours=8),
            execution(9, f"{MARK}", hours=9),
            {**execution(10, "success"), "startedAt": None},  # not started: no date, not counted
            {**execution(11, "success"), "startedAt": f"{MARK}"},
            execution(12, "success", days=3),
        ]
        found = self.read(executions=listed)["executions"]
        self.assertEqual((found["total"], found["failed"]), (4, 2))

    def test_a_list_without_status_gives_executions_per_day_and_no_failure_rate(self):
        # n8n 1.x: `finished`, `stoppedAt` and `waitTill`, no `status`.
        def old(number, finished=True, stopped=True, waiting=False, **delta):
            item = execution(number, **delta)
            del item["status"]
            item["finished"] = finished
            item["stoppedAt"] = item["startedAt"] if stopped else None
            item["waitTill"] = ago(hours=-1) if waiting else None
            return item

        listed = [
            old(1, hours=1),
            old(2, finished=False, hours=2),  # failed or canceled: counted, outcome unknown
            old(3, finished=False, stopped=False, hours=3),  # still running
            old(4, finished=False, waiting=True, hours=4),  # waiting
            old(5, days=4),
        ]
        section = self.read(executions=listed)
        found = section["executions"]
        self.assertEqual((found["method"], found["total"], found["failed"], found["estimate"]), ("listed", 3, None, True))
        numbers = self.numbers(section)
        self.assertAlmostEqual(numbers["executions_per_day"][0], 0.75, delta=0.01)
        self.assertEqual(
            numbers["failure_rate_percent"],
            (None, "this n8n version does not report the outcome of an execution in the list"),
        )
        self.assertEqual((numbers["method"][0], numbers["estimate"][0]), ("listed", True))

    def test_executions_known_not_to_be_saved(self):
        section = self.read(executions=spread(27, 3) + spread(3, 3.1, "error", first=50))
        none = "EXECUTIONS_DATA_SAVE_ON_SUCCESS"
        cases = {
            "an env file says successes are not saved": Snapshot(usage=section, env={none: "none"}),
            "the debug information says so": Snapshot(usage=section, debug_info=debug_info.parse(pasted.FULL.replace("- success: all", "- success: none"))),
        }
        self.assertIn("- success: all", pasted.FULL)
        for name, snap in cases.items():
            with self.subTest(name):
                found = usage.numbers(snap)
                for fact in ("executions_per_day", "failure_rate_percent", "method"):
                    self.assertEqual(found[fact], (None, "successful executions are not saved, so the list cannot count them"))
        found = usage.numbers(Snapshot(usage=section, env={"EXECUTIONS_DATA_SAVE_ON_ERROR": "none"}))
        self.assertIsNotNone(found["executions_per_day"][0])
        self.assertEqual(found["failure_rate_percent"], (None, "failed executions are not saved, so the list cannot count them"))
        # A probe wins: the env file states the variable, the text is not used for it.
        both = Snapshot(usage=section, env={none: "all"}, debug_info=cases["the debug information says so"].debug_info)
        self.assertIsNotNone(usage.numbers(both)["executions_per_day"][0])
        # Insights counts by itself: what is saved does not matter.
        counted = self.read(insights=(200, summary(7, 0)))
        self.assertEqual(usage.numbers(Snapshot(usage=counted, env={none: "none"}))["executions_per_day"][0], 1)

    # --- users, and what the key can see ---------------------------------------------

    def test_pending_users_are_not_counted(self):
        section = self.read(users=[user(1), user(2, pending=True), user(3), user(4, pending=True), user(5)])
        self.assertEqual(section["users"], 3)
        self.assertEqual(self.numbers(section)["users"], (3, "without pending invitations"))

    def test_a_refused_users_list_leaves_only_the_users_unknown(self):
        section = self.read(insights=(200, summary(7, 1)), users=403)
        self.assertEqual((section["users"], section["refused"]["users"]), (None, 403))
        self.assertEqual(section["active_workflows"], 3)
        found = self.numbers(section)
        self.assertEqual(found["users"][0], None)
        self.assertIn("the API key cannot list users", found["users"][1])
        self.assertNotIn("owner", found["users"][1])
        self.assertEqual(found["executions_per_day"][0], 1)
        self.assertNotIn("/api/v1/users/", " ".join(self.n8n.api_paths()))  # no id to ask about

    def test_a_key_that_reads_a_users_detail_sees_the_whole_instance(self):
        section = self.read(executions=spread(20, 3))
        self.assertIs(section["whole_instance"], True)
        self.assertIn(f"/api/v1/users/{MARK}-user-1", self.n8n.api_paths())
        found = self.numbers(section)
        self.assertEqual(found["active_workflows"], (3, ""))
        self.assertEqual(found["active_workflows_partial"], (False, ""))
        self.assertNotIn("this key's user", found["executions_per_day"][1])

    def test_a_members_key_lists_the_users_and_is_still_marked_partial(self):
        # What n8n 2.16.1 and 3.3.0 do: the users list answers, the detail of a user does not.
        section = self.read(executions=spread(20, 3), detail=403, workflows=1)
        self.assertEqual((section["users"], section["refused"]["users"], section["whole_instance"]), (2, None, False))
        found = self.numbers(section)
        self.assertEqual(found["users"], (2, "without pending invitations"))
        self.assertEqual(found["active_workflows"], (1, "as far as this key's user can see"))
        self.assertIs(found["active_workflows_partial"][0], True)
        self.assertEqual(found["estimate"][0], True)
        self.assertEqual(found["estimate_reason"][0], "partial_view")
        self.assertIn("as far as this key's user can see", found["executions_per_day"][1])
        # Insights counts the whole instance: it is not marked.
        counted = self.numbers(self.read(insights=(200, summary(70, 7)), detail=403))
        self.assertEqual((counted["estimate"][0], counted["estimate_reason"]), (False, (None, "")))
        self.assertIs(counted["active_workflows_partial"][0], True)

    def test_a_user_id_that_is_not_an_id_is_never_put_in_a_request(self):
        for bad in ("../workflows", "a b", "x" * 65, "é", 7, None):
            with self.subTest(id=bad):
                section = self.read(users=[{**user(1), "id": bad}])
                self.assertEqual((section["users"], section["whole_instance"]), (1, False))
                self.assertEqual([path for path in self.n8n.api_paths() if "/users/" in path], [])

    # --- active workflows -------------------------------------------------------------

    def test_active_workflows_are_counted_over_the_pages(self):
        section = self.read(workflows=603)
        self.assertEqual(section["active_workflows"], 603)
        paths = [path for path in self.n8n.api_paths() if "/workflows" in path]
        self.assertEqual(paths, ["/api/v1/workflows?active=true&limit=250", "/api/v1/workflows?active=true&limit=250&cursor=c250", "/api/v1/workflows?active=true&limit=250&cursor=c500"])

    def test_more_than_ten_thousand_active_workflows(self):
        section = self.read(workflows=lambda offset: [{"id": f"{MARK}{offset}"}] * api.PAGE_SIZE)
        self.assertEqual((section["active_workflows"], section["refused"]["active_workflows"]), (None, "limit"))
        found = self.numbers(section)
        self.assertEqual(found["active_workflows"], (None, "more than 10 000 active workflows"))
        self.assertEqual(found["active_workflows_partial"][0], None)
        self.assertEqual(section["users"], 2)

    # --- failures ----------------------------------------------------------------------

    def test_one_number_failing_leaves_the_other_three(self):
        for broken in ("/api/v1/users", "/api/v1/workflows"):
            with self.subTest(broken=broken):
                fake = self.fake(insights=(200, summary(14, 7)))
                fake.lists[broken] = 500
                section = self.read(fake)
                found = self.numbers(section)
                self.assertEqual((found["executions_per_day"][0], found["failure_rate_percent"][0]), (2, 50))
                name = "users" if broken.endswith("users") else "active_workflows"
                other = "active_workflows" if name == "users" else "users"
                self.assertEqual(found[name], (None, "the n8n API answered with HTTP status 500"))
                self.assertIsNotNone(found[other][0])

    def test_a_refused_key_stops_the_collector_after_one_request(self):
        section = self.read(key="n8n_api_another-made-up-key")
        self.assertEqual(self.n8n.api_paths(), ["/api/v1/insights/summary"])
        self.assertEqual(section["refused"], {"executions": 401, "users": 401, "active_workflows": 401})
        self.assertEqual((section["executions"], section["users"], section["active_workflows"]), (None, None, None))
        for value, detail in self.numbers(section).values():
            if detail:
                self.assertEqual((value, detail), (None, "the API key was refused"))

    def test_an_instance_that_is_not_there(self):
        fake = self.fake()
        fake.close()
        section = api.collect(Target(url=fake.url, api_key=KEY))["usage"]
        self.assertEqual(section["refused"], dict.fromkeys(usage.NUMBERS, "unreachable"))

    def test_an_answer_that_is_not_json_or_not_a_list(self):
        fake = self.fake(insights=(200, summary(7, 0)))
        fake.lists["/api/v1/workflows"] = [1, "two", None, workflow(1)]  # only objects are items
        self.assertEqual(self.read(fake)["active_workflows"], 1)
        for body in ({"data": f"{MARK}"}, {"data": [], "nextCursor": 5}, {"data": [], "nextCursor": "c" * 3000}, [], f"{MARK}"):
            with self.subTest(body=body):
                with mock.patch.object(api._Session, "get", return_value=body):
                    with self.assertRaises(api._Refused) as caught:
                        list(api._pages(api._Session("http://127.0.0.1:9", KEY), "/users?"))
                self.assertEqual(caught.exception.why, "invalid")

    def test_the_url_must_be_given_and_hold_no_credentials(self):
        for target in (Target(api_key=KEY), Target(url=f"http://user:{MARK}@127.0.0.1:9", api_key=KEY)):
            with self.assertRaises(ValueError) as caught:
                api.collect(target)
            self.assertNotIn(MARK, str(caught.exception))

    # --- the key travels in the clear -----------------------------------------------------

    def test_unencrypted_is_plain_http_to_another_machine(self):
        cases = {
            "http://n8n.example.test": True,
            "http://10.0.0.5:5678": True,
            "http://localhost.example.test": True,
            "https://n8n.example.test": False,
            "http://localhost:5678": False,
            "http://app.localhost": False,
            "http://127.0.0.1:5678": False,
            "http://127.8.8.8": False,
            "http://[::1]:5678": False,
        }
        for url, expected in cases.items():
            self.assertIs(api._unencrypted(url), expected, url)
        self.assertIs(self.read()["key_unencrypted"], False)


class SafetyTest(Base):
    """Read only, one origin, bounded: the three things the key is trusted on."""

    def test_every_request_is_a_get_with_the_key_in_one_header(self):
        self.read(executions=spread(600, 9), workflows=300, users=[user(i) for i in range(300)])
        self.read(self.n8n, key="n8n_api_refused")
        self.assertGreater(len(self.n8n.requests), 8)
        for method, path, key in self.n8n.requests:
            self.assertEqual(method, "GET", path)
            self.assertIn(key, (KEY, "n8n_api_refused"))
            self.assertNotIn(MARK + "_key", path)  # never in a URL
            self.assertTrue(path.startswith("/api/v1/"), path)
        allowed = ("/api/v1/insights/summary", "/api/v1/executions?", "/api/v1/users?", "/api/v1/users/", "/api/v1/workflows?active=true&")
        for path in self.n8n.api_paths():
            self.assertTrue(path.startswith(allowed), path)

    def test_a_redirect_to_another_host_is_not_followed(self):
        other = self.fake()
        for scheme_host in (other.url, other.url.replace("127.0.0.1", "localhost")):
            other.requests.clear()
            redirect = {path: ("redirect", scheme_host + path) for path in ("/api/v1/insights/summary", "/api/v1/users", "/api/v1/workflows")}
            section = self.read(odd=redirect)
            self.assertEqual(other.requests, [], "the other host was contacted")
            self.assertEqual(section["refused"], dict.fromkeys(usage.NUMBERS, "redirect"))
            self.assertEqual(self.numbers(section)["users"][1], "the n8n API redirected to another address, which was not followed")

    def test_a_redirect_to_https_on_the_same_host_says_to_give_the_https_url(self):
        fake = self.fake()
        secure = fake.url.replace("http://", "https://")
        for path in ("/api/v1/insights/summary", "/api/v1/users", "/api/v1/workflows"):
            fake.odd[path] = ("redirect", secure + path)
        section = self.read(fake)
        self.assertEqual(section["refused"], dict.fromkeys(usage.NUMBERS, "https"))
        self.assertEqual(self.numbers(section)["users"][1], "the instance redirects to https, which was not followed: give the URL with https")
        self.assertEqual(len(fake.api_paths()), 3)  # each call once, none again over https
        # Another host over https is another address, as before.
        fake.odd["/api/v1/users"] = ("redirect", "https://other.example.test/api/v1/users")
        self.assertEqual(self.read(fake)["refused"]["users"], "redirect")

    def test_a_proxy_of_the_machine_never_gets_the_key(self):
        proxy = self.fake()
        variables = {name: proxy.url for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")}
        with mock.patch.dict(os.environ, variables):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            section = self.read(insights=(200, summary(7, 0)))
        self.assertEqual(proxy.requests, [], "a request went to the proxy")
        self.assertEqual((section["executions"]["total"], section["users"], section["active_workflows"]), (7, 2, 3))
        self.assertEqual(len(self.n8n.api_paths()), 4)

    def test_a_redirect_within_the_origin_keeps_working(self):
        fake = self.fake(insights=(200, summary(7, 0)))
        fake.odd["/api/v1/users"] = ("redirect", fake.url + "/api/v1/workflows?active=true&limit=250")
        self.assertEqual(self.read(fake)["users"], 3)  # the three workflows, read as the page it was sent to

    def bounded(self, limit=1.5, **config):
        """Run the collector with a short time limit; return (section, seconds it took)."""
        with mock.patch.object(api, "TIME_LIMIT", limit), mock.patch.object(api, "TIMEOUT", 0.5):
            started = time.monotonic()
            section = self.read(**config)
            return section, time.monotonic() - started

    def test_a_server_that_never_stops_paging_gets_a_bounded_number_of_requests(self):
        def endless(offset):
            return [execution(offset + i, seconds=offset + i + 1) for i in range(api.PAGE_SIZE)]

        section = self.read(executions=endless, users=lambda offset: [user(offset)] * api.PAGE_SIZE, workflows=lambda offset: [workflow(offset)] * api.PAGE_SIZE)
        self.assertLessEqual(len(self.n8n.api_paths()), api.MAX_REQUESTS)
        self.assertEqual(len(self.n8n.api_paths()), 1 + 3 * api.MAX_PAGES)
        self.assertEqual(section["refused"], {"executions": "busy", "users": "limit", "active_workflows": "limit"})
        self.assertEqual(self.numbers(section)["users"], (None, "more than 10 000 users"))

    def test_the_request_bound_holds_whatever_the_lists_do(self):
        fake = self.fake()
        session = api._Session(fake.url, KEY)
        self.addCleanup(session.close)
        for _ in range(api.MAX_REQUESTS):
            session.get("/workflows?limit=1")
        with self.assertRaises(api._Refused) as caught:
            session.get("/workflows?limit=1")
        self.assertEqual((caught.exception.why, len(fake.api_paths())), ("limit", api.MAX_REQUESTS))

    def test_a_server_that_never_answers(self):
        every = dict.fromkeys(("/api/v1/insights/summary", "/api/v1/executions", "/api/v1/users", "/api/v1/workflows"), "hang")
        section, took = self.bounded(odd=every)
        self.assertLess(took, 3)
        self.assertEqual(section["refused"], dict.fromkeys(usage.NUMBERS, "slow"))
        self.assertEqual(self.numbers(section)["users"], (None, "the instance answered too slowly"))

    def test_a_server_that_answers_one_byte_at_a_time(self):
        for odd in ("drip", "drip_headers"):
            with self.subTest(odd):
                section, took = self.bounded(odd={"/api/v1/insights/summary": odd})
                self.assertLess(took, 3)
                self.assertEqual(section["refused"], dict.fromkeys(usage.NUMBERS, "slow"))
                self.assertEqual(len(self.n8n.api_paths()), 1)  # nothing is asked once the time is over

    def test_a_slow_number_leaves_the_ones_already_read(self):
        section, took = self.bounded(insights=(200, summary(7, 0)), odd={"/api/v1/workflows": "drip"})
        self.assertLess(took, 3)
        self.assertEqual((section["executions"]["total"], section["users"]), (7, 2))
        self.assertEqual(section["refused"]["active_workflows"], "slow")

    def test_an_endless_body_is_cut_at_the_size_bound(self):
        with mock.patch.object(api, "MAX_BYTES", 2 * 1024 * 1024):
            section, took = self.bounded(limit=20, insights=(200, summary(7, 0)), odd={"/api/v1/workflows": "endless"})
        self.assertLess(took, 15)
        self.assertEqual(section["refused"]["active_workflows"], "large")
        self.assertEqual(self.numbers(section)["active_workflows"], (None, "the answer was too large"))
        self.assertEqual(section["users"], 2)

    def test_a_declared_length_over_the_bound_is_not_read(self):
        with mock.patch.object(api, "MAX_BYTES", 10):
            section = self.read(insights=(200, summary(7, 0)))
        self.assertEqual(section["refused"]["executions"], "large")

    def test_the_bounds_are_the_ones_of_the_design(self):
        self.assertEqual((api.PAGE_SIZE, api.MAX_PAGES, api.MAX_REQUESTS, api.MAX_BYTES, api.TIME_LIMIT), (250, 40, 130, 32 * 1024 * 1024, 60))
        self.assertLessEqual(1 + 3 * api.MAX_PAGES + 1, api.MAX_REQUESTS)
        self.assertEqual(usage.NUMBERS, ("executions", "users", "active_workflows"))  # the heaviest answers last


class KeyTest(unittest.TestCase):
    def test_a_key_is_one_line_of_visible_ascii(self):
        self.assertEqual(usage.check_key(f"  {KEY}\r\n"), KEY)
        refused = {
            "": "empty",
            " \n\t": "empty",
            f"{KEY}\n{KEY}": "single line",
            f"{KEY}\rX-Made-Up: 1": "single line",
            f"{MARK} two words": "not an n8n API key",
            f"{MARK}é": "not an n8n API key",
            f"{MARK}\x00": "not an n8n API key",
            MARK * 500: "longer than",
        }
        for text, why in refused.items():
            with self.assertRaises(usage.KeyRefused) as caught:
                usage.check_key(text)
            self.assertIn(why, str(caught.exception))
            self.assertNotIn(MARK, str(caught.exception))


class ReaderTest(unittest.TestCase):
    """A snapshot can be loaded from a file: the section is never read as it is."""

    GOOD = {
        "executions": {"method": "listed", "total": 30, "failed": 3, "period_days": 5.5, "estimate": True, "estimate_reason": "saved_only"},
        "active_workflows": 4,
        "users": 2,
        "refused": {"executions": None, "users": None, "active_workflows": None},
        "whole_instance": True,
        "key_unencrypted": False,
    }

    def test_what_the_collector_writes_is_kept(self):
        checked = usage.checked(self.GOOD)
        self.assertEqual(checked["executions"], {"method": "listed", "total": 30, "failed": 3, "period_days": 5.5, "estimate_reason": "saved_only"})
        self.assertEqual((checked["active_workflows"], checked["users"], checked["whole_instance"]), (4, 2, True))
        self.assertEqual(usage.checked({**self.GOOD, "executions": {**self.GOOD["executions"], "failed": None}})["executions"]["failed"], None)

    def test_anything_else_is_dropped(self):
        for section in (None, [], "usage", 3):
            self.assertIsNone(usage.checked(section))
        bad_executions = (
            {"method": f"{MARK}"},
            {"total": -1},
            {"total": 1.5},
            {"total": True},
            {"failed": 31},
            {"failed": f"{MARK}"},
            {"period_days": 0.5},
            {"period_days": 8},
            {"period_days": float("nan")},
            {"period_days": f"{MARK}"},
            {"estimate_reason": f"{MARK}"},
            {"estimate_reason": "partial_view"},
            {"method": "insights", "period_days": 5.5},
            {"method": "insights", "failed": None, "period_days": 7},
        )
        for change in bad_executions:
            with self.subTest(change=change):
                self.assertIsNone(usage.checked({**self.GOOD, "executions": {**self.GOOD["executions"], **change}})["executions"])
        odd = usage.checked({
            "executions": f"{MARK}",
            "active_workflows": f"{MARK}",
            "users": -4,
            "refused": {"executions": f"{MARK}", "users": 99999, "active_workflows": True, MARK: MARK},
            "whole_instance": f"{MARK}",
            "key_unencrypted": 1,
            MARK: MARK,
        })
        self.assertEqual(odd, {
            "executions": None, "active_workflows": None, "users": None,
            "refused": {"executions": None, "users": None, "active_workflows": None},
            "whole_instance": False, "key_unencrypted": False,
        })  # fmt: skip
        text = json.dumps([usage.numbers(Snapshot(usage={**self.GOOD, "refused": f"{MARK}", MARK: MARK})), profile.build_profile(Snapshot(usage={MARK: MARK}), [])])
        self.assertNotIn(MARK, text)


class ProfileTest(Base):
    def test_the_eight_ids_come_after_the_existing_ones(self):
        self.assertEqual(profile.FACT_IDS[-8:], USAGE_FACTS)
        self.assertEqual(len(profile.FACT_IDS), 32 + 14 + 13 + 8)
        self.assertFalse(set(USAGE_FACTS) & profile.FORMAT_1_FACT_IDS)
        self.assertEqual(len(profile.FORMAT_1_FACT_IDS), 32)
        self.assertEqual((n8n_healthcheck.__version__, profile.build_profile(Snapshot(), [])["tool_version"]), ("0.5.0", "0.5.0"))
        self.assertEqual(profile.FORMAT_VERSION, 1)
        for fact_id in USAGE_FACTS:
            self.assertTrue(questions.sentence(fact_id).endswith("?"))
            self.assertNotIn(fact_id, questions.OPERATOR_BY_ID)  # none becomes a question a person answers

    def test_without_a_key_each_is_unknown_and_says_so(self):
        for snap in (Snapshot(), Snapshot(env={"EXECUTIONS_MODE": "queue"})):
            facts = facts_of(snap)
            for fact_id in USAGE_FACTS:
                self.assertEqual(facts[fact_id], {"id": fact_id, "value": None, "provenance": "unknown", "source": None, "detail": "The n8n API key was not provided"})
            self.assertEqual(profile.build_profile(snap, [])["sources"]["api"], "not_provided")

    def test_the_facts_of_a_run_with_a_key(self):
        section = self.read(insights=(200, summary(70, 7)))
        built = profile.build_profile(Snapshot(usage=section), [])
        facts = {fact["id"]: fact for fact in built["facts"]}
        self.assertEqual(built["sources"]["api"], "collected")
        self.assertEqual(list(built["sources"])[:6], list(report.SECTIONS))
        expected = dict(zip(USAGE_FACTS, (10, 10, 7, "insights", False, 3, False, 2)))
        for fact_id, value in expected.items():
            self.assertEqual((facts[fact_id]["value"], facts[fact_id]["provenance"], facts[fact_id]["source"]), (value, "measured", "api"), fact_id)
        self.assertIn("Insights", facts["usage.executions_per_day"]["detail"])
        for fact in built["facts"]:
            if fact["id"] not in USAGE_FACTS:
                self.assertEqual(fact["provenance"], "unknown", fact["id"])  # the numbers fill nothing else

    def test_an_estimate_says_so_and_why(self):
        facts = facts_of(Snapshot(usage=self.read(executions=spread(40, 2), detail=403)))
        self.assertIs(facts["usage.estimate"]["value"], True)
        self.assertEqual(facts["usage.method"]["value"], "listed")
        self.assertIn("estimate", facts["usage.executions_per_day"]["detail"])
        self.assertIn("as far as this key's user can see", facts["usage.executions_per_day"]["detail"])
        self.assertIs(facts["usage.active_workflows_partial"]["value"], True)

    def test_the_source_is_failed_when_a_key_gave_nothing(self):
        refused = self.read(key="n8n_api_refused")
        self.assertEqual(profile.build_profile(Snapshot(usage=refused), [])["sources"]["api"], "failed")
        self.assertEqual(facts_of(Snapshot(usage=refused))["usage.users"]["detail"], "the API key was refused")
        crashed = profile.build_profile(Snapshot(), [], ("usage",))
        self.assertEqual(crashed["sources"]["api"], "failed")
        self.assertEqual(facts_of(Snapshot(), ("usage",))["usage.users"]["detail"], "The n8n API could not be read")

    def test_a_file_of_this_version_passes_the_0_2_0_rules_and_older_files_still_open(self):
        section = self.read(executions=spread(40, 2), detail=403)
        for snap in (Snapshot(usage=section), Snapshot(usage=self.read(insights=(200, summary(3, 1)))), Snapshot()):
            findings = engine.run(snap, ALL_RULES)
            data = json.loads(profile.to_json(profile.build_profile(snap, findings)))
            self.assertIs(validate_0_2_0.validate(data), data)
            self.assertEqual(profile.validate(data), data)
            self.assertIn("| api |", profile.render_markdown(data))
        built = profile.build_profile(Snapshot(env={"EXECUTIONS_MODE": "queue"}), [])
        six = {name: state for name, state in built["sources"].items() if name in report.SECTIONS}
        older = {
            "0.2.0": {**built, "tool_version": "0.2.0", "sources": six, "facts": built["facts"][:32]},
            "0.3.0": {**built, "tool_version": "0.3.0", "sources": six, "facts": built["facts"][:46]},
            "0.4.0": {**built, "tool_version": "0.4.0", "sources": {**six, "debug_info": "not_provided"}, "facts": built["facts"][:59]},
        }
        for version, data in older.items():
            with self.subTest(version=version):
                data = json.loads(profile.to_json(data))
                self.assertEqual(profile.validate(data), data)
                self.assertNotIn("| api |", profile.render_markdown(data))
                sheet = profile.render_questions(data)  # the ids it never held are open questions
                for fact_id in USAGE_FACTS:
                    self.assertIn(f"(`{fact_id}`)", sheet)

    def test_a_usage_fact_can_be_withheld_like_any_other(self):
        built = profile.build_profile(Snapshot(usage=self.read(insights=(200, summary(70, 7)))), [], withhold=("usage.users",))
        fact = {fact["id"]: fact for fact in built["facts"]}["usage.users"]
        self.assertEqual((fact["value"], fact["provenance"]), (None, "withheld"))

    def test_the_format_document_lists_the_source_and_the_ids(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "docs", "profile-format.md"), encoding="utf-8") as handle:
            text = handle.read()
        added = text.split("### Added in tool version 0.5.0")[1].split("\nThese ids")[0]
        self.assertEqual(tuple(line.split("`")[1] for line in added.splitlines() if line.startswith("| `")), USAGE_FACTS)
        self.assertIn("an eighth source, `api`", text)


class ReportTest(Base):
    SNAPSHOTS = (
        Snapshot(),
        Snapshot(env={"EXECUTIONS_MODE": "queue", "DB_TYPE": "sqlite"}, health={"healthz": True, "readiness": True, "metrics_disabled": True}),
        Snapshot(env={"EXECUTIONS_MODE": "regular"}, n8n_version="1.70.0", metrics={"n8n_x": 1.0}, health={"healthz": True, "readiness": False, "metrics_disabled": False}),
    )

    def test_without_a_key_the_report_does_not_mention_usage(self):
        for snap in self.SNAPSHOTS:
            findings = engine.run(snap, ALL_RULES)
            data = report.build_report(snap, findings)
            self.assertIsNone(data["usage"])
            text = report.render_markdown(snap, findings)
            for word in ("Usage", "usage", "API key", "n8n API"):
                self.assertNotIn(word, text)
            self.assertIn("## Summary\n\n" + data["summary"] + "\n\n## Scores\n", text)

    def test_the_numbers_change_no_score_no_finding_and_no_count_of_sources(self):
        sections = (
            self.read(insights=(200, summary(700000, 699999)), workflows=900),  # numbers a rule would jump at
            self.read(executions=spread(40, 2), detail=403),
            self.read(key="n8n_api_refused"),
        )
        for snap in self.SNAPSHOTS:
            for section in sections:
                with_key = Snapshot(**{**asdict(snap), "usage": section})
                plain = report.build_report(snap, engine.run(snap, ALL_RULES))
                read = report.build_report(with_key, engine.run(with_key, ALL_RULES))
                for key in ("n8n_version", "sources", "partial", "scores", "not_assessed", "findings"):
                    self.assertEqual(read[key], plain[key], key)
                self.assertTrue(read["summary"].startswith(plain["summary"]))
                self.assertEqual(read["summary_notes"][: len(plain["summary_notes"])], plain["summary_notes"])
                old_facts = [fact for fact in profile.build_profile(with_key, [])["facts"] if fact["id"] not in USAGE_FACTS]
                self.assertEqual(old_facts, [fact for fact in profile.build_profile(snap, [])["facts"] if fact["id"] not in USAGE_FACTS])

    def test_no_rule_reads_the_section(self):
        class Tripwire(dict):
            def __getitem__(self, key):
                raise AssertionError("a rule read snapshot.usage")

            get = __getitem__

        findings = engine.run(Snapshot(env={"EXECUTIONS_MODE": "queue"}, usage=Tripwire(x=1)), ALL_RULES)
        self.assertNotIn(engine.SKIPPED_RULE_ID, [finding.rule_id for finding in findings])

    def test_the_usage_section_of_an_insights_run(self):
        snap = Snapshot(usage=self.read(insights=(200, summary(27, 5))), health={"healthz": True, "readiness": True, "metrics_disabled": True})
        text = report.render_markdown(snap, [])
        self.assertIn(
            "## Usage\n\nRead through the n8n API. These numbers describe the instance: no finding or score rests on them.\n\n"
            "Executions over the last 7 days, as n8n's Insights counts them. Insights can lag behind by up to an hour.\n\n"
            "- **Executions per day:** 3.86\n- **Failure rate:** 18.5%\n- **Active workflows:** 3\n"
            "- **Users:** 2 (without pending invitations)\n\n## Scores\n",
            text,
        )
        self.assertIn(" Usage numbers were read through the n8n API.\n", text)
        self.assertNotIn("encryption", text)

    def test_an_estimate_is_written_with_about_and_its_period_and_reason(self):
        snap = Snapshot(usage={**self.read(executions=spread(40, 2) , detail=403), "key_unencrypted": True})
        data = report.build_report(snap, [])["usage"]
        self.assertRegex(data["basis"], r"^Executions over the last (2|1\.9\d|2\.0\d) days, counted from the executions list\. An estimate: only as far as this key's user can see\.$")
        rows = {row["id"]: row for row in data["rows"]}
        self.assertRegex(rows["usage.executions_per_day"]["value"], r"^about (19|20)(\.\d+)?$")
        self.assertEqual(rows["usage.failure_rate_percent"]["value"], "about 0%")
        self.assertEqual((rows["usage.active_workflows"]["value"], rows["usage.active_workflows"]["note"]), ("3", "as far as this key's user can see"))
        summary_text = report.build_report(snap, [])["summary"]
        self.assertTrue(summary_text.endswith("Usage numbers were read through the n8n API. The API key was sent without encryption: the URL is http and the host is not this machine."))

    def test_a_number_that_could_not_be_read_is_listed_with_its_reason(self):
        snap = Snapshot(usage=self.read(insights=(200, summary(0, 0)), users=403))
        text = report.render_markdown(snap, [])
        zero = "n8n's Insights counted no execution in the last 7 days; it can lag behind by up to an hour"
        self.assertIn(f"- **Executions per day:** 0 ({zero})\n", text)
        self.assertIn(f"- **Failure rate:** not read ({zero})\n", text)
        self.assertIn("- **Users:** not read (the API key cannot list users: its scopes leave that out, or the licence limits the number of users)\n", text)

    def test_a_refused_key_is_said_in_the_summary(self):
        snap = Snapshot(usage=self.read(key="n8n_api_refused"))
        data = report.build_report(snap, [])
        self.assertTrue(data["summary"].endswith("The n8n API key was refused, so no usage number was read."))
        self.assertEqual(data["summary_notes"][-1], "The n8n API key was refused, so no usage number was read.")
        self.assertEqual({row["value"] for row in data["usage"]["rows"]}, {None})
        self.assertEqual({row["note"] for row in data["usage"]["rows"]}, {"the API key was refused"})
        self.assertIsNone(data["usage"]["basis"])
        gone = report.build_report(Snapshot(), [], failed=("usage",))
        self.assertTrue(gone["summary"].endswith("No usage number could be read through the n8n API."))
        self.assertIsNone(report.build_report(snap, [], ("health",), could_not_run=())["usage"])


class RunTest(Base):
    """Whole runs, from the command line and through the web interface, and what they let out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.key_file = self.path("key.txt")
        with open(self.key_file, "w", encoding="utf-8") as handle:
            handle.write(KEY + "\n")

    def path(self, name):
        return os.path.join(self.dir, name)

    def run_cli(self, argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = mock.Mock(buffer=io.BytesIO(stdin))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", fake_stdin):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def leaky(self, **config):
        """A fake whose every answer carries markers, with all five calls answered."""
        return self.fake(**{"executions": spread(27, 3) + spread(3, 3.2, "error", first=50), "workflows": 4, **config})

    def test_the_flag_reads_the_key_from_a_file_and_the_report_has_the_section(self):
        fake = self.leaky(insights=(200, summary(27, 5)))
        code, out, err = self.run_cli(["--url", fake.url, "--api-key-file", self.key_file, "--save-snapshot", self.path("s.json"), "--save-profile", self.path("p.json")])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("## Usage\n", out)
        self.assertIn("- **Executions per day:** 3.86\n", out)
        self.assertIn("- **Active workflows:** 4\n", out)
        with open(self.path("s.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["usage"]["executions"]["total"], 27)
        self.assertNotIn("api_key", json.dumps(saved))
        # The saved snapshot gives the same Usage section again, offline.
        again = self.run_cli(["--snapshot", self.path("s.json")])[1]
        self.assertEqual(again[again.index("## Usage") : again.index("## Scores")], out[out.index("## Usage") : out.index("## Scores")])
        self.assertEqual(profile.load_profile(self.path("p.json"))["sources"]["api"], "collected")

    def test_the_key_from_standard_input(self):
        fake = self.leaky()
        code, out, err = self.run_cli(["--url", fake.url, "--api-key-file", "-"], stdin=KEY.encode() + b"\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("counted from the executions list", out)
        self.assertIn("- **Failure rate:** about 10%\n", out)
        self.assertEqual({key for _, path, key in fake.requests if path.startswith("/api/")}, {KEY})

    def test_a_key_file_written_on_windows_is_read(self):
        fake = self.leaky()
        written = {"bom": b"\xef\xbb\xbf" + KEY.encode() + b"\r\n", "utf-16": (KEY + "\r\n").encode("utf-16"), "utf-16-be": b"\xfe\xff" + KEY.encode("utf-16-be")}
        for name, content in written.items():
            with self.subTest(name):
                with open(self.path(name), "wb") as handle:
                    handle.write(content)
                fake.requests.clear()
                code, out, err = self.run_cli(["--url", fake.url, "--api-key-file", self.path(name)])
                self.assertEqual((code, err), (0, ""))
                self.assertIn("## Usage\n", out)
                self.assertEqual({key for _, path, key in fake.requests if path.startswith("/api/")}, {KEY})

    def test_without_a_key_nothing_of_the_api_is_asked_and_the_snapshot_has_no_section(self):
        fake = self.leaky()
        code, out, _ = self.run_cli(["--url", fake.url, "--save-snapshot", self.path("s.json")])
        self.assertEqual(code, 0)
        self.assertEqual(fake.api_paths(), [])
        self.assertNotIn("Usage", out)
        with open(self.path("s.json"), encoding="utf-8") as handle:
            self.assertNotIn("usage", json.load(handle))  # so that an older version loads the file

    def test_usage_errors(self):
        fake = self.fake()
        written = {"empty.txt": b"", "blank.txt": b" \n\n", "two.txt": f"{KEY}\n{MARK}second\n".encode(), "words.txt": f"{MARK} not a key".encode(), "binary.txt": b"\x80" + MARK.encode(), "odd.txt": b"\xff\xfe" + MARK.encode() + b"\x00"}
        for name, content in written.items():
            with open(self.path(name), "wb") as handle:
                handle.write(content)
        with open(self.path("snapshot.json"), "w", encoding="utf-8") as handle:
            json.dump(asdict(Snapshot(env={})), handle)
        cases = {
            "a file that is not there": (["--url", fake.url, "--api-key-file", self.path("missing.txt")], "cannot read the API key file"),
            "an empty file": (["--url", fake.url, "--api-key-file", self.path("empty.txt")], "--api-key-file: the API key is empty"),
            "a blank file": (["--url", fake.url, "--api-key-file", self.path("blank.txt")], "--api-key-file: the API key is empty"),
            "two lines": (["--url", fake.url, "--api-key-file", self.path("two.txt")], "--api-key-file: the API key must be a single line"),
            "words": (["--url", fake.url, "--api-key-file", self.path("words.txt")], "--api-key-file: this is not an n8n API key"),
            "not text": (["--url", fake.url, "--api-key-file", self.path("binary.txt")], "--api-key-file: the file is not text"),
            "a broken UTF-16": (["--url", fake.url, "--api-key-file", self.path("odd.txt")], "--api-key-file: the file is not text"),
            "no file name": (["--url", fake.url, "--api-key-file", " "], "--api-key-file must not be empty"),
            "without a URL": (["--env-file", self.key_file, "--api-key-file", self.key_file], "--api-key-file needs --url"),
            "with a snapshot": (["--snapshot", self.path("snapshot.json"), "--api-key-file", self.key_file], "--api-key-file cannot be combined with --snapshot"),
            "standard input twice": (["--url", fake.url, "--api-key-file", "-", "--debug-info", "-"], "cannot both be read from standard input"),
        }
        for name, (argv, message) in cases.items():
            with self.subTest(name):
                code, out, err = self.run_cli(argv, stdin=KEY.encode())
                self.assertEqual((code, out), (cli.EXIT_ERROR, ""))
                self.assertIn(message, err)
                self.assertNotIn(MARK, err)
                self.assertEqual(fake.requests, [])  # stopped before anything was collected

    def test_there_is_no_flag_or_variable_that_takes_the_key_itself(self):
        fake = self.fake()
        with mock.patch.dict(os.environ, {"N8N_API_KEY": KEY, "N8N_HEALTHCHECK_API_KEY": KEY}):
            self.assertEqual(self.run_cli(["--url", fake.url])[0], 0)
        self.assertEqual(fake.api_paths(), [])
        code, _, err = self.run_cli(["--url", fake.url, "--api-key", KEY])
        self.assertEqual(code, cli.EXIT_ERROR)
        code, _, err = self.run_cli(["--url", fake.url, "--a", "x"])  # still --answer, as before this flag
        self.assertIn("--answer needs --save-profile", err)

    def test_a_key_given_while_the_url_cannot_be_reached_ends_as_a_url_that_cannot_be_reached(self):
        fake = self.fake()
        fake.close()
        code, out, err = self.run_cli(["--url", fake.url, "--api-key-file", self.key_file])
        self.assertEqual((code, out), (cli.EXIT_ERROR, ""))
        self.assertIn("none of the requested sources could be collected", err)
        self.assertNotIn(MARK, err)

    def test_a_collector_that_breaks_never_shows_the_key(self):
        fake = self.fake()
        with mock.patch.object(api, "_executions", side_effect=RuntimeError(f"boom {KEY}")):
            code, out, err = self.run_cli(["--url", fake.url, "--api-key-file", self.key_file, "--save-profile", self.path("p.json")])
        self.assertEqual(code, 0)
        self.assertIn("api_key: not collected (boom <redacted>)", err)
        self.assertNotIn(MARK, err + out)
        self.assertIn("No usage number could be read through the n8n API.", out)
        self.assertEqual(profile.load_profile(self.path("p.json"))["sources"]["api"], "failed")

    # --- zero leak -----------------------------------------------------------------------

    def assert_clean(self, *texts):
        for text in texts:
            self.assertNotIn(MARK, text)
            self.assertNotIn("example.test", text)

    def test_nothing_of_the_key_or_of_an_answer_reaches_an_output_of_the_command_line(self):
        variants = (
            {"insights": (200, summary(27, 5))},
            {},  # the listed path
            {"detail": 403},
            {"users": 403},
            {"insights": (500, None), "users": 500, "workflows": 500},
            {"key": "n8n_api_another-key"},  # the key is refused, with a marker in n8n's message
        )
        for config in variants:
            with self.subTest(config=config):
                fake = self.leaky(**config)
                argv = ["--url", fake.url, "--api-key-file", self.key_file, "--save-snapshot", self.path("s.json"), "--save-profile", self.path("p.json"), "-o", self.path("r.md")]
                code, out, err = self.run_cli(argv)
                self.assertEqual(code, 0)
                self.assertGreater(len(fake.api_paths()), 0)
                files = []
                for name in ("s.json", "p.json", "r.md"):
                    with open(self.path(name), encoding="utf-8") as handle:
                        files.append(handle.read())
                self.assertIn("## Usage", files[2])
                self.assert_clean(out, err, *files)
                shown = self.run_cli(["--show-profile", self.path("p.json")])[1] + self.run_cli(["--show-questions", self.path("p.json")])[1]
                self.assert_clean(shown)
                self.assertIn("usage.users", shown)

    def start_web(self):
        log = io.StringIO()
        redirect = contextlib.redirect_stderr(log)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        server = web.make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            self.addCleanup(connection.close)
            connection.request("POST", "/api/run", body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read().decode()

        return post, log

    def test_the_web_interface_takes_the_key_and_lets_nothing_out(self):
        post, log = self.start_web()
        for config in ({"insights": (200, summary(27, 5))}, {"detail": 403}, {"key": "n8n_api_another-key"}):
            with self.subTest(config=config):
                fake = self.leaky(**config)
                status, raw = post({"url": fake.url, "api_key": f"  {KEY}\n"})
                self.assertEqual(status, 200)
                result = json.loads(raw)
                self.assertEqual(len(result["usage"]["rows"]), 4)
                self.assertIn("## Usage", result["markdown"])
                self.assertIn("usage", result["snapshot"])
                self.assertNotIn("api_key", raw)
                self.assertEqual({key for _, path, key in fake.requests if path.startswith("/api/")}, {KEY})
                self.assert_clean(raw, log.getvalue())
        fake = self.leaky(insights=(200, summary(27, 5)))
        result = json.loads(post({"url": fake.url, "api_key": KEY})[1])
        self.assertEqual([row["value"] for row in result["usage"]["rows"]], ["3.86", "18.5%", "4", "2"])
        self.assertEqual(result["profile"]["sources"]["api"], "collected")
        self.assertEqual(result["summary_notes"][-1], "Usage numbers were read through the n8n API.")
        without = json.loads(post({"url": fake.url})[1])
        self.assertIsNone(without["usage"])
        self.assertIsNone(without["snapshot"]["usage"])

    def test_the_web_interface_refuses_a_key_without_a_url_and_a_text_that_is_no_key(self):
        post, log = self.start_web()
        cases = {
            "the API key needs the n8n URL: it is used on the API of that instance": {"api_key": KEY, "env_text": "A=1"},
            "the API key must be a single line": {"url": "http://127.0.0.1:9", "api_key": f"{KEY}\n{MARK}"},
            "this is not an n8n API key: it holds a space or a character that is not plain ASCII": {"url": "http://127.0.0.1:9", "api_key": f"{MARK} é"},
        }
        for message, body in cases.items():
            status, raw = post(body)
            self.assertEqual((status, json.loads(raw)), (400, {"error": message, "field": "api_key"}))
            self.assert_clean(raw)
        status, raw = post({"api_key": KEY})  # alone
        self.assertEqual((status, json.loads(raw)["field"]), (400, "api_key"))
        status, raw = post({"url": "http://127.0.0.1:9", "api_key": 5})
        self.assertEqual((status, json.loads(raw)), (400, {"error": "api_key must be a string"}))
        self.assert_clean(log.getvalue())

    def test_meta_lists_the_source_and_the_run_fields_take_the_key(self):
        sources = {source["id"]: source for source in web.meta()["sources"]}
        self.assertEqual(sources["api"]["label"], "n8n API key")
        self.assertTrue(sources["api"]["available"])
        self.assertEqual(web.RUN_FIELDS["api_key"], "api_key")
        fields = web.parse_run_request(json.dumps({"url": "http://127.0.0.1:9", "api_key": f" {KEY} "}).encode())
        self.assertEqual(fields, {"url": "http://127.0.0.1:9", "api_key": KEY})

    def test_collect_keeps_the_key_out_of_the_snapshot(self):
        fake = self.leaky()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            snapshot = collect(Target(url=fake.url, api_key=KEY))
        self.assertIsNotNone(snapshot.usage)
        self.assert_clean(json.dumps(asdict(snapshot)), err.getvalue())


@unittest.skipUnless(page_tests.NODE, page_tests.NO_NODE)
class PageTest(Base):
    """The page's own script, run under Node.js: the key field and the Usage section."""

    def response(self, section, **extra):
        snap = Snapshot(usage=section, env={"EXECUTIONS_MODE": "queue"})
        data = report.build_report(snap, [])
        return json.dumps({**data, "exit_code": 0, "warnings": [], "markdown": "", "snapshot": asdict(snap), **extra})

    def test_the_key_is_sent_with_the_url_and_is_not_kept_in_the_form(self):
        got = page_tests.run_page(
            """async (page, input) => {
              page.type("f-url", "https://n8n.example.test");
              page.type("f-apikey", "  " + input.key + " ");
              await page.submit();
              const sent = JSON.parse(page.sent[0]);
              await page.finishRun(input.response);
              const tiles = page.walk(page.$("usage")).filter((node) => node.tagName === "div");
              return { sent, view: page.view(), key: page.$("f-apikey").value, block: page.visible("usage-block"),
                       basis: page.visible("usage-basis") && page.$("usage-basis").textContent,
                       tiles: tiles.map((node) => [node.className, ...node.children.map((child) => child.textContent)]),
                       points: page.$("summary-points").textContent, whole: page.$("view-report").textContent + page.$("usage").textContent };
            }""",
            key=KEY, response=self.response(self.read(insights=(200, summary(27, 5)), users=403)),
        )  # fmt: skip
        self.assertEqual(got["sent"], {"url": "https://n8n.example.test", "api_key": KEY})
        self.assertEqual((got["view"], got["key"], got["block"]), ("report", "", True))
        self.assertEqual(got["basis"], "Executions over the last 7 days, as n8n's Insights counts them. Insights can lag behind by up to an hour.")
        self.assertEqual(
            got["tiles"],
            [
                ["usage__item", "Executions per day", "3.86"],
                ["usage__item", "Failure rate", "18.5%"],
                ["usage__item", "Active workflows", "3", "as far as this key's user can see"],
                ["usage__item usage__item--unread", "Users", "Not read", "the API key cannot list users: its scopes leave that out, or the licence limits the number of users"],
            ],
        )
        self.assertIn("Usage numbers were read through the n8n API.", got["points"])
        self.assertNotIn(MARK, json.dumps([got["tiles"], got["basis"], got["points"], got["whole"]]))

    def test_without_a_key_there_is_no_usage_block(self):
        got = page_tests.run_page(
            """async (page, input) => {
              page.type("f-url", "https://n8n.example.test");
              await page.submit();
              const sent = JSON.parse(page.sent[0]);
              await page.finishRun(input.response);
              return { sent, block: page.visible("usage-block"), tiles: page.walk(page.$("usage")).length };
            }""",
            response=self.response(None),
        )
        self.assertEqual(got, {"sent": {"url": "https://n8n.example.test"}, "block": False, "tiles": 0})

    def test_a_key_without_a_url_is_stopped_on_the_page(self):
        got = page_tests.run_page(
            """async (page, input) => {
              page.type("f-apikey", input.key);
              page.type("f-env", "A=1");
              await page.submit();
              return { sent: page.sent.length, error: page.visible("e-apikey") && page.$("e-apikey").textContent,
                       invalid: page.$("f-apikey").getAttribute("aria-invalid"), focus: document.activeElement.id, kept: page.$("f-apikey").value === input.key };
            }""",
            key=KEY,
        )
        self.assertEqual(got, {"sent": 0, "error": "Add the n8n URL this key belongs to, or clear the key. The key alone is not a source.", "invalid": "true", "focus": "f-url", "kept": True})

    def test_the_error_that_asks_for_the_url_goes_when_the_url_is_typed(self):
        got = page_tests.run_page(
            """async (page, input) => {
              const state = () => [page.visible("e-apikey"), page.$("f-apikey").getAttribute("aria-invalid")];
              page.type("f-apikey", input.key);
              page.type("f-env", "A=1");
              await page.submit();
              const shown = state();
              page.type("f-env", "A=2");
              const other = state();
              page.type("f-url", "h");
              const typed = state();
              // A key the server refused is not put right by the URL: that error stays.
              page.type("f-url", "https://n8n.example.test");
              await page.submit();
              await page.finishRun(JSON.stringify({ error: "the API key must be a single line", field: "api_key" }), 400);
              page.type("f-url", "https://n8n.example.test/");
              return { shown, other, typed, refused: state() };
            }""",
            key=KEY,
        )
        self.assertEqual(got, {"shown": [True, "true"], "other": [True, "true"], "typed": [False, None], "refused": [True, "true"]})

    def test_a_key_the_server_refuses_is_shown_next_to_its_field(self):
        got = page_tests.run_page(
            """async (page, input) => {
              page.type("f-url", "https://n8n.example.test");
              page.type("f-apikey", input.key);
              await page.submit();
              await page.finishRun(JSON.stringify({ error: "the API key must be a single line", field: "api_key" }), 400);
              return { view: page.view(), error: page.visible("e-apikey") && page.$("e-apikey").textContent, focus: document.activeElement.id,
                       banner: page.visible("form-error"), debug: page.visible("e-debug") };
            }""",
            key=KEY,
        )
        self.assertEqual(got, {"view": "form", "error": "The API key must be a single line.", "focus": "f-apikey", "banner": False, "debug": False})

    def test_a_refused_key_and_a_partial_result_are_shown_as_not_read(self):
        got = page_tests.run_page(
            """async (page, input) => {
              page.type("f-url", "https://n8n.example.test");
              page.type("f-apikey", input.key);
              await page.submit();
              await page.finishRun(input.response);
              return { basis: page.visible("usage-basis"), values: page.walk(page.$("usage")).filter((node) => node.className === "usage__value").map((node) => node.textContent),
                       notes: page.walk(page.$("usage")).filter((node) => node.className === "usage__note").map((node) => node.textContent), points: page.$("summary-points").textContent };
            }""",
            key=KEY, response=self.response(self.read(key="n8n_api_refused")),
        )  # fmt: skip
        self.assertEqual(got["basis"], False)
        self.assertEqual(got["values"], ["Not read"] * 4)
        self.assertEqual(got["notes"], ["the API key was refused"] * 4)
        self.assertIn("The n8n API key was refused, so no usage number was read.", got["points"])

    def test_the_page_names_the_source_and_the_field_is_masked_and_explained(self):
        with open(web.STATIC_DIR / "index.html", encoding="utf-8") as handle:
            html = handle.read()
        with open(web.STATIC_DIR / "app.js", encoding="utf-8") as handle:
            script = handle.read()
        field = html.split('id="f-apikey"')[1].split(">")[0]
        self.assertIn('data-masked="true"', field)  # not readable over the shoulder; "Show" reveals it
        self.assertIn('autocomplete="off"', field)
        self.assertNotIn("value=", field)
        hint = html.split('id="h-apikey">')[1].split("</p>")[0]
        for said in ("Settings &gt; n8n API", "<code>GET</code>", "insights:read", "execution:list", "workflow:list", "user:list", "user:read", "owner or of an admin", "delete it there afterwards"):
            self.assertIn(said, hint)
        self.assertLess(html.index('id="status"'), html.index('id="usage-block"'))
        self.assertLess(html.index('id="usage-block"'), html.index('id="scores-title"'))
        self.assertIn('api: { label: "n8n API (usage numbers)" }', script)
        self.assertIn('$("f-apikey").value = "";', script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)


if __name__ == "__main__":
    unittest.main()
