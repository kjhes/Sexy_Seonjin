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
# typing.Optional[str]은 "str이거나 None"이라는 뜻. 파이썬 최신 문법으로는
# "str | None"이라고도 쓰는데(main.py에서 그 스타일을 씀), 이 파일은 옛날 스타일인
# Optional을 쓰고 있다 — 의미는 완전히 같다.
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

# 다른 파일(policy_engine.py)에 정의해둔 클래스/데이터 모양들을 가져온다.
# 파이썬은 파일 하나가 "모듈"이 되고, 그 안의 이름(클래스, 함수, 변수)들을
# from 파일이름 import 이름 형태로 다른 파일에서 그대로 갖다 쓸 수 있다.
from policy_engine import PaymentRequest, PolicyConfig, PolicyEngine, RequestGuard, SpendTracker
from semantic_layer import SemanticGuard

load_dotenv()

# main.py 말고 아무나 /evaluate를 직접 호출해서 예산을 소진시키거나 판단을 우회하지
# 못하도록, 공유 비밀키로 요청을 검증한다. 값이 없으면 인증 없이 뜨는 걸 막기 위해
# 서버 시작 자체를 거부한다(fail-safe) — main.py의 필수 환경변수 검증과 같은 원칙.
POLICY_SHARED_SECRET = os.getenv("POLICY_SHARED_SECRET")
if not POLICY_SHARED_SECRET:
    raise RuntimeError("POLICY_SHARED_SECRET 환경변수가 필요합니다 (.env 확인, main.py와 동일한 값 사용)")

app = FastAPI(title="Agentic Commerce Policy Engine")

# 아래 config/tracker/guard/engine/semantic_guard는 전부 "모듈 최상단"에서 딱 한 번만
# 만들어지는 객체들이다. 이 파일이 서버로 실행되는 동안 계속 살아있는 하나의
# 인스턴스(싱글턴)를 여러 요청이 공유해서 쓴다 — 그래서 SpendTracker에 쌓인
# "오늘 얼마 썼는지" 기록이 요청과 요청 사이에도 유지되는 것이다.
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


# main.py가 POST /evaluate로 보내는 JSON이 이 모양과 맞는지 FastAPI가 자동으로 검사해준다.
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


# main.py의 라우트들과 다르게 여기는 "def"만 쓰고 "async def"가 아니다. 이 안에서 하는
# 일(policy_engine 계산, semantic_guard.check())이 네트워크 대기 없이 그 자리에서 바로
# 끝나는 동기(sync) 작업이라 async가 필요 없다 — FastAPI가 알아서 별도 스레드에서 돌려준다.
@app.post("/evaluate")
def evaluate(
    req: PaymentRequestIn,
    x_policy_secret: str | None = Header(default=None),
):
    # hmac.compare_digest로 타이밍 공격(문자를 한 글자씩 추측)을 방지한다.
    if not x_policy_secret or not hmac.compare_digest(x_policy_secret, POLICY_SHARED_SECRET):
        raise HTTPException(status_code=401, detail="인증되지 않은 호출입니다.")

    # req는 pydantic 모델(PaymentRequestIn) 객체다. .model_dump()는 그걸 평범한
    # 파이썬 딕셔너리로 바꿔준다. 그 앞의 별표 두 개(**)는 "이 딕셔너리를 풀어헤쳐서
    # key=value 형태의 인자들로 나눠 넣어라"는 뜻 — 즉
    # PaymentRequest(amount=..., category=..., wallet_connected=..., ...) 를
    # 한 줄로 압축해서 쓴 것과 같다.
    request = PaymentRequest(**req.model_dump())
    result = engine.evaluate(request)   # Layer 1(하드 규칙) 판단

    # Layer 1을 통과했고, 판단할 프롬프트(user_prompt)가 있을 때만 Layer 2(LLM)까지 감.
    # (프롬프트가 없으면 의미 판단할 대상이 없으니 그냥 건너뜀 — Gemini 호출 비용도 아낌)
    if result["approved"] and request.user_prompt:
        try:
            semantic_result = semantic_guard.check(request.user_prompt, request.task_plan)
        except Exception:
            # LLM 호출 자체가 실패하면 "API 상태 불안정"(조건 14)으로 취급해 거부한다.
            result["approved"] = False
            result["policy_check"].append("semantic_layer_unavailable_fail")
            result["reason"] = "의미 판단 서비스(Gemini)에 연결할 수 없어 요청을 거부했습니다."
            return result

        # 리스트 두 개를 += 로 합침 (result["policy_check"].extend(semantic_result["checks"])와 동일).
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

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
