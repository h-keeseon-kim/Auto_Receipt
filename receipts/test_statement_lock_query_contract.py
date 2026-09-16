"""Dependency-free query-shape regressions; these do not execute SQL.

Run: python -m unittest receipts.test_statement_lock_query_contract -v
DB execution/locking tests live in test_statement_locking_db.py.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


class QueryRecorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        if method not in {"select_for_update", "filter", "exclude", "select_related",
                          "prefetch_related", "order_by"}:
            raise AttributeError(method)
        def record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            return self
        return record


def row_query(function_name):
    path = Path(__file__).with_name("statement_processing.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == function_name)
    assignment = next(n for n in function.body
                      if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "rows" for t in n.targets))
    assert isinstance(assignment.value, ast.Call)
    assert isinstance(assignment.value.func, ast.Name) and assignment.value.func.id == "list"
    expression = ast.Expression(body=assignment.value.args[0])
    recorder = QueryRecorder()
    result = eval(compile(expression, str(path), "eval"), {
        "__builtins__": {},
        "CardStatementReceiptEvidence": SimpleNamespace(objects=recorder),
        "StatementReceiptEvidenceUsageMode": SimpleNamespace(CONSUME="consume"),
    })
    assert result is recorder
    return recorder.calls


class StatementLockQueryContractTests(unittest.TestCase):
    BACKFILL = "_backfill_missing_evidence_fingerprints"
    OWNERSHIP = "_partition_global_consume_ownership"

    def assert_lock_preserved(self, function):
        calls = row_query(function)
        self.assertEqual([call for call in calls if call[0] == "select_for_update"],
                         [("select_for_update", (), {})])
        self.assertIn(("select_related", ("statement_item__statement",), {}), calls)

    def assert_nullable_receipt_read_separately(self, function):
        calls = row_query(function)
        self.assertIn(("prefetch_related", ("receipt",), {}), calls)
        for method, args, _ in calls:
            if method == "select_related":
                self.assertTrue(all(not a.startswith("receipt") for a in args))

    def assert_deleted_receipt_history_not_filtered(self, function):
        for method, _, kwargs in row_query(function):
            if method in {"filter", "exclude"}:
                self.assertTrue(all(not key.startswith("receipt") for key in kwargs))

    def test_backfill_preserves_row_lock(self):
        self.assert_lock_preserved(self.BACKFILL)

    def test_ownership_preserves_row_lock(self):
        self.assert_lock_preserved(self.OWNERSHIP)

    def test_backfill_prefetches_nullable_receipt(self):
        self.assert_nullable_receipt_read_separately(self.BACKFILL)

    def test_ownership_prefetches_nullable_receipt(self):
        self.assert_nullable_receipt_read_separately(self.OWNERSHIP)

    def test_backfill_keeps_deleted_receipt_history(self):
        self.assert_deleted_receipt_history_not_filtered(self.BACKFILL)

    def test_ownership_keeps_deleted_receipt_history(self):
        self.assert_deleted_receipt_history_not_filtered(self.OWNERSHIP)

    def test_backfill_selection_is_unchanged(self):
        self.assertIn(("filter", (), {"component_fingerprint": ""}), row_query(self.BACKFILL))

    def test_ownership_selection_and_order_are_unchanged(self):
        calls = row_query(self.OWNERSHIP)
        self.assertIn(("filter", (), {"usage_mode": "consume"}), calls)
        self.assertIn(("exclude", (), {"component_fingerprint": ""}), calls)
        self.assertIn(("order_by", (
            "statement_item__statement__period_month",
            "statement_item__statement__uploaded_at",
            "statement_item__statement_id", "statement_item__sequence", "pk",
        ), {}), calls)


if __name__ == "__main__":
    unittest.main()
