"""Static guardrail: the shipped scripts must not contain mock or simulated output paths."""
import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "migrate-from-claude" / "scripts"
FORBIDDEN_NAMES = {"mock", "Mock", "MagicMock", "patch", "fake_result", "simulate", "dummy"}
FORBIDDEN_IMPORTS = {"unittest.mock", "mock"}


def test_scripts_have_no_mock_paths():
    for script in SCRIPTS.glob("*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = {a.name for a in node.names}
                assert mod not in FORBIDDEN_IMPORTS and not names & FORBIDDEN_IMPORTS, f"{script.name} imports a mock library"
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES, f"{script.name} uses '{node.id}'"
            if isinstance(node, ast.FunctionDef):
                assert not any(w in node.name.lower() for w in ("mock", "fake", "simulate")), f"{script.name}: {node.name}"


def test_scripts_are_stdlib_only():
    allowed = {"__future__", "argparse", "datetime", "json", "os", "re", "shutil", "sys", "pathlib"}
    for script in SCRIPTS.glob("*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] in allowed, f"{script.name} imports {a.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in allowed, f"{script.name} imports {node.module}"
