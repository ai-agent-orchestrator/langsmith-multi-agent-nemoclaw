from domains.common.holds import HoldStore
from domains.payment.filters import amount_limit, hold_expiry, idempotency, state_transition
from domains.payment.ledger import MockPaymentProvider
from domains.payment.state_machine import can_apply
from orchestra.filters.base import Action, FilterContext, Stage


def prov(now=[0.0]):
    return MockPaymentProvider(HoldStore(lambda: now[0])), now


def c(tool, args, **state):
    return FilterContext(Stage.PRE_TOOL, args, tool=tool, state=state)


def test_state_machine_table():
    assert can_apply("create_payment_intent", None)[0]
    assert not can_apply("capture_payment", None)[0]
    assert can_apply("capture_payment", "created")[0]
    assert not can_apply("capture_payment", "captured")[0]          # 이중 결제 방지
    assert not can_apply("confirm_booking", "created")[0]           # 결제 전 확정 금지
    assert can_apply("refund_payment", "captured")[0] and not can_apply("refund_payment", "created")[0]


def test_idempotent_replay_returns_same_result():
    p, _ = prov()
    h = p.holds.create("room", "h1", "x", 100)
    a = p.create_intent([h.hold_id], "k1"); b = p.create_intent([h.hold_id], "k1")
    assert a["intent_id"] == b["intent_id"] and b["idempotent_replay"] and len(p.intents) == 1
    assert p.create_intent([h.hold_id], "k2")["intent_id"] != a["intent_id"]


def test_idempotency_filter_deterministic_key():
    f = idempotency()
    d1 = f(c("capture_payment", {"intent_id": "pi_1"}, thread_id="t"))
    d2 = f(c("capture_payment", {"intent_id": "pi_1"}, thread_id="t"))
    d3 = f(c("capture_payment", {"intent_id": "pi_1"}, thread_id="other"))
    assert d1.payload["idempotency_key"] == d2.payload["idempotency_key"] != d3.payload["idempotency_key"]
    keep = f(c("capture_payment", {"intent_id": "pi_1", "idempotency_key": "mine"}))
    assert keep.action == Action.ALLOW


def test_amount_limit_block_and_approval():
    p, _ = prov()
    small = p.holds.create("room", "h", "s", 180_000); big = p.holds.create("room", "h", "b", 2_400_000)
    f = amount_limit(p, max_krw=1_000_000, approval_krw=0)
    assert f(c("create_payment_intent", {"hold_ids": [big.hold_id]})).action == Action.BLOCK
    i = p.create_intent([small.hold_id], "k")["intent_id"]
    d = f(c("capture_payment", {"intent_id": i}))
    assert d.action == Action.REQUIRE_APPROVAL and d.approval["amount"] == 180_000 and "180,000" in d.approval["ask"]
    assert f(c("capture_payment", {"intent_id": i}, approval_granted=True)).action == Action.ALLOW
    hi = amount_limit(p, max_krw=1_000_000, approval_krw=500_000)      # 승인 기준 미만이면 승인 없이 통과
    assert hi(c("capture_payment", {"intent_id": i})).action == Action.ALLOW


def test_hold_expiry_uses_clock():
    p, now = prov()
    h = p.holds.create("room", "h", "s", 1)
    f = hold_expiry(p)
    assert f(c("create_payment_intent", {"hold_ids": [h.hold_id]})).action == Action.ALLOW
    now[0] = 16 * 60                                                   # TTL 15분 경과
    assert f(c("create_payment_intent", {"hold_ids": [h.hold_id]})).action == Action.BLOCK
    assert f(c("create_payment_intent", {"hold_ids": []})).action == Action.BLOCK
    assert f(c("create_payment_intent", {"hold_ids": ["nope"]})).action == Action.BLOCK


def test_state_transition_filter_blocks_unknown_and_double_capture():
    p, _ = prov()
    h = p.holds.create("room", "h", "s", 1)
    i = p.create_intent([h.hold_id], "k")["intent_id"]
    f = state_transition(p)
    assert f(c("capture_payment", {"intent_id": "pi_x"})).action == Action.BLOCK
    assert f(c("capture_payment", {"intent_id": i})).action == Action.ALLOW
    p.capture(i, "k2")
    assert f(c("capture_payment", {"intent_id": i})).action == Action.BLOCK
    assert f(c("confirm_booking", {"intent_id": i})).action == Action.ALLOW
