# Proof: ledger correctness at the earn/spend/refund boundary

**Targets risk R1** in `../feasibility.md`: *balances must stay correct, auditable and reconcilable when earn and spend events are retried, concurrent or reversed.*

## Run it

```
cd proof
python3 -m unittest -v     # 26 tests
python3 demo.py            # narrated scenario used in the video
```
Requires Python 3 only (written and run on Python 3.13.16 with SQLite 3.45.1). No packages to install.

## Evidence (from executed runs; raw output is saved next to this file)

| Claim | Evidence |
|---|---|
| 26 tests pass | `test-output.txt`: `Ran 26 tests in 2.063s — OK` |
| Not flaky in this environment | The suite was run 20 times in a row: 20 passed, 0 failed (loop output recorded in the session; not saved as a file). |
| Tests were written first and failed for the right reason | Before `ledger.py` existed the run ended `ModuleNotFoundError: No module named 'ledger'` / `FAILED (errors=1)`. |
| The tests can actually catch bugs | `mutation-results.txt`: 8 of 9 deliberately broken variants of `ledger.py` were caught by the suite. |
| Demo behaves as narrated | `demo-output.txt` |

**The one surviving mutant (M8)** removes the explicit `ROLLBACK`. It survives because the connection is closed in a `finally` block, and SQLite discards an uncommitted transaction when its connection closes. I believe it is behaviourally equivalent rather than a test gap, but I have not proven that equivalence; M9 (a real atomicity break) is caught.

**What I fixed along the way:** the first run printed `ResourceWarning: unclosed database`. I traced it to my own test helpers (`with sqlite3.connect()` commits but does not close), fixed them, and re-ran with warnings enabled: 0 warnings.

## What is tested

- **Idempotency:** a replayed key returns the original result and applies once; the same key with a different request is rejected and changes nothing.
- **Concurrency:** 30 simultaneous spends against a balance that affords 10 → exactly 10 succeed, never negative; 20 threads with the same key → applied once; concurrent refunds cannot exceed the spend.
- **Reversals:** full and partial refunds; over-refund rejected; a refund referencing a non-existent or non-spend transaction rejected (this covers a refund arriving before its spend); earn clawback rejected if already spent.
- **Atomicity:** a simulated crash between the two legs of a posting leaves no trace.
- **Reconciliation:** recomputed from entries; detects a tampered cached balance and an unbalanced transaction. The database itself also refuses a negative user balance.
- **Randomized check:** 400 seeded random operations (including replays and invalid requests) compared against a simple in-memory model, reconciling after every step.

## What this does NOT prove (read before relying on it)

- **Not evidence that six weeks is achievable.** The larger risks (R2 token definition and liability, R3 partner integration, R5 security review) are untouched.
- **SQLite is a stand-in.** Its single-writer locking is why concurrency is correct here. A production database with different isolation behaviour would need its own concurrency test; nothing here transfers automatically. I have not run any other database.
- **No real integration.** There is no partner app, payment provider, authentication or webhook transport. "Webhook delivered twice" is simulated by calling the function twice with the same key.
- **No load or performance claim.** Tests use tens of threads and hundreds of operations, not production volumes (which are unknown, Q12).
- **No compliance, security or fraud logic.** Nothing about licensing, KYC, abuse limits or access control.
- **Policy choices are placeholders for partner decisions:** rejected operations are not recorded; clawback of already-spent units is rejected rather than allowed to go negative; amounts are integers in an abstract unit. Each maps to an open question (Q5, Q6, Q7).
- **Idempotency keys are caller-supplied.** Correctness depends on the integration choosing stable keys per business event; the proof cannot check that.
- `demo.py` prints expected values but does not assert them; the assertions are in the tests.

## Files

| File | Purpose |
|---|---|
| `ledger.py` | The implementation (297 lines) |
| `test_ledger.py` | 26 tests |
| `demo.py` | Narrated scenario |
| `test-output.txt`, `demo-output.txt`, `mutation-results.txt` | Captured output of the runs above |
