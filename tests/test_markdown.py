"""README renderer checks, run with Node when it is installed (skipped otherwise)."""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "node not installed")
class MarkdownRenderer(unittest.TestCase):
    def test_markdown_js(self):
        out = subprocess.run(["node", str(HERE / "markdown_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("all markdown checks passed", out.stdout)


if __name__ == "__main__":
    unittest.main()
