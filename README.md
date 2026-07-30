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

## 3. 환경변수 설정 (Gemini 의미판단 레이어 사용 시 필수)

`semantic_layer.py`(Layer 2, LLM 기반 의미 판단)를 쓰려면 Gemini API 키가 필요하다.
키 발급: https://aistudio.google.com/apikey

```bash
# macOS/Linux
export GEMINI_API_KEY="발급받은키"

# Windows PowerShell
$env:GEMINI_API_KEY = "발급받은키"
```

키가 없어도 서버 자체는 실행되지만, `user_prompt`가 포함된 요청은 전부 자동 거부(fail-safe)된다.

## 4. 실행

```bash
uvicorn main:app --reload
```

기본적으로 http://127.0.0.1:8000 에서 뜬다. Swagger UI로 바로 테스트해보려면
http://127.0.0.1:8000/docs 접속.

## 5. 동작 확인

```bash
curl http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/evaluate \
  -H "Content-Type: application/json" \
  -d '{"amount": 0.5, "category": "gemini", "wallet_balance": 5.0, "request_id": "test-1"}'
```

각 파일 단독 테스트:

```bash
python policy_engine.py    # 레이어1(하드 규칙) 시나리오 10개
python semantic_layer.py   # 레이어2(LLM 의미판단), GEMINI_API_KEY 필요
```

## 구조

| 파일 | 역할 |
|---|---|
| `main.py` | FastAPI 서버, `POST /evaluate` 엔드포인트 |
| `policy_engine.py` | Layer 1 — 하드 규칙(한도, 카테고리, 지갑상태 등) |
| `semantic_layer.py` | Layer 2 — Gemini 기반 의미 판단(목적이탈, 인젝션, 민감정보, 반복호출) |
| `project.md` | 전체 기획/아키텍처/진행상황 문서 |
| `BACKEND_연동_가이드.md` | 백엔드가 `/evaluate` 요청 필드를 채우는 방법 상세 |
