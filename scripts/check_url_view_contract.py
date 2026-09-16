#!/usr/bin/env python3
"""Validate URL handlers, template files, and static assets before deployment.

The default mode uses only the Python standard library. ``--django`` also loads
and compiles every project template using the real configured Django loader.
It deliberately does not render views, contact external APIs, or query the DB.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
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


def check_url_view_contract() -> int:
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



# Explicit release inventory: discovering only files that still exist would
# silently pass after an entire template/static directory was replaced.
REQUIRED_TEMPLATES = (
    'base.html',
    'receipts/_month_picker.html',
    'receipts/_receipt_table.html',
    'receipts/_staff_card_statement_item.html',
    'receipts/_staff_card_statements.html',
    'receipts/_staff_receipt_review_panel.html',
    'receipts/_staff_receipt_rows.html',
    'receipts/dashboard.html',
    'receipts/history.html',
    'receipts/service_exception_request_form.html',
    'receipts/service_form.html',
    'receipts/staff_card_statements.html',
    'receipts/staff_catalog_form.html',
    'receipts/staff_dashboard.html',
    'receipts/staff_email.html',
    'receipts/staff_exception_requests.html',
    'receipts/staff_history.html',
    'receipts/staff_receipt_review.html',
    'receipts/staff_service_form.html',
    'receipts/staff_services.html',
    'receipts/staff_submission_detail.html',
    'receipts/staff_user_create.html',
    'receipts/staff_user_month_status.html',
    'receipts/staff_user_services.html',
    'receipts/submission_detail.html',
    'receipts/user_service_form.html',
    'receipts/user_service_p_card_form.html',
    'receipts/user_service_stop.html',
    'receipts/user_services.html',
    'registration/login.html',
    'registration/password_change_done.html',
    'registration/password_change_form.html',
    'registration/register.html',
)

REQUIRED_STATIC_ASSETS = (
    'css/app.css',
    'js/auto_submit_forms.js',
    'js/file_dropzone.js',
    'js/month_picker.js',
    'js/staff_ai_processing.js',
    'js/staff_receipt_review.js',
    'js/staff_statement_processing.js',
    'js/tutorial.js',
)


_TEMPLATE_REFERENCE = re.compile(
    r"\{%\s*(?:extends|include)\s+(['\"])([^'\"]+)\1"
)
_STATIC_REFERENCE = re.compile(r"\{%\s*static\s+(['\"])([^'\"]+)\1")
_TEMPLATE_COMMENTS = re.compile(r"\{#.*?#\}|\{%\s*comment\b.*?%\}.*?\{%\s*endcomment\s*%\}", re.S)


def _validate_file(directory: Path, name: str) -> tuple[Path | None, str | None]:
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        return None, f"Invalid path outside {directory.name}/: {name}"
    if not path.is_file():
        return None, f"Missing file: {directory.name}/{name}"
    if path.stat().st_size == 0:
        return None, f"Empty file: {directory.name}/{name}"
    return path, None


def check_template_asset_contract(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """Return all template names and errors without loading Django or a DB."""
    template_dir = root / "templates"
    static_dir = root / "static"
    templates = set(REQUIRED_TEMPLATES)
    templates.update(p.relative_to(template_dir).as_posix() for p in template_dir.rglob("*.html"))
    # Also inspect the Python-side literal template names, including auth views.
    for source_dir in (root / "receipts", root / "auto_receipt"):
        for source in source_dir.rglob("*.py"):
            if "migrations" in source.parts or source.name.startswith("test"):
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    name = node.value
                    if name.endswith(".html") and (
                        name.startswith(("receipts/", "registration/")) or name == "base.html"
                    ):
                        templates.add(name)
    errors: list[str] = []
    assets = set(REQUIRED_STATIC_ASSETS)
    checked: set[str] = set()
    while templates - checked:
        name = sorted(templates - checked)[0]
        checked.add(name)
        path, error = _validate_file(template_dir, name)
        if error:
            errors.append(error)
            continue
        text = _TEMPLATE_COMMENTS.sub("", path.read_text(encoding="utf-8"))
        for match in _TEMPLATE_REFERENCE.finditer(text):
            reference = match.group(2)
            if reference.startswith(("./", "../")):
                dependency = (path.parent / reference).resolve()
                if not dependency.is_relative_to(template_dir.resolve()):
                    errors.append(f"Invalid template reference in {name}: {reference}")
                    continue
                reference = dependency.relative_to(template_dir.resolve()).as_posix()
            templates.add(reference)
        assets.update(match.group(2) for match in _STATIC_REFERENCE.finditer(text))
    for name in sorted(assets):
        _, error = _validate_file(static_dir, name)
        if error:
            errors.append(error)
    return sorted(templates), sorted(set(errors))


def check_django_templates(names: list[str]) -> int:
    # Direct script execution places scripts/, not the project root, on sys.path.
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "auto_receipt.settings")
    import django
    from django.template.loader import get_template

    django.setup()
    failed = []
    for name in names:
        try:
            get_template(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    if failed:
        print("Django template loading/compilation failed:", file=sys.stderr)
        for error in failed:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Django template check passed: {len(names)} templates loaded and compiled.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--django", action="store_true", help="Also load/compile templates with configured Django settings."
    )
    args = parser.parse_args(argv)
    try:
        url_result = check_url_view_contract()
        names, errors = check_template_asset_contract()
        if errors:
            print("Template/static package check failed:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            print(
                "Restore the full templates/ and static/ trees next to manage.py; "
                "a UI-only patch is not a complete source package.",
                file=sys.stderr,
            )
        if url_result or errors:
            return 1
        print(
            f"Template/static package check passed: {len(names)} templates; "
            f"{len(REQUIRED_STATIC_ASSETS)} release static assets plus literal references verified."
        )
        if args.django:
            return check_django_templates(names)
    except Exception as exc:
        print(f"Source contract check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
