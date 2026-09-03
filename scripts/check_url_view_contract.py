#!/usr/bin/env python3
"""Dependency-free check that every ``views.<name>`` URL target exists.

Django's ``manage.py check`` performs the authoritative runtime validation.
This small AST check is also run while building an offline release package so
an omitted view handler cannot reach Railway's pre-deploy phase unnoticed.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
URLS_PATH = ROOT / "receipts" / "urls.py"
VIEWS_PATH = ROOT / "receipts" / "views.py"


REQUIRED_SIGNATURES = {
    "staff_start_receipt_ai_processing": ("request", "pk"),
    "staff_receipt_ai_status": ("request", "pk"),
    "staff_preview_receipt": ("request", "pk"),
}


def _top_level_names(module: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(
                        item.id for item in target.elts if isinstance(item, ast.Name)
                    )
    return names


def main() -> int:
    urls_tree = ast.parse(URLS_PATH.read_text(encoding="utf-8"), filename=str(URLS_PATH))
    views_tree = ast.parse(VIEWS_PATH.read_text(encoding="utf-8"), filename=str(VIEWS_PATH))

    referenced = {
        node.attr
        for node in ast.walk(urls_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "views"
    }
    available = _top_level_names(views_tree)
    missing = sorted(referenced - available)
    if missing:
        print("URL/view contract check failed. Missing views:")
        for name in missing:
            print(f"  - receipts.views.{name}")
        return 1

    functions = {
        node.name: node
        for node in views_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    signature_errors: list[str] = []
    for name, expected in REQUIRED_SIGNATURES.items():
        node = functions.get(name)
        if node is None:
            signature_errors.append(f"receipts.views.{name} must be a function")
            continue
        actual = tuple(arg.arg for arg in node.args.args[: len(expected)])
        if actual != expected:
            signature_errors.append(
                f"receipts.views.{name} positional arguments are {actual!r}; expected {expected!r}"
            )
    if signature_errors:
        print("URL/view signature check failed:")
        for error in signature_errors:
            print(f"  - {error}")
        return 1

    print(
        f"URL/view contract check passed: {len(referenced)} targets resolved; "
        f"{len(REQUIRED_SIGNATURES)} receipt-specific signatures verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
