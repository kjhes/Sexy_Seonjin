"""Gemini x402 결제 서버.

흐름 (project.md 3~10단계):
  1. AI 친구가 POST /api/gemini 호출
  2. x402 미들웨어가 결제 없으면 402로 가격 정보 응답, 결제 있으면 검증 후 통과
  3. call_policy_engine()이 방금 검증된 결제 트랜잭션에서 실제 지갑 주소를 뽑아
     Solana Devnet RPC로 온체인 USDC 잔액을 조회하고, AI 친구의 /evaluate로 재검증 요청
  4. 승인되면 결제 정산(settle)이 이루어지고, 진짜 Gemini API를 호출해 결과를 돌려줌
"""

import json
import logging
import os
import uuid

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption, RouteConfig
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.mechanisms.svm.constants import SOLANA_DEVNET_CAIP2, USDC_DEVNET_ADDRESS
from x402.mechanisms.svm.exact import ExactSvmServerScheme
from x402.mechanisms.svm.types import ExactSvmPayload
from x402.mechanisms.svm.utils import decode_transaction_from_payload, extract_transaction_info
from x402.schemas import Network
from x402.server import x402ResourceServer

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gemini_x402")

# ---------------------------------------------------------------------------
# 환경 변수
# ---------------------------------------------------------------------------
SOLANA_WALLET_ADDRESS = os.getenv("SOLANA_WALLET_ADDRESS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POLICY_ENGINE_URL = os.getenv("POLICY_ENGINE_URL")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
GEMINI_PRICE_USD = float(os.getenv("GEMINI_PRICE_USD", "0.005"))

if not SOLANA_WALLET_ADDRESS:
    raise RuntimeError("SOLANA_WALLET_ADDRESS 환경변수가 필요합니다 (.env 확인)")
if not POLICY_ENGINE_URL:
    raise RuntimeError("POLICY_ENGINE_URL 환경변수가 필요합니다 (AI 친구 /evaluate 서버 주소)")

SVM_NETWORK: Network = SOLANA_DEVNET_CAIP2  # "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# ---------------------------------------------------------------------------
# FastAPI 앱 + x402 결제 미들웨어
# ---------------------------------------------------------------------------
app = FastAPI(title="Gemini x402 결제 서버")

facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
resource_server = x402ResourceServer(facilitator)
resource_server.register(SVM_NETWORK, ExactSvmServerScheme())

routes = {
    "POST /api/gemini": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=SOLANA_WALLET_ADDRESS,
                price=f"${GEMINI_PRICE_USD}",
                network=SVM_NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Gemini API 호출 (x402 결제 필요)",
    ),
}
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=resource_server)


class GeminiRequestIn(BaseModel):
    prompt: str
    task_plan: str | None = None


