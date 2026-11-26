"""One fixture folder per detection rule in the README table (rules 1-16, plus 1b for AI projects)."""
import json
import tempfile
import unittest
from pathlib import Path

from support import make_tree, server

# rule: (files, expected kind, expected command prefix or None, extra checks)
RULES = {
    "01 slothmore.json": ({"slothmore.json": json.dumps({"name": "Custom", "cmd": "python3 tool.py"})},
                          "Script", "python3 tool.py", {"name": "Custom"}),
    "01 launcher.json (compat)": ({"launcher.json": json.dumps({"cmd": "echo hi", "web": True})},
                                  "Web", "echo hi", {"web": True}),
    "01 override beats package.json": ({"slothmore.json": json.dumps({"cmd": "echo override"}),
                                        "package.json": json.dumps({"scripts": {"dev": "vite"}})},
                                       "Script", "echo override", {}),
    "1b streamlit": ({"app.py": "import streamlit as st\nst.write('hi')\n"}, "Streamlit", "streamlit run app.py", {}),
    "02 package.json dev (pnpm)": ({"package.json": json.dumps({"scripts": {"dev": "vite", "build": "x"}}),
                                    "pnpm-lock.yaml": ""}, "Node", "pnpm run dev", {"choice": "dev"}),
    "02 package.json start (npm)": ({"package.json": json.dumps({"scripts": {"start": "node s.js"}})},
                                    "Node", "npm start", {}),
    "03 Makefile dev": ({"Makefile": "dev:\n\techo dev\nbuild:\n\techo b\n"}, "Make", "make dev", {}),
    "04 app.py Flask": ({"app.py": "from flask import Flask\napp = Flask(__name__)\n"}, "Python web", None,
                        {"web": True}),
    "04 main.py script": ({"main.py": "print('x')\n"}, "Script", None, {"web": False}),
    "05 pyproject scripts": ({"pyproject.toml": "[project]\nname='x'\n[project.scripts]\nxcli = 'x.cli:main'\n"},
                             "Python", None, {"choice": "xcli"}),
    "06 deno tasks": ({"deno.json": json.dumps({"tasks": {"dev": "deno run main.ts"}})}, "Deno", "deno task dev", {}),
    "07 go.mod": ({"go.mod": "module x\n", "main.go": "package main\nfunc main(){}\n"}, "Go", "go run .", {}),
    "07 Cargo.toml": ({"Cargo.toml": "[package]\nname='x'\n"}, "Rust", "cargo run", {}),
    "08 config.ru": ({"config.ru": "run ->(e){[200,{},['hi']]}\n"}, "Ruby web", "bundle exec rackup", {}),
    "09 compose.yaml": ({"compose.yaml": "services:\n  web:\n    image: nginx\n"}, "Compose", "docker compose up", {}),
    "10 run.sh": ({"run.sh": "#!/bin/bash\necho hi\n"}, "Script", "bash run.sh", {}),
    "11 index.js": ({"index.js": "console.log(1)\n"}, "Node", "node index.js", {}),
    "12 index.html": ({"index.html": "<h1>x</h1>"}, "HTML", None, {"static": True, "web": True}),
    "13 loose .py files": ({"a.py": "print(1)\n", "b.py": "if __name__ == '__main__':\n    print(2)\n"},
                           "Script", "python b.py", {"choice": "b.py"}),
    "14 package.json other scripts": ({"package.json": json.dumps({"scripts": {"lint": "eslint .", "build": "tsc"}})},
                                      "Node", None, {"needs_choice": True}),
    "15 Makefile other targets": ({"Makefile": "test:\n\techo t\nlint:\n\techo l\n"}, "Make", None,
                                  {"needs_choice": True}),
    "16 notebook": ({"nb.ipynb": "{}"}, "Jupyter", "jupyter lab", {}),
}


class DetectionRules(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="detect-"))

    def check(self, name, files, kind, cmd, extra):
        d = make_tree(self.tmp / name.replace(" ", "_").replace("(", "").replace(")", ""), files)
        info = server.detect(d)
        self.assertEqual(info["kind"], kind, "%s: %r" % (name, info))
        if cmd is not None:
            self.assertTrue((info.get("cmd") or "").startswith(cmd), "%s: cmd %r" % (name, info.get("cmd")))
        for k, v in extra.items():
            self.assertEqual(info.get(k), v, "%s: %s=%r" % (name, k, info.get(k)))

    def test_rule_count(self):
        numbered = {k.split()[0] for k in RULES}
        self.assertEqual(len(numbered - {"1b"}), 16)

    def test_all_rules(self):
        for name, (files, kind, cmd, extra) in RULES.items():
            with self.subTest(rule=name):
                self.check(name, files, kind, cmd, extra)

    def test_nothing_runnable(self):
        d = make_tree(self.tmp / "none", {"notes.txt": "x"})
        info = server.detect(d)
        self.assertFalse(info["detected"])
        self.assertIn("slothmore.json", info["error"])


if __name__ == "__main__":
    unittest.main()
