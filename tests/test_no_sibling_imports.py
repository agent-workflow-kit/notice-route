"""No test or fake imports a sibling repository; the fakes are copied per repository."""

from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SIBLINGS = frozenset(
    {
        "slack_notice",
        "ha_notice",
        "wsl_notice",
        "desktop_notice",
        "agent_spawn",
        "claude_session",
        "termctl",
        "wtctl",
        "forgectl",
        "pr_watch",
        "pr_comment",
        "memoctl",
        "ticketctl",
        "ticket_queue",
        "ticket_create",
        "review_run",
        "review_watch",
        "review_respond",
        "ticket_work",
        "session_audit",
        "self_improve",
        "jobsctl",
        "gatectl",
        "awkit",
    }
)


def imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_checker_sees_each_import_form() -> None:
    assert imported_roots("import ha_notice.cli") == {"ha_notice"}
    assert imported_roots("from slack_notice import x") == {"slack_notice"}
    assert imported_roots("from . import x\nimport os") == {"os"}


def offenders_under(root: Path) -> dict[str, list[str]]:
    return {
        path.relative_to(root).as_posix(): sorted(
            imported_roots(path.read_text("utf-8")) & SIBLINGS
        )
        for path in root.rglob("*.py")
        if imported_roots(path.read_text("utf-8")) & SIBLINGS
    }


def test_no_python_file_under_tests_imports_a_sibling() -> None:
    assert offenders_under(TESTS) == {}


def test_the_check_can_fail_for_every_listed_sibling(tmp_path: Path) -> None:
    for name in sorted(SIBLINGS):
        (tmp_path / f"{name}_user.py").write_text(f"import {name}\n", "utf-8")
    found = offenders_under(tmp_path)
    assert sorted(found) == sorted(f"{name}_user.py" for name in SIBLINGS)


def test_the_listed_siblings_include_the_tools_of_the_architecture_catalog() -> None:
    assert {
        "ticketctl", "ticket_queue", "ticket_create", "review_run", "review_watch",
        "review_respond", "ticket_work", "session_audit", "self_improve",
    } <= SIBLINGS  # fmt: skip


def test_the_fake_imports_only_the_standard_library() -> None:
    import sys

    roots = imported_roots((TESTS / "fakebin" / "notifier.py").read_text("utf-8"))
    assert roots - {"__future__"} <= set(sys.stdlib_module_names)


def test_the_fake_notifier_is_self_contained_in_this_repository() -> None:
    stub_source = (TESTS / "fakes.py").read_text("utf-8")
    assert "import notifier" in stub_source  # the stub loads tests/fakebin/notifier.py only
    assert not (TESTS / "fakebin" / "__init__.py").exists()  # not an importable shared package
