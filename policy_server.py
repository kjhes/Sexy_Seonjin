"""
정책 판단 엔진 REST API
백엔드(main.py, x402 결제 서버) → POST /evaluate {amount, category, ...} → 승인/거부 JSON

Layer 1(policy_engine.py, 하드 규칙)을 먼저 통과한 요청만
Layer 2(semantic_layer.py, LLM 의미 판단)로 넘어간다.

main.py는 백엔드 친구의 x402 결제 서버가 차지하고 있어서, 이 서버는 별도 프로세스로
띄운다. .env의 POLICY_ENGINE_URL(기본 http://localhost:8000)이 이 서버를 가리킨다.
"""

import hmac
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from policy_engine import PaymentRequest, PolicyConfig, PolicyEngine, RequestGuard, SpendTracker
from semantic_layer import SemanticGuard

load_dotenv()

# main.py 말고 아무나 /evaluate를 직접 호출해서 예산을 소진시키거나 판단을 우회하지
# 못하도록, 공유 비밀키로 요청을 검증한다. 값이 없으면 인증 없이 뜨는 걸 막기 위해
# 서버 시작 자체를 거부한다(fail-safe) — main.py의 필수 환경변수 검증과 같은 원칙.
POLICY_SHARED_SECRET = os.environ.get("POLICY_SHARED_SECRET")
if not POLICY_SHARED_SECRET:
    raise RuntimeError("POLICY_SHARED_SECRET 환경변수가 필요합니다 (.env 확인, main.py와 동일한 값 사용)")

app = FastAPI(title="Agentic Commerce Policy Engine")

config = PolicyConfig(
    daily_limit=10.0,
    per_tx_limit=2.0,
    allowed_categories=["gemini", "bigquery"],
    max_calls_per_day=50,
    max_calls_per_minute=5,
    max_consecutive_ai_failures=3,
    official_recipient_addresses={
        # main.py(백엔드)가 결제를 받는 실제 지갑 주소. 같은 .env를 공유하므로 항상 동기화된다.
        "gemini": os.environ.get("SOLANA_WALLET_ADDRESS", ""),
        # BigQuery는 아직 x402 라우트가 없고 전용 주소도 안 받아서 비워둠 (받는 즉시 채울 것)
        "bigquery": "",
    },
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
def evaluate(req: PaymentRequestIn, x_policy_secret: str = Header(default="")):
    # hmac.compare_digest로 타이밍 공격(문자를 한 글자씩 추측)을 방지한다.
    if not hmac.compare_digest(x_policy_secret, POLICY_SHARED_SECRET):
        raise HTTPException(status_code=401, detail="인증되지 않은 호출입니다.")

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

    # Layer 1 + Layer 2를 모두 통과했을 때만 실제 지출로 기록한다.
    # (예전엔 engine.evaluate() 안에서 Layer 1 통과 시점에 바로 커밋했는데, 그 뒤
    # Layer 2가 거부해도 이미 지출이 깎여 있어서 승인 안 된 요청이 하루 한도를
    # 갉아먹는 버그가 있었다. 최종 승인 여부가 확정된 지금 시점에만 커밋한다.)
    if result["approved"]:
        result["spent_today_after"] = engine.commit(request.amount)

    return result


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
