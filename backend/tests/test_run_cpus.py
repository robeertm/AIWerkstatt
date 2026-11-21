"""CPU cap per agent container (0.40.0).

Run: cd backend && python3 -m unittest tests.test_run_cpus
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="aiw-cpus-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ.setdefault("AIWERKSTATT_DATA", _TMP)
os.environ.setdefault("AIWERKSTATT_WORKSPACES", _TMP)
os.environ.setdefault("AIWERKSTATT_PROGRESS_DIR", _TMP)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import orchestrator  # noqa: E402


class RunCpus(unittest.TestCase):
    def test_unset_or_invalid_means_no_cap(self):
        for raw in ("", " ", "0", "-1", "abc", "nan-x"):
            self.assertIsNone(orchestrator.run_nano_cpus(raw), raw)

    def test_cores_become_nano_cpus(self):
        self.assertEqual(orchestrator.run_nano_cpus("2"), 2_000_000_000)
        self.assertEqual(orchestrator.run_nano_cpus("1.5"), 1_500_000_000)


if __name__ == "__main__":
    unittest.main()
