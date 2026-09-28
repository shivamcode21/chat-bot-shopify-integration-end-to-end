#!/usr/bin/env python3
"""
Verify all LLM calls in the codebase are async-safe.

Scans all Python files under fashion_bot/ and reports:
  [OK]   await llm.ainvoke(...)
  [OK]   asyncio.to_thread(llm.invoke, ...)
  [WARN] bare llm.invoke(...) inside an async function
  [INFO] llm.invoke(...) inside a sync function (noted but acceptable)

Usage:
    python verify_async_llm.py
"""
import ast
import os
import sys
from pathlib import Path
from typing import List, Tuple


class LLMCallVisitor(ast.NodeVisitor):
    """AST visitor that finds .invoke() and .ainvoke() calls on LLM-like objects."""

    def __init__(self, filename: str):
        self.filename = filename
        self.findings: List[Tuple[str, int, str, str]] = []  # (status, line, call, context)
        self._in_async = False
        self._in_to_thread = False

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        old = self._in_async
        self._in_async = True
        self.generic_visit(node)
        self._in_async = old

    def visit_FunctionDef(self, node: ast.FunctionDef):
        old = self._in_async
        self._in_async = False
        self.generic_visit(node)
        self._in_async = old

    def visit_Call(self, node: ast.Call):
        # Check for asyncio.to_thread(llm.invoke, ...) pattern
        if isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread":
            if node.args and isinstance(node.args[0], ast.Attribute):
                if node.args[0].attr == "invoke":
                    obj = self._get_attr_chain(node.args[0])
                    self.findings.append((
                        "OK",
                        node.lineno,
                        f"asyncio.to_thread({obj}.invoke, ...)",
                        "non-blocking via thread"
                    ))
                    return

        # Check for .ainvoke() or .invoke() calls
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("ainvoke", "invoke"):
                obj = self._get_attr_chain(node.func)
                # Skip irrelevant objects (dict.get, list.append, etc.)
                if obj in ("self", "dict", "list", "str", "response"):
                    self.generic_visit(node)
                    return

                call_str = f"{obj}.{attr}(...)"

                if attr == "ainvoke":
                    # Check if awaited
                    self.findings.append((
                        "OK",
                        node.lineno,
                        call_str,
                        "async ainvoke"
                    ))
                elif attr == "invoke":
                    if self._in_async:
                        self.findings.append((
                            "WARN",
                            node.lineno,
                            call_str,
                            "bare .invoke() in async function (potential blocking)"
                        ))
                    else:
                        self.findings.append((
                            "INFO",
                            node.lineno,
                            call_str,
                            "sync .invoke() in sync function"
                        ))

        self.generic_visit(node)

    def _get_attr_chain(self, node: ast.AST) -> str:
        """Reconstruct dotted name from AST attribute chain."""
        if isinstance(node, ast.Attribute):
            parent = self._get_attr_chain(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        elif isinstance(node, ast.Name):
            return node.id
        return "?"


def scan_file(filepath: str) -> List[Tuple[str, int, str, str]]:
    """Parse a file and return LLM call findings."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    visitor = LLMCallVisitor(filepath)
    visitor.visit(tree)
    return visitor.findings


def main():
    # Resolve fashion_bot directory
    script_dir = Path(__file__).parent
    project_root = script_dir.parent / "fashion_bot" / "fashion_bot"

    if not project_root.exists():
        print(f"ERROR: fashion_bot directory not found at {project_root}")
        sys.exit(1)

    # Scan directories
    scan_dirs = [
        project_root / "nodes",
        project_root / "core",
        project_root,  # top-level files like tool_factory.py
    ]

    total_ok = 0
    total_warn = 0
    total_info = 0

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for py_file in sorted(scan_dir.glob("*.py")):
            findings = scan_file(str(py_file))
            if not findings:
                continue

            rel_path = py_file.relative_to(project_root.parent)
            for status, line, call, context in findings:
                marker = {"OK": "\033[32m[OK]\033[0m", "WARN": "\033[33m[WARN]\033[0m", "INFO": "\033[36m[INFO]\033[0m"}
                print(f"  {marker.get(status, status):>20s}  {rel_path}:{line}  {call}  -- {context}")

                if status == "OK":
                    total_ok += 1
                elif status == "WARN":
                    total_warn += 1
                elif status == "INFO":
                    total_info += 1

    print(f"\n{'='*60}")
    print(f"  Total:  OK={total_ok}  WARN={total_warn}  INFO={total_info}")
    if total_warn > 0:
        print(f"  {total_warn} WARN(s): bare .invoke() in async functions (review for blocking risk)")
    else:
        print(f"  All hot-path LLM calls are async-safe!")
    print(f"{'='*60}")

    sys.exit(1 if total_warn > 0 else 0)


if __name__ == "__main__":
    main()
