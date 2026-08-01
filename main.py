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

# ---------------------------------------------------------------------------
# import: 이 파일에서 쓸 "도구 상자"들을 가져오는 부분.
# 파이썬 표준 라이브러리(json, logging, os, uuid)부터, 외부에서 설치한
# 패키지(httpx, dotenv, fastapi, pydantic, x402)까지 전부 여기서 미리 불러와야
# 아래 코드에서 이름만으로 바로 쓸 수 있다.
# ---------------------------------------------------------------------------

# json: 파이썬 딕셔너리 <-> JSON 문자열을 서로 변환해주는 표준 라이브러리.
# Gemini 응답 안에 JSON 문자열로 박혀 있는 값을 다시 파이썬 객체로 꺼낼 때(json.loads) 씀.
import json
# logging: print() 대신 쓰는 정식 로그 기록 도구. "언제, 어디서, 무슨 일이 있었는지"를
# 레벨(INFO/ERROR 등)별로 남길 수 있어서 서버 코드에서는 print보다 이걸 표준으로 쓴다.
import logging
# os: 운영체제 관련 기능 모음. 여기서는 .env에 저장된 환경변수(os.getenv)를 읽는 데 씀.
import os
# uuid: 절대 겹치지 않는 고유한 식별자(ID)를 만들어주는 라이브러리.
# 결제 요청마다 이 값을 하나씩 붙여서 "같은 요청 두 번 처리" 같은 사고를 막는다.
import uuid

# httpx: 다른 서버에게 HTTP 요청(GET/POST)을 보낼 때 쓰는 외부 패키지.
# 이 서버 자신도 클라이언트가 되어서 Solana RPC, 정책 엔진, Gemini API 등
# "바깥"에 있는 서버들한테 요청을 보내야 하는데, 그때 전부 httpx를 쓴다.
import httpx
# dotenv: 프로젝트 루트의 .env 파일(비밀번호/설정값 모음)을 읽어서
# 파이썬이 os.getenv()로 꺼내 쓸 수 있게 등록해주는 패키지.
from dotenv import load_dotenv
# FastAPI: 이 서버 전체를 만드는 프레임워크. "이런 주소로 요청 오면 이 함수 실행해라"를
# 쉽게 연결해준다. HTTPException은 "이런 에러 상황이면 이 상태코드로 응답해라"를 만드는 도구,
# Request는 지금 들어온 HTTP 요청 자체(헤더, 상태값 등)에 접근할 때 쓴다.
from fastapi import FastAPI, HTTPException, Request
# CORSMiddleware: 브라우저가 "다른 포트(5500)에서 이 서버(3000)를 불러도 되나요?"라고
# 물어볼 때(CORS, Cross-Origin Resource Sharing) 허용해주는 부품.
from fastapi.middleware.cors import CORSMiddleware
# pydantic: "이 데이터는 반드시 이런 모양(타입)이어야 한다"는 규격(스키마)을 정의하는 패키지.
# BaseModel을 상속하면 FastAPI가 요청 본문을 자동으로 검증하고 파이썬 객체로 변환해준다.
# Field는 그 항목에 기본값을 "함수 호출로" 만들어야 할 때(예: 매번 새 객체) 쓰는 보조 도구.
from pydantic import BaseModel, Field

