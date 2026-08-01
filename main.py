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
from solders.keypair import Keypair

from x402.client import x402Client
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption, RouteConfig
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.mechanisms.svm.constants import SOLANA_DEVNET_CAIP2, USDC_DEVNET_ADDRESS
from x402.mechanisms.svm.exact import ExactSvmServerScheme, register_exact_svm_client
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
# "Agent Wallet" 결제 방식(프론트 settings의 'agent' 옵션)이 자율로 서명할 전용 지갑.
# 사용자의 Phantom 지갑과 별개로, 이 서버 프로세스가 개인키를 직접 들고 있는 devnet 전용
# 지갑이다 — 그래서 결제할 때 사람이 승인 팝업을 누를 필요가 없다(demo_chain.py와 동일한 방식).
AGENT_WALLET_PATH = os.getenv("AGENT_WALLET_PATH", "agent_wallet.json")
SELF_BASE_URL = os.getenv("SELF_BASE_URL", "http://127.0.0.1:3000")

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
    # 체인의 첫 호출이면 둘 다 비워서 보낸다 (Gemini가 목표를 분석해 계획을 새로 만듦).
    # 두 번째 호출부터는 직전 응답의 plan_steps/plan_step_status를 그대로 echo해서 보내야
    # 계획이 잠긴 채로 이어진다.
    plan_steps: list[str] | None = None
    plan_step_status: list[bool] | None = None


class DemoWalletIn(BaseModel):
    connected: bool = False
    public_key: str = ""
    balance: float | None = None


class ExecuteRequestIn(BaseModel):
    prompt: str
    request_id: str | None = None
    plan_steps: list[str] | None = None
    plan_step_status: list[bool] | None = None
    wallet: DemoWalletIn = Field(default_factory=DemoWalletIn)
    # 프론트에서 Phantom으로 실제 서명해서 제출한 devnet USDC 결제 트랜잭션 서명.
    # 채워져 있으면 온체인에서 직접 검증하고, 없으면 기존 데모(미결제) 경로로 처리한다.
    transaction_signature: str | None = None
    # true면 Phantom 서명 팝업 없이, 서버가 들고 있는 Agent Wallet이 정책 통과 시
    # 스스로 서명·결제한다 (진짜 자율 결제 경로. 아래 "Agent Wallet 자율 결제" 참고).
    use_agent_wallet: bool = False


# AI 소비자별 연속 실패 횟수 (BACKEND_연동_가이드.md의 ai_consecutive_failures).
# 단일 프로세스 데모 서버라 프로세스 메모리에 카운터를 둔다.
_consecutive_failures = 0


# ---------------------------------------------------------------------------
# Agent Wallet 자율 결제 — Phantom(사람 승인 팝업) 없이 서버가 직접 서명한다.
#
# Phantom 같은 브라우저 지갑 확장은 보안모델 자체가 "웹사이트가 몰래 서명 못 하게"
# 사람의 클릭을 강제한다 — 그래서 Phantom 결제 방식으론 원천적으로 완전 자율 결제를
# 보여줄 수 없다. 이 서버가 자기 전용 devnet 지갑(개인키를 직접 들고 있음)으로 서명하면
# 그 제약이 없어진다: demo_chain.py가 별도 Python 프로세스로 증명했던 것과 동일한 방식을,
# 웹 UI에서 "Agent Wallet" 결제 방식을 고르면 이 서버가 대신 수행한다.
#
# 실제 x402 핸드셰이크(402 -> 서명 -> 재요청 -> 정산)를 처음부터 새로 구현하지 않고,
# 이미 검증된 /api/gemini 라우트를 서버 자신이 x402 클라이언트로 호출하는 방식으로
# 재사용한다 (핸드셰이크 로직 중복 없음).
# ---------------------------------------------------------------------------
class _LocalKeypairSigner:
    """x402의 ClientSvmSigner 프로토콜을 로컬 Keypair로 구현한 것 (demo_chain.py와 동일)."""

    def __init__(self, kp: Keypair):
        self._kp = kp

    @property
    def address(self) -> str:
        return str(self._kp.pubkey())

    @property
    def keypair(self) -> Keypair:
        return self._kp

    def sign_transaction(self, tx):
        tx.sign([self._kp])
        return tx


