# Google-X-Solana-AI-Agentic-Hackathon

Google Cloud × Solana Foundation Agentic Commerce 해커톤 — AI 결제 정책 판단 엔진

에이전트가 Google Cloud API(Gemini, BigQuery) 사용료를 정책 한도 안에서 자동으로 결제할지
승인/거부를 판단하는 REST API 서버. 아키텍처와 판단 조건 상세는 [project.md](project.md),
백엔드 연동 필드 상세는 [BACKEND_연동_가이드.md](BACKEND_연동_가이드.md) 참고.

## 1. 클론

```bash
git clone https://github.com/dodo323232/Google-X-Solana-AI-Agentic-Hackathon.git
cd Google-X-Solana-AI-Agentic-Hackathon
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

이 저장소에는 `.env` 파일이 없다 — 의도적으로 `.gitignore`에 등록해서 안 올린 것이다.
`.env`에는 API 키·비밀값이 들어가서 공개 저장소에 올리면 안 되기 때문이다. 대신 어떤
값이 필요한지만 적어둔 `.env.example`이 있으니, 이걸 복사해서 **자기 자신의** 키/값으로
채운 `.env`를 각자 만들면 된다. 우리 팀의 실제 키가 없어도 아래 안내대로 자기 키를
발급받아 채우면 로컬에서 완전히 재현·실행할 수 있다.

```bash
cp .env.example .env
```

### 최소로 필요한 것 (빠르게 돌려보고 싶다면)

아래 3개만 채우면 두 서버가 뜨고 정책 판단 흐름을 바로 테스트할 수 있다(`DEMO_MODE=true`가
기본값이라 실제 결제 없이도 동작함).

1. `GEMINI_API_KEY` — Google AI Studio(https://aistudio.google.com/apikey)에서 **무료로** 발급
2. `POLICY_SHARED_SECRET` — 아무 문자열이나 직접 만들어서 넣으면 됨(두 서버에 같은 값)
3. `SOLANA_WALLET_ADDRESS` — Devnet 지갑 주소면 되고, 정책 판단만 테스트할 거면 충전 안 된 주소여도 무방

| 변수 | 필수 여부 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | **필수** | Google AI Studio에서 발급(무료). 레이어2 의미판단 + 실제 Gemini 호출에 사용 |
| `POLICY_SHARED_SECRET` | **필수** | `main.py`↔`policy_server.py` 간 인증용 공유 비밀키. 아무 문자열이나 직접 정하면 됨. **두 서버에 반드시 같은 값**을 넣어야 하고, 없으면 둘 다 시작을 거부함(fail-safe) |
| `SOLANA_WALLET_ADDRESS` | **필수** | 결제 받는 Solana Devnet 지갑 주소 (`main.py`, `policy_server.py` 공통). Devnet 지갑은 [Phantom](https://phantom.app)에서 네트워크를 Devnet으로 바꾸고 새 지갑을 만들면 바로 얻을 수 있음 |
| `POLICY_ENGINE_URL` | 선택(기본값 있음) | `policy_server.py`가 뜨는 주소 (기본 `http://localhost:8000`). `main.py`가 이 주소로 `/evaluate`를 호출함 |
| `SOLANA_RPC_URL` | 선택(기본값 있음) | Solana Devnet RPC (온체인 USDC 잔액 조회용). 공식 공개 RPC가 기본값으로 이미 채워져 있음 |
| `FACILITATOR_URL` | 선택(기본값 있음) | x402 결제 검증/정산 대행 서비스. 공식 테스트용 서비스가 기본값 |
| `GEMINI_PRICE_USD` | 선택(기본값 있음) | Gemini 1회 호출당 청구 금액 |
| `DEMO_MODE` | 선택(기본값 `true`) | `true`면 지갑 연결/결제 없이도 `/execute`가 정책 판단 흐름만 보여주는 데모 모드로 동작함 — 로컬에서 빠르게 확인할 때 유용 |
| `CORS_ORIGINS` | 선택(기본값 `*`) | 프론트엔드(`index.html`)가 다른 포트에서 API를 호출할 수 있게 허용할 origin |
| `AGENT_WALLET_PRIVATE_KEY` | 선택(비워둬도 됨) | Agent Wallet(자율 결제)용 개인키. 로컬에서는 비워두면 `agent_wallet.json` 파일로 자동 대체되니 안 채워도 실행에는 문제없음. 클라우드 배포 시에만 필요(재배포마다 파일시스템이 초기화되기 때문) |

`GEMINI_API_KEY`가 없어도 `policy_server.py`는 뜨지만, `user_prompt`가 포함된 요청은
전부 자동 거부(fail-safe)된다. `POLICY_SHARED_SECRET`이 없으면 아예 서버가 안 뜬다.

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

## 6. 프론트엔드 (로봇 친구)

두 서버(8000, 3000) 띄운 상태에서 정적 파일 서버를 하나 더 띄운다.

```bash
python -m http.server 5500
```

브라우저에서 http://localhost:5500 접속 → 설정에서 Phantom 지갑 연결 → 목표 입력 후 실행하면
실제로 Phantom 서명 팝업이 뜨고, 승인하면 진짜 devnet USDC가 이동한다 (데모용으로 결제를
건너뛰고 싶으면 지갑을 연결하지 않은 채로 실행 — `/execute`가 정책 판단만 보여주는 데모 모드로 대체됨).

## 7. Pay.sh 공식 CLI로 검증

`main.py`는 x402 프로토콜을 표준대로 구현해서, Google Cloud × Solana Foundation의 공식 결제 CLI인
[Pay.sh](https://github.com/solana-foundation/pay)로도 서버 코드 수정 없이 그대로 결제된다.
설치·검증 방법은 [tools/paysh/README.md](tools/paysh/README.md) 참고.

```bash
tools/paysh/pay.exe curl -X POST http://localhost:3000/api/gemini \
  -H "Content-Type: application/json" \
  -d '{"prompt":"...", "task_plan":"..."}'
```

## 구조

| 파일 | 역할 |
|---|---|
| `main.py` | 백엔드(게임 친구) — x402 결제 미들웨어, 온체인 USDC 잔액 조회, 실제 Gemini 호출. `POST /api/gemini`, `POST /execute`(프론트 데모용), `GET /config`, `GET /wallet/balance` |
| `policy_server.py` | AI 친구 — FastAPI 서버, `POST /evaluate` 엔드포인트 (정책 판단 레이어1+레이어2 오케스트레이션) |
| `policy_engine.py` | Layer 1 — 하드 규칙(한도, 카테고리, 지갑상태 등) |
| `semantic_layer.py` | Layer 2 — Gemini 기반 의미 판단(목적이탈, 인젝션, 민감정보, 반복호출) |
| `demo_chain.py` | 실제 Solana 지갑(키페어)으로 서명해서 `/api/gemini`를 연쇄 호출하는 Python 데모 클라이언트 |
| `index.html` / `style.css` / `script.js` | 로봇 친구가 만든 프론트엔드. Phantom 지갑으로 실제 결제 서명까지 수행 |
| `tools/paysh/` | Pay.sh 공식 CLI 연동 검증 (설치법·실제 결제 테스트 기록) |
| `project.md` | 전체 기획/아키텍처/진행상황 문서 |
| `BACKEND_연동_가이드.md` | 백엔드가 `/evaluate` 요청 필드를 채우는 방법 상세 |
