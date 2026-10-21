"""Walkthrough of the ledger proof, for the video. Run: python3 demo.py"""
import os
import tempfile
import threading

from ledger import InsufficientBalance, Ledger, OverRefund


def show(label, value=""):
    print(f"{label:<62} {value}")


fd, path = tempfile.mkstemp(suffix=".db")
os.close(fd)
try:
    L = Ledger(path)

    print("== 1. Earn, then the partner's webhook is delivered twice ==")
    L.earn("order-1001:reward", "alice", 100)
    r = L.earn("order-1001:reward", "alice", 100)  # retry with same key
    show("balance after earn + duplicate delivery (expect 100)", L.balance("alice"))
    show("duplicate recognised as replay", r.replayed)

    print("\n== 2. 30 simultaneous checkouts of 10 units against a balance of 100 ==")
    ok, rejected = [], []
    barrier = threading.Barrier(30)

    def checkout(i):
        barrier.wait()
        try:
            ok.append(L.spend(f"checkout-{i}", "alice", 10))
        except InsufficientBalance:
            rejected.append(i)

    ts = [threading.Thread(target=checkout, args=(i,)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    show("succeeded (expect 10)", len(ok))
    show("rejected for insufficient balance (expect 20)", len(rejected))
    show("balance (expect 0, never negative)", L.balance("alice"))

    print("\n== 3. Refunds: partial, replayed, and over-limit ==")
    spend_txn = ok[0].txn_id
    L.refund("refund-a", spend_txn, 4)
    L.refund("refund-a", spend_txn, 4)  # duplicate
    show("balance after refund of 4, delivered twice (expect 4)", L.balance("alice"))
    L.refund("refund-b", spend_txn, 6)
    try:
        L.refund("refund-c", spend_txn, 1)
    except OverRefund as e:
        show("refund beyond the original spend", f"rejected: {e}")
    show("balance (expect 10)", L.balance("alice"))

    print("\n== 4. Reconciliation (recomputed from entries, not cached balances) ==")
    show("problems found (expect [])", L.reconcile())
finally:
    for s in ("", "-wal", "-shm"):
        try:
            os.remove(path + s)
        except FileNotFoundError:
            pass
