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
import asyncio
from collections import defaultdict, deque
from datetime import date
# logging: print() 대신 쓰는 정식 로그 기록 도구. "언제, 어디서, 무슨 일이 있었는지"를
# 레벨(INFO/ERROR 등)별로 남길 수 있어서 서버 코드에서는 print보다 이걸 표준으로 쓴다.
import logging
# os: 운영체제 관련 기능 모음. 여기서는 .env에 저장된 환경변수(os.getenv)를 읽는 데 씀.
import os
# uuid: 절대 겹치지 않는 고유한 식별자(ID)를 만들어주는 라이브러리.
# 결제 요청마다 이 값을 하나씩 붙여서 "같은 요청 두 번 처리" 같은 사고를 막는다.
import uuid
import time

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
from solders.keypair import Keypair

# 아래 x402.* 들은 전부 "x402 프로토콜"(API 쓰려면 결제부터 하라는 HTTP 402 기반 표준)을
# 파이썬에서 구현해주는 공식 패키지(x402[fastapi,svm])에서 가져오는 부품들이다.
# FacilitatorConfig/HTTPFacilitatorClient: 결제 검증·정산을 대신 처리해주는 외부
#   서비스(facilitator)에 접속하기 위한 설정값과 접속 클라이언트.
# PaymentOption: "이 API는 얼마짜리고, 어떤 지갑으로 받을지"를 정의하는 값.
# RouteConfig: 어떤 주소(라우트)를 결제로 막을지 정의하는 값.
from x402.client import x402Client
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption, RouteConfig
from x402.http.clients.httpx import wrapHttpxWithPayment
# PaymentMiddlewareASGI: FastAPI 앱에 "결제 문지기"를 끼워넣는 미들웨어.
# 이걸 붙인 라우트는 결제 증빙 없이 요청하면 자동으로 402 응답을 대신 만들어준다.
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
# SOLANA_DEVNET_CAIP2: "solana:이러이러한긴문자열" 형태로 Solana Devnet을 가리키는 표준 이름.
# USDC_DEVNET_ADDRESS: Devnet에서 쓰는 가짜(테스트용) USDC 토큰의 민트(발행) 주소.
from x402.mechanisms.svm.constants import SOLANA_DEVNET_CAIP2, USDC_DEVNET_ADDRESS
# ExactSvmServerScheme: "정확히 이 금액만큼 SPL 토큰(USDC)을 받는" 결제 방식을
# Solana(SVM) 네트워크용으로 구현해둔 클래스. register_exact_svm_client는 클라이언트
# 쪽(Agent Wallet이 스스로 서명할 때)에 같은 결제방식을 등록하는 함수.
from x402.mechanisms.svm.exact import ExactSvmServerScheme, register_exact_svm_client
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
# "Agent Wallet" 결제 방식(프론트 settings의 'agent' 옵션)이 자율로 서명할 전용 지갑.
# 사용자의 Phantom 지갑과 별개로, 이 서버 프로세스가 개인키를 직접 들고 있는 devnet 전용
# 지갑이다 — 그래서 결제할 때 사람이 승인 팝업을 누를 필요가 없다(demo_chain.py와 동일한 방식).
AGENT_WALLET_PATH = os.getenv("AGENT_WALLET_PATH", "agent_wallet.json")
# Render 같은 클라우드 배포는 실제로 뜨는 포트를 자기가 정해서 PORT 환경변수로
# 넘겨준다. Agent Wallet이 자기 자신의 /api/gemini를 호출할 때 쓰는 이 주소가
# 항상 3000으로 고정돼 있으면, 실제 포트가 다를 경우 자체호출이 매번 실패한다
# (외부에서 오는 요청은 Render가 알아서 실제 포트로 라우팅해주니 안 보이던 문제).
# 아래 uvicorn.run()도 같은 PORT를 쓰도록 맞춰서 항상 서로 일치하게 한다.
SELF_BASE_URL = os.getenv("SELF_BASE_URL", f"http://127.0.0.1:{os.getenv('PORT', '3000')}")
# policy_server.py의 /evaluate를 호출할 때 같이 보내는 인증 헤더 값.
# policy_server.py와 반드시 같은 값이어야 한다 (거기서도 없으면 서버가 안 뜬다).
POLICY_SHARED_SECRET = os.getenv("POLICY_SHARED_SECRET")
# 체인 한 번에 허용할 최대 계획 단계 수 (plan_steps 길이 자체를 여기서 자름).
MAX_CHAIN_STEPS = 3
# 같은 IP가 1분 안에 보낼 수 있는 최대 요청 수.
IP_REQUESTS_PER_MINUTE = 10
# 서버 전체가 하루에 실행할 수 있는 최대 Gemini 호출 수 (policy_engine.py의
# 카테고리별 50회 한도와는 별개로, 서버 프로세스 차원의 코스 상한선).
DAILY_GEMINI_CALL_LIMIT = 100
# /execute/prepare에서 발급한 사전 승인이 몇 초간 유효한지.
PREPARE_TTL_SECONDS = 300

