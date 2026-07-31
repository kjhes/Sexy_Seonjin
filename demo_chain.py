"""
연쇄 호출 데모: Gemini 자신이 "이 작업이 끝났는지, 안 끝났으면 다음엔 뭘 물어봐야
하는지"를 구조화된 JSON(answer/task_complete/next_prompt)으로 판단해서 돌려주고,
이 스크립트는 task_complete=false인 동안 next_prompt로 자동으로 다음 결제+호출을
잇는다. task_complete=true가 되는 즉시 종료한다.

무한/과다 반복 방지 안전장치:
  1. Gemini 프롬프트 자체가 "애매하면 완료로 판단하라"고 편향돼 있음 (main.py 참고)
  2. task_plan을 정책엔진(레이어2)이 매 호출마다 대조 판단함 (근거 부실한 continuation은 거부됨)
  3. 이 스크립트의 MAX_STEPS 하드캡 (최후 방어선)

main.py, policy_server.py 코드는 여기서 건드리지 않고 그대로 재사용한다.

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

TASK_PLAN = (
    "1) 국내 가족 여행지 3곳 추천, 2) 추천된 곳 중 한 곳으로 2박3일 일정 완성. "
    "이 두 가지가 모두 답변에 포함되면 완료."
)
INITIAL_PROMPT = "국내 가족 여행지 3곳을 추천해줘. 각각 간단한 이유도 함께."
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


async def call_step(http, label: str, prompt: str) -> dict:
    print(f"\n=== {label} ===")
    print(f"프롬프트: {prompt}")
    resp = await http.post(f"{MAIN_SERVER_URL}/api/gemini", json={"prompt": prompt, "task_plan": TASK_PLAN})
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

        for step in range(1, MAX_STEPS + 1):
            data = await call_step(http, f"{step}단계 호출", prompt)

            if not data.get("approved"):
                reason = data.get("policy_decision", {}).get("reason") or data.get("detail")
                print(f"\n{step}단계에서 거부되어 중단합니다. 사유: {reason}")
                return

            answer = data.get("answer", "")
            print(f"\n{step}단계 실제 답변:\n{answer}")

            if data.get("task_complete"):
                print(f"\n=== Gemini가 작업 완료로 판단해 {step}단계에서 자동 종료 ===")
                return

            next_prompt = data.get("next_prompt")
            if not next_prompt:
                print("\n=== task_complete=false인데 next_prompt가 없어 안전하게 중단합니다 ===")
                return

            prompt = next_prompt

        print(f"\n=== 안전장치 작동: 최대 {MAX_STEPS}단계에 도달해 강제 종료합니다 ===")


if __name__ == "__main__":
    asyncio.run(main())
