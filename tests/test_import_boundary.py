"""Only gateway.py may reach processes, locks, clocks and the filesystem.

Banned outside ``gateway.py``: importing ``subprocess``, ``fcntl``, ``time``, ``os``,
``signal``, ``shutil``, ``tempfile``, ``glob``, ``multiprocessing``, ``ctypes``, ``select``,
``selectors`` and the dynamic-import helpers ``importlib``, ``builtins``, ``posix`` and
``_posixsubprocess``; ``__import__`` calls whose argument is not exactly one string constant;
re-importing a banned name from the gateway module (``from .gateway import os``); calls to
clock-reading attributes (``now``, ``utcnow``, ``today``, ``fromtimestamp``); and calls to the
builtin ``open``.

Deliberately not banned: ``importlib.metadata`` (package version lookup only), and
``pathlib`` and ``datetime`` imports, because pure path and time arithmetic is legitimate
(design.md needs ``datetime`` and ``zoneinfo``). ``sqlite3`` stays allowed. Filesystem IO
through ``pathlib`` methods is therefore covered by review only. No module that design.md or
plan.md needs outside the gateway is banned here. ``eval`` and ``exec`` are not checked.
"""

import ast
from pathlib import Path

import notice_route

BANNED = frozenset(
    {
        "subprocess",
        "fcntl",
        "time",
        "os",
        "signal",
        "shutil",
        "tempfile",
        "glob",
        "multiprocessing",
        "ctypes",
        "select",
        "selectors",
        "importlib",
        "builtins",
        "posix",
        "_posixsubprocess",
    }
)
REEXPORT_BANNED = frozenset({"os", "subprocess", "fcntl", "time"})
CLOCK_ATTRS = frozenset({"now", "utcnow", "today", "fromtimestamp"})
ALLOWED = frozenset({"gateway.py"})
PACKAGE_DIR = Path(notice_route.__file__).parent


def _is_gateway_module(module: str, level: int) -> bool:
    if level == 0:
        return module == "notice_route.gateway"
    return module == "gateway" or module.endswith(".gateway")


def _banned_module(name: str) -> bool:
    # importlib.metadata only reads installed package metadata (the version); it is allowed.
    if name == "importlib.metadata" or name.startswith("importlib.metadata."):
        return False
    return name.split(".")[0] in BANNED


def violations(source: str) -> list[str]:
    """Return a description of every boundary violation in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if _banned_module(a.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and module == "importlib":
                if any(a.name != "metadata" for a in node.names):
                    found.append(module)
            elif node.level == 0 and _banned_module(module):
                found.append(module)
            if _is_gateway_module(module, node.level):
                found.extend(f"gateway.{a.name}" for a in node.names if a.name in REEXPORT_BANNED)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                arg = node.args[0] if len(node.args) == 1 and not node.keywords else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    found.append("__import__(dynamic)")
                elif _banned_module(arg.value):
                    found.append(arg.value)
            elif isinstance(func, ast.Name) and func.id == "open":
                found.append("open()")
            elif isinstance(func, ast.Attribute) and func.attr in CLOCK_ATTRS:
                found.append(f".{func.attr}()")
    return found


def test_checker_catches_each_banned_form() -> None:
    assert violations("import subprocess") == ["subprocess"]
    assert violations("import os.path") == ["os.path"]
    assert violations("from fcntl import flock") == ["fcntl"]
    assert violations("from time import monotonic") == ["time"]
    assert violations("def f():\n    import time as t\n") == ["time"]
    assert violations("x = __import__('os')") == ["os"]


def test_checker_catches_the_wider_banned_modules() -> None:
    for name in (
        "signal",
        "shutil",
        "tempfile",
        "glob",
        "multiprocessing",
        "ctypes",
        "select",
        "selectors",
    ):
        assert violations(f"import {name}") == [name]


def test_checker_catches_dynamic_import_helpers() -> None:
    for name in ("importlib", "builtins", "posix", "_posixsubprocess"):
        assert violations(f"import {name}") == [name]
    assert violations("from importlib import import_module") == ["importlib"]
    assert violations("from importlib import metadata") == []
    assert violations("from importlib.metadata import version") == []
    assert violations("import importlib.metadata") == []
    assert violations("import importlib.util") == ["importlib.util"]
    assert violations("__import__(name)") == ["__import__(dynamic)"]
    assert violations("__import__('a' + 'b')") == ["__import__(dynamic)"]
    assert violations("__import__('os', fromlist=['x'])") == ["__import__(dynamic)"]
    assert violations("__import__('os', {}, {})") == ["__import__(dynamic)"]
    assert violations("__import__()") == ["__import__(dynamic)"]


def test_checker_catches_reexports_from_the_gateway() -> None:
    assert violations("from .gateway import os") == ["gateway.os"]
    assert violations("from notice_route.gateway import subprocess") == ["gateway.subprocess"]
    assert violations("from notice_route.gateway import fcntl, time") == [
        "gateway.fcntl",
        "gateway.time",
    ]
    assert violations("from .gateway import Gateway, FakeClock") == []


def test_checker_catches_clock_reads_and_open() -> None:
    assert violations("import datetime\nx = datetime.datetime.now()") == [".now()"]
    assert violations("d.utcnow()") == [".utcnow()"]
    assert violations("d.today()") == [".today()"]
    assert violations("d.fromtimestamp(1)") == [".fromtimestamp()"]
    assert violations("f = open('x')") == ["open()"]


def test_checker_allows_pathlib_datetime_and_sqlite3() -> None:
    source = "import pathlib\nimport datetime\nimport sqlite3\nfrom zoneinfo import ZoneInfo"
    assert violations(source) == []


def test_checker_ignores_relative_and_unrelated_imports() -> None:
    assert violations("import sys\nfrom . import time\nfrom .os import x") == []


def scan(package_dir: Path) -> dict[str, list[str]]:
    """Violations per file keyed by relative path; only top-level gateway.py is exempt."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        rel = path.relative_to(package_dir).as_posix()
        if rel in ALLOWED:
            continue
        if hits := violations(path.read_text("utf-8")):
            offenders[rel] = hits
    return offenders


def test_only_gateway_touches_process_lock_clock_and_filesystem() -> None:
    assert scan(PACKAGE_DIR) == {}


def test_scan_is_recursive_and_only_top_level_gateway_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "gateway.py").write_text("import os\n", "utf-8")
    (tmp_path / "sub" / "deep.py").write_text("import time\n", "utf-8")
    (tmp_path / "gateway.py").write_text("import os\n", "utf-8")
    assert scan(tmp_path) == {"sub/gateway.py": ["os"], "sub/deep.py": ["time"]}


def test_gateway_is_the_one_module_that_does_import_them() -> None:
    assert violations((PACKAGE_DIR / "gateway.py").read_text("utf-8"))