# 아래 x402.* 들은 전부 "x402 프로토콜"(API 쓰려면 결제부터 하라는 HTTP 402 기반 표준)을
# 파이썬에서 구현해주는 공식 패키지(x402[fastapi,svm])에서 가져오는 부품들이다.
# FacilitatorConfig/HTTPFacilitatorClient: 결제 검증·정산을 대신 처리해주는 외부
#   서비스(facilitator)에 접속하기 위한 설정값과 접속 클라이언트.
# PaymentOption: "이 API는 얼마짜리고, 어떤 지갑으로 받을지"를 정의하는 값.
# RouteConfig: 어떤 주소(라우트)를 결제로 막을지 정의하는 값.
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption, RouteConfig
# PaymentMiddlewareASGI: FastAPI 앱에 "결제 문지기"를 끼워넣는 미들웨어.
# 이걸 붙인 라우트는 결제 증빙 없이 요청하면 자동으로 402 응답을 대신 만들어준다.
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
# SOLANA_DEVNET_CAIP2: "solana:이러이러한긴문자열" 형태로 Solana Devnet을 가리키는 표준 이름.
# USDC_DEVNET_ADDRESS: Devnet에서 쓰는 가짜(테스트용) USDC 토큰의 민트(발행) 주소.
from x402.mechanisms.svm.constants import SOLANA_DEVNET_CAIP2, USDC_DEVNET_ADDRESS
# ExactSvmServerScheme: "정확히 이 금액만큼 SPL 토큰(USDC)을 받는" 결제 방식을
# Solana(SVM) 네트워크용으로 구현해둔 클래스.
from x402.mechanisms.svm.exact import ExactSvmServerScheme
# ExactSvmPayload: 클라이언트가 보낸 결제 증빙(base64로 인코딩된 서명 트랜잭션)을
# 담는 자료구조.
from x402.mechanisms.svm.types import ExactSvmPayload
# decode_transaction_from_payload: base64 문자열을 실제 Solana 트랜잭션 객체로 해독.
# extract_transaction_info: 그 트랜잭션 안에서 "누가 얼마를 누구에게 보냈는지" 뽑아냄.
from x402.mechanisms.svm.utils import decode_transaction_from_payload, extract_transaction_info
# Network: "solana:xxxx" 같은 네트워크 식별자 문자열에 붙이는 타입 이름(타입 힌트용).
from x402.schemas import Network
# x402ResourceServer: 이 서버(우리)가 "결제를 받는 쪽(seller)"임을 나타내는 핵심 객체.
# facilitator랑 결제 방식(scheme)을 등록해서 만든다.
from x402.server import x402ResourceServer

# .env 파일의 내용을 읽어서 os.getenv()로 꺼내 쓸 수 있게 등록한다.
# 이 줄이 없으면 .env에 SOLANA_WALLET_ADDRESS=... 라고 적어놔도 파이썬이 못 읽는다.
load_dotenv()

# 로그를 INFO 레벨 이상(INFO, WARNING, ERROR...)까지 콘솔에 출력하도록 설정.
logging.basicConfig(level=logging.INFO)
# 이 파일 전용 로거(기록기) 하나를 만든다. logger.info(...), logger.exception(...) 형태로 씀.
logger = logging.getLogger("gemini_x402")

# ---------------------------------------------------------------------------
# 환경 변수
# .env 파일(커밋 안 됨, 비밀값 보관용)에서 값을 읽어온다.
# os.getenv("KEY", "기본값")은 KEY가 .env에 없으면 기본값을 쓰겠다는 뜻.
# ---------------------------------------------------------------------------
SOLANA_WALLET_ADDRESS = os.getenv("SOLANA_WALLET_ADDRESS")  # 우리가 결제 받는 지갑 주소
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")                # 진짜 Gemini API 호출용 키
POLICY_ENGINE_URL = os.getenv("POLICY_ENGINE_URL")          # AI 친구의 정책 서버 주소
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
GEMINI_PRICE_USD = float(os.getenv("GEMINI_PRICE_USD", "0.005"))  # 문자열로 읽히므로 float()로 변환
# .lower() in {...}: "true"/"1"/"yes"/"on" 중 아무거나 적어도 참으로 인식하게 하는 흔한 패턴.
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() in {"1", "true", "yes", "on"}
# CORS_ORIGINS="a,b,c" 형태로 넣으면 콤마 기준으로 쪼개서 리스트로 만듦 (기본은 "*" = 전체 허용).
CORS_ORIGINS = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",") if origin.strip()]

# 필수 환경변수가 비어있으면, 서버를 아예 시작도 하지 말고 바로 에러를 내며 죽게 한다.
# (나중에 요청 들어왔을 때 조용히 이상하게 동작하는 것보다, 시작 시점에 바로 알아채는 게 안전함)
if not SOLANA_WALLET_ADDRESS:
    raise RuntimeError("SOLANA_WALLET_ADDRESS 환경변수가 필요합니다 (.env 확인)")
if not POLICY_ENGINE_URL:
    raise RuntimeError("POLICY_ENGINE_URL 환경변수가 필요합니다 (AI 친구 /evaluate 서버 주소)")

