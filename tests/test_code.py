"""The code's shape: no function so long that it is hard to follow."""

import ast
import re
from pathlib import Path

import pytest

LONGEST = 50
SHELL_FUNCTION = re.compile(r"^([a-z_]+)\(\) \{\n(.*?)^\}", re.S | re.M)


def python_functions():
    for path in sorted(Path("deploy").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield f"{path}:{node.name}", node.end_lineno - node.lineno + 1


def shell_functions():
    for path in sorted([*Path("deploy").rglob("*.sh"), *Path("dev").glob("*.sh")]):
        for match in SHELL_FUNCTION.finditer(path.read_text()):
            yield f"{path}:{match[1]}", match[2].count("\n") + 2


@pytest.mark.parametrize("functions", [python_functions, shell_functions])
def test_no_function_is_longer_than_50_lines(functions):
    assert [name for name, length in functions() if length > LONGEST] == []
