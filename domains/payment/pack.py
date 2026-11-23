"""payment DomainPack: 결제 의도 생성 → 승인 → capture → 예약 확정 (+ 환불). 모든 결제 툴은 샌드박스 전용."""
from __future__ import annotations

import os
from typing import Any, Callable

from langchain_core.tools import BaseTool

from domains.common.holds import get_store
from orchestra import registry
from orchestra.filters.base import Filter
from orchestra.specs import AgentSpec, EgressRule, FilterSpec, SideEffect, TeamSpec, ToolSpec

from .filters import amount_limit, hold_expiry, idempotency, state_transition, _amount
from .ledger import MockPaymentProvider
from .tools import make_tools

SANDBOX = "orchestra-payments"
PLACEHOLDER_HOST = "payments.example.invalid"   # 실제 값은 PAYMENT_PROVIDER_HOST (policy 생성 시 필수)

_SPECS = {
    "create_payment_intent": (SideEffect.RESERVE, False),
    "capture_payment": (SideEffect.COMMIT, False),
    "confirm_booking": (SideEffect.RESERVE, False),
    "refund_payment": (SideEffect.COMMIT, False),
    "get_receipt": (SideEffect.READ, True),
}
_PATHS = {
    "create_payment_intent": ("POST", "/v1/payment_intents"),
    "capture_payment": ("POST", "/v1/payment_intents/**"),
    "confirm_booking": ("POST", "/v1/payment_intents/**"),
    "refund_payment": ("POST", "/v1/refunds"),
    "get_receipt": ("GET", "/v1/payment_intents/**"),
}


def provider_host() -> str:
    return os.environ.get("PAYMENT_PROVIDER_HOST") or PLACEHOLDER_HOST


class PaymentPack:
    name = "payment"

    def __init__(self, services: dict[str, Any]):
        holds = get_store(services)
        self.provider: MockPaymentProvider = services.setdefault("payments", MockPaymentProvider(holds))
        self._tools = make_tools(self.provider)
        services["approval_amount"] = lambda tool, args: _amount(self.provider, tool, args)

    def tools(self) -> dict[str, tuple[BaseTool, ToolSpec]]:
        out = {}
        for n, t in self._tools.items():
            se, idem = _SPECS[n]
            method, path = _PATHS[n]
            out[n] = (t, ToolSpec(
                name=n, side_effect=se, idempotent=idem, sensitivity="pci", sandbox="required", sandbox_name=SANDBOX,
                egress=[EgressRule(host=provider_host(), rules=[{"method": method, "path": path}])],
            ))
        return out

    def agents(self) -> dict[str, AgentSpec]:
        return {
            "payment/checkout_agent": AgentSpec(
                id="payment/checkout_agent", name="checkout_agent",
                tools=["create_payment_intent", "capture_payment", "confirm_booking"],
                prompt="결제 담당. hold_ids 로 intent 를 만들고, capture_payment 는 사용자 승인 후에만 실행되며, 성공하면 confirm_booking 한다."),
            "payment/refund_agent": AgentSpec(
                id="payment/refund_agent", name="refund_agent", tools=["get_receipt", "refund_payment"],
                prompt="환불 담당. 영수증을 확인한 뒤 환불한다 (승인 필요)."),
        }

    def teams(self) -> dict[str, TeamSpec]:
        return {}

    def filters(self) -> list[FilterSpec]:
        money = ["create_payment_intent", "capture_payment", "confirm_booking", "refund_payment"]
        return [
            FilterSpec(name="idempotency", stages=["pre_tool"], order=20, applies_to=money),
            FilterSpec(name="state_transition", stages=["pre_tool"], order=30,
                       applies_to=["capture_payment", "confirm_booking", "refund_payment"]),
            FilterSpec(name="hold_expiry", stages=["pre_tool"], order=35, applies_to=["create_payment_intent"]),
            FilterSpec(name="amount_limit", stages=["pre_tool"], order=40,
                       applies_to=["create_payment_intent", "capture_payment", "refund_payment"]),
        ]

    def filter_impls(self) -> dict[str, Callable[..., Filter]]:
        p = self.provider
        return {
            "idempotency": idempotency,
            "state_transition": lambda: state_transition(p),
            "hold_expiry": lambda: hold_expiry(p),
            "amount_limit": lambda **kw: amount_limit(p, **kw),
        }


registry.register("payment", PaymentPack)
