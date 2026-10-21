"""Token-agnostic double-entry ledger: a proof for risk R1 (feasibility.md).

Scope (what this is): integer-unit balances with earn, spend, refund and
earn-reversal; idempotent operations; atomic multi-leg postings; a
reconciliation check that recomputes balances from the entries.

Scope (what this is NOT): no partner integration, no payment provider, no
authentication, no wallet, no blockchain, no compliance logic. SQLite stands in
for a production database; its write-locking behaviour is NOT evidence about
any other database. The unit is deliberately abstract ("units"): whether it is
points, stored value or something else is an open question (Q1).

Design decisions (each is also a question for the partner, see questions.md):
  * Amounts are positive integers in the smallest unit. No floats.
  * Every operation carries a caller-supplied idempotency key. Replaying the
    same key with the same request returns the original result; the same key
    with a different request is rejected.
  * Rejected operations (e.g. insufficient balance) are NOT recorded, so a
    later retry with the same key is evaluated afresh. (Policy choice.)
  * A refund must reference an existing spend; one that arrives before its
    spend is rejected rather than guessed at (UnknownReference).
  * Reversing an earn whose tokens were already spent is rejected rather than
    driving the balance negative (policy: Q6/Q7).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import List, NamedTuple, Tuple

ISSUANCE = "system:issuance"      # source of earned units; negative = outstanding
REDEMPTION = "system:redemption"  # sink for spent units

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id      TEXT PRIMARY KEY,
    kind    TEXT NOT NULL CHECK (kind IN ('user', 'system')),
    balance INTEGER NOT NULL DEFAULT 0,
    CHECK (kind != 'user' OR balance >= 0)
);
CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key     TEXT NOT NULL UNIQUE,
    op           TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    ref_txn      INTEGER REFERENCES transactions(id)
);
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id     INTEGER NOT NULL REFERENCES transactions(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount     INTEGER NOT NULL CHECK (amount != 0)
);
CREATE INDEX IF NOT EXISTS idx_entries_txn ON entries(txn_id);
CREATE INDEX IF NOT EXISTS idx_entries_acct ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_ref ON transactions(ref_txn);
"""


class LedgerError(Exception):
    pass


class InvalidAmount(LedgerError):
    pass