# 우리가 결제를 받을 네트워크를 Solana Devnet으로 고정.
SVM_NETWORK: Network = SOLANA_DEVNET_CAIP2  # "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# ---------------------------------------------------------------------------
# FastAPI 앱 + x402 결제 미들웨어
# ---------------------------------------------------------------------------
# FastAPI()로 앱(서버) 객체를 하나 만든다. 앞으로 이 app에 라우트(주소)와
# 미들웨어(공통 처리 로직)를 계속 붙여나간다. title은 /docs 자동문서에 표시될 이름.
app = FastAPI(title="Gemini x402 결제 서버")
# 미들웨어: 모든 요청/응답이 실제 라우트 함수에 닿기 전/후에 공통으로 거치는 관문.
# 여기서는 "다른 포트(프론트엔드)에서 온 요청도 허용해줘라"는 CORS 미들웨어를 붙임.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,        # 허용할 출처 목록 (기본 "*" = 전부 허용)
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# facilitator: 결제 트랜잭션이 진짜인지 검증하고, 최종적으로 블록체인에 확정(정산)까지
# 대신 처리해주는 외부 서비스에 접속하는 클라이언트.
facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
# resource_server: "나(이 서버)는 결제를 받는 쪽이다"를 나타내는 객체를 만들고,
# 어떤 네트워크(Devnet)에서 어떤 결제 방식(Exact, 정확한 금액)을 쓸지 등록한다.
resource_server = x402ResourceServer(facilitator)
resource_server.register(SVM_NETWORK, ExactSvmServerScheme())

# routes: "이 주소로 오는 요청은 결제가 필요하다"를 정의하는 딕셔너리.
# 키는 "메서드 경로" 형식("POST /api/gemini"), 값은 그 결제 조건(가격/받는지갑/네트워크).
routes = {
    "POST /api/gemini": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",                      # "정확히 이 금액만" 받는 방식
                pay_to=SOLANA_WALLET_ADDRESS,         # 돈 받을 지갑
                price=f"${GEMINI_PRICE_USD}",         # 가격 (문자열로 "$0.005" 형태)
                network=SVM_NETWORK,                  # Solana Devnet
            ),
        ],
        mime_type="application/json",
        description="Gemini API 호출 (x402 결제 필요)",
    ),
}
# 위에서 정의한 routes를 실제로 앱에 미들웨어로 장착.
# 이제 POST /api/gemini로 결제 증빙 없이 요청이 오면, 이 라인 덕분에 자동으로
# 402(Payment Required) 응답이 나가고, 우리가 만든 핸들러 함수는 아예 실행되지 않는다.
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=resource_server)


# ---------------------------------------------------------------------------
# 요청/응답 데이터 모양 정의 (pydantic 모델)
# class 이름(BaseModel): 필드이름: 타입 = 기본값  -> 이렇게 적으면
# FastAPI가 요청 JSON을 자동으로 이 모양에 맞춰 검증·변환해준다.
# 타입이 안 맞으면 우리 코드가 실행되기도 전에 FastAPI가 알아서 에러 응답을 준다.
# ---------------------------------------------------------------------------
class GeminiRequestIn(BaseModel):
    prompt: str                      # 필수값 (기본값 없음 = 반드시 있어야 함)
    task_plan: str | None = None     # str이거나 None, 없으면 기본값 None


class DemoWalletIn(BaseModel):
    connected: bool = False
    public_key: str = ""
    balance: float | None = None


class ExecuteRequestIn(BaseModel):
    prompt: str
    request_id: str | None = None
    task_plan: str | None = None
    # Field(default_factory=DemoWalletIn): 기본값을 "DemoWalletIn()을 새로 호출한 결과"로
    # 만들라는 뜻. 그냥 "= DemoWalletIn()"이라고 안 쓰는 이유는, 파이썬은 기본값을 딱 한 번만
    # 만들어서 모든 요청이 그 객체를 공유해버리는 함정이 있어서, 매번 새로 만들게 하는 것.
    wallet: DemoWalletIn = Field(default_factory=DemoWalletIn)
    # 프론트에서 Phantom으로 실제 서명해서 제출한 devnet USDC 결제 트랜잭션 서명.
    # 채워져 있으면 온체인에서 직접 검증하고, 없으면 기존 데모(미결제) 경로로 처리한다.
    transaction_signature: str | None = None


