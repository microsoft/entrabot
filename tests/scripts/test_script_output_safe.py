"""Every script in ``scripts/`` that can be run on its own makes its output safe first.

Run directly on Windows with its output redirected or piped, a script prints through the ANSI
code page, and one character it cannot encode ("✅", "—" in an error message) raised
UnicodeEncodeError after the work was done. The function a script's ``__main__`` block calls
must therefore start with ``make_output_safe()`` (``scripts/script_output.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _runnable() -> list[Path]:
    scripts = SCRIPTS.glob("*.py")
    return sorted(p for p in scripts if '__name__ == "__main__"' in p.read_text("utf-8"))


def _entry_function(tree: ast.Module) -> str:
    """The name of the function the ``if __name__ == "__main__":`` block calls."""
    for node in tree.body:
        if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id != "SystemExit"
                ):
                    return inner.func.id
    raise AssertionError("no __main__ block calling a function")


def _first_statement(function: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt:
    body = function.body
    docstring = (
        isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    )
    return body[1] if docstring else body[0]


def test_there_are_scripts_to_check() -> None:
    assert len(_runnable()) >= 40


@pytest.mark.parametrize("script", _runnable(), ids=lambda p: p.name)
def test_the_entry_point_makes_its_output_safe_first(script: Path) -> None:
    tree = ast.parse(script.read_text("utf-8"))
    name = _entry_function(tree)
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert functions, f"{script.name}: __main__ calls {name}(), which is not defined here"
    first = _first_statement(functions[0])
    assert ast.unparse(first) == "make_output_safe()", (
        f"{script.name}: {name}() must start with make_output_safe()"
    )