class InsufficientBalance(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class UnknownReference(LedgerError):
    pass


class OverRefund(LedgerError):
    pass


class Result(NamedTuple):
    txn_id: int
    replayed: bool


Leg = Tuple[str, int]  # (account id, signed amount)


def _user(user: str) -> str:
    return f"user:{user}"


def _check_amount(amount) -> None:
    # type() is int excludes bool, float, str, None.
    if type(amount) is not int or amount <= 0:
        raise InvalidAmount(f"amount must be a positive integer, got {amount!r}")


class Ledger:
    def __init__(self, path: str):
        self.path = path
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO accounts(id, kind) VALUES (?, 'system')", (ISSUANCE,))
            conn.execute("INSERT OR IGNORE INTO accounts(id, kind) VALUES (?, 'system')", (REDEMPTION,))
        finally:
            conn.close()

    # ------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _insert_entry(self, conn, txn_id: int, account: str, amount: int) -> None:
        kind = "user" if account.startswith("user:") else "system"
        conn.execute("INSERT OR IGNORE INTO accounts(id, kind) VALUES (?, ?)", (account, kind))
        conn.execute(
            "INSERT INTO entries(txn_id, account_id, amount) VALUES (?, ?, ?)",
            (txn_id, account, amount),
        )
        # The CHECK on accounts is the backstop if application logic is wrong.
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, account))

    @staticmethod
    def _hash(op: str, payload: dict) -> str:
        return hashlib.sha256(json.dumps([op, payload], sort_keys=True).encode()).hexdigest()

    def _post(self, idem_key: str, op: str, payload: dict, ref_txn, build_legs) -> Result:
        """Run one operation atomically under a write lock.

        BEGIN IMMEDIATE serialises writers, so the balance check, the
        idempotency lookup and the postings cannot interleave with another
        writer (no check-then-act race).
        """
        request_hash = self._hash(op, payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id, request_hash FROM transactions WHERE idem_key = ?", (idem_key,)
                ).fetchone()
                if row is not None:
                    if row[1] != request_hash:
                        raise IdempotencyConflict(
                            f"idempotency key {idem_key!r} already used for a different request"
                        )
                    conn.execute("COMMIT")
                    return Result(row[0], True)

                legs: List[Leg] = build_legs(conn)
                cur = conn.execute(
                    "INSERT INTO transactions(idem_key, op, request_hash, ref_txn) VALUES (?, ?, ?, ?)",
                    (idem_key, op, request_hash, ref_txn),
                )
                txn_id = cur.lastrowid
                for account, amount in legs:
                    self._insert_entry(conn, txn_id, account, amount)
                conn.execute("COMMIT")
                return Result(txn_id, False)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @staticmethod
    def _balance(conn, account: str) -> int:
        row = conn.execute("SELECT balance FROM accounts WHERE id = ?", (account,)).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------ operations
    def earn(self, idem_key: str, user: str, amount: int) -> Result:
        _check_amount(amount)
        return self._post(
            idem_key, "earn", {"user": user, "amount": amount}, None,
            lambda conn: [(ISSUANCE, -amount), (_user(user), amount)],
        )

    def spend(self, idem_key: str, user: str, amount: int) -> Result:
        _check_amount(amount)

        def legs(conn):
            if self._balance(conn, _user(user)) < amount:
                raise InsufficientBalance(f"{user} has less than {amount}")
            return [(_user(user), -amount), (REDEMPTION, amount)]

        return self._post(idem_key, "spend", {"user": user, "amount": amount}, None, legs)

    def refund(self, idem_key: str, spend_txn_id: int, amount: int) -> Result:
        _check_amount(amount)

        def legs(conn):
            spend = conn.execute(
                "SELECT op FROM transactions WHERE id = ?", (spend_txn_id,)
            ).fetchone()
            if spend is None or spend[0] != "spend":
                raise UnknownReference(f"no spend transaction {spend_txn_id}")
            acct, signed = conn.execute(
                "SELECT account_id, amount FROM entries WHERE txn_id = ? AND account_id LIKE 'user:%'",
                (spend_txn_id,),
            ).fetchone()
            spent = -signed
            refunded = conn.execute(
                """SELECT COALESCE(SUM(e.amount), 0) FROM entries e
                   JOIN transactions t ON t.id = e.txn_id
                   WHERE t.op = 'refund' AND t.ref_txn = ? AND e.account_id = ?""",
                (spend_txn_id, acct),
            ).fetchone()[0]
            if refunded + amount > spent:
                raise OverRefund(
                    f"refund {amount} exceeds remaining {spent - refunded} of spend {spend_txn_id}"
                )
            return [(REDEMPTION, -amount), (acct, amount)]

        return self._post(
            idem_key, "refund", {"ref": spend_txn_id, "amount": amount}, spend_txn_id, legs
        )

    def reverse_earn(self, idem_key: str, earn_txn_id: int) -> Result:
        """Claw back an earn in full. Rejected if the units were already spent."""

        def legs(conn):
            earn = conn.execute(
                "SELECT op FROM transactions WHERE id = ?", (earn_txn_id,)
            ).fetchone()
            if earn is None or earn[0] != "earn":
                raise UnknownReference(f"no earn transaction {earn_txn_id}")
            acct, earned = conn.execute(
                "SELECT account_id, amount FROM entries WHERE txn_id = ? AND account_id LIKE 'user:%'",
                (earn_txn_id,),
            ).fetchone()
            already = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE op = 'reverse_earn' AND ref_txn = ?",
                (earn_txn_id,),
            ).fetchone()[0]
            if already:
                raise OverRefund(f"earn {earn_txn_id} already reversed")
            if self._balance(conn, acct) < earned:
                raise InsufficientBalance("units from this earn have already been spent")
            return [(acct, -earned), (ISSUANCE, earned)]

        return self._post(idem_key, "reverse_earn", {"ref": earn_txn_id}, earn_txn_id, legs)

    # ------------------------------------------------------------ queries
    def balance(self, user: str) -> int:
        conn = self._connect()
        try:
            return self._balance(conn, _user(user))
        finally:
            conn.close()

    def reconcile(self) -> List[str]:
        """Recompute everything from the entries; return a list of problems.

        An empty list means: every transaction nets to zero, every cached
        balance equals the sum of its entries, all balances net to zero and no
        user balance is negative.
        """
        problems: List[str] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN")  # one consistent read snapshot
            for txn_id, total, n in conn.execute(
                """SELECT t.id, COALESCE(SUM(e.amount), 0), COUNT(e.id)
                   FROM transactions t LEFT JOIN entries e ON e.txn_id = t.id
                   GROUP BY t.id"""
            ):
                if n == 0:
                    problems.append(f"transaction {txn_id} has no entries")
                elif total != 0:
                    problems.append(f"transaction {txn_id} does not sum to zero (sum={total})")
            for acct, kind, cached, ledger_sum in conn.execute(
                """SELECT a.id, a.kind, a.balance, COALESCE(SUM(e.amount), 0)
                   FROM accounts a LEFT JOIN entries e ON e.account_id = a.id
                   GROUP BY a.id"""
            ):
                if cached != ledger_sum:
                    problems.append(
                        f"account {acct}: cached balance {cached} != ledger sum {ledger_sum}"
                    )
                if kind == "user" and ledger_sum < 0:
                    problems.append(f"account {acct}: negative balance {ledger_sum}")
            grand = conn.execute("SELECT COALESCE(SUM(balance), 0) FROM accounts").fetchone()[0]
            if grand != 0:
                problems.append(f"all balances sum to {grand}, expected 0")
            conn.execute("COMMIT")
        finally:
            conn.close()
        return problems