# 필수 환경변수가 비어있으면, 서버를 아예 시작도 하지 말고 바로 에러를 내며 죽게 한다.
# (나중에 요청 들어왔을 때 조용히 이상하게 동작하는 것보다, 시작 시점에 바로 알아채는 게 안전함)
if not SOLANA_WALLET_ADDRESS:
    raise RuntimeError("SOLANA_WALLET_ADDRESS 환경변수가 필요합니다 (.env 확인)")
if not POLICY_ENGINE_URL:
    raise RuntimeError("POLICY_ENGINE_URL 환경변수가 필요합니다 (AI 친구 /evaluate 서버 주소)")
if not POLICY_SHARED_SECRET:
    raise RuntimeError("POLICY_SHARED_SECRET 환경변수가 필요합니다 (.env 확인, policy_server.py와 동일한 값 사용)")

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
# ---------------------------------------------------------------------------
class GeminiRequestIn(BaseModel):
    prompt: str = Field(min_length=5, max_length=500)
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
    prompt: str = Field(min_length=5, max_length=500)
    request_id: str | None = None
    plan_steps: list[str] | None = None
    plan_step_status: list[bool] | None = None
    # Field(default_factory=DemoWalletIn): 기본값을 "DemoWalletIn()을 새로 호출한 결과"로
    # 만들라는 뜻. 그냥 "= DemoWalletIn()"이라고 안 쓰는 이유는, 파이썬은 기본값을 딱 한 번만
    # 만들어서 모든 요청이 그 객체를 공유해버리는 함정이 있어서, 매번 새로 만들게 하는 것.
    wallet: DemoWalletIn = Field(default_factory=DemoWalletIn)
    # 프론트에서 Phantom으로 실제 서명해서 제출한 devnet USDC 결제 트랜잭션 서명.
    # 채워져 있으면 온체인에서 직접 검증하고, 없으면 기존 데모(미결제) 경로로 처리한다.
    transaction_signature: str | None = None
    # true면 Phantom 서명 팝업 없이, 서버가 들고 있는 Agent Wallet이 정책 통과 시
    # 스스로 서명·결제한다 (진짜 자율 결제 경로. 아래 "Agent Wallet 자율 결제" 참고).
    use_agent_wallet: bool = False


class PrepareRequestIn(BaseModel):
    prompt: str = Field(min_length=5, max_length=500)
    plan_steps: list[str] | None = None
    plan_step_status: list[bool] | None = None
    wallet: DemoWalletIn = Field(default_factory=DemoWalletIn)


# AI 소비자별 연속 실패 횟수 (BACKEND_연동_가이드.md의 ai_consecutive_failures).
# 단일 프로세스 데모 서버라 프로세스 메모리에 카운터를 둔다.
_consecutive_failures = 0
_prepared_requests: dict[str, dict] = {}
_prepare_lock = asyncio.Lock()
_ip_request_times: dict[str, deque[float]] = defaultdict(deque)
_daily_usage = {'date': date.today(), 'count': 0}


# ---------------------------------------------------------------------------
# 체인 스텝 하드캡 — 서버 사이드
#
# demo_chain.py/script.js의 MAX_STEPS는 클라이언트 측 for문일 뿐이라, 클라이언트가
# 그걸 안 쓰고 /api/gemini나 /execute를 직접 반복 호출하면(plan_step_status를 계속
# false로 위장해서) 서버는 이를 막을 방법이 없었다. plan_steps는 첫 호출 이후 고정되므로,
# 이 내용 자체를 "이 체인의 식별자"로 삼아 서버가 직접 호출 횟수를 센다 —
# 클라이언트가 plan_step_status를 뭐라고 보내든 이 카운트는 조작할 수 없다.
# (아래 IP 레이트리밋/일일 한도와는 별개 — 이건 "같은 계획 하나"를 얼마나 우려먹을 수
# 있는지를 막는 것이고, 저건 서버 전체/IP 전체를 막는 것이다. 둘 다 필요하다.)
# ---------------------------------------------------------------------------
_PLAN_CALL_LIMIT = 5
_plan_call_counts: dict[str, int] = {}