# AI 소비자별 연속 실패 횟수 (BACKEND_연동_가이드.md의 ai_consecutive_failures).
# 단일 프로세스 데모 서버라 프로세스 메모리에 카운터를 둔다.
# (참고로 이 변수는 "전역 변수"라서, 함수 안에서 값을 바꾸려면 global 키워드가 필요하다 —
#  아래 call_policy_engine(), execute_demo() 함수에서 그렇게 쓰고 있다.)
_consecutive_failures = 0


# @app.get("/health"): "GET /health 요청이 오면 바로 아래 함수를 실행해라"는 표시(데코레이터).
# async def: 이 함수는 "비동기" 함수다. 네트워크 요청처럼 기다리는(await) 작업이 있을 때
# 그 기다리는 동안 서버가 다른 요청도 같이 처리할 수 있게 해주는 파이썬 문법.
# 함수 안에서 뭔가를 await 하려면, 그 함수 자체가 async여야 한다.
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "demo_mode": str(DEMO_MODE).lower()}


# ---------------------------------------------------------------------------
# 온체인 USDC 잔액 조회
# ---------------------------------------------------------------------------
async def get_onchain_usdc_balance(wallet_address: str) -> float:
    """Solana Devnet RPC(getTokenAccountsByOwner)로 지갑의 실제 USDC 잔액을 조회한다."""
    # Solana RPC는 "JSON-RPC"라는 표준 형식으로 요청을 받는다.
    # method가 부를 함수 이름, params가 그 함수에 넘길 인자들.
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            wallet_address,
            {"mint": USDC_DEVNET_ADDRESS},   # 이 지갑이 갖고 있는 계좌들 중, USDC 민트인 것만
            {"encoding": "jsonParsed"},       # 결과를 사람이 읽기 쉬운 JSON으로 달라
        ],
    }
    # httpx.AsyncClient: 비동기로 HTTP 요청을 보내는 클라이언트.
    # "with ... as client:" (async with)는 요청 다 끝나면 연결을 자동으로 정리해주는 문법.
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(SOLANA_RPC_URL, json=payload)
        # raise_for_status(): 응답이 200번대(성공)가 아니면 여기서 바로 예외를 던짐.
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Solana RPC 오류: {data['error']}")

    # .get("result", {}).get("value", [])처럼 연쇄로 .get()을 쓰는 이유:
    # 딕셔너리에 그 키가 없어도 에러 없이 빈 값(기본값)을 돌려주기 때문에 안전하다.
    accounts = data.get("result", {}).get("value", [])
    total = 0.0
    for acc in accounts:
        try:
            token_amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(token_amount["uiAmount"] or 0.0)
        except (KeyError, TypeError):
            # 혹시 응답 구조가 예상과 다른 계좌가 섞여 있어도, 그것 때문에 전체가
            # 죽지 않고 그 항목만 건너뛰도록 방어.
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
        # logger.exception(...): 에러 메시지 + 어디서 터졌는지(스택 트레이스)까지 자동으로 로그에 남김.
        logger.exception("Failed to query the Solana Devnet USDC balance")
        # "raise ... from exc": 새 예외를 던지되, 원래 예외(exc)도 원인으로 같이 남겨서
        # 나중에 로그 볼 때 "진짜 원인이 뭐였는지" 추적하기 쉽게 해준다.
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
# 모듈(파일) 최상단에 만든 변수라서, 서버가 켜져 있는 동안 계속 유지되는
# "메모리 위의 저장소" 역할을 한다 (재시작하면 당연히 초기화됨).
_used_payment_signatures: set[str] = set()   # 이미 검증에 성공한 서명들의 집합(set) — 재사용 방지용
_recipient_ata_cache: str | None = None       # 한 번 조회한 값은 캐시해서 매번 다시 안 물어보게 함


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
    # 파이썬의 "A if 조건 else B" 문법 (삼항 표현식): accounts가 있으면 첫 번째 것의
    # pubkey를, 없으면 None을 돌려준다.
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

    # global: 이 함수 안에서 _recipient_ata_cache를 새로 만드는 게 아니라, 함수 바깥
    # (모듈 최상단)에 있는 그 변수를 그대로 가리켜서 수정하겠다는 선언. 이게 없으면
    # 파이썬은 "이건 함수 안에서만 쓰는 새 지역변수구나"라고 오해한다.
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
    # result가 아예 없거나(트랜잭션 못 찾음), 트랜잭션은 있는데 실패(err 있음)로 끝났으면 거부.
    if not result or result.get("meta", {}).get("err") is not None:
        return None

    # USDC는 소수점 6자리(decimals=6)라서, "0.005 USDC"를 정수 단위로 바꾸면 5000이 된다.
    expected_raw_amount = round(expected_amount_usd * 1_000_000)  # USDC는 소수점 6자리

    # 트랜잭션 안의 최상위 명령어들 + "내부 명령어"(다른 명령어 실행 중에 파생된 것들)를
    # 하나의 리스트로 합쳐서 전부 검사한다. (list(...)로 복사해서 원본 리스트는 안 건드림)
    instructions = list(result["transaction"]["message"].get("instructions", []))
    for inner in result.get("meta", {}).get("innerInstructions", []) or []:
        instructions.extend(inner.get("instructions", []))

    for ix in instructions:
        parsed = ix.get("parsed")
        # SPL 토큰 프로그램이 보낸 "transferChecked"(금액+토큰종류까지 같이 확인하는 전송)
        # 명령어가 아니면 건너뜀.
        if not parsed or ix.get("program") != "spl-token" or parsed.get("type") != "transferChecked":
            continue
        info = parsed.get("info", {})
        if info.get("mint") != USDC_DEVNET_ADDRESS:      # 우리가 아는 devnet USDC가 맞는지
            continue
        if info.get("destination") != _recipient_ata_cache:  # 우리 수신 계좌로 온 게 맞는지
            continue
        raw_amount = int(info.get("tokenAmount", {}).get("amount", "0"))
        if raw_amount < expected_raw_amount:              # 가격만큼 충분히 보냈는지
            continue
        # 여기까지 다 통과하면 진짜 결제로 인정. 이 서명을 "사용됨"으로 기록해서
        # 똑같은 서명으로 다시 요청해도 이 함수가 다시 통과시켜주지 않게 막는다.
        _used_payment_signatures.add(signature)
        return info.get("authority")  # 실제로 서명해서 보낸 지갑 주소

    return None


