# tests/test_no_print_statements.py
"""AST-walk all app/*.py files and assert zero print() calls."""
import ast
import pathlib


def _find_print_calls(filepath: pathlib.Path) -> list[int]:
    """Return line numbers of print() calls in the given Python file."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(filepath))
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # print(...)
            if isinstance(func, ast.Name) and func.id == "print":
                lines.append(node.lineno)
            # builtins.print(...)
            elif isinstance(func, ast.Attribute) and func.attr == "print":
                lines.append(node.lineno)
    return lines


class TestNoPrintStatements:
    def test_no_print_in_app_modules(self):
        """Every app/*.py file must use logging instead of print()."""
        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
        violations = {}
        for py_file in sorted(app_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            lines = _find_print_calls(py_file)
            if lines:
                violations[py_file.name] = lines

        assert violations == {}, (
            "Found print() calls in app/ files (use logging instead):\n"
            + "\n".join(
                f"  {name}: lines {lines}" for name, lines in violations.items()
            )
        )