def _plan_fingerprint(plan_steps: list[str]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(plan_steps, ensure_ascii=False).encode()).hexdigest()


def _register_plan_call_and_check_limit(plan_steps: list[str] | None) -> bool:
    """이 계획으로 몇 번째 호출인지 세고, 하드캡을 넘었으면 True(초과)를 반환한다.
    plan_steps가 없는 첫 호출(계획을 만드는 호출)은 카운트하지 않는다."""
    if not plan_steps:
        return False
    key = _plan_fingerprint(plan_steps)
    count = _plan_call_counts.get(key, 0) + 1
    _plan_call_counts[key] = count
    return count > _PLAN_CALL_LIMIT


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
    """Agent Wallet 개인키를 로드한다.

    Render 같은 클라우드 배포 환경은 파일시스템이 재배포·재시작마다 초기화되는
    경우가 많다 — 그러면 매번 agent_wallet.json이 사라지고 새 지갑이 생성돼서,
    충전해둔 자금을 계속 잃어버리는 문제가 있었다(실제로 여러 번 재현됨).
    그래서 `AGENT_WALLET_PRIVATE_KEY`(Phantom "개인 키 내보내기"와 같은 base58
    문자열) 환경변수가 있으면 그걸 최우선으로 쓴다 — 이 값은 재배포돼도 그대로
    남아있으니 같은 지갑을 계속 쓸 수 있다. 로컬 개발 편의를 위해, 이 환경변수가
    없으면 기존처럼 로컬 파일(없으면 새로 생성)을 쓴다.

    로컬 파일(.gitignore에 포함, 커밋 안 됨)이든 환경변수든, 이 서버 프로세스만
    접근 가능하고 프론트엔드나 API 응답으로는 절대 노출하지 않는다(주소만 노출).
    """
    global _agent_keypair
    if _agent_keypair is not None:
        return _agent_keypair

    env_key = os.getenv("AGENT_WALLET_PRIVATE_KEY", "").strip()
    if env_key:
        _agent_keypair = Keypair.from_base58_string(env_key)
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

    if resp.status_code == 402:
        # x402 클라이언트가 결제를 시도했지만 끝내 완료 못 함(대부분 Agent Wallet
        # 잔액 부족). 정책이 거부한 게 아니라 결제 자체가 안 된 것이므로 구분한다.
        _consecutive_failures += 1
        return {
            "approved": False,
            "status": "rejected",
            "reason": (
                "Agent Wallet 결제가 완료되지 않았습니다. 지갑 잔액이 부족할 수 있습니다. "
                f"(주소: {result['payer_address']}) Devnet SOL/USDC를 충전한 뒤 다시 시도해 주세요."
            ),
            "rejected_stage": "payment",
            "request_id": request_id,
            "category": "gemini",
            "amount": GEMINI_PRICE_USD,
            "policy_check": [],
            "completed_steps": ["request_analysis", "price_check", "policy_check"],
            "demo_mode": False,
            "payment_status": "payment_failed",
            "transaction_signature": signature,
        }

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
            "completed_steps": ["request_analysis", "price_check"],
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


# ---------------------------------------------------------------------------
# IP 레이트리밋 / 일일 전체 호출 한도 / 체인 계획 길이 검증
# ---------------------------------------------------------------------------
def _validate_chain_state(plan_steps: list[str] | None, plan_step_status: list[bool] | None) -> None:
    if plan_steps is not None and len(plan_steps) > MAX_CHAIN_STEPS:
        raise HTTPException(status_code=422, detail=f'AI 연속 단계는 최대 {MAX_CHAIN_STEPS}개입니다.')
    if plan_step_status is not None and plan_steps is not None and len(plan_step_status) != len(plan_steps):
        raise HTTPException(status_code=422, detail='plan_steps와 plan_step_status 길이가 일치해야 합니다.')


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _enforce_ip_rate_limit(request: Request) -> None:
    now = time.monotonic()
    bucket = _ip_request_times[_client_ip(request)]
    while bucket and now - bucket[0] >= 60:
        bucket.popleft()
    if len(bucket) >= IP_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail='요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.')
    bucket.append(now)