def extract_payer_wallet(request: Request) -> str | None:
    """x402 미들웨어가 검증을 마친 뒤 request.state에 실어둔 결제 트랜잭션에서
    실제로 서명해서 USDC를 보낸 지갑(SPL 토큰 payer) 주소를 꺼낸다.

    이 값이 채워져 있다는 것 자체가 '결제 트랜잭션에 서명 가능한 지갑이 연결되어
    있었다'는 뜻이므로 wallet_connected 판단 근거로 쓴다.
    """
    # request.state: x402 미들웨어가 검증에 성공했을 때 "이번 요청에 딸린 정보"를
    # 몰래 붙여두는 자리. getattr(객체, "이름", 기본값)은 그 속성이 없어도 에러 없이
    # 기본값을 돌려주는, .get()의 객체 버전이라고 보면 된다.
    payment_payload = getattr(request.state, "payment_payload", None)
    if payment_payload is None:
        return None
    try:
        svm_payload = ExactSvmPayload.from_dict(payment_payload.payload)
        tx = decode_transaction_from_payload(svm_payload)
        tx_info = extract_transaction_info(tx)
        return tx_info.payer if tx_info else None
    except Exception:
        # 여기서 실패해도 서버 전체가 죽으면 안 되니, 로그만 남기고 None을 돌려줘서
        # 호출한 쪽이 "지갑 정보 못 뽑았다"로 자연스럽게 처리하게 한다.
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

    # 정책 엔진이 판단하는 데 필요한 "사실값들"을 하나의 딕셔너리로 모음.
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

    # .rstrip('/'): 문자열 끝에 붙은 '/'를 전부 제거. POLICY_ENGINE_URL이
    # "http://localhost:8000/"이든 "http://localhost:8000"이든 결과가 같아지게 하는 방어.
    url = f"{POLICY_ENGINE_URL.rstrip('/')}/evaluate"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            decision = resp.json()
    except Exception:
        logger.exception("정책 엔진(/evaluate) 호출 실패")
        _consecutive_failures += 1
        raise  # 그냥 raise만 쓰면 "방금 잡은 그 예외를 그대로 다시 던져라"는 뜻

    if not decision.get("approved"):
        _consecutive_failures += 1
    else:
        _consecutive_failures = 0  # 성공했으니 연속 실패 카운트 초기화

    return decision


