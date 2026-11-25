"""Only gateway.py may import the process, lock, clock and filesystem modules."""

import ast
from pathlib import Path

import notice_route

BANNED = frozenset({"subprocess", "fcntl", "time", "os"})
ALLOWED = frozenset({"gateway.py"})
PACKAGE_DIR = Path(notice_route.__file__).parent


def banned_imports(source: str) -> list[str]:
    """Return banned top-level module names imported anywhere in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else []
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]
        else:
            continue
        found.extend(n for n in names if n.split(".")[0] in BANNED)
    return found


def test_checker_catches_each_banned_form() -> None:
    assert banned_imports("import subprocess") == ["subprocess"]
    assert banned_imports("import os.path") == ["os.path"]
    assert banned_imports("from fcntl import flock") == ["fcntl"]
    assert banned_imports("from time import monotonic") == ["time"]
    assert banned_imports("def f():\n    import time as t\n") == ["time"]
    assert banned_imports("x = __import__('os')") == ["os"]


def test_checker_ignores_relative_and_unrelated_imports() -> None:
    assert banned_imports("import sys\nfrom . import time\nfrom .os import x") == []


def test_only_gateway_imports_process_lock_clock_and_filesystem_modules() -> None:
    offenders = {
        path.name: hits
        for path in sorted(PACKAGE_DIR.glob("*.py"))
        if path.name not in ALLOWED and (hits := banned_imports(path.read_text("utf-8")))
    }
    assert offenders == {}


def test_gateway_is_the_one_module_that_does_import_them() -> None:
    assert banned_imports((PACKAGE_DIR / "gateway.py").read_text("utf-8"))