def _reset_daily_usage_if_new_day() -> None:
    today = date.today()
    if _daily_usage['date'] != today:
        _daily_usage.update(date=today, count=0)


def _check_daily_gemini_quota_available() -> None:
    """실제로 아직 소모하진 않고, 지금 한도가 남아있는지만 확인한다.
    /execute/prepare에서 씀 — 실제 실행 전에 미리 걸러서, 어차피 한도 초과로
    실패할 요청에 결제(Phantom 서명)까지 하게 만들지 않기 위함."""
    _reset_daily_usage_if_new_day()
    if _daily_usage['count'] >= DAILY_GEMINI_CALL_LIMIT:
        raise HTTPException(status_code=429, detail='서버의 일일 AI 호출 한도에 도달했습니다.')


def _consume_daily_gemini_quota() -> None:
    """실제로 Gemini를 호출하기 직전에만 불러서 진짜로 카운트를 깎는다.
    (prepare에서 소모해버리면, 승인만 받고 실제 실행은 안 한 요청 때문에
    실제 호출 횟수보다 한도가 더 빨리 줄어드는 문제가 생긴다.)"""
    _reset_daily_usage_if_new_day()
    if _daily_usage['count'] >= DAILY_GEMINI_CALL_LIMIT:
        raise HTTPException(status_code=429, detail='서버의 일일 AI 호출 한도에 도달했습니다.')
    _daily_usage['count'] += 1


# @app.get("/health"): "GET /health 요청이 오면 바로 아래 함수를 실행해라"는 표시(데코레이터).
# async def: 이 함수는 "비동기" 함수다. 네트워크 요청처럼 기다리는(await) 작업이 있을 때
# 그 기다리는 동안 서버가 다른 요청도 같이 처리할 수 있게 해주는 파이썬 문법.
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
    # "async with ... as client:"는 요청 다 끝나면 연결을 자동으로 정리해주는 문법.
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(SOLANA_RPC_URL, json=payload)
        # raise_for_status(): 응답이 200번대(성공)가 아니면 여기서 바로 예외를 던짐.
        resp.raise_for_status()
        data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Solana RPC 오류: {data['error']}")

    # .get("result", {}).get("value", [])처럼 연쇄로 .get()을 쓰면, 딕셔너리에 그 키가
    # 없어도 에러 없이 빈 값(기본값)을 돌려주기 때문에 안전하다.
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
    # "A if 조건 else B" 문법 (삼항 표현식): accounts가 있으면 첫 번째 것의 pubkey를,
    # 없으면 None을 돌려준다.
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
    # (모듈 최상단)에 있는 그 변수를 그대로 가리켜서 수정하겠다는 선언.
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
    # "제너레이터 표현식"이 담긴 " / ".join(...): 리스트 컴프리헨션이랑 비슷한데
    # []가 아니라 그냥 괄호만 써서, 리스트를 통째로 안 만들고 하나씩 즉석에서 만들어
    # join에 넘긴다 (메모리를 조금 아끼는 방식). enumerate(plan_steps)는 (인덱스, 값)
    # 쌍을 순서대로 준다 — i는 0부터 시작하니 "1)"부터 보여주려고 i + 1을 씀.
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

    # [False] * len(plan_steps): "False를 plan_steps 개수만큼 반복한 리스트"를 만드는
    # 파이썬 관용구 (예: [False] * 3 == [False, False, False]).
    status = plan_step_status or [False] * len(plan_steps)
    # next(제너레이터, 기본값): "조건을 만족하는 첫 번째 값"을 찾는다. 여기서는
    # "아직 안 끝난(done이 False인) 첫 단계의 인덱스"를 찾되, 전부 끝났으면(못 찾으면)
    # 기본값으로 마지막 인덱스를 쓴다. (i for i, done in enumerate(status) if not done)도
    # 위와 같은 제너레이터 표현식 — 조건에 맞는 i만 하나씩 만들어낸다.
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
            resp = await client.post(url, headers={"X-Policy-Secret": POLICY_SHARED_SECRET}, json=body)
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

