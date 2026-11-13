import contextlib
import importlib.util
import io
import subprocess
import unittest
from pathlib import Path
from unittest import mock

LOAD_PY = Path(__file__).resolve().parent.parent / "lab" / "load.py"
spec = importlib.util.spec_from_file_location("lab_load", LOAD_PY)
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


class LoadTest(unittest.TestCase):
    def run_main(self, ps_result, ready=None):
        """Run main() for the bad stack with a canned `compose ps` result."""
        with mock.patch.object(load, "compose", return_value=ps_result) as compose, \
                mock.patch.object(load, "ready", side_effect=ready) as ready_mock, \
                mock.patch("sys.argv", ["load.py", "bad"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                load.main()
        return str(raised.exception.code), compose, ready_mock

    def test_fails_at_once_when_the_stack_is_not_up(self):
        message, compose, ready = self.run_main(subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""))
        self.assertIn("Is the bad stack up?", message)
        self.assertEqual(compose.call_args.args[:2], ("bad", "ps"))
        ready.assert_not_called()  # never entered the two-minute wait

    def test_waits_for_a_stack_that_is_starting(self):
        # n8n sits in "created" until Postgres and Redis are healthy, then may restart.
        message, compose, ready = self.run_main(
            subprocess.CompletedProcess([], 0, stdout=b"0123abcd\n", stderr=b""),
            ready=SystemExit("reached the readiness wait"),
        )
        self.assertEqual(message, "reached the readiness wait")
        ready.assert_called_once()
        statuses = [a for flag, a in zip(compose.call_args.args, compose.call_args.args[1:]) if flag == "--status"]
        self.assertEqual(sorted(statuses), ["created", "restarting", "running"])

    def test_docker_compose_failure_is_not_reported_as_a_stopped_stack(self):
        down = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"Cannot connect to the Docker daemon")
        message, _, ready = self.run_main(down)
        self.assertIn("docker compose failed", message)
        self.assertIn("Cannot connect to the Docker daemon", message)
        self.assertNotIn("stack up?", message)
        ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
