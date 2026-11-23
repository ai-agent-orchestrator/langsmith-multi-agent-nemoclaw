"""Persistent masked approval/payment atomicity, restart safety, and memory compatibility."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from orchestra.hitl import make_approval_record


@pytest.fixture
def ledger_env(monkeypatch):
    import secrets
    monkeypatch.setenv("ORCHESTRA_LEDGER_HMAC_KEY", secrets.token_hex(32))
    monkeypatch.delenv("ORCHESTRA_LEDGER_KEY_ID", raising=False)
    monkeypatch.delenv("ORCHESTRA_LEDGER", raising=False)


def record(**changes):
    return make_approval_record("capture_payment", {"intent_id": "pi_0001", "idempotency_key": "pay-one"},
                                user_id="alice", amount=100, expires_at=20, request_id="request", **changes)


def test_sqlite_without_key_fails_before_file_creation(tmp_path, monkeypatch):
    from orchestra.ledger import create_ledger_store, LedgerConfigurationError
    monkeypatch.setenv("ORCHESTRA_LEDGER", f"sqlite:///{tmp_path / 'ledger.db'}")
    monkeypatch.delenv("ORCHESTRA_LEDGER_HMAC_KEY", raising=False)
    with pytest.raises(LedgerConfigurationError, match="HMAC"):
        create_ledger_store()
    assert list(tmp_path.iterdir()) == []


def test_consumed_approval_survives_restart_and_cannot_be_granted_again(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    path = tmp_path / "ledger.db"
    approval = record()
    ledger = ApprovalLedger(store=SQLiteLedgerStore(path))
    assert ledger.grant(approval, now=10)
    assert ledger.claim(approval, now=10)
    restarted = ApprovalLedger(store=SQLiteLedgerStore(path))
    assert [entry["decision"] for entry in restarted.history] == [True]
    assert not restarted.is_granted(approval.key, binding=approval, now=10)
    assert not restarted.claim(approval, now=10)
    assert not restarted.grant(approval, now=10)


def test_expiry_and_cleanup(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    store = SQLiteLedgerStore(tmp_path / "ledger.db")
    ledger = ApprovalLedger(store=store)
    approval = record()
    assert ledger.grant(approval, now=10)
    assert not ledger.is_granted(approval.key, binding=approval, now=20)
    assert not ledger.claim(approval, now=20)
    assert store.cleanup_expired(20) == 1
    assert not ledger.grant(approval, now=20)


def test_rotation_rejects_same_arguments_with_different_key(tmp_path, ledger_env, monkeypatch):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    approval = record()
    path = tmp_path / "ledger.db"
    old = SQLiteLedgerStore(path)
    assert ApprovalLedger(store=old).grant(approval, now=10)
    monkeypatch.setenv("ORCHESTRA_LEDGER_HMAC_KEY", "different-secret-material" * 3)
    new = SQLiteLedgerStore(path)
    assert old.key_id != new.key_id
    assert not ApprovalLedger(store=new).claim(approval, now=10)


def test_database_and_sidecars_never_contain_raw_sentinels(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger, EventLog
    pii, pci = "900101-1234567", "4111-1111-1111-1111"
    store = SQLiteLedgerStore(tmp_path / "ledger.db")
    approval = make_approval_record("capture", {"card": pci, "person": pii}, user_id=pii,
                                    amount=100, expires_at=20, request_id=pci)
    with sqlite3.connect(tmp_path / "ledger.db") as wal_reader:
        assert wal_reader.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        wal_reader.execute("BEGIN")
        wal_reader.execute("SELECT count(*) FROM approvals").fetchone()
        assert ApprovalLedger(store=store).grant(approval, now=10)
        EventLog(store=store, scope="sentinel").add("test", card=pci, person=pii,
                                                   args={"card": pci, "ssn": pii})
        assert (tmp_path / "ledger.db-wal").exists() and (tmp_path / "ledger.db-shm").exists()
        for path in tmp_path.iterdir():
            data = path.read_bytes()
            assert pii.encode() not in data and pci.encode() not in data
            assert approval.key.encode() not in data
    with sqlite3.connect(tmp_path / "ledger.db") as connection:
        stored = connection.execute("SELECT display, binding_hash, user_hash FROM approvals").fetchone()
        assert "REDACTED" in stored[0]
        assert len(stored[1]) == len(stored[2]) == 64


def test_two_connections_consume_exactly_once(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    path, approval = tmp_path / "ledger.db", record()
    assert ApprovalLedger(store=SQLiteLedgerStore(path)).grant(approval, now=10)
    barrier = Barrier(2)

    def claim(_):
        ledger = ApprovalLedger(store=SQLiteLedgerStore(path))
        barrier.wait(timeout=10)
        return ledger.claim(approval, now=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1


def test_claim_and_payment_idempotency_commit_together_under_two_requests(tmp_path, ledger_env):
    from domains.common.holds import HoldStore
    from domains.payment.ledger import MockPaymentProvider
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    path = tmp_path / "ledger.db"
    store = SQLiteLedgerStore(path)
    holds = HoldStore(lambda: 10)
    hold = holds.create("room", "hotel", "Room", 100)
    provider = MockPaymentProvider(holds, store=store)
    provider.create_intent([hold.hold_id], "create-one")
    approval = record()
    assert ApprovalLedger(store=store).grant(approval, now=10)
    barrier = Barrier(2)

    def pay(_):
        connection = SQLiteLedgerStore(path)
        ledger = ApprovalLedger(store=connection)
        local_provider = MockPaymentProvider(HoldStore(lambda: 10), store=connection)
        barrier.wait(timeout=10)
        with connection.transaction():
            if not ledger.claim(approval, now=10):
                return False
            local_provider.capture("pi_0001", "pay-one")
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(pay, range(2))) == 1
    restart_store = SQLiteLedgerStore(path)
    restarted = MockPaymentProvider(HoldStore(lambda: 10), store=restart_store)
    assert restarted.captures == 1
    assert restarted.capture("pi_0001", "pay-one")["idempotent_replay"]
    assert restarted.captures == 1
    assert not ApprovalLedger(store=restart_store).claim(approval, now=10)


def test_payment_write_failure_rolls_back_claim_and_idempotency(tmp_path, ledger_env):
    from domains.common.holds import HoldStore
    from domains.payment.ledger import MockPaymentProvider
    from orchestra.ledger import SQLiteLedgerStore, LedgerConfigurationError
    from orchestra.runtime import ApprovalLedger
    path = tmp_path / "ledger.db"
    store = SQLiteLedgerStore(path)
    holds = HoldStore(lambda: 10)
    hold = holds.create("room", "hotel", "Room", 100)
    provider = MockPaymentProvider(holds, store=store)
    provider.create_intent([hold.hold_id], "create-one")
    approval = record()
    ledger = ApprovalLedger(store=store)
    assert ledger.grant(approval, now=10)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_payment BEFORE INSERT ON component_state BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(LedgerConfigurationError):
        with store.transaction():
            assert ledger.claim(approval, now=10)
            provider.capture("pi_0001", "pay-one")
    restarted = SQLiteLedgerStore(path)
    assert ApprovalLedger(store=restarted).is_granted(approval.key, binding=approval, now=10)
    payment = MockPaymentProvider(HoldStore(lambda: 10), store=restarted)
    assert payment.captures == 0 and payment.intents["pi_0001"].status == "created"


def test_event_deduplication_and_chain_hook_survive_restart(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import EventLog
    path = tmp_path / "ledger.db"
    seen = []
    events = EventLog(store=SQLiteLedgerStore(path, event_hook=seen.append), scope="test")
    events.add("blocked", dedupe_key="once", reason="900101-1234567")
    assert len(seen) == 1 and "900101-1234567" not in str(seen)
    restarted = EventLog(store=SQLiteLedgerStore(path), scope="test")
    restarted.add("blocked", dedupe_key="once", reason="duplicate")
    assert len(restarted.events) == 1
    assert restarted.events[0].data["reason"] == "[REDACTED_ID]"


def test_full_graph_uses_single_sqlite_store_and_preserves_approval_resume(tmp_path, ledger_env, monkeypatch):
    from orchestra.simulation import run_scenario, check_expect
    monkeypatch.setenv("ORCHESTRA_LEDGER", f"sqlite:///{tmp_path / 'graph.db'}")
    result = run_scenario("jeju_trip_payment")
    assert check_expect(result) == []
    runtime = result.app.runtime
    assert runtime.approvals.store is runtime.events.store is runtime.services["payments"].store
    assert runtime.services["payments"].captures == 1
    from orchestra.observability.evaluators import payment_safety
    assert payment_safety(runtime.events.events) == 1

@pytest.mark.parametrize("change", [{"user_id": "bob"}, {"amount": 101},
                                    {"args": {"intent_id": "pi_0002", "idempotency_key": "pay-one"}}])
def test_persistent_binding_rejects_changed_user_amount_or_arguments(tmp_path, ledger_env, change):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import ApprovalLedger
    store = SQLiteLedgerStore(tmp_path / "ledger.db")
    approval = record()
    ledger = ApprovalLedger(store=store)
    assert ledger.grant(approval, now=10)
    values = {"tool": "capture_payment", "args": {"intent_id": "pi_0001", "idempotency_key": "pay-one"},
              "user_id": "alice", "amount": 100, "expires_at": 20, "request_id": "request", **change}
    changed = make_approval_record(**values)
    restarted = ApprovalLedger(store=SQLiteLedgerStore(tmp_path / "ledger.db"))
    assert not restarted.claim(changed, now=10)
    assert restarted.claim(approval, now=10)


def test_schema_is_versioned_and_unknown_versions_fail_closed(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore, LedgerConfigurationError
    path = tmp_path / "ledger.db"
    SQLiteLedgerStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        fields = {row[1] for row in connection.execute("PRAGMA table_info(approvals)")}
        assert fields == {"display", "binding_hash", "user_hash", "expires_at", "consumed", "schema_version", "key_id"}
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(LedgerConfigurationError, match="schema"):
        SQLiteLedgerStore(path)


def test_future_chain_hook_exception_rolls_back_event_only(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import EventLog

    def fail(event):
        raise RuntimeError("hook failed")

    path = tmp_path / "ledger.db"
    store = SQLiteLedgerStore(path, event_hook=fail)
    events = EventLog(store=store, scope="test")
    with pytest.raises(RuntimeError, match="hook failed"):
        events.add("test", dedupe_key="same", reason="safe")
    assert events.events == []
    retry = EventLog(store=SQLiteLedgerStore(path), scope="test")
    retry.add("test", dedupe_key="same", reason="safe")
    assert len(retry.events) == 1


def test_default_memory_creates_no_database_and_needs_no_key(tmp_path, monkeypatch):
    from orchestra.ledger import MemoryLedgerStore, create_ledger_store
    from orchestra.runtime import Runtime
    monkeypatch.delenv("ORCHESTRA_LEDGER", raising=False)
    monkeypatch.delenv("ORCHESTRA_LEDGER_HMAC_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    store = create_ledger_store()
    assert isinstance(store, MemoryLedgerStore)
    runtime = Runtime(ledger_store=store)
    assert runtime.approvals.store is runtime.events.store is store
    assert list(tmp_path.iterdir()) == []


def test_event_store_never_persists_raw_args_or_unkeyed_binding(tmp_path, ledger_env):
    from orchestra.ledger import SQLiteLedgerStore
    from orchestra.runtime import EventLog
    path = tmp_path / "ledger.db"
    store = SQLiteLedgerStore(path)
    events = EventLog(store=store)
    approval = make_approval_record("capture", {"account_number": "123456789012"}, user_id="person@example.com",
                                    amount=100, expires_at=20)
    events.add("approval", tool="capture", key=approval.key, approved=True,
               args={"account_number": "123456789012"}, binding=approval.model_dump())
    raw = path.read_bytes()
    assert b"123456789012" not in raw and b"person@example.com" not in raw
    assert approval.key.encode() not in raw
    assert events.events[0].data["approval_ref"] == store.token("approval-ref", approval.key)


def test_changed_idempotency_request_is_rejected(tmp_path, ledger_env):
    from domains.common.holds import HoldStore
    from domains.payment.ledger import MockPaymentProvider
    from orchestra.ledger import SQLiteLedgerStore, LedgerConfigurationError
    store = SQLiteLedgerStore(tmp_path / "ledger.db")
    holds = HoldStore(lambda: 10)
    one = holds.create("room", "one", "One", 100)
    two = holds.create("room", "two", "Two", 200)
    provider = MockPaymentProvider(holds, store=store)
    provider.create_intent([one.hold_id], "same")
    with pytest.raises(LedgerConfigurationError, match="arguments changed"):
        provider.create_intent([two.hold_id], "same")
    assert len(provider.intents) == 1


def test_memory_failed_commit_still_consumes_authorization(monkeypatch):
    from langchain_core.tools import StructuredTool
    from orchestra import hitl
    from orchestra.filters.builtin import BUILTIN_FILTERS
    from orchestra.filters.chain import build_chain
    from orchestra.filters.tool_wrapper import wrap_tool
    from orchestra.runtime import Runtime
    from orchestra.specs import ToolSpec, SideEffect, FilterSpec

    calls = []

    def fail_after_effect():
        calls.append("charged")
        raise RuntimeError("provider failed")

    runtime = Runtime(clock=lambda: 10)
    raw = StructuredTool.from_function(func=fail_after_effect, name="capture", description="Capture")
    spec = ToolSpec(name="capture", side_effect=SideEffect.COMMIT)
    chain = build_chain([FilterSpec(name="side_effect_policy", stages=["pre_tool"])], BUILTIN_FILTERS)
    monkeypatch.setattr(hitl, "request", lambda payload: True)
    with pytest.raises(RuntimeError, match="provider failed"):
        wrap_tool(raw, spec, chain, runtime).invoke({})
    approval = next(iter(runtime.approvals._records.values()))
    assert calls == ["charged"]
    assert not runtime.approvals.is_granted(approval.key, binding=approval, now=10)
    assert not runtime.approvals.claim(approval, now=10)
