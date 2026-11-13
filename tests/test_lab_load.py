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
    def test_fails_at_once_when_the_stack_is_not_up(self):
        no_container = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with mock.patch.object(load, "compose", return_value=no_container) as compose, \
                mock.patch.object(load, "ready") as ready, \
                mock.patch("sys.argv", ["load.py", "bad"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                load.main()
        self.assertIn("Is the bad stack up?", str(raised.exception.code))
        self.assertEqual(compose.call_args.args[:2], ("bad", "ps"))
        ready.assert_not_called()  # never entered the two-minute wait


if __name__ == "__main__":
    unittest.main()
