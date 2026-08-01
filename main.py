"""Gemini x402 결제 서버.

흐름 (project.md 3~10단계):
  1. AI 친구가 POST /api/gemini 호출
  2. x402 미들웨어가 결제 없으면 402로 가격 정보 응답, 결제 있으면 검증 후 통과
  3. call_policy_engine()이 방금 검증된 결제 트랜잭션에서 실제 지갑 주소를 뽑아
     Solana Devnet RPC로 온체인 USDC 잔액을 조회하고, AI 친구의 /evaluate로 재검증 요청
  4. 승인되면 결제 정산(settle)이 이루어지고, 진짜 Gemini API를 호출해 결과를 돌려줌

/execute는 로봇 친구(프론트엔드) 데모용 어댑터다. DEMO_MODE에서는 x402 결제 미들웨어를
거치지 않고 정책 판단만 실제로 수행한 뒤 Gemini를 호출한다 (USDC는 실제로 이동하지 않음).
실제 온체인 결제까지 검증된 경로는 /api/gemini + demo_chain.py 참고.
"""

import json
import logging
import os
import uuid

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() in {"1", "true", "yes", "on"}
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

if not SOLANA_WALLET_ADDRESS:
    raise RuntimeError("SOLANA_WALLET_ADDRESS 환경변수가 필요합니다 (.env 확인)")
if not POLICY_ENGINE_URL:
    raise RuntimeError("POLICY_ENGINE_URL 환경변수가 필요합니다 (AI 친구 /evaluate 서버 주소)")

SVM_NETWORK: Network = SOLANA_DEVNET_CAIP2  # "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# ---------------------------------------------------------------------------
# FastAPI 앱 + x402 결제 미들웨어
# ---------------------------------------------------------------------------
app = FastAPI(title="Gemini x402 결제 서버")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

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


class DemoWalletIn(BaseModel):
    connected: bool = False
    public_key: str = ""
    balance: float | None = None


class ExecuteRequestIn(BaseModel):
    prompt: str
    request_id: str | None = None
    task_plan: str | None = None
    wallet: DemoWalletIn = Field(default_factory=DemoWalletIn)
    # 프론트에서 Phantom으로 실제 서명해서 제출한 devnet USDC 결제 트랜잭션 서명.
    # 채워져 있으면 온체인에서 직접 검증하고, 없으면 기존 데모(미결제) 경로로 처리한다.
    transaction_signature: str | None = None


# AI 소비자별 연속 실패 횟수 (BACKEND_연동_가이드.md의 ai_consecutive_failures).
# 단일 프로세스 데모 서버라 프로세스 메모리에 카운터를 둔다.
_consecutive_failures = 0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "demo_mode": str(DEMO_MODE).lower()}


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


@app.get("/wallet/balance")
async def wallet_balance(address: str) -> dict:
    """Return a wallet's Solana Devnet USDC balance for the frontend."""
    if not address.strip():
        raise HTTPException(status_code=400, detail="Wallet address is required.")
    try:
        balance = await get_onchain_usdc_balance(address.strip())
    except Exception as exc:
        logger.exception("Failed to query the Solana Devnet USDC balance")
        raise HTTPException(status_code=502, detail="Solana balance lookup failed.") from exc
    return {"address": address.strip(), "usdc_balance": balance, "network": "solana-devnet"}


@app.get("/config")
async def get_config() -> dict:
    """프론트엔드가 결제 트랜잭션을 직접 구성할 때 필요한 값들.
    비밀값이 아니라 전부 공개 정보(수신 주소, 가격, 민트 주소)다."""
    return {
        "recipient_address": SOLANA_WALLET_ADDRESS,
        "price_usd": GEMINI_PRICE_USD,
        "usdc_mint": USDC_DEVNET_ADDRESS,
        "decimals": 6,
        "rpc_url": SOLANA_RPC_URL,
        "network": "solana-devnet",
    }


# ---------------------------------------------------------------------------
# /execute(데모 프론트) 전용: 브라우저가 보낸 실제 결제 트랜잭션 서명을
# 온체인에서 직접 검증한다. x402 프로토콜(파실리테이터 검증/정산)을 그대로
# 타지는 않지만, 실제 devnet USDC가 우리 수신 지갑으로 이동했는지는 진짜로 확인한다.
# ---------------------------------------------------------------------------
_used_payment_signatures: set[str] = set()
_recipient_ata_cache: str | None = None