# ---------------------------------------------------------------------------
# 실제 Gemini API 호출
# ---------------------------------------------------------------------------
# """...""" (삼중따옴표)로 여러 줄 문자열을 만들고, {task_plan}/{prompt} 자리는
# 나중에 .format(task_plan=..., prompt=...)으로 실제 값을 채워넣을 자리표시자(placeholder)다.
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

# Gemini한테 "반드시 이 모양의 JSON으로만 답해라"고 강제하는 스키마.
# 이렇게 하면 Gemini가 이상한 형식으로 답해서 파싱이 깨지는 일을 막을 수 있다.
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

    # task_plan이 None이거나 빈 문자열이면 "(없음)"을 대신 넣는다 (or의 흔한 활용법).
    contents_text = _CHAIN_SYSTEM_INSTRUCTION.format(task_plan=task_plan or "(없음)", prompt=prompt)

    # Google Gemini의 실제 REST API 주소. 모델 이름과 API 키를 URL에 직접 포함시킨다.
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
    # Gemini 응답은 겹겹이 싸여있는 구조라, 실제 우리가 원하는 텍스트까지
    # candidates -> content -> parts -> text 순서로 파고들어가야 한다.
    text = gemini_result["candidates"][0]["content"]["parts"][0]["text"]
    # 그 text 자체가 우리가 요청한 JSON 형식의 "문자열"이라서, 다시 한번 json.loads로 해독.
    parsed = json.loads(text)
    return {
        "answer": parsed.get("answer", ""),
        "task_complete": bool(parsed.get("task_complete", True)),
        # or None: next_prompt가 빈 문자열("")이면 그것도 "없다"고 취급해서 None으로 정리.
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

    # request_id가 프론트에서 안 왔으면 새로 하나 만들어서 채운다.
    request_id = payload.request_id or str(uuid.uuid4())
    real_payment = False  # 이번 요청이 "진짜 온체인 결제"였는지 표시해두는 플래그

    if payload.transaction_signature:
        # 프론트가 실제 결제 서명을 보냈다 -> 온체인에서 진짜인지 검증부터.
        payer_address = await verify_onchain_usdc_payment(payload.transaction_signature, GEMINI_PRICE_USD)
        if payer_address is None:
            # 검증 실패 -> 이 시점에서 바로 거부 응답을 만들어서 함수를 끝낸다 (조기 반환).
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
        # 실제 결제 서명이 없는 경우 = 데모(미결제) 경로. 프론트가 보낸 지갑 정보를
        # 그대로 참고하되, 잔액이 안 왔으면 온체인에서 대신 조회를 시도해본다.
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
            # 조건에 따라 다른 리스트를 고르는 "삼항 표현식"을 리스트 리터럴에 바로 적용한 형태.
            "completed_steps": ["request_analysis", "price_check", "payment"] if real_payment else ["request_analysis", "price_check"],
            "demo_mode": not real_payment,
            "payment_status": "charged_but_rejected" if real_payment else "not_charged_demo",
            "transaction_signature": payload.transaction_signature or "",
        }

    try:
        gemini_result = await call_gemini_api(payload.prompt, payload.task_plan or payload.prompt)
        chain_result = _extract_chain_result(gemini_result)
    except HTTPException:
        # 이미 우리가 의도적으로 만든 HTTPException이면, 카운터만 올리고 그대로 다시 던짐.
        _consecutive_failures += 1
        raise
    except Exception as exc:
        # 예상 못한 다른 종류의 에러면, 우리가 원하는 형식(502)으로 감싸서 다시 던짐.
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

# 이 라우트는 위의 /execute(프론트 데모용)와 달리, 실제 x402 미들웨어가 지키고 있는 진짜
# 결제 라우트다. 여기 들어왔다는 것 자체가 "이미 결제 검증까지 끝났다"는 뜻이라, 함수
# 본문에서는 결제 관련 코드가 하나도 안 보인다 — 그건 미들웨어가 이 함수보다 먼저 처리한다.
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


# "이 파일을 직접 실행했을 때만"(다른 파일에서 import할 땐 실행 안 됨) 서버를 띄우는 부분.
if __name__ == "__main__":
    import uvicorn  # uvicorn: FastAPI 앱을 실제로 구동시키는 서버 프로그램(ASGI 서버)

    uvicorn.run(app, host="0.0.0.0", port=3000)
