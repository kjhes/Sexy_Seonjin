"""
연쇄 호출 데모: 뭉툭한 목표 하나만 주면, 첫 호출에서 Gemini가 그 목표를 구체적인
단계 리스트(plan_steps)로 분석해서 만들고, 그 이후 호출부터는 이 리스트를 그대로
잠근 채(다시 못 만듦) "각 단계가 이번 답변으로 충족됐는지"만 항목별로 판단한다.
전체 완료 여부(task_complete)는 Gemini의 통짜 자기판단이 아니라, 이 스크립트가
아니라 main.py가 plan_step_status(단계별 bool)를 집계해서(all done) 코드로 확정한다.
이 스크립트는 그 결과를 받아 task_complete=false인 동안 next_prompt로 다음 결제+
호출을 이어가면서, plan_steps/plan_step_status를 계속 echo해서 계획이 안 흔들리게 한다.

무한/과다 반복 방지 안전장치:
  1. 완료 판단이 모델의 통짜 판단이 아니라 "항목별 판단 + 코드 집계"라 애매함이 낄 여지가 적음
  2. plan_steps는 첫 호출 이후 코드가 강제로 고정함(모델이 다시 못 만듦) — main.py 참고
  3. task_plan(plan_steps를 풀어쓴 문자열)을 정책엔진(레이어2)이 매 호출마다 대조 판단함
  4. 이 스크립트의 MAX_STEPS 하드캡 (최후 방어선)

policy_server.py 코드는 여기서 건드리지 않고 그대로 재사용한다.

실행 전 준비:
  1. 터미널 1: uvicorn policy_server:app --port 8000
  2. 터미널 2: python main.py   (포트 3000)
  3. python demo_chain.py

첫 실행 시 demo_wallet.json이 없으면 새로 만들고, Devnet SOL/USDC를 받아야 한다는
안내를 출력하고 종료한다. (SOL: https://faucet.solana.com, USDC: https://faucet.circle.com)
"""

import asyncio
import json
import os
import sys

from solders.keypair import Keypair

from x402.client import x402Client
from x402.http.clients.httpx import wrapHttpxWithPayment
from x402.mechanisms.svm.exact import register_exact_svm_client

MAIN_SERVER_URL = os.environ.get("MAIN_SERVER_URL", "http://127.0.0.1:3000")
WALLET_PATH = os.environ.get("DEMO_WALLET_PATH", "demo_wallet.json")

# 뭉툭한 목표만 준다 — 구체적 단계 분해는 main.py의 첫 호출에서 Gemini가 직접 한다.
INITIAL_PROMPT = "국내 가족 여행지를 추천해주고, 그중 한 곳으로 2박3일 일정까지 짜줘."
MAX_STEPS = 5  # 안전장치: Gemini나 정책판단이 다 뚫려도 여기서 강제 종료


class LocalKeypairSigner:
    """x402의 ClientSvmSigner 프로토콜을 로컬 Keypair로 구현한 것."""

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


def load_or_create_wallet() -> Keypair:
    if os.path.exists(WALLET_PATH):
        with open(WALLET_PATH) as f:
            return Keypair.from_bytes(bytes(json.load(f)))

    kp = Keypair()
    with open(WALLET_PATH, "w") as f:
        json.dump(list(bytes(kp)), f)

    print(f"새 데모 지갑을 만들었습니다: {kp.pubkey()}")
    print(f"({WALLET_PATH}에 저장됨. 이 파일은 절대 커밋하지 마세요 — .gitignore에 이미 있음)")
    print("실행 전에 이 주소로 Devnet SOL과 USDC를 받아주세요:")
    print("  SOL:  https://faucet.solana.com")
    print("  USDC: https://faucet.circle.com (Solana Devnet 선택)")
    sys.exit(0)


async def call_step(
    http,
    label: str,
    prompt: str,
    plan_steps: list[str] | None,
    plan_step_status: list[bool] | None,
) -> dict:
    print(f"\n=== {label} ===")
    print(f"프롬프트: {prompt}")
    resp = await http.post(
        f"{MAIN_SERVER_URL}/api/gemini",
        json={"prompt": prompt, "plan_steps": plan_steps, "plan_step_status": plan_step_status},
    )
    print(f"HTTP 상태: {resp.status_code}")
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200:
        print("본문:", resp.text[:1000])
    return data


async def main():
    keypair = load_or_create_wallet()
    client = x402Client()
    register_exact_svm_client(client, LocalKeypairSigner(keypair))

    async with wrapHttpxWithPayment(client, timeout=30.0) as http:
        prompt = INITIAL_PROMPT
        plan_steps: list[str] | None = None
        plan_step_status: list[bool] | None = None

        for step in range(1, MAX_STEPS + 1):
            data = await call_step(http, f"{step}단계 호출", prompt, plan_steps, plan_step_status)

            if not data.get("approved"):
                reason = data.get("policy_decision", {}).get("reason") or data.get("detail")
                print(f"\n{step}단계에서 거부되어 중단합니다. 사유: {reason}")
                return

            answer = data.get("answer", "")
            print(f"\n{step}단계 실제 답변:\n{answer}")

            # 서버가 확정한 계획/진행상황을 그대로 이어받는다 — 이후 호출에서 계획이
            # 다시 만들어지지 않도록(잠긴 채로) 매번 echo한다.
            plan_steps = data.get("plan_steps") or plan_steps
            plan_step_status = data.get("plan_step_status") or plan_step_status
            print(f"단계별 진행상황: {list(zip(plan_steps or [], plan_step_status or []))}")

            if data.get("task_complete"):
                print(f"\n=== 모든 계획 단계가 완료로 집계되어 {step}단계에서 자동 종료 ===")
                return

            next_prompt = data.get("next_prompt")
            if not next_prompt:
                print("\n=== task_complete=false인데 next_prompt가 없어 안전하게 중단합니다 ===")
                return

            prompt = next_prompt

        print(f"\n=== 안전장치 작동: 최대 {MAX_STEPS}단계에 도달해 강제 종료합니다 ===")


if __name__ == "__main__":
    asyncio.run(main())