async def get_associated_token_account(owner_address: str) -> str | None:
    """지정한 지갑이 보유한 USDC(devnet) 계좌(ATA) 주소를 온체인에서 조회한다."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner_address,
            {"mint": USDC_DEVNET_ADDRESS},
            {"encoding": "jsonParsed"},
        ],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(SOLANA_RPC_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    accounts = data.get("result", {}).get("value", [])
    return accounts[0]["pubkey"] if accounts else None


async def verify_onchain_usdc_payment(signature: str, expected_amount_usd: float) -> str | None:
    """서명이 실제로 우리 수신 지갑의 USDC 계좌로 기대 금액 이상을 이체했는지
    Solana Devnet RPC(getTransaction)로 직접 검증한다.

    성공하면 실제 결제를 보낸 지갑(authority) 주소를 돌려주고, 실패하면 None.
    같은 서명을 여러 번 재사용해 결제 없이 통과하는 걸(리플레이) 막기 위해
    한 번 검증에 성공한 서명은 프로세스 메모리에 기록해 재사용을 거부한다.
    """
    if not signature or signature in _used_payment_signatures:
        return None

    global _recipient_ata_cache
    if _recipient_ata_cache is None:
        _recipient_ata_cache = await get_associated_token_account(SOLANA_WALLET_ADDRESS)
    if _recipient_ata_cache is None:
        logger.error("수신 지갑의 USDC 계좌(ATA)를 찾을 수 없음")
        return None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(SOLANA_RPC_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("트랜잭션 조회 실패 (Solana RPC)")
        return None

    result = data.get("result")
    if not result or result.get("meta", {}).get("err") is not None:
        return None

    expected_raw_amount = round(expected_amount_usd * 1_000_000)  # USDC는 소수점 6자리

    instructions = list(result["transaction"]["message"].get("instructions", []))
    for inner in result.get("meta", {}).get("innerInstructions", []) or []:
        instructions.extend(inner.get("instructions", []))

    for ix in instructions:
        parsed = ix.get("parsed")
        if not parsed or ix.get("program") != "spl-token" or parsed.get("type") != "transferChecked":
            continue
        info = parsed.get("info", {})
        if info.get("mint") != USDC_DEVNET_ADDRESS:
            continue
        if info.get("destination") != _recipient_ata_cache:
            continue
        raw_amount = int(info.get("tokenAmount", {}).get("amount", "0"))
        if raw_amount < expected_raw_amount:
            continue
        _used_payment_signatures.add(signature)
        return info.get("authority")

    return None


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
@app.post("/execute")
async def execute_demo(payload: ExecuteRequestIn) -> dict:
    """Frontend orchestration adapter.

    payload.transaction_signature가 있으면 브라우저(Phantom)가 실제로 서명해서
    제출한 devnet USDC 결제를 온체인에서 직접 검증하고, 그 결과(진짜 지갑 주소·
    진짜 잔액)로 정책 판단을 돌린다. 서명이 없으면 기존 데모(미결제) 경로로 처리한다.
    """
    global _consecutive_failures

    if not DEMO_MODE:
        raise HTTPException(status_code=503, detail="The demo payment endpoint is disabled.")

    request_id = payload.request_id or str(uuid.uuid4())
    real_payment = False

    if payload.transaction_signature:
        payer_address = await verify_onchain_usdc_payment(payload.transaction_signature, GEMINI_PRICE_USD)
        if payer_address is None:
            _consecutive_failures += 1
            return {
                "approved": False,
                "status": "rejected",
                "reason": "온체인 결제 검증에 실패했습니다. 트랜잭션이 아직 확정되지 않았거나, "
                          "결제 금액·수신 주소가 일치하지 않거나, 이미 사용된 서명입니다.",
                "rejected_stage": "payment",
                "request_id": request_id,
                "category": "gemini",
                "amount": GEMINI_PRICE_USD,
                "policy_check": [],
                "completed_steps": ["request_analysis", "price_check"],
                "demo_mode": False,
                "payment_status": "verification_failed",
            }
        real_payment = True
        wallet_connected = True
        try:
            wallet_balance_value = await get_onchain_usdc_balance(payer_address)
        except Exception:
            logger.exception("결제 검증 후 잔액 조회 실패")
            wallet_balance_value = 0.0
    else:
        wallet_connected = payload.wallet.connected
        wallet_balance_value = payload.wallet.balance
        if wallet_balance_value is None and payload.wallet.public_key:
            try:
                wallet_balance_value = await get_onchain_usdc_balance(payload.wallet.public_key)
            except Exception:
                logger.exception("Demo wallet balance lookup failed; policy will fail safely")
                wallet_balance_value = 0.0

    policy_payload = {
        "amount": GEMINI_PRICE_USD,
        "category": "gemini",
        "wallet_connected": wallet_connected,
        "wallet_balance": wallet_balance_value or 0.0,
        "api_key_valid": bool(GEMINI_API_KEY),
        "recipient_address": SOLANA_WALLET_ADDRESS,
        "request_id": request_id,
        "ai_consecutive_failures": _consecutive_failures,
        "has_required_permission": True,
        "infra_stable": True,
        "user_prompt": payload.prompt,
        "task_plan": payload.task_plan or payload.prompt,
    }

    try:
        url = f"{POLICY_ENGINE_URL.rstrip('/')}/evaluate"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=policy_payload)
            response.raise_for_status()
            decision = response.json()
    except Exception as exc:
        _consecutive_failures += 1
        logger.exception("Demo policy evaluation failed")
        raise HTTPException(status_code=502, detail="Policy server is unavailable.") from exc

    if not decision.get("approved"):
        _consecutive_failures += 1
        reason = decision.get("reason", "The policy engine rejected this request.")
        if real_payment:
            # 결제는 이미 온체인에서 확정된 뒤라 되돌릴 수 없다. 정책 판단은 이 결제를
            # "쓸 자격이 있는 요청인지" 사후 검증하는 것이라, 여기서 거부되면 실제로
            # 돈은 나갔는데 서비스는 제공되지 않는 상황이 된다 (알려진 한계, README 참고).
            reason = f"{reason} (결제는 이미 완료되었으나 서비스 제공은 거부되었습니다)"
        return {
            "approved": False,
            "status": "rejected",
            "reason": reason,
            "rejected_stage": "policy_check",
            "request_id": request_id,
            "category": "gemini",
            "amount": GEMINI_PRICE_USD,
            "policy_check": decision.get("policy_check", []),
            "completed_steps": ["request_analysis", "price_check", "payment"] if real_payment else ["request_analysis", "price_check"],
            "demo_mode": not real_payment,
            "payment_status": "charged_but_rejected" if real_payment else "not_charged_demo",
            "transaction_signature": payload.transaction_signature or "",
        }

    try:
        gemini_result = await call_gemini_api(payload.prompt, payload.task_plan or payload.prompt)
        chain_result = _extract_chain_result(gemini_result)
    except HTTPException:
        _consecutive_failures += 1
        raise
    except Exception as exc:
        _consecutive_failures += 1
        logger.exception("Gemini execution failed")
        raise HTTPException(status_code=502, detail="Gemini execution failed.") from exc

    _consecutive_failures = 0
    return {
        "approved": True,
        "status": "completed",
        "answer": chain_result["answer"],
        "task_complete": chain_result["task_complete"],
        "next_prompt": chain_result["next_prompt"],
        "request_id": request_id,
        "category": "gemini",
        "amount": GEMINI_PRICE_USD,
        "policy_check": decision.get("policy_check", []),
        "payment_status": "confirmed" if real_payment else "demo_not_charged",
        "transaction_signature": payload.transaction_signature or "",
        "completed_steps": [
            "request_analysis",
            "price_check",
            "policy_check",
            "payment",
            "onchain_confirmation",
            "api_execution",
            "result_delivery",
        ],
        "demo_mode": not real_payment,
        "demo_notice": None if real_payment else "Demo mode: policy was evaluated, but no USDC payment was signed or transferred.",
    }

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
