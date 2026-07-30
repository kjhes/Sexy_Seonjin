"""
정책 판단 엔진 REST API
백엔드 → POST /evaluate {amount, category, ...} → 승인/거부 JSON

Layer 1(policy_engine.py, 하드 규칙)을 먼저 통과한 요청만
Layer 2(semantic_layer.py, LLM 의미 판단)로 넘어간다.
"""

from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from policy_engine import PaymentRequest, PolicyConfig, PolicyEngine, RequestGuard, SpendTracker
from semantic_layer import SemanticGuard

app = FastAPI(title="Agentic Commerce Policy Engine")

config = PolicyConfig(
    daily_limit=10.0,
    per_tx_limit=2.0,
    allowed_categories=["gemini", "bigquery"],
    max_calls_per_day=50,
    max_calls_per_minute=5,
    max_consecutive_ai_failures=3,
    official_recipient_addresses={
        "gemini": "TODO_GEMINI_공식_솔라나_주소",
        "bigquery": "TODO_BIGQUERY_공식_솔라나_주소",
    },  # TODO: 정보보안 친구한테 실제 Solana 주소 문자열 받아서 교체
)
tracker = SpendTracker()
guard = RequestGuard()
engine = PolicyEngine(config, tracker, guard)
semantic_guard = SemanticGuard()


class PaymentRequestIn(BaseModel):
    amount: float
    category: str
    wallet_connected: bool = True
    wallet_balance: float = 0.0
    api_key_valid: bool = True
    recipient_address: str = ""
    request_id: str = ""
    ai_consecutive_failures: int = 0
    has_required_permission: bool = True
    infra_stable: bool = True
    user_prompt: Optional[str] = ""
    task_plan: Optional[str] = ""


@app.post("/evaluate")
def evaluate(req: PaymentRequestIn):
    request = PaymentRequest(**req.model_dump())
    result = engine.evaluate(request)

    if result["approved"] and request.user_prompt:
        try:
            semantic_result = semantic_guard.check(request.user_prompt, request.task_plan)
        except Exception:
            # LLM 호출 자체가 실패하면 "API 상태 불안정"(조건 14)으로 취급해 거부한다.
            result["approved"] = False
            result["policy_check"].append("semantic_layer_unavailable_fail")
            result["reason"] = "의미 판단 서비스(Gemini)에 연결할 수 없어 요청을 거부했습니다."
            return result

        result["policy_check"] += semantic_result["checks"]
        if not semantic_result["approved"]:
            result["approved"] = False
            result["reason"] = semantic_result["reason"]

    return result


@app.get("/health")
def health():
    return {"status": "ok"}
