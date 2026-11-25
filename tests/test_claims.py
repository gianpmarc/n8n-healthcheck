"""The claims of docs/what-it-does.md and of the first step of the page, held by tests.

That document is written for someone deciding whether to allow the tool, so every
sentence of it that says what the tool does or never does has to be true of the code.
Most were already held by a test elsewhere; the document's last section names them. The
tests here hold the ones that were not: what each collector asks of its source, where a
run connects to, which modules can open a connection at all, and that the document and
the page say the same thing as each other and as the code.
"""

import ast
import contextlib
import io
import os
import re
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import n8n_healthcheck
import test_debug_info as pasted  # the synthetic debug information; imported as a module so its tests run once
from n8n_healthcheck import debug_info, profile
from n8n_healthcheck.collectors import collect, postgres
from n8n_healthcheck.models import Target
from n8n_healthcheck.rules import ALL_RULES
from n8n_healthcheck.web import server as web

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "n8n_healthcheck"
DOCUMENT = (ROOT / "docs" / "what-it-does.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
INDEX_HTML = (web.STATIC_DIR / "index.html").read_text(encoding="utf-8")
STEP_ONE = INDEX_HTML[INDEX_HTML.index('id="step-1"') : INDEX_HTML.index('id="step-2"')]
TESTS = Path(__file__).resolve().parent

KEY = "n8n_api_" + "k" * 40  # synthetic


def quiet(target):
    """collect() with its warnings kept off the terminal; returns (snapshot, warnings)."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        return collect(target), stderr.getvalue()


class FakeStream(io.BytesIO):
    """Stands in for sock.makefile(): replays canned replies and records what was sent."""

    def __init__(self, replies: bytes):
        super().__init__(replies)
        self.sent = b""

    def write(self, data: bytes) -> int:
        self.sent += data
        return len(data)


def resp_commands(sent: bytes) -> list:
    """The commands in what a client wrote to Redis, each as a list of its words."""
    commands, parts = [], sent.split(b"\r\n")
    index = 0
    while index < len(parts) and parts[index].startswith(b"*"):
        count = int(parts[index][1:])
        commands.append([parts[index + 2 + 2 * word].decode() for word in range(count)])
        index += 1 + 2 * count
    return commands


class OnlyReadsTest(unittest.TestCase):
    """"It only reads": what each collector asks of its source, in full."""

    def test_the_n8n_url_gets_three_get_requests_without_a_body(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def handle_one_request(self):
                super().handle_one_request()

            def do_GET(self):
                seen.append((self.command, self.path, self.headers.get("Content-Length"), sorted(name.lower() for name in self.headers)))
                body = b"n8n_version_info{version=\"v2.47.0\"} 1\n" if self.path == "/metrics" else b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def __getattr__(self, name):  # any other method (POST, PUT, DELETE, PATCH...) is recorded too
                if name.startswith("do_"):
                    return lambda: (seen.append((self.command, self.path, None, [])), self.send_error(405))
                raise AttributeError(name)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        snapshot, warnings = quiet(Target(url=f"http://127.0.0.1:{server.server_address[1]}"))
        self.assertEqual(warnings, "")
        self.assertEqual(snapshot.n8n_version, "2.47.0")
        self.assertEqual([(method, path, length) for method, path, length, _ in seen], [("GET", "/healthz", None), ("GET", "/healthz/readiness", None), ("GET", "/metrics", None)])
        # Nothing of the operator goes with them: no cookie, no authorization, no key.
        for _, _, _, headers in seen:
            self.assertEqual(set(headers) & {"authorization", "cookie", "x-n8n-api-key"}, set())

    def test_postgres_gets_one_read_only_transaction_of_selects(self):
        calls = []
        with mock.patch.object(postgres, "run", side_effect=lambda args, **kwargs: calls.append(args) or ""):
            quiet(Target(postgres_dsn="postgresql://n8n:synthetic@db.example.test:5432/n8n"))
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertEqual(args[0], "psql")
        self.assertIn("-X", args)  # no start-up file of the user is run
        self.assertEqual(args.count("-c"), 1)
        sql = args[args.index("-c") + 1]
        # The server itself is told to refuse a write, before the query, in the same transaction.
        self.assertTrue(sql.startswith("SET LOCAL transaction_read_only = on;"))
        statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
        self.assertEqual([statement.split()[0].upper() for statement in statements], ["SET", "SET", "SELECT"])
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE", "COPY", "VACUUM", "CALL", "DO", "LOCK", "REINDEX", "COMMIT", "NOTIFY"):
            self.assertIsNone(re.search(rf"\b{word}\b", sql, re.I), word)
        self.assertNotIn("synthetic", " ".join(args))  # the password is not on the command line

    def test_redis_gets_only_commands_that_read(self):
        info = "redis_version:7.2.4\r\nused_memory:1\r\nmaxmemory:0\r\nmaxmemory_policy:noeviction\r\n"
        replies = b"+OK\r\n+OK\r\n" + b"$%d\r\n%s\r\n" % (len(info), info.encode()) + b":1\r\n:7\r\n:2\r\n:31\r\n"
        sock = mock.MagicMock()
        stream = sock.makefile.return_value = FakeStream(replies)
        with mock.patch("socket.create_connection", return_value=sock):
            snapshot, warnings = quiet(Target(redis_url="redis://:synthetic@cache.example.test/2"))
        self.assertEqual((warnings, snapshot.redis["waiting_jobs"]), ("", 7))
        commands = resp_commands(stream.sent)
        self.assertEqual([command[0] for command in commands], ["AUTH", "SELECT", "INFO", "EXISTS", "LLEN", "LLEN", "ZCARD"])
        # AUTH and SELECT open the session; the rest only read. No key is written, moved or deleted.
        self.assertEqual({command[0] for command in commands} - {"AUTH", "SELECT"}, {"INFO", "EXISTS", "LLEN", "ZCARD"})

    def test_kubectl_gets_two_get_commands_and_never_asks_for_a_secret(self):
        calls = []
        with mock.patch("n8n_healthcheck.collectors.kubernetes.run", side_effect=lambda args, **kwargs: calls.append(args) or '{"items": []}'):
            snapshot, warnings = quiet(Target(kube_namespace="n8n", kube_context="synthetic-context"))
        self.assertEqual((warnings, snapshot.kubernetes), ("", {"deployments": []}))
        self.assertEqual(
            calls,
            [
                ["kubectl", "--context", "synthetic-context", "get", kind, "-n", "n8n", "-o", "json", "--request-timeout=5s"]
                for kind in ("deployments", "configmaps")
            ],
        )
        self.assertNotIn("secret", " ".join(part for call in calls for part in call).lower())
        # Without a context, the current one of kubectl is used: no flag chooses another.
        calls.clear()
        with mock.patch("n8n_healthcheck.collectors.kubernetes.run", side_effect=lambda args, **kwargs: calls.append(args) or '{"items": []}'):
            quiet(Target(kube_namespace="n8n"))
        self.assertEqual([call[:2] for call in calls], [["kubectl", "get"], ["kubectl", "get"]])

    def test_the_env_file_is_opened_to_be_read_and_left_as_it_was(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "n8n.env")
            content = "EXECUTIONS_MODE=queue\nN8N_ENCRYPTION_KEY=synthetic-value\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(path, stat.S_IRUSR)  # a file that cannot be written is collected all the same
            before = os.stat(path).st_mtime_ns
            snapshot, warnings = quiet(Target(env_file=path))
            self.assertEqual(warnings, "")
            self.assertEqual(snapshot.env["EXECUTIONS_MODE"], "queue")
            self.assertNotIn("synthetic-value", str(snapshot.env))  # a secret value is redacted as it is read
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), content)
            self.assertEqual(os.stat(path).st_mtime_ns, before)
            self.assertEqual(os.listdir(folder), ["n8n.env"])


class ContactsOnlyWhatWasGivenTest(unittest.TestCase):
    """"It runs here and contacts only what you point it at"."""

    def test_a_run_with_every_source_connects_to_the_hosts_given_and_starts_psql_and_kubectl(self):
        reached, started = [], []

        def connect(address, *args, **kwargs):
            reached.append(address)
            raise OSError("refused")

        def run(args, **kwargs):
            started.append(args)
            raise FileNotFoundError(args[0])

        with tempfile.TemporaryDirectory() as folder:
            env_file = os.path.join(folder, "n8n.env")
            with open(env_file, "w", encoding="utf-8") as handle:
                handle.write("EXECUTIONS_MODE=queue\n")
            target = Target(
                url="http://n8n.example.test:5678",
                api_key=KEY,
                env_file=env_file,
                postgres_dsn="postgresql://n8n:synthetic@db.example.test:5433/n8n",
                redis_url="redis://:synthetic@cache.example.test:6380/0",
                kube_namespace="n8n",
                kube_context="synthetic-context",
                debug_info=debug_info.parse(pasted.FULL),
            )
            # No proxy of the machine running the tests: this is about where the tool itself connects.
            with mock.patch.dict(os.environ, {}, clear=False), mock.patch("urllib.request.getproxies", return_value={}):
                with mock.patch("socket.create_connection", side_effect=connect), mock.patch("subprocess.run", side_effect=run):
                    snapshot, _ = quiet(target)
        self.assertEqual(snapshot.env, {"EXECUTIONS_MODE": "queue"})
        self.assertEqual(set(reached), {("n8n.example.test", 5678), ("cache.example.test", 6380)})
        self.assertEqual({args[0] for args in started}, {"psql", "kubectl"})
        psql = next(args for args in started if args[0] == "psql")
        self.assertIn("db.example.test:5433", psql[psql.index("-d") + 1])
        for args in started:
            if args[0] == "kubectl":
                self.assertEqual(args[1:3], ["--context", "synthetic-context"])
                self.assertEqual(args[args.index("-n") + 1], "n8n")

    def test_a_source_that_was_not_given_is_not_contacted(self):
        with mock.patch("socket.create_connection", side_effect=AssertionError("a connection was opened")):
            with mock.patch("subprocess.run", side_effect=AssertionError("a command was started")):
                snapshot, warnings = quiet(Target(debug_info=debug_info.parse(pasted.FULL)))
        self.assertEqual(warnings, "")
        self.assertEqual(snapshot.debug_info["n8n_version"], "3.3.0")

    def test_only_the_collectors_and_the_local_server_can_open_a_connection_or_start_a_command(self):
        # Every module of the package, by what it imports. A new import of one of these
        # anywhere else has to be added here, and to docs/what-it-does.md.
        able = {"socket", "ssl", "urllib.request", "http.client", "http.server", "socketserver", "subprocess", "webbrowser", "smtplib", "ftplib", "poplib",
                "imaplib", "telnetlib", "xmlrpc", "asyncio", "multiprocessing", "ctypes", "requests", "urllib3", "httpx", "aiohttp"}  # fmt: skip
        found = {}
        for path in sorted(PACKAGE.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            names = set()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names.add(node.module)
                    names.update(f"{node.module}.{alias.name}" for alias in node.names)
            used = {name for name in names if name in able or name.split(".")[0] in able and name.rsplit(".", 1)[0] in able}
            used = {name for name in used if name in able}
            if used:
                found[str(path.relative_to(PACKAGE))] = used
            for call in ("os.system", "os.popen", "os.exec", "os.spawn", "__import__", "importlib"):
                self.assertNotIn(call, source, f"{path.name}: {call}")
        self.assertEqual(
            found,
            {
                "collectors/_util.py": {"subprocess"},  # psql and kubectl
                "collectors/api.py": {"socket", "http.client", "urllib.request"},  # the n8n API, with the key
                "collectors/http.py": {"urllib.request"},  # the health endpoints and /metrics
                "collectors/redis.py": {"socket", "ssl"},
                "web/server.py": {"socket", "http.server", "webbrowser"},  # listens; opens the local page
            },
        )
        # The server listens and answers. It never connects out, and the page it opens is its own.
        server = (PACKAGE / "web" / "server.py").read_text(encoding="utf-8")
        for way_out in ("create_connection", "urlopen", ".connect(", "urllib.request", "http.client"):
            self.assertNotIn(way_out, server)
        self.assertEqual(server.count("webbrowser.open"), 1)
        self.assertIn('url = f"http://{shown}:{server.server_address[1]}/"', server)

    def test_the_requests_to_the_n8n_url_follow_a_redirect_to_http_only_and_the_document_says_so(self):
        # The health and metrics requests use the default opener of urllib: they follow an
        # http(s) redirect and use the proxy settings of the machine. The key never does either
        # (tests/test_usage.py). The document and the first step say this instead of "only".
        from n8n_healthcheck.collectors import http

        handlers = {type(handler).__name__ for handler in http._open.__self__.handlers}
        self.assertIn("_HttpOnlyRedirects", handlers)
        self.assertIn("A redirect the n8n URL answers with is followed", STEP_ONE)
        self.assertIn("proxy settings", STEP_ONE)
        self.assertIn("use the proxy settings of the machine, if it has any, and follow a redirect to another `http` or `https` address", " ".join(DOCUMENT.split()))


class DocumentTest(unittest.TestCase):
    """The document, the README and the first step say the same thing, and it is what the code does."""

    NEVER = ("secret", "host name", "URL", "IP address", "file path", "e-mail address", "Deployment", "namespace", "registry name")

    def test_the_readme_and_the_first_step_link_to_the_document(self):
        self.assertIn("(docs/what-it-does.md)", README.split("## Why")[0])  # from the top of the README
        self.assertIn('href="https://github.com/gianpmarc/n8n-healthcheck/blob/main/docs/what-it-does.md" target="_blank" rel="noopener noreferrer"', STEP_ONE)
        # The only address outside this server in the whole page, and it is a link the operator follows, not a request.
        outside = set(re.findall(r'(?:href|src|action)="(https?://[^"]+)"', INDEX_HTML))
        self.assertEqual(outside, {"https://github.com/gianpmarc/n8n-healthcheck/blob/main/docs/what-it-does.md"})

    def test_the_three_statements_of_the_first_step(self):
        text = re.sub(r"<[^>]+>", "", STEP_ONE)
        for sentence in (
            "It only reads. Nothing is changed on your instance, your database or your cluster.",
            "The check runs on the machine where n8n-healthcheck was started.",
            "It contacts the n8n URL, the database, Redis, and the cluster of the kubectl context you choose.",
            "The report and the file are sent nowhere.",
            "You end with one file, which you review and send yourself.",
            "You can leave out any entry of it except the n8n version, which is always part of the file.",
        ):
            self.assertIn(sentence, text)
        self.assertEqual(profile.ALWAYS_SHARED, "n8n.version")

    def test_what_the_file_never_contains_is_the_same_list_everywhere(self):
        for name, text in (("the first step", re.sub(r"<[^>]+>", "", STEP_ONE)), ("the document", DOCUMENT), ("the README", README)):
            with self.subTest(text=name):
                for item in self.NEVER:
                    self.assertIn(item, text)
        # With the one exception, stated wherever the list is.
        self.assertIn("One exception. The optional one-line reference you can type in step 5", re.sub(r"<[^>]+>", "", STEP_ONE))
        self.assertIn("reference", DOCUMENT.split("## What the profile never contains")[1].split("##")[0])
        # The exception is real: the reference is written as it is typed, whatever it holds.
        typed = "ticket 4711 at https://tickets.example.test/4711"
        self.assertEqual(profile.build_profile(n8n_healthcheck.models.Snapshot(), [], reference=typed)["run"]["reference"], typed)

    def test_the_numbers_and_names_the_document_gives_are_the_codes(self):
        self.assertIn(f"{len(ALL_RULES)} checks", DOCUMENT)
        self.assertIn(f"{debug_info.MAX_BYTES // 1024} KB", DOCUMENT)
        self.assertIn("`format_version` is 1", DOCUMENT)
        self.assertEqual(profile.FORMAT_VERSION, 1)
        self.assertIn("exit code 3", DOCUMENT)
        for call in ("/healthz", "/healthz/readiness", "/metrics", "/api/v1/insights/summary", "/api/v1/executions", "/api/v1/workflows?active=true", "/api/v1/users",
                     "kubectl get deployments", "kubectl get configmaps", "INFO", "EXISTS", "LLEN", "ZCARD", "SET LOCAL transaction_read_only = on"):  # fmt: skip
            self.assertIn(call, DOCUMENT, call)
        # The query the document describes is the one that is sent.
        for read in ("server_version", "execution_entity", "pg_stat_activity", "max_connections"):
            self.assertIn(read, postgres.QUERY)
            self.assertIn(read, DOCUMENT)

    def test_every_test_the_document_names_exists(self):
        named = re.findall(r"`(tests/test_\w+\.py)`(?:, `|: `)?((?:`?test_\w+`?(?:, )?)*)", DOCUMENT)
        table = re.findall(r"\| `(test_\w+\.py)` \| ((?:`\w+`(?:, )?)+) \|", DOCUMENT)
        self.assertGreater(len(table), 15, named)
        for file, tests in table:
            source = (TESTS / file).read_text(encoding="utf-8")
            for test in re.findall(r"`(\w+)`", tests):
                self.assertRegex(source, rf"\n    def {test}\(self", f"{file}: {test}")

    def test_the_document_is_for_whoever_asked_and_names_no_internal_system(self):
        for text in (DOCUMENT, re.sub(r"<[^>]+>", "", INDEX_HTML)):
            self.assertNotIn("customer", text.lower())
            self.assertNotIn("support engineer", text.lower())
        self.assertIn("Not an official n8n project", DOCUMENT)


if __name__ == "__main__":
    unittest.main()
