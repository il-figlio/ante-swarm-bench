"""Every module-qualified name used in the deployed files must be imported. Catches the `re.fullmatch` without
`import re` that would have crashed every hub-started run (found 17 Sep 2026); unit tests that never call main() cannot."""
import ast, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "runner" / "swarm_run.py", ROOT / "hub" / "server.py"]

def used_modules(tree):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name): names.add(n.value.id)
    return names

def bound_names(tree):
    bound = set(dir(__builtins__))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import): bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom): bound |= {a.asname or a.name for a in n.names}
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): bound.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): bound.add(n.id)
        elif isinstance(n, ast.arg): bound.add(n.arg)
        elif isinstance(n, (ast.For, ast.comprehension)):
            for t in ast.walk(n.target if hasattr(n, "target") else n):
                if isinstance(t, ast.Name): bound.add(t.id)
        elif isinstance(n, ast.ExceptHandler) and n.name: bound.add(n.name)
        elif isinstance(n, ast.Global): bound |= set(n.names)
    return bound

class ImportCoverage(unittest.TestCase):
    def test_every_used_module_is_imported(self):
        for f in FILES:
            tree = ast.parse(f.read_text()); missing = sorted(m for m in used_modules(tree) - bound_names(tree) if m in sys.stdlib_module_names)
            self.assertEqual(missing, [], f"{f.name}: stdlib modules used but never imported: {missing}")

if __name__ == "__main__": unittest.main()