1. 이 목표를 달성하기 위해 필요한 구체적이고 유한한 단계들로 나눠라(1개~3개 정도,
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
    # zip(previous, latest): 두 리스트를 나란히 짝지어서 (previous[0], latest[0]),
    # (previous[1], latest[1]) ... 순서로 하나씩 꺼내준다 (파이썬 zip은 다른 언어의
    # zip과 동일한 개념). "or"로 묶었으니 둘 중 하나라도 True면 그 단계는 True로 확정.
    return [bool(p) or bool(n) for p, n in zip(previous, latest)]


async def call_gemini_api(
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY가 설정되지 않았습니다")

    # plan_steps가 없으면(빈 리스트거나 None) "첫 호출"이라고 판단.
    is_first_call = not plan_steps
    if is_first_call:
        contents_text = _FIRST_CALL_INSTRUCTION.format(prompt=prompt)
        schema = _FIRST_CALL_SCHEMA
    else:
        # json.dumps(..., ensure_ascii=False): 파이썬 리스트를 JSON 텍스트로 바꾸는데,
        # ensure_ascii=False를 안 주면 한글 같은 비-ASCII 문자가 \uXXXX 이스케이프
        # 코드로 깨져서 나온다. 프롬프트에 그대로 넣을 거라 사람이 읽는 그대로(False) 유지.
        contents_text = _CONTINUATION_INSTRUCTION.format(
            plan_steps=json.dumps(plan_steps, ensure_ascii=False),
            plan_step_status=json.dumps(plan_step_status or [False] * len(plan_steps), ensure_ascii=False),
            prompt=prompt,
        )
        schema = _CONTINUATION_SCHEMA

    # API 키를 쿼리파라미터(?key=...)로 보내면 httpx의 INFO 레벨 요청 로그에 URL
    # 전체(키 포함)가 그대로 찍힌다. 헤더로 보내면 로그엔 URL만 남고 키는 안 남는다.
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    body = {
        "contents": [{"parts": [{"text": contents_text}]}],
        "generationConfig": {
            "temperature": 0,
            # thinking을 꺼도(budget=0) 계획 분해·단계 판단 정확도와 답변 품질이
            # 그대로인 것을 A/B 테스트로 확인함(thoughtsTokenCount가 실제로 0으로 찍힘).
            # thinking 토큰이 답변 토큰보다도 많이 나가서(702 vs 498) 실제 비용의
            # 절반 이상을 차지하고 있었음.
            "thinkingConfig": {"thinkingBudget": 0},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    # 첫 호출은 목표 분해 + 답변 + 단계별 판단을 한 번에 하므로 기존 30초보다 여유를 둔다.
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=headers, json=body)
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
    plan_steps = (prior_plan_steps or parsed.get("plan_steps") or [])[:MAX_CHAIN_STEPS]
    latest_status = [bool(v) for v in parsed.get("plan_step_status", [])][:MAX_CHAIN_STEPS]
    # 길이가 안 맞으면(모델이 스키마를 어겼거나 계획이 비어있으면) 안전하게 전부 미완료로 취급한다.
    if len(latest_status) != len(plan_steps):
        latest_status = [False] * len(plan_steps)
    plan_step_status = _merge_step_status(prior_plan_step_status, latest_status)

    # all(리스트): 리스트 안의 모든 값이 참이면 True (하나라도 False면 전체 False).
    # bool(plan_step_status)를 앞에 붙인 이유: 리스트가 아예 비어있으면 all([])이
    # 파이썬에서 True를 주는 함정이 있어서, "단계가 하나라도 있어야만" 완료로 치게 방어.
    task_complete = bool(plan_step_status) and all(plan_step_status)

    return {
        "answer": parsed.get("answer", ""),
        "plan_steps": plan_steps,
        "plan_step_status": plan_step_status,
        "task_complete": task_complete,
        "next_prompt": parsed.get("next_prompt") or None,
    }


# ---------------------------------------------------------------------------
# 메인 라우트: 정책 사전 승인 -> 결제 검증 -> 실행(Gemini)
# ---------------------------------------------------------------------------
def _approval_snapshot(payload: PrepareRequestIn | ExecuteRequestIn) -> dict:
    return {
        'prompt': payload.prompt,
        'plan_steps': payload.plan_steps,
        'plan_step_status': payload.plan_step_status,
    }


@app.post('/execute/prepare')
async def prepare_execute(payload: PrepareRequestIn, request: Request) -> dict:
    global _consecutive_failures
    _enforce_ip_rate_limit(request)
    _validate_chain_state(payload.plan_steps, payload.plan_step_status)

    wallet_balance = payload.wallet.balance
    if wallet_balance is None and payload.wallet.public_key:
        try:
            wallet_balance = await get_onchain_usdc_balance(payload.wallet.public_key)
        except Exception:
            logger.exception('Prepare wallet balance lookup failed')
            wallet_balance = 0.0

    request_id = str(uuid.uuid4())
    policy_payload = {
        'amount': GEMINI_PRICE_USD,
        'category': 'gemini',
        'wallet_connected': payload.wallet.connected,
        'wallet_balance': wallet_balance or 0.0,
        'api_key_valid': bool(GEMINI_API_KEY),
        'recipient_address': SOLANA_WALLET_ADDRESS,
        'request_id': request_id,
        'ai_consecutive_failures': _consecutive_failures,
        'has_required_permission': True,
        'infra_stable': True,
        'user_prompt': _semantic_check_prompt(payload.prompt, payload.plan_steps, payload.plan_step_status),
        'task_plan': _plan_steps_to_text(payload.plan_steps),
    }
    try:
        url = POLICY_ENGINE_URL.rstrip('/') + '/evaluate'
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json=policy_payload,
                headers={'X-Policy-Secret': POLICY_SHARED_SECRET},
            )
            response.raise_for_status()
            decision = response.json()
    except Exception as exc:
        _consecutive_failures += 1
        logger.exception('Prepare policy evaluation failed')
        raise HTTPException(status_code=502, detail='Policy server is unavailable.') from exc

    if not decision.get('approved'):
        _consecutive_failures += 1
        return {
            'approved': False,
            'status': 'rejected',
            'reason': decision.get('reason', 'The policy engine rejected this request.'),
            'rejected_stage': 'policy_check',
            'request_id': request_id,
            'amount': GEMINI_PRICE_USD,
            'policy_check': decision.get('policy_check', []),
            'payment_status': 'not_charged',
        }

    _consecutive_failures = 0
    _check_daily_gemini_quota_available()
    async with _prepare_lock:
        now = time.monotonic()
        expired = [key for key, value in _prepared_requests.items() if value['expires_at'] <= now]
        for key in expired:
            _prepared_requests.pop(key, None)
        _prepared_requests[request_id] = {
            'expires_at': now + PREPARE_TTL_SECONDS,
            'snapshot': _approval_snapshot(payload),
            'decision': decision,
            'status': 'approved',
        }

    return {
        'approved': True,
        'status': 'prepared',
        'request_id': request_id,
        'amount': GEMINI_PRICE_USD,
        'recipient_address': SOLANA_WALLET_ADDRESS,
        'network': 'solana-devnet',
        'demo_mode': DEMO_MODE,
        'asset': 'USDC',
        'policy_check': decision.get('policy_check', []),
        'expires_in': PREPARE_TTL_SECONDS,
        'completed_steps': ['request_analysis', 'price_check', 'policy_check'],
    }


@app.post("/execute")
async def execute_demo(payload: ExecuteRequestIn, request: Request) -> dict:
    """Frontend orchestration adapter.

    /execute/prepare에서 발급한 일회성 승인을 먼저 확인한다. Phantom 서명이 있으면
    devnet USDC 결제를 온체인에서 검증하고, 서명이 없으면 데모(미결제)로 처리한 뒤
    Gemini를 실행한다. 이 단계에서는 정책을 다시 판단해 결제 후 거절하지 않는다.
    """
    global _consecutive_failures

    request_id = payload.request_id or str(uuid.uuid4())

    _validate_chain_state(payload.plan_steps, payload.plan_step_status)

    if payload.use_agent_wallet:
        # 이 아래 _execute_via_agent_wallet가 내부적으로 /api/gemini를 자체 호출하고,
        # 그 라우트가 이미 같은 plan_steps로 _register_plan_call_and_check_limit를
        # 검사한다. 여기서 한 번 더 세면 계획 하나당 카운트가 두 배로 올라가서
        # Agent Wallet 경로만 Phantom 경로보다 실질 한도가 절반이 되는 불일치가
        # 생긴다 — 그래서 여기서는 세지 않고 /api/gemini의 검사에 맡긴다.
        return await _execute_via_agent_wallet(payload, request_id)

    # Production Phantom requests must include an on-chain payment signature.
    # Check before consuming or locking the one-time approval.
    if not DEMO_MODE and not payload.transaction_signature:
        raise HTTPException(
            status_code=400,
            detail="transaction_signature is required when DEMO_MODE is false.",
        )

    if _register_plan_call_and_check_limit(payload.plan_steps):
        _consecutive_failures += 1
        return {
            "approved": False,
            "status": "rejected",
            "reason": f"이 작업 계획은 이미 최대 {_PLAN_CALL_LIMIT}번 호출됐습니다. 더 이상 이어갈 수 없습니다.",
            "rejected_stage": "policy_check",
            "request_id": request_id,
            "category": "gemini",
            "amount": GEMINI_PRICE_USD,
            "policy_check": [],
            "completed_steps": ["request_analysis", "price_check"],
            "demo_mode": True,
            "payment_status": "not_charged_demo",
        }

    # Phantom 데모 경로: /execute/prepare에서 정책판단을 이미 마치고 발급한 일회성
    # 승인이 있어야만 여기로 진입할 수 있다 (승인 스냅샷과 이번 요청 내용이 달라도 거부).
    if not payload.request_id:
        raise HTTPException(status_code=400, detail='사전 승인 request_id가 필요합니다.')

    async with _prepare_lock:
        approval = _prepared_requests.get(request_id)
        if not approval or approval['expires_at'] <= time.monotonic():
            _prepared_requests.pop(request_id, None)
            raise HTTPException(status_code=403, detail='사전 승인이 없거나 만료되었습니다.')
        if approval['status'] != 'approved':
            raise HTTPException(status_code=409, detail='이미 사용 중이거나 사용된 사전 승인입니다.')
        if approval['snapshot'] != _approval_snapshot(payload):
            raise HTTPException(status_code=403, detail='사전 승인된 요청 내용과 일치하지 않습니다.')
        approval['status'] = 'processing'

    decision = approval['decision']
    real_payment = False

    if payload.transaction_signature:
        payer_address = await verify_onchain_usdc_payment(payload.transaction_signature, GEMINI_PRICE_USD)
        if payer_address is None:
            _consecutive_failures += 1
            async with _prepare_lock:
                if request_id in _prepared_requests:
                    _prepared_requests[request_id]['status'] = 'approved'
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

    # 정책 판단은 /execute/prepare에서 이미 끝났다 (decision은 그때 승인된 그대로).
    # 여기서는 결제 검증만 하고, 이 승인을 다 썼으니 저장소에서 지운다(재사용 방지).
    async with _prepare_lock:
        _prepared_requests.pop(request_id, None)

    # 실제로 Gemini를 부르기 직전인 지금 소모한다 (prepare 시점이 아니라).
    _consume_daily_gemini_quota()

    try:
        gemini_result = await call_gemini_api(payload.prompt, payload.plan_steps, payload.plan_step_status)
        chain_result = _extract_chain_result(gemini_result, payload.plan_steps, payload.plan_step_status)
    except HTTPException:
        _consecutive_failures += 1
        raise
    except Exception as exc:
        _consecutive_failures += 1
        logger.exception("Gemini execution failed")
        # 실결제(Phantom)는 이 시점에 이미 온체인에서 확정된 뒤라 되돌릴 수 없다.
        # 정책거절 케이스와 마찬가지로, 돈이 이미 나갔다는 걸 명확히 알려야 한다.
        detail = "Gemini execution failed."
        if real_payment:
            detail += " (결제는 이미 완료되었으나 서비스 제공에는 실패했습니다)"
        raise HTTPException(status_code=502, detail=detail) from exc

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
    _enforce_ip_rate_limit(request)
    _validate_chain_state(payload.plan_steps, payload.plan_step_status)

    if _register_plan_call_and_check_limit(payload.plan_steps):
        raise HTTPException(
            status_code=429,
            detail=f"이 작업 계획은 이미 최대 {_PLAN_CALL_LIMIT}번 호출됐습니다. 더 이상 이어갈 수 없습니다.",
        )

    decision = await call_policy_engine(request, payload.prompt, payload.plan_steps, payload.plan_step_status)

    if not decision.get("approved"):
        raise HTTPException(
            status_code=403,
            detail=decision.get("reason", "정책 엔진이 이 결제를 거부했습니다"),
        )

    _consume_daily_gemini_quota()
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

    # Render가 PORT 환경변수로 실제 뜰 포트를 지정해준다. 이걸 안 따라가면
    # 위 SELF_BASE_URL(자기 자신 호출용 주소)과 실제 포트가 어긋난다.
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3000")))