_agent_keypair: Keypair | None = None


def _get_agent_keypair() -> Keypair:
    """Agent Wallet 개인키를 로드한다. 없으면 새로 만들어서 로컬 파일에 저장한다.

    이 파일은 .gitignore에 포함되어 있어 커밋되지 않는다. Devnet 전용이라 실제
    자산 가치는 없지만, 이 서버 프로세스만 접근 가능한 로컬 파일에만 존재하고
    프론트엔드나 API 응답으로는 절대 노출하지 않는다(주소만 노출한다).
    """
    global _agent_keypair
    if _agent_keypair is not None:
        return _agent_keypair

    if os.path.exists(AGENT_WALLET_PATH):
        with open(AGENT_WALLET_PATH) as f:
            _agent_keypair = Keypair.from_bytes(bytes(json.load(f)))
    else:
        _agent_keypair = Keypair()
        with open(AGENT_WALLET_PATH, "w") as f:
            json.dump(list(bytes(_agent_keypair)), f)
        logger.info("새 Agent Wallet 생성됨: %s (Devnet SOL/USDC 충전 필요)", _agent_keypair.pubkey())

    return _agent_keypair


async def call_gemini_via_agent_wallet(
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> dict:
    """Agent Wallet이 사람 승인 없이 스스로 서명해서 /api/gemini를 호출한다.

    반환값의 "data"는 /api/gemini의 성공 응답 그대로이고, "transaction_signature"는
    실제로 정산된 온체인 트랜잭션 서명이다(결제가 거부/실패해 정산이 안 되면 빈 문자열).
    """
    keypair = _get_agent_keypair()
    client = x402Client()
    register_exact_svm_client(client, _LocalKeypairSigner(keypair))

    settled: dict = {}

    def _capture_settlement(ctx):
        if ctx.settle_response is not None:
            settled["transaction"] = ctx.settle_response.transaction
        return None

    client.on_payment_response(_capture_settlement)

    async with wrapHttpxWithPayment(client, timeout=90.0) as http:
        resp = await http.post(
            f"{SELF_BASE_URL}/api/gemini",
            json={
                "prompt": prompt,
                "plan_steps": plan_steps,
                "plan_step_status": plan_step_status,
            },
        )

    return {
        "response": resp,
        "transaction_signature": settled.get("transaction", ""),
        "payer_address": str(keypair.pubkey()),
    }


async def _execute_via_agent_wallet(payload: ExecuteRequestIn, request_id: str) -> dict:
    """/execute를 Agent Wallet 경로로 처리한다 — Phantom 팝업 없이 서버가 스스로 결제한다."""
    global _consecutive_failures

    try:
        result = await call_gemini_via_agent_wallet(payload.prompt, payload.plan_steps, payload.plan_step_status)
    except Exception as exc:
        _consecutive_failures += 1
        logger.exception("Agent Wallet 자율 결제 실패")
        raise HTTPException(status_code=502, detail="Agent Wallet 결제 처리 중 오류가 발생했습니다.") from exc

    resp = result["response"]
    signature = result["transaction_signature"]

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        _consecutive_failures += 1
        reason = data.get("detail") or data.get("reason") or f"HTTP {resp.status_code}"
        return {
            "approved": False,
            "status": "rejected",
            "reason": reason,
            "rejected_stage": "policy_check",
            "request_id": request_id,
            "category": "gemini",
            "amount": GEMINI_PRICE_USD,
            "policy_check": (data.get("policy_decision") or {}).get("policy_check", []),
            "completed_steps": ["request_analysis", "price_check", "policy_check"],
            "demo_mode": False,
            "payment_status": "not_charged_rejected",
            "transaction_signature": signature,
        }

    _consecutive_failures = 0

    return {
        "approved": True,
        "status": "completed",
        "answer": data.get("answer", ""),
        "plan_steps": data.get("plan_steps"),
        "plan_step_status": data.get("plan_step_status"),
        "task_complete": data.get("task_complete"),
        "next_prompt": data.get("next_prompt"),
        "request_id": request_id,
        "category": "gemini",
        "amount": GEMINI_PRICE_USD,
        "policy_check": (data.get("policy_decision") or {}).get("policy_check", []),
        "payment_status": "confirmed",
        "transaction_signature": signature,
        "completed_steps": [
            "request_analysis",
            "price_check",
            "policy_check",
            "payment",
            "onchain_confirmation",
            "api_execution",
            "result_delivery",
        ],
        "demo_mode": False,
        "demo_notice": None,
    }


@app.get("/agent-wallet/info")
async def agent_wallet_info() -> dict:
    """프론트 설정 화면이 Agent Wallet 주소·잔액을 보여줄 때 쓴다 (개인키는 절대 노출 안 함)."""
    keypair = _get_agent_keypair()
    address = str(keypair.pubkey())
    try:
        balance = await get_onchain_usdc_balance(address)
    except Exception:
        logger.exception("Agent Wallet 잔액 조회 실패")
        balance = None
    return {"address": address, "usdc_balance": balance, "network": "solana-devnet"}


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
def _plan_steps_to_text(plan_steps: list[str] | None) -> str:
    """레이어2(semantic_layer.py)는 task_plan을 사람이 읽는 문자열로 받으므로,
    구조화된 plan_steps를 번호 매긴 문장으로 풀어서 전달한다. 아직 계획이 없으면(첫 호출)
    빈 문자열을 돌려준다."""
    if not plan_steps:
        return ""
    return " / ".join(f"{i + 1}) {step}" for i, step in enumerate(plan_steps))


def _semantic_check_prompt(
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> str:
    """레이어2(의미 판단)에 넘길 user_prompt를 결정한다.

    plan_steps가 없는 첫 호출은 '사용자의 최초 작업 요청' 그 자체다. project.md에 이미
    명시된 원칙대로, 이건 스코프의 정의 기준점이라 자기 자신과 비교하는 셈이라 검사 대상이
    아니다(비교할 확정된 계획이 아직 없기도 하다). 그래서 첫 호출은 빈 문자열을 보내
    semantic_layer.py가 자동으로 건너뛰게 한다.

    두 번째 호출부터는 semantic_layer.py가 기대하는 형태("이 호출이 왜 필요한지"에 대한
    근거 텍스트)로 직접 만들어서 보낸다. next_prompt(=Gemini한테 실제로 시킬 작업 지시문)를
    그대로 user_prompt로 흘려보내면 안 된다 — 그건 "근거"가 아니라 "지시"라서, 레이어2가
    "근거가 불분명함(goal_clear=false)"으로 오판하는 원인이 된다. 대신 고정된 계획에서
    아직 안 끝난 단계가 몇 번째인지 코드가 직접 찾아서 근거 문장으로 조립한다.
    """
    if not plan_steps:
        return ""

    status = plan_step_status or [False] * len(plan_steps)
    next_index = next(
        (i for i, done in enumerate(status) if not done),
        len(plan_steps) - 1,
    )

    return (
        f"고정된 작업 계획의 {next_index + 1}번째 단계('{plan_steps[next_index]}')를 "
        f"수행하기 위해, 이번 요청 '{prompt}'으로 호출함."
    )


async def call_policy_engine(
    request: Request,
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> dict:
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
        "user_prompt": _semantic_check_prompt(prompt, plan_steps, plan_step_status),
        "task_plan": _plan_steps_to_text(plan_steps),
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
#
# 계획(plan_steps)은 딱 한 번(호출자가 아직 plan_steps를 안 보낸 "첫 호출")만 Gemini가
# 사용자의 뭉툭한 목표를 분석해서 만든다. 그 이후 호출부터는 이 목록을 그대로 고정해서
# 다시 프롬프트에 박아 넣고, Gemini는 목록을 다시 쓸 권한 없이 "각 단계가 이번 답변으로
# 충족됐는지"만 판단한다. 완료 여부(all done)는 Gemini의 자기 판단이 아니라 코드가
# plan_step_status를 집계해서 결정한다 — "완료됐는데도 애매하다고 계속 부른다"는 리스크를
# 통짜 판단 대신 항목별 판단 + 코드 집계로 줄이기 위함.
# ---------------------------------------------------------------------------
_FIRST_CALL_INSTRUCTION = """너는 자율 에이전트 체인의 첫 단계를 처리하는 실행기다.

사용자의 목표: "{prompt}"

1. 이 목표를 달성하기 위해 필요한 구체적이고 유한한 단계들로 나눠라(1개~5개 정도,
   목표가 한 번에 끝나면 1개만). 이 목록은 지금 한 번만 정하고 이후 절대 바뀌지 않는다.
2. 목표(또는 그 일부)에 실제로 답하라.
3. 이번 답변으로 위 각 단계가 충족됐는지 하나씩 판단하라.

- answer: 사용자에게 보여줄 실제 답변
- plan_steps: 방금 나눈 단계 목록 (문자열 배열, 순서 유지)
- plan_step_status: plan_steps와 같은 순서·같은 개수의 boolean 배열. 이번 답변이 그
  단계를 충족시켰으면 true, 아니면 false
- next_prompt: 아직 안 끝난 단계가 있다면, 다음 호출에 그대로 쓸 완전한 프롬프트.
  전부 끝났다고 판단되면 빈 문자열로 둬라
"""

_CONTINUATION_INSTRUCTION = """너는 자율 에이전트 체인의 다음 단계를 처리하는 실행기다.

고정된 작업 계획(이 목록은 이미 확정되어 절대 바뀌지 않는다): {plan_steps}
지금까지 단계별 완료 상태: {plan_step_status}
이번 요청: "{prompt}"

이번 요청에 실제로 답하라. 그리고 위 고정된 계획의 각 단계에 대해, 이번 답변으로
새로 충족된 게 있는지만 판단하라 (이미 완료된 단계는 그대로 완료로 유지, 계획
목록 자체를 새로 만들거나 항목을 추가/삭제하려 하지 마라).

- answer: 사용자에게 보여줄 실제 답변
- plan_step_status: plan_steps와 같은 순서·같은 개수의 boolean 배열 (이번 판단 기준)
- next_prompt: 아직 안 끝난 단계가 있다면, 다음 호출에 그대로 쓸 완전한 프롬프트.
  전부 끝났다고 판단되면 빈 문자열로 둬라
"""

_FIRST_CALL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "plan_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "plan_step_status": {"type": "ARRAY", "items": {"type": "BOOLEAN"}},
        "next_prompt": {"type": "STRING"},
    },
    "required": ["answer", "plan_steps", "plan_step_status"],
}

_CONTINUATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answer": {"type": "STRING"},
        "plan_step_status": {"type": "ARRAY", "items": {"type": "BOOLEAN"}},
        "next_prompt": {"type": "STRING"},
    },
    "required": ["answer", "plan_step_status"],
}


