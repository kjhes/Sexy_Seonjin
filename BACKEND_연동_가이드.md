# 백엔드 연동 가이드 — `POST /evaluate` 입력 필드

정책 판단 엔진(`main.py`)이 받는 `PaymentRequestIn`의 각 필드를 백엔드가
**실제로 어느 시점에, 어디서 값을 채워서 보내야 하는지** 정리한 문서.
`project.md`의 최종 아키텍처(1~10단계) 기준.

## 요청 예시

```
POST /evaluate
Content-Type: application/json

{
  "amount": 0.005,
  "category": "gemini",
  "wallet_connected": true,
  "wallet_balance": 4.2,
  "api_key_valid": true,
  "recipient_address": "실제_공식_솔라나_주소",
  "request_id": "req-3f2a9c",
  "ai_consecutive_failures": 0,
  "has_required_permission": true,
  "infra_stable": true,
  "user_prompt": "Gemini API로 이미지 설명 생성해줘",
  "task_plan": "1) BigQuery로 이번 주 매출 집계, 2) Gemini로 요약문 생성, 3) 요약문 이미지 캡션 첨부"
}
```

## 필드별 상세

| 필드 | 타입 | 기본값 | 값을 채우는 시점 / 출처 |
|---|---|---|---|
| `amount` | float | (필수) | **아키텍처 3단계** — Google Cloud가 402(x402) 응답으로 돌려준 확정 가격. 추정치 아님, 반드시 402 응답값 그대로. |
| `category` | string | (필수) | 어떤 Google Cloud API를 호출했는지 (`"gemini"`, `"bigquery"`). 백엔드가 402 요청을 보낼 때 이미 알고 있는 값. |
| `wallet_connected` | bool | `true` | Solana 지갑 세션/키페어 로드 여부. 백엔드가 트랜잭션 서명 전에 지갑 클라이언트 상태를 확인해서 채움. **생략하면 항상 "연결됨"으로 간주되므로 반드시 실제값을 보내야 함.** |
| `wallet_balance` | float | `0.0` | 트랜잭션 서명 직전, 온체인 잔액 조회(RPC `getBalance` 등)로 확인한 USDC 잔액. 기본값이 0이라 **안 보내면 항상 "잔액부족"으로 거부됨** — 반드시 채워야 함. |
| `api_key_valid` | bool | `true` | 호출하려는 Google Cloud API(Gemini/BigQuery)의 키/등록 상태. 402 요청이 인증 오류 없이 성공했다면 `true`로 판단 가능. |
| `recipient_address` | string | `""` | 백엔드가 실제 트랜잭션을 보내려는 대상 주소(서명 전 구성한 트랜잭션의 수신 주소). 엔진이 `PolicyConfig.official_recipient_addresses`와 대조함. |
| `request_id` | string | `""` | 이 결제 요청의 멱등성 키. 백엔드가 사용자 액션(버튼 클릭 등) 1회당 고유 ID를 생성해서 보냄 (예: UUID). 같은 ID 재전송 시 자동 거부(조건7)됨. |
| `ai_consecutive_failures` | int | `0` | 백엔드(또는 AI 로직 호출부)가 유지하는 연속 실패 카운터. Gemini 호출 실패, 트랜잭션 실패 등이 반복될 때 증가시켜서 전달. |
| `has_required_permission` | bool | `true` | 사용자/세션이 해당 카테고리 API를 쓸 권한이 있는지(OAuth 스코프, 프로젝트 권한 등). 백엔드의 인증 계층에서 판단. |
| `infra_stable` | bool | `true` | Solana RPC 노드 / Google Cloud 상태 확인 결과 (헬스체크, RPC 응답 지연 등). 불안정하면 `false`로 보내 자동 거부 유도. |
| `user_prompt` | string \| null | `""` | 이 특정 호출을 왜 하는지, 호출 직전에 AI가 스스로 생성한 근거 텍스트. 사람이 입력한 문장이 아니라 AI가 매 호출마다 재생성하는 justification. **비워서 보내면 레이어2(의미 판단)가 통째로 생략됨.** |
| `task_plan` | string \| null | `""` | **작업 시작 시점(외부 콘텐츠를 읽기 전)에 한 번 확정된 전체 작업 계획.** 백엔드/오케스트레이팅 AI가 사용자 요청을 받자마자 "이 작업을 위해 어떤 호출들을 할 것인지" 계획을 세워서, 그 계획 문자열을 이후 모든 하위 호출에 동일하게 실어 보냄. 레이어2가 `user_prompt`(개별 호출 근거)를 이 계획과 대조해서, 계획에 없는 단계를 시도하면 범위 이탈로 판단하는 데 씀. 인젝션은 보통 실행 도중(외부 콘텐츠를 읽는 시점)에 발생하므로, 미리 고정된 계획과 대조하면 방어에 도움이 됨. |

## 주의할 점

- **기본값 = "통과"** 설계다. 필드를 안 보내면 대부분 "이상 없음"으로 간주되어 레이어1을 그냥 통과한다.
  데모/실연동에서는 위 표의 실제 상태값을 최대한 채워서 보내야 정책 판단이 의미가 있다.
- `recipient_address` 검사(조건6)는 `PolicyConfig.official_recipient_addresses`에 공식 주소가
  등록된 카테고리에서만 작동한다. 현재 `main.py`에 `TODO_GEMINI_공식_솔라나_주소` /
  `TODO_BIGQUERY_공식_솔라나_주소` 플레이스홀더 상태라, 실제 주소를 받기 전까지는
  이 검사가 사실상 항상 통과한다.
- `user_prompt`가 있어도 레이어2(Gemini) 호출 자체가 실패하면(`GEMINI_API_KEY` 미설정 등)
  조건14(인프라 불안정)로 간주되어 **자동 거부**된다 (fail-safe).
- `task_plan`은 선택 필드다 (없어도 동작함, 그 경우 레이어2는 계획 대조 없이 `user_prompt`만 보고 판단).
  **레이어2는 AI가 스스로 쓴 근거 텍스트를 믿는 구조라 완전히 조작된 거짓 근거까지는 못 잡는다.**
  실제 금전적 피해 상한은 항상 레이어1의 하드 규칙(`per_tx_limit`, `daily_limit`)이 결정하므로,
  레이어2를 최종 방어선으로 취급하면 안 된다.

## 응답 형식

```json
{
  "approved": true,
  "amount": 0.005,
  "category": "gemini",
  "reason": "모든 정책 통과",
  "policy_check": ["category_ok", "api_registered_ok", "wallet_connected_ok", "balance_ok",
                    "recipient_ok", "rate_limit_ok", "ai_stability_ok", "permission_ok",
                    "infra_stable_ok", "per_tx_limit_ok", "daily_limit_ok", "call_count_ok",
                    "in_scope_ok", "prompt_injection_ok", "sensitive_info_ok", "goal_clear_ok"],
  "timestamp": "2026-07-30T12:34:56.789012",
  "spent_today_after": 0.005
}
```

`approved: false`면 백엔드는 6단계(지갑 서명·전송)로 넘어가지 않고, `reason`을 그대로
사용자에게 거부 알림으로 전달하면 된다 (아키텍처 5단계).
