"""Exercise time selection and duplicate protection without submitting real scans."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
class ScheduleTests(unittest.TestCase):
    def test_slots_and_duplicate_claims(self):
        script = Path(__file__).with_name("macos-schedule.sh")
        for stamp, expected in (("1-0910", "0920"), ("2-1020", "1030"),
                                ("3-1445", "1455"), ("4-1530", "review"),
                                ("5-1545", "weekly"), ("4-1545", None),
                                ("6-0910", None), ("1-0920", None)):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bindir = root / "bin"
                bindir.mkdir()
                date = bindir / "date"
                date.write_text('#!/bin/sh\n[ "$TZ" = Asia/Shanghai ] || exit 1\n'
                                'case "$1" in +%u-%H%M) echo "$TEST_STAMP";; *) echo 2026-09-14;; esac\n')
                date.chmod(0o755)
                app = root / "Stock King.app"
                exe = app / "Contents/MacOS/Stock King"
                exe.parent.mkdir(parents=True)
                exe.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$HOME/calls"\n')
                exe.chmod(0o755)
                env = {**os.environ, "HOME": str(root), "TEST_STAMP": stamp,
                       "PATH": f"{bindir}:/usr/bin:/bin"}
                for _ in range(2):
                    for slot in ("0920", "1030", "1455", "review", "weekly"):
                        subprocess.run(["/bin/sh", str(script), str(app), slot], env=env, check=True)
                calls = root / "calls"
                self.assertEqual(calls.read_text() if calls.exists() else "",
                                 f"--stock-king-task={expected}\n" if expected else "")


if __name__ == "__main__":
    unittest.main()