# AI 소비자별 연속 실패 횟수 (BACKEND_연동_가이드.md의 ai_consecutive_failures).
# 단일 프로세스 데모 서버라 프로세스 메모리에 카운터를 둔다.
_consecutive_failures = 0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 온체인 USDC 잔액 조회
# ---------------------------------------------------------------------------
async def get_onchain_usdc_balance(wallet_address: str) -> float:
    """Solana Devnet RPC(getTokenAccountsByOwner)로 지갑의 실제 USDC 잔액을 조회한다."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet_address,
            {"mint": USDC_DEVNET_ADDRESS},
            {"encoding": "jsonParsed"},
        ],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(SOLANA_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Solana RPC 오류: {data['error']}")

    accounts = data.get("result", {}).get("value", [])
    total = 0.0
    for acc in accounts:
        try:
            token_amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(token_amount["uiAmount"] or 0.0)
        except (KeyError, TypeError):
            continue
    return total


def extract_payer_wallet(request: Request) -> str | None:
    """x402 미들웨어가 검증을 마친 뒤 request.state에 실어둔 결제 트랜잭션에서
    실제로 서명해서 USDC를 보낸 지갑(SPL 토큰 payer) 주소를 꺼낸다.

    이 값이 채워져 있다는 것 자체가 '결제 트랜잭션에 서명 가능한 지갑이 연결되어
    있었다'는 뜻이므로 wallet_connected 판단 근거로 쓴다.
    """
    payment_payload = getattr(request.state, "payment_payload", None)
    if payment_payload is None:
        return None
    try:
        svm_payload = ExactSvmPayload.from_dict(payment_payload.payload)
        tx = decode_transaction_from_payload(svm_payload)
        tx_info = extract_transaction_info(tx)
        return tx_info.payer if tx_info else None
    except Exception:
        logger.exception("결제 트랜잭션에서 payer 지갑 주소 추출 실패")
        return None


# ---------------------------------------------------------------------------
# 정책 엔진(/evaluate) 연동
# ---------------------------------------------------------------------------
async def call_policy_engine(request: Request, prompt: str, task_plan: str | None) -> dict:
    """AI 친구의 정책 판단 엔진(POST {POLICY_ENGINE_URL}/evaluate)에 이번 결제 건의
    실제 사실값을 채워 보낸다.

    wallet_connected / wallet_balance는 절대 placeholder를 쓰지 않는다:
    - wallet_connected: 이 요청에서 x402 결제가 이미 검증된 트랜잭션에 서명한
      지갑 주소를 실제로 뽑아낼 수 있었는지로 판단.
    - wallet_balance: 그 지갑 주소를 Solana Devnet RPC로 조회한 실제 온체인
      USDC 잔액. 조회에 실패하면 fail-safe로 0.0 + infra_stable=False를 보낸다
      (BACKEND_연동_가이드.md: 값을 비우면 항상 통과로 오인되므로 반드시 채워야 함).
    """
    global _consecutive_failures

    payer_address = extract_payer_wallet(request)
    wallet_connected = payer_address is not None

    wallet_balance = 0.0
    infra_stable = True
    if payer_address:
        try:
            wallet_balance = await get_onchain_usdc_balance(payer_address)
        except Exception:
            logger.exception("온체인 USDC 잔액 조회 실패 (Solana RPC 불안정)")
            infra_stable = False
            wallet_balance = 0.0

    body = {
        "amount": GEMINI_PRICE_USD,
        "category": "gemini",
        "wallet_connected": wallet_connected,
        "wallet_balance": wallet_balance,
        "api_key_valid": bool(GEMINI_API_KEY),
        "recipient_address": SOLANA_WALLET_ADDRESS,
        "request_id": str(uuid.uuid4()),
        "ai_consecutive_failures": _consecutive_failures,
        "has_required_permission": True,
        "infra_stable": infra_stable,
        "user_prompt": prompt,
        "task_plan": task_plan,
    }

    url = f"{POLICY_ENGINE_URL.rstrip('/')}/evaluate"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            decision = resp.json()
    except Exception:
        logger.exception("정책 엔진(/evaluate) 호출 실패")
        _consecutive_failures += 1
        raise

    if not decision.get("approved"):
        _consecutive_failures += 1
    else:
        _consecutive_failures = 0

    return decision


# ---------------------------------------------------------------------------
# 실제 Gemini API 호출
# ---------------------------------------------------------------------------
_CHAIN_SYSTEM_INSTRUCTION = """너는 자율 에이전트 체인의 한 단계를 처리하는 실행기다.

작업 계획: "{task_plan}"
사용자 요청: "{prompt}"

위 요청에 실제로 답하라. 그리고 이 답변으로 작업 계획 전체가 실질적으로 완료됐는지 판단하라.

- answer: 사용자에게 보여줄 실제 답변
- task_complete: 계획의 모든 단계가 충족됐으면 true. 완료 여부가 조금이라도 애매하면
  true로 판단하라 — 추가로 이어서 호출하면 실제 비용(USDC 결제)이 발생하므로,
  불필요하게 이어가는 것보다 여기서 멈추는 편이 훨씬 안전하다.
- next_prompt: task_complete가 false일 때만, 다음 단계에 그대로 사용할 완전한 프롬프트를
  작성하라. task_complete가 true면 빈 문자열로 둬라.
"""

_CHAIN_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "task_complete": {"type": "BOOLEAN"},
        "next_prompt": {"type": "STRING"},
    },
    "required": ["answer", "task_complete"],
}


async def call_gemini_api(prompt: str, task_plan: str | None) -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY가 설정되지 않았습니다")

    contents_text = _CHAIN_SYSTEM_INSTRUCTION.format(task_plan=task_plan or "(없음)", prompt=prompt)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": contents_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _CHAIN_RESPONSE_SCHEMA,
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()


def _extract_chain_result(gemini_result: dict) -> dict:
    """Gemini의 구조화된 JSON 응답(answer/task_complete/next_prompt)을 꺼낸다."""
    text = gemini_result["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    return {
        "answer": parsed.get("answer", ""),
        "task_complete": bool(parsed.get("task_complete", True)),
        "next_prompt": parsed.get("next_prompt") or None,
    }


# ---------------------------------------------------------------------------
# 메인 라우트: 결제(x402 미들웨어, 이미 통과) -> 승인(정책 엔진) -> 실행(Gemini)
# ---------------------------------------------------------------------------
@app.post("/api/gemini")
async def call_gemini(payload: GeminiRequestIn, request: Request) -> dict:
    decision = await call_policy_engine(request, payload.prompt, payload.task_plan)

    if not decision.get("approved"):
        raise HTTPException(
            status_code=403,
            detail=decision.get("reason", "정책 엔진이 이 결제를 거부했습니다"),
        )

    gemini_result = await call_gemini_api(payload.prompt, payload.task_plan)
    chain_result = _extract_chain_result(gemini_result)

    return {
        "approved": True,
        "policy_decision": decision,
        "answer": chain_result["answer"],
        "task_complete": chain_result["task_complete"],
        "next_prompt": chain_result["next_prompt"],
        "gemini_response": gemini_result,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
