"""Offline check that requirements-hosted.lock pins every transitive dependency.

Runs only under the hosted venv (where `mcp` is importable). It compares the
lock with the installed environment: every installed dependency (transitively)
of a locked package must itself be locked, and every pin must equal the
installed version.
"""
import importlib.util
import re
import unittest
from importlib import metadata
from pathlib import Path

LOCK = Path(__file__).resolve().parent.parent / "requirements-hosted.lock"


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock():
    pins = {}
    for line in LOCK.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pins[norm(name)] = version.strip()
    return pins


def requirement_name(req):
    if re.search(r"extra\s*==", req):
        return None
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    return norm(m.group(1)) if m else None


@unittest.skipUnless(
    importlib.util.find_spec("mcp") is not None,
    "not running under the hosted venv (mcp is not importable)",
)
class HostedLockTest(unittest.TestCase):
    def test_every_transitive_dependency_is_pinned(self):
        pins = read_lock()
        installed = {norm(d.metadata["Name"]): d for d in metadata.distributions()}
        seen, todo = set(), [n for n in pins if n in installed]
        while todo:
            name = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            for req in installed[name].requires or []:
                dep = requirement_name(req)
                if dep in installed and dep not in seen:
                    todo.append(dep)
        missing = sorted(installed[n].metadata["Name"] + "==" + installed[n].version
                         for n in seen if n not in pins)
        self.assertEqual(missing, [], "unpinned transitive dependencies in the lock")

    def test_pins_match_installed_versions(self):
        mismatched = []
        for name, version in read_lock().items():
            try:
                have = metadata.version(name)
            except metadata.PackageNotFoundError:
                have = None
            if have != version:
                mismatched.append((name, version, have))
        self.assertEqual(mismatched, [])


if __name__ == "__main__":
    unittest.main()
