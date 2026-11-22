"""payment 도메인 필터 (DESIGN §11.4). 모두 pre_tool, 장부(provider)를 읽기만 한다."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from orchestra.filters.base import Filter, FilterContext, FilterDecision

from .ledger import MockPaymentProvider
from .state_machine import can_apply

MERCHANT = "Orchestra Travel (mock)"


def _amount(provider: MockPaymentProvider, tool: str, args: dict[str, Any]) -> int | None:
    if tool == "create_payment_intent":
        return provider.amount_for_holds(list(args.get("hold_ids", [])))
    if tool == "capture_payment":
        i = provider.intents.get(args.get("intent_id", ""))
        return i.amount if i else None
    if tool == "refund_payment":
        return int(args.get("amount", 0))
    return None


def idempotency() -> Filter:
    """idempotency_key 가 비어 있으면 (thread, tool, 인자) 해시로 결정적 키를 주입한다. 중복 방지는 장부가 한다."""

    class _F:
        name = "idempotency"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            args = dict(ctx.payload or {})
            if args.get("idempotency_key"):
                return FilterDecision.allow()
            base = {k: v for k, v in args.items() if k != "idempotency_key"}
            blob = json.dumps([ctx.state.get("thread_id", ""), ctx.tool, base], sort_keys=True, default=str)
            args["idempotency_key"] = "idem_" + hashlib.sha256(blob.encode()).hexdigest()[:20]
            return FilterDecision.modify(args, "idempotency_key injected")

    return _F()


def state_transition(provider: MockPaymentProvider) -> Filter:
    class _F:
        name = "state_transition"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            args = ctx.payload or {}
            status = provider.status_of(args.get("intent_id"))
            if ctx.tool != "create_payment_intent" and status is None:
                return FilterDecision.block(f"unknown intent {args.get('intent_id')!r}")
            ok, why = can_apply(ctx.tool or "", status)
            return FilterDecision.allow() if ok else FilterDecision.block(why)

    return _F()


def hold_expiry(provider: MockPaymentProvider) -> Filter:
    """create_payment_intent 시 모든 hold 가 존재·유효(TTL 이내, 미사용)해야 한다."""

    class _F:
        name = "hold_expiry"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            ids = list((ctx.payload or {}).get("hold_ids", []))
            if not ids:
                return FilterDecision.block("hold_ids is empty")
            bad = [h for h in ids if not provider.holds.is_active(h)]
            return FilterDecision.block(f"hold expired or invalid: {bad}; re-hold required") if bad else FilterDecision.allow()

    return _F()


def amount_limit(provider: MockPaymentProvider, max_krw: int | None = None,
                 approval_krw: int | None = None) -> Filter:
    """금액 > 상한이면 BLOCK, > 승인 기준이면 REQUIRE_APPROVAL(금액·가맹점 포함 문구)."""
    max_krw = max_krw if max_krw is not None else int(os.environ.get("PAYMENT_MAX_KRW", "1000000"))
    approval_krw = approval_krw if approval_krw is not None else int(os.environ.get("PAYMENT_APPROVAL_KRW", "0"))

    class _F:
        name = "amount_limit"

        def __call__(self, ctx: FilterContext) -> FilterDecision:
            amt = _amount(provider, ctx.tool or "", ctx.payload or {})
            if amt is None:
                return FilterDecision.allow()
            if amt > max_krw:
                return FilterDecision.block(f"amount {amt:,} exceeds limit {max_krw:,}")
            if ctx.tool in ("capture_payment", "refund_payment") and amt >= approval_krw \
                    and not ctx.state.get("approval_granted"):
                verb = "결제" if ctx.tool == "capture_payment" else "환불"
                return FilterDecision.require_approval(
                    f"{verb} {amt:,}원 사용자 승인 필요",
                    {"kind": "payment" if ctx.tool == "capture_payment" else "refund", "amount": amt,
                     "currency": "KRW", "merchant": MERCHANT,
                     "ask": f"{amt:,}원을 {MERCHANT}에 {verb}할까요? (결제 후 환불 가능)"},
                )
            return FilterDecision.allow()

    return _F()
