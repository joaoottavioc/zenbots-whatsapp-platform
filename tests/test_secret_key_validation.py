# tests/test_secret_key_validation.py
"""Verify SECRET_KEY startup validation in app/auth.py."""
import ast
import pathlib


class TestSecretKeyValidation:
    def test_secret_key_guard_exists_in_source(self):
        """auth.py must raise RuntimeError if SECRET_KEY is missing."""
        auth_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "auth.py"
        source = auth_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find: if not SECRET_KEY: raise RuntimeError(...)
        found_guard = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                # Check for `not SECRET_KEY` test
                test = node.test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    if isinstance(test.operand, ast.Name) and test.operand.id == "SECRET_KEY":
                        # Check the body has a Raise with RuntimeError
                        for stmt in node.body:
                            if isinstance(stmt, ast.Raise) and stmt.exc is not None:
                                if isinstance(stmt.exc, ast.Call):
                                    func = stmt.exc.func
                                    if isinstance(func, ast.Name) and func.id == "RuntimeError":
                                        found_guard = True

        assert found_guard, (
            "auth.py must contain: if not SECRET_KEY: raise RuntimeError(...)"
        )

    def test_guard_is_before_first_function_definition(self):
        """The SECRET_KEY guard must be at module level, before any def/class."""
        auth_path = pathlib.Path(__file__).resolve().parent.parent / "app" / "auth.py"
        source = auth_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        guard_line = None
        first_func_line = None

        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                    if isinstance(test.operand, ast.Name) and test.operand.id == "SECRET_KEY":
                        guard_line = node.lineno

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first_func_line = node.lineno
                break

        assert guard_line is not None, "SECRET_KEY guard not found"
        assert first_func_line is not None, "No function definitions found"
        assert guard_line < first_func_line, (
            f"Guard (line {guard_line}) must be before first function (line {first_func_line})"
        )
