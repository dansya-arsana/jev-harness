"""Every --flag on a README line that mentions onboard.py, install.sh or
install.ps1 must be an option of onboard.build_parser()."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"
ONBOARD = SCRIPTS / "onboard.py"
IGNORED = {"--legacy", "--hooks"}  # wrapper-only flags; -Legacy has one dash
TRIGGERS = ("onboard.py", "install.sh", "install.ps1")
FLAG_RE = re.compile(r"(?<![\w-])--[A-Za-z][A-Za-z0-9-]*")


@unittest.skipUnless(ONBOARD.exists(), "scripts/onboard.py not present yet")
class ReadmeFlags(unittest.TestCase):
    def test_flags_exist(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import onboard
        finally:
            sys.path.remove(str(SCRIPTS))
        known = set()
        for action in onboard.build_parser()._actions:
            known.update(o for o in action.option_strings if o.startswith("--"))
        text = (REPO / "README.md").read_text(encoding="utf-8")
        bad = []
        seen = 0
        for n, line in enumerate(text.splitlines(), 1):
            if not any(t in line for t in TRIGGERS):
                continue
            for flag in FLAG_RE.findall(line):
                if flag in IGNORED:
                    continue
                seen += 1
                if flag not in known:
                    bad.append("README.md:%d %s" % (n, flag))
        self.assertGreater(seen, 0, "no flags found; check the regex")
        self.assertEqual(bad, [], "README flags not in onboard.build_parser()")


if __name__ == "__main__":
    unittest.main()