def _merge_step_status(previous: list[bool] | None, latest: list[bool]) -> list[bool]:
    """한 번 done=true가 된 단계는 이후 판단에서 false로 되돌아가지 않게 OR로 누적한다."""
    if not previous:
        return latest
    return [bool(p) or bool(n) for p, n in zip(previous, latest)]


async def call_gemini_api(
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY가 설정되지 않았습니다")

    is_first_call = not plan_steps
    if is_first_call:
        contents_text = _FIRST_CALL_INSTRUCTION.format(prompt=prompt)
        schema = _FIRST_CALL_SCHEMA
    else:
        contents_text = _CONTINUATION_INSTRUCTION.format(
            plan_steps=json.dumps(plan_steps, ensure_ascii=False),
            plan_step_status=json.dumps(plan_step_status or [False] * len(plan_steps), ensure_ascii=False),
            prompt=prompt,
        )
        schema = _CONTINUATION_SCHEMA

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": contents_text}]}],
        "generationConfig": {
            "temperature": 0,
            # thinking을 꺼도(budget=0) 계획 분해·단계 판단 정확도와 답변 품질이
            # 그대로인 것을 A/B 테스트로 확인함. thinking 토큰이 답변 토큰보다도
            # 많이 나가서(702 vs 498) 실제 비용의 절반 이상을 차지하고 있었음.
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    # 첫 호출은 목표 분해 + 답변 + 단계별 판단을 한 번에 하므로 기존 30초보다 여유를 둔다.
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()


def _extract_chain_result(
    gemini_result: dict,
    prior_plan_steps: list[str] | None,
    prior_plan_step_status: list[bool] | None,
) -> dict:
    """Gemini의 구조화된 JSON 응답을 꺼내고, 계획 잠금 + 완료 여부 집계를 코드가 확정한다."""
    text = gemini_result["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)

    # 계획은 첫 호출에서 온 것만 채택한다. 이후 호출에서 모델이 뭘 보내든(스키마상 안 보내지만
    # 방어적으로) 무시하고 이미 잠긴 prior_plan_steps를 그대로 쓴다 — 재계획 여지를 코드 레벨에서 차단.
    plan_steps = prior_plan_steps or parsed.get("plan_steps") or []
    latest_status = [bool(v) for v in parsed.get("plan_step_status", [])]
    # 길이가 안 맞으면(모델이 스키마를 어겼거나 계획이 비어있으면) 안전하게 전부 미완료로 취급한다.
    if len(latest_status) != len(plan_steps):
        latest_status = [False] * len(plan_steps)
    plan_step_status = _merge_step_status(prior_plan_step_status, latest_status)

    task_complete = bool(plan_step_status) and all(plan_step_status)

    return {
        "answer": parsed.get("answer", ""),
        "plan_steps": plan_steps,
        "plan_step_status": plan_step_status,
        "task_complete": task_complete,
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

    if payload.use_agent_wallet:
        return await _execute_via_agent_wallet(payload, request_id)

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
        "user_prompt": _semantic_check_prompt(payload.prompt, payload.plan_steps, payload.plan_step_status),
        "task_plan": _plan_steps_to_text(payload.plan_steps),
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
        gemini_result = await call_gemini_api(payload.prompt, payload.plan_steps, payload.plan_step_status)
        chain_result = _extract_chain_result(gemini_result, payload.plan_steps, payload.plan_step_status)
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
        "plan_steps": chain_result["plan_steps"],
        "plan_step_status": chain_result["plan_step_status"],
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
    decision = await call_policy_engine(request, payload.prompt, payload.plan_steps, payload.plan_step_status)

    if not decision.get("approved"):
        raise HTTPException(
            status_code=403,
            detail=decision.get("reason", "정책 엔진이 이 결제를 거부했습니다"),
        )

    gemini_result = await call_gemini_api(payload.prompt, payload.plan_steps, payload.plan_step_status)
    chain_result = _extract_chain_result(gemini_result, payload.plan_steps, payload.plan_step_status)

    return {
        "approved": True,
        "policy_decision": decision,
        "answer": chain_result["answer"],
        "plan_steps": chain_result["plan_steps"],
        "plan_step_status": chain_result["plan_step_status"],
        "task_complete": chain_result["task_complete"],
        "next_prompt": chain_result["next_prompt"],
        "gemini_response": gemini_result,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=3000)
