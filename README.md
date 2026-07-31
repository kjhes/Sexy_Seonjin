# Sexy_Seonjin

Google Cloud × Solana Foundation Agentic Commerce 해커톤 — AI 결제 정책 판단 엔진

에이전트가 Google Cloud API(Gemini, BigQuery) 사용료를 정책 한도 안에서 자동으로 결제할지
승인/거부를 판단하는 REST API 서버. 아키텍처와 판단 조건 상세는 [project.md](project.md),
백엔드 연동 필드 상세는 [BACKEND_연동_가이드.md](BACKEND_연동_가이드.md) 참고.

## 1. 클론

```bash
git clone https://github.com/kjhes/Sexy_Seonjin.git
cd Sexy_Seonjin
```

## 2. 설치

Python 3.11 이상 필요.

```bash
pip install -r requirements.txt
```

이 저장소엔 서버가 **두 개** 있다 (다른 프로세스, 다른 포트).
- `main.py` — 백엔드(게임 친구)의 x402 결제 서버. `POST /api/gemini`, 포트 3000.
- `policy_server.py` — AI 친구의 정책 판단 엔진. `POST /evaluate`, 포트 8000.
  `main.py`가 결제 승인 여부를 물어보려고 내부적으로 호출하는 서버다.

## 3. 환경변수 설정

`.env.example`을 복사해서 `.env`를 만들고 값을 채운다.

```bash
cp .env.example .env
```

| 변수 | 설명 |
|---|---|
| `SOLANA_WALLET_ADDRESS` | 결제 받는 Solana Devnet 지갑 주소 (`main.py`, `policy_server.py` 공통) |
| `GEMINI_API_KEY` | Google AI Studio에서 발급 (https://aistudio.google.com/apikey). 레이어2 의미판단 + 실제 Gemini 호출에 사용 |
| `POLICY_ENGINE_URL` | `policy_server.py`가 뜨는 주소 (기본 `http://localhost:8000`). `main.py`가 이 주소로 `/evaluate`를 호출함 |
| `SOLANA_RPC_URL` | Solana Devnet RPC (온체인 USDC 잔액 조회용) |
| `FACILITATOR_URL` | x402 결제 검증/정산 대행 서비스 |
| `GEMINI_PRICE_USD` | Gemini 1회 호출당 청구 금액 |

`GEMINI_API_KEY`가 없어도 `policy_server.py`는 뜨지만, `user_prompt`가 포함된 요청은
전부 자동 거부(fail-safe)된다.

## 4. 실행 (두 서버 다 띄워야 함)

**터미널 1 — 정책 판단 엔진 (먼저 띄울 것, `main.py`가 이걸 호출함)**
```bash
uvicorn policy_server:app --reload --port 8000
```

**터미널 2 — 백엔드 x402 결제 서버**
```bash
python main.py
```
(포트 3000. `main.py` 안에 `uvicorn.run(..., port=3000)`으로 고정돼 있음)

## 5. 동작 확인

```bash
# 정책 판단 엔진 단독 확인
curl http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/evaluate \
  -H "Content-Type: application/json" \
  -d '{"amount": 0.5, "category": "gemini", "wallet_balance": 5.0, "request_id": "test-1"}'

# 백엔드 서버 확인
curl http://127.0.0.1:3000/health
```

각 파일 단독 테스트:

```bash
python policy_engine.py    # 레이어1(하드 규칙) 시나리오 10개
python semantic_layer.py   # 레이어2(LLM 의미판단), GEMINI_API_KEY 필요
```

## 구조

| 파일 | 역할 |
|---|---|
| `main.py` | 백엔드(게임 친구) — x402 결제 미들웨어, 온체인 USDC 잔액 조회, 실제 Gemini 호출. `POST /api/gemini` |
| `policy_server.py` | AI 친구 — FastAPI 서버, `POST /evaluate` 엔드포인트 (정책 판단 레이어1+레이어2 오케스트레이션) |
| `policy_engine.py` | Layer 1 — 하드 규칙(한도, 카테고리, 지갑상태 등) |
| `semantic_layer.py` | Layer 2 — Gemini 기반 의미 판단(목적이탈, 인젝션, 민감정보, 반복호출) |
| `project.md` | 전체 기획/아키텍처/진행상황 문서 |
| `BACKEND_연동_가이드.md` | 백엔드가 `/evaluate` 요청 필드를 채우는 방법 상세 |
