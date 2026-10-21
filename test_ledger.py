"""Tests for the token-agnostic ledger proof (risk R1 in feasibility.md).

Written before the implementation. Each test encodes one correctness property
that a naive earn/spend integration tends to get wrong.
Run:  python3 -m unittest -v
"""
import os
import random
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from ledger import (
    IdempotencyConflict,
    InsufficientBalance,
    InvalidAmount,
    Ledger,
    OverRefund,
    UnknownReference,
)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(self._cleanup)
        self.ledger = Ledger(self.path)

    def _cleanup(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def assertReconciles(self):
        self.assertEqual(self.ledger.reconcile(), [])

    def txn_count(self):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def raw(self, sql):
        """Execute SQL directly, bypassing the Ledger (to tamper or probe)."""
        with closing(sqlite3.connect(self.path)) as c:
            with c:  # commit on success
                c.execute(sql)


class TestBasics(LedgerTestCase):
    def test_earn_credits_user(self):
        r = self.ledger.earn("k1", "alice", 50)
        self.assertFalse(r.replayed)
        self.assertEqual(self.ledger.balance("alice"), 50)
        self.assertReconciles()

    def test_unknown_user_has_zero_balance(self):
        self.assertEqual(self.ledger.balance("nobody"), 0)

    def test_spend_debits_user(self):
        self.ledger.earn("e1", "alice", 50)
        self.ledger.spend("s1", "alice", 20)
        self.assertEqual(self.ledger.balance("alice"), 30)
        self.assertReconciles()

    def test_spend_more_than_balance_rejected_and_leaves_no_trace(self):
        self.ledger.earn("e1", "alice", 10)
        before = self.txn_count()
        with self.assertRaises(InsufficientBalance):
            self.ledger.spend("s1", "alice", 11)
        self.assertEqual(self.ledger.balance("alice"), 10)
        self.assertEqual(self.txn_count(), before)
        self.assertReconciles()


class TestAmountValidation(LedgerTestCase):
    def test_rejects_bad_amounts(self):
        for bad in (0, -5, 1.5, True, "10", None):
            with self.subTest(amount=bad):
                with self.assertRaises(InvalidAmount):
                    self.ledger.earn("k-" + repr(bad), "alice", bad)
        self.assertEqual(self.txn_count(), 0)


class TestIdempotency(LedgerTestCase):
    def test_earn_replay_returns_same_txn_and_credits_once(self):
        a = self.ledger.earn("k1", "alice", 50)
        b = self.ledger.earn("k1", "alice", 50)
        self.assertTrue(b.replayed)
        self.assertEqual(a.txn_id, b.txn_id)
        self.assertEqual(self.ledger.balance("alice"), 50)
        self.assertReconciles()

    def test_spend_replay_debits_once(self):
        self.ledger.earn("e1", "alice", 50)
        a = self.ledger.spend("s1", "alice", 20)
        b = self.ledger.spend("s1", "alice", 20)
        self.assertEqual(a.txn_id, b.txn_id)
        self.assertTrue(b.replayed)
        self.assertEqual(self.ledger.balance("alice"), 30)

    def test_same_key_different_payload_is_conflict_and_changes_nothing(self):
        self.ledger.earn("k1", "alice", 50)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.earn("k1", "alice", 500)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.earn("k1", "bob", 50)
        with self.assertRaises(IdempotencyConflict):
            self.ledger.spend("k1", "alice", 50)  # different operation
        self.assertEqual(self.ledger.balance("alice"), 50)
        self.assertEqual(self.ledger.balance("bob"), 0)
        self.assertReconciles()


class TestConcurrency(LedgerTestCase):
    def _run_threads(self, fn, n):
        results, errors = [], []
        barrier = threading.Barrier(n)

        def worker(i):
            barrier.wait()
            try:
                results.append(fn(i))
            except Exception as e:  # noqa: BLE001 - we classify below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, errors

    def test_concurrent_distinct_spends_never_overdraw(self):
        self.ledger.earn("seed", "alice", 100)
        results, errors = self._run_threads(
            lambda i: self.ledger.spend(f"s{i}", "alice", 10), 30
        )
        self.assertEqual(len(results), 10)  # exactly balance/amount succeed
        self.assertEqual(len(errors), 20)
        self.assertTrue(all(isinstance(e, InsufficientBalance) for e in errors))
        self.assertEqual(self.ledger.balance("alice"), 0)
        self.assertReconciles()

    def test_concurrent_same_key_spend_applies_exactly_once(self):
        self.ledger.earn("seed", "alice", 100)
        results, errors = self._run_threads(
            lambda i: self.ledger.spend("same", "alice", 40), 20
        )
        self.assertEqual(errors, [])
        self.assertEqual(len({r.txn_id for r in results}), 1)
        self.assertEqual(sum(1 for r in results if not r.replayed), 1)
        self.assertEqual(self.ledger.balance("alice"), 60)
        self.assertReconciles()

    def test_concurrent_same_key_earn_applies_exactly_once(self):
        results, errors = self._run_threads(
            lambda i: self.ledger.earn("same", "alice", 25), 20
        )
        self.assertEqual(errors, [])
        self.assertEqual(self.ledger.balance("alice"), 25)
        self.assertReconciles()


class TestRefunds(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.ledger.earn("e1", "alice", 100)
        self.spend = self.ledger.spend("s1", "alice", 60)

    def test_full_refund_restores_balance(self):
        self.ledger.refund("r1", self.spend.txn_id, 60)
        self.assertEqual(self.ledger.balance("alice"), 100)
        self.assertReconciles()

    def test_refund_replay_does_not_double_refund(self):
        self.ledger.refund("r1", self.spend.txn_id, 60)
        again = self.ledger.refund("r1", self.spend.txn_id, 60)
        self.assertTrue(again.replayed)
        self.assertEqual(self.ledger.balance("alice"), 100)

    def test_partial_refunds_accumulate_up_to_spend_amount(self):
        self.ledger.refund("r1", self.spend.txn_id, 25)
        self.ledger.refund("r2", self.spend.txn_id, 35)
        self.assertEqual(self.ledger.balance("alice"), 100)
        with self.assertRaises(OverRefund):
            self.ledger.refund("r3", self.spend.txn_id, 1)
        self.assertEqual(self.ledger.balance("alice"), 100)
        self.assertReconciles()

    def test_over_refund_rejected_in_one_step(self):
        with self.assertRaises(OverRefund):
            self.ledger.refund("r1", self.spend.txn_id, 61)
        self.assertEqual(self.ledger.balance("alice"), 40)

    def test_refund_arriving_before_its_spend_is_rejected_not_guessed(self):
        # Out-of-order delivery: the refund references a txn that does not exist yet.
        with self.assertRaises(UnknownReference):
            self.ledger.refund("r-early", 9999, 10)
        self.assertEqual(self.ledger.balance("alice"), 40)
        self.assertReconciles()

    def test_refund_must_reference_a_spend(self):
        earn = self.ledger.earn("e2", "alice", 5)
        with self.assertRaises(UnknownReference):
            self.ledger.refund("r-bad", earn.txn_id, 5)

    def test_concurrent_refunds_cannot_exceed_spend(self):
        results, errors = [], []
        barrier = threading.Barrier(10)

        def worker(i):
            barrier.wait()
            try:
                results.append(self.ledger.refund(f"cr{i}", self.spend.txn_id, 10))
            except OverRefund as e:
                errors.append(e)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(results), 6)  # 60 / 10
        self.assertEqual(len(errors), 4)
        self.assertEqual(self.ledger.balance("alice"), 100)
        self.assertReconciles()


class TestEarnReversal(LedgerTestCase):
    def test_reverse_earn_when_balance_available(self):
        e = self.ledger.earn("e1", "alice", 30)
        self.ledger.reverse_earn("rv1", e.txn_id)
        self.assertEqual(self.ledger.balance("alice"), 0)
        self.assertReconciles()

    def test_reverse_earn_rejected_if_already_spent_policy_is_open_question(self):
        e = self.ledger.earn("e1", "alice", 30)
        self.ledger.spend("s1", "alice", 20)
        with self.assertRaises(InsufficientBalance):
            self.ledger.reverse_earn("rv1", e.txn_id)
        self.assertEqual(self.ledger.balance("alice"), 10)

    def test_reverse_earn_twice_is_rejected(self):
        e = self.ledger.earn("e1", "alice", 30)
        self.ledger.reverse_earn("rv1", e.txn_id)
        with self.assertRaises(OverRefund):
            self.ledger.reverse_earn("rv2", e.txn_id)


class TestAtomicity(LedgerTestCase):
    def test_failure_midway_leaves_no_partial_state(self):
        self.ledger.earn("e1", "alice", 50)
        before = self.txn_count()
        original = Ledger._insert_entry
        calls = {"n": 0}

        def flaky(self_, conn, txn_id, account, amount):
            calls["n"] += 1
            if calls["n"] == 2:  # second leg of the transaction blows up
                raise RuntimeError("simulated crash between legs")
            return original(self_, conn, txn_id, account, amount)

        with mock.patch.object(Ledger, "_insert_entry", flaky):
            with self.assertRaises(RuntimeError):
                self.ledger.spend("s1", "alice", 10)
        self.assertEqual(self.ledger.balance("alice"), 50)
        self.assertEqual(self.txn_count(), before)
        self.assertReconciles()


class TestReconciliation(LedgerTestCase):
    def test_reconcile_detects_balance_drift(self):
        self.ledger.earn("e1", "alice", 50)
        self.raw("UPDATE accounts SET balance = balance + 7 WHERE id = 'user:alice'")
        problems = self.ledger.reconcile()
        self.assertTrue(problems)
        self.assertTrue(any("user:alice" in p for p in problems))

    def test_reconcile_detects_unbalanced_transaction(self):
        self.ledger.earn("e1", "alice", 50)
        self.raw("UPDATE entries SET amount = amount + 1 WHERE amount > 0")
        problems = self.ledger.reconcile()
        self.assertTrue(any("does not sum to zero" in p for p in problems))

    def test_db_rejects_negative_user_balance_even_if_app_logic_is_bypassed(self):
        self.ledger.earn("e1", "alice", 5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE accounts SET balance = -1 WHERE id = 'user:alice'")


class TestRandomizedAgainstModel(LedgerTestCase):
    """Seeded random operations (including replays and bad requests) checked
    against a trivial in-memory model, reconciling after every step."""

    def test_model_agreement(self):
        rng = random.Random(20261021)
        model = {}          # user -> balance
        spends = {}         # txn_id -> [user, amount, refunded]
        issued = []         # (key, kind, args, result) for replays
        users = ["u1", "u2", "u3"]

        for step in range(400):
            op = rng.choice(["earn", "spend", "refund", "replay"])
            if op == "earn":
                u, amt, key = rng.choice(users), rng.randint(1, 50), f"k{step}"
                r = self.ledger.earn(key, u, amt)
                model[u] = model.get(u, 0) + amt
                issued.append((key, "earn", (u, amt), r))
            elif op == "spend":
                u, amt, key = rng.choice(users), rng.randint(1, 60), f"k{step}"
                if model.get(u, 0) >= amt:
                    r = self.ledger.spend(key, u, amt)
                    model[u] -= amt
                    spends[r.txn_id] = [u, amt, 0]
                    issued.append((key, "spend", (u, amt), r))
                else:
                    with self.assertRaises(InsufficientBalance):
                        self.ledger.spend(key, u, amt)
            elif op == "refund" and spends:
                tid = rng.choice(list(spends))
                u, amt, done = spends[tid]
                ramt, key = rng.randint(1, amt), f"k{step}"
                if done + ramt <= amt:
                    r = self.ledger.refund(key, tid, ramt)
                    spends[tid][2] += ramt
                    model[u] += ramt
                    issued.append((key, "refund", (tid, ramt), r))
                else:
                    with self.assertRaises(OverRefund):
                        self.ledger.refund(key, tid, ramt)
            elif op == "replay" and issued:
                key, kind, args, first = rng.choice(issued)
                again = {
                    "earn": lambda: self.ledger.earn(key, *args),
                    "spend": lambda: self.ledger.spend(key, *args),
                    "refund": lambda: self.ledger.refund(key, *args),
                }[kind]()
                self.assertTrue(again.replayed)
                self.assertEqual(again.txn_id, first.txn_id)

            for u in users:
                self.assertEqual(self.ledger.balance(u), model.get(u, 0), f"step {step}")
            self.assertEqual(self.ledger.reconcile(), [], f"step {step}")


if __name__ == "__main__":
    unittest.main()
