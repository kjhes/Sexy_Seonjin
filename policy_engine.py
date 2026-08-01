"""
정책 판단 엔진 (Layer 1 - 하드 규칙)
B트랙: Autonomous On-chain Settlement

역할: 백엔드가 결제 실행 전에, 이 엔진한테 물어봐서
     승인/거부 판단을 받는다.

보안 담당 파트너가 정의한 거부 조건 중 결정론적으로(상태값만으로) 판단 가능한
2,3,4,5,6,7,8,9,13,14번을 이 레이어에서 처리한다.
의미 판단이 필요한 1,10,11,12번은 semantic_layer.py (Layer 2)에서 처리한다.

입력: PaymentRequest (금액, 카테고리 + 백엔드가 넘겨주는 상태값들)
출력: {approved, reason, amount, category, policy_check, timestamp, spent_today_after}
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from time import monotonic


# ----------------------------
# 1. 정책 설정 (정보보안 친구가 넘겨준 스펙으로 교체할 부분)
# ----------------------------
@dataclass
class PolicyConfig:
    daily_limit: float = 10.0          # 일일 총 한도 (USDC 기준)
    per_tx_limit: float = 1.0          # 건당 한도
    allowed_categories: list = field(default_factory=lambda: ["gemini"])  # 허용 카테고리
    max_calls_per_day: int = 50        # 일일 최대 호출 횟수 (승인된 건만 카운트)
    max_calls_per_minute: int = 5      # 조건 8: 분당 요청 과다 방지
    max_consecutive_ai_failures: int = 3   # 조건 9: AI 반복 실패 임계치
    official_recipient_addresses: dict = field(default_factory=dict)  # 조건 6: {category: 공식주소}


# ----------------------------
# 2. 결제 판단 요청 입력
# ----------------------------
@dataclass
class PaymentRequest:
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
    user_prompt: str = ""              # Layer 2(semantic_layer.py)로 전달될 원본 요청
    task_plan: str = ""                # 작업 시작 시점에 확정된 계획 (Layer 2에서 일치 여부 대조용)


# ----------------------------
# 3. 지출 추적기 (메모리 기반, 데모용. 실서비스면 DB로 교체)
# ----------------------------
class SpendTracker:
    def __init__(self):
        self._today = date.today()
        self._spent_today = 0.0
        self._calls_today = 0

    def _reset_if_new_day(self):
        if date.today() != self._today:
            self._today = date.today()
            self._spent_today = 0.0
            self._calls_today = 0

    def get_spent_today(self) -> float:
        self._reset_if_new_day()
        return self._spent_today

    def get_calls_today(self) -> int:
        self._reset_if_new_day()
        return self._calls_today

    def record(self, amount: float):
        self._reset_if_new_day()
        self._spent_today += amount
        self._calls_today += 1


# ----------------------------
# 4. 요청 방어 추적기 (조건 7: 중복결제, 조건 8: 요청 과다)
# ----------------------------
class RequestGuard:
    def __init__(self, rate_window_seconds: float = 60.0, dedup_window_seconds: float = 300.0):
        self._rate_window = rate_window_seconds
        self._dedup_window = dedup_window_seconds
        self._call_timestamps = deque()
        self._seen_requests = {}

    def _prune(self, now: float):
        while self._call_timestamps and now - self._call_timestamps[0] > self._rate_window:
            self._call_timestamps.popleft()
        expired = [rid for rid, ts in self._seen_requests.items() if now - ts > self._dedup_window]
        for rid in expired:
            del self._seen_requests[rid]

    def is_duplicate(self, request_id: str) -> bool:
        """같은 request_id가 최근에 이미 들어왔으면 True (버튼 두 번 누름 등)"""
        if not request_id:
            return False
        now = monotonic()
        self._prune(now)
        if request_id in self._seen_requests:
            return True
        self._seen_requests[request_id] = now
        return False

    def register_call_and_check_rate(self, max_calls_per_minute: int) -> bool:
        """호출 시도를 기록하고, 최근 1분 내 허용 횟수를 넘었으면 True(초과) 리턴"""
        now = monotonic()
        self._prune(now)
        self._call_timestamps.append(now)
        return len(self._call_timestamps) > max_calls_per_minute


# ----------------------------
# 5. 정책 판단 엔진 본체
# ----------------------------
class PolicyEngine:
    def __init__(self, config: PolicyConfig, tracker: SpendTracker, guard: RequestGuard):
        self.config = config
        self.tracker = tracker
        self.guard = guard

    def evaluate(self, request: PaymentRequest) -> dict:
        # 조건 7: 중복 결제 - 다른 체크보다 먼저 걸러서 지출 상태 오염을 막는다
        if self.guard.is_duplicate(request.request_id):
            return self._result(
                approved=False,
                request=request,
                checks=["duplicate_request_fail"],
                reasons=["이미 처리된 요청입니다. 중복 결제를 방지하기 위해 거부되었습니다."],
            )

        checks = []
        reasons = []
        approved = True

        def fail(code: str, message: str):
            nonlocal approved
            approved = False
            checks.append(code)
            reasons.append(message)

        def ok(code: str):
            checks.append(code)

        # 조건 2: 카테고리 화이트리스트
        if request.category not in self.config.allowed_categories:
            fail(
                "category_fail",
                f"{request.category} 사용이 허용되지 않았습니다. 설정에서 해당 서비스를 허용한 후 다시 시도해 주세요.",
            )
        else:
            ok("category_ok")

        # 조건 3: API 등록/연결 여부
        if not request.api_key_valid:
            fail(
                "api_not_registered_fail",
                f"{request.category} API가 등록되지 않았거나 연결되지 않았습니다. API 키를 확인한 후 다시 시도해 주세요.",
            )
        else:
            ok("api_registered_ok")

        # 조건 4: 지갑 연결 여부
        if not request.wallet_connected:
            fail("wallet_not_connected_fail", "지갑이 연결되어 있지 않습니다. 지갑을 연결한 후 다시 시도해 주세요.")
        else:
            ok("wallet_connected_ok")

        # 조건 5: 지갑 잔액 부족 (지갑이 연결된 경우에만 의미 있음)
        if request.wallet_connected and request.wallet_balance < request.amount:
            fail(
                "insufficient_balance_fail",
                f"지갑 잔액이 부족합니다. (필요 금액: {request.amount}, 보유 잔액: {request.wallet_balance})",
            )
        else:
            ok("balance_ok")

        # 조건 6: 결제 대상 주소 - 카테고리별 공식 주소가 등록된 경우에만 검사
        official_address = self.config.official_recipient_addresses.get(request.category)
        if official_address and request.recipient_address != official_address:
            fail("recipient_mismatch_fail", "허용되지 않은 결제 대상입니다. 등록된 공식 주소와 일치하지 않습니다.")
        else:
            ok("recipient_ok")

        # 조건 8: 요청 횟수 과다 (분당 레이트리밋, 승인 여부와 무관하게 모든 시도 카운트)
        if self.guard.register_call_and_check_rate(self.config.max_calls_per_minute):
            fail("rate_limit_fail", "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요.")
        else:
            ok("rate_limit_ok")

        # 조건 9: AI가 계속 실패/반복하는 경우
        if request.ai_consecutive_failures >= self.config.max_consecutive_ai_failures:
            fail("ai_repeated_failure_fail", "AI가 반복적으로 실패하고 있어 요청을 중단합니다. 잠시 후 다시 시도해 주세요.")
        else:
            ok("ai_stability_ok")

        # 조건 13: 필요 데이터/권한 부재
        if not request.has_required_permission:
            fail("missing_permission_fail", "필요한 데이터 접근 권한이 없습니다. 권한을 확인한 후 다시 시도해 주세요.")
        else:
            ok("permission_ok")

        # 조건 14: API/블록체인 상태 불안정
        if not request.infra_stable:
            fail("infra_unstable_fail", "API 또는 블록체인 상태가 불안정합니다. 잠시 후 다시 시도해 주세요.")
        else:
            ok("infra_stable_ok")

        # 건당 한도
        if request.amount > self.config.per_tx_limit:
            fail("per_tx_limit_fail", f"건당 한도 초과: {request.amount} > {self.config.per_tx_limit}")
        else:
            ok("per_tx_limit_ok")

        # 일일 누적 한도
        spent_today = self.tracker.get_spent_today()
        projected_total = spent_today + request.amount
        if projected_total > self.config.daily_limit:
            fail("daily_limit_fail", f"일일 한도 초과: 누적예상 {projected_total} > {self.config.daily_limit}")
        else:
            ok("daily_limit_ok")

        # 일일 호출 횟수 (이상 패턴 최소 방어, 승인된 건만 카운트)
        calls_today = self.tracker.get_calls_today()
        if calls_today >= self.config.max_calls_per_day:
            fail("call_count_fail", f"일일 호출 횟수 초과: {calls_today} >= {self.config.max_calls_per_day}")
        else:
            ok("call_count_ok")

        if approved:
            reasons.append("모든 정책 통과")

        return self._result(approved, request, checks, reasons)

    def commit(self, amount: float) -> float:
        """실제 지출로 기록한다.

        evaluate()는 여기서 더 이상 자동으로 커밋하지 않는다 — 이 레이어(하드 규칙)만
        통과했다고 지출을 확정해버리면, 뒤이어 Layer 2(의미 판단)가 거부했을 때도 이미
        하루 한도가 깎여버리는 문제가 있었다 (승인 안 된 요청이 예산을 갉아먹음).
        그래서 호출 측(policy_server.py)이 Layer 1 + Layer 2 모두 통과를 확인한
        뒤에만 이 메서드를 명시적으로 호출해서 커밋한다.

        Returns:
            커밋 후의 spent_today_after.
        """
        self.tracker.record(amount)
        return self.tracker.get_spent_today()

    def _result(self, approved: bool, request: PaymentRequest, checks: list, reasons: list) -> dict:
        return {
            "approved": approved,
            "amount": request.amount,
            "category": request.category,
            "reason": " / ".join(reasons),
            "policy_check": checks,
            "timestamp": datetime.now().isoformat(),
            "spent_today_after": self.tracker.get_spent_today(),
        }


# ----------------------------
# 6. 테스트 케이스 (Day1 검증용)
# 각 시나리오는 독립된 엔진을 써서 조건 하나만 격리해서 검증한다.
# (분당 레이트리밋/일일누적처럼 상태를 공유해야 하는 시나리오만 예외)
# ----------------------------
if __name__ == "__main__":
    OFFICIAL_ADDRESS = "OFFICIAL_GEMINI_ADDRESS"

    def evaluate_and_commit(engine: PolicyEngine, request: PaymentRequest) -> dict:
        """policy_server.py의 실제 흐름(Layer1 평가 -> 승인이면 커밋)을 그대로 흉내낸 테스트용 헬퍼.
        (실제 서비스에서는 Layer2까지 통과해야 커밋하지만, 여기는 Layer1 단독 테스트라 Layer1
        승인 시점에 바로 커밋한다.)"""
        result = engine.evaluate(request)
        if result["approved"]:
            result["spent_today_after"] = engine.commit(request.amount)
        return result

    def make_engine(max_calls_per_minute: int = 5) -> PolicyEngine:
        cfg = PolicyConfig(
            daily_limit=10.0,
            per_tx_limit=2.0,
            allowed_categories=["gemini", "bigquery"],
            max_calls_per_day=50,
            max_calls_per_minute=max_calls_per_minute,
            max_consecutive_ai_failures=3,
            official_recipient_addresses={"gemini": OFFICIAL_ADDRESS, "bigquery": "OFFICIAL_BIGQUERY_ADDRESS"},
        )
        return PolicyEngine(cfg, SpendTracker(), RequestGuard())

    print("=== 테스트 1: 정상 승인 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.005, category="gemini", wallet_balance=5.0,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-1",
    )))
    print()

    print("=== 테스트 2: 건당 한도 초과 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=5.0, category="gemini", wallet_balance=5.0,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-2",
    )))
    print()

    print("=== 테스트 3: 허용 안 된 카테고리 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.005, category="translate", wallet_balance=5.0,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-3",
    )))
    print()

    print("=== 테스트 4: 지갑 미연결 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.005, category="gemini", wallet_connected=False,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-4",
    )))
    print()

    print("=== 테스트 5: 지갑 잔액 부족 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.5, category="gemini", wallet_balance=0.1,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-5",
    )))
    print()

    print("=== 테스트 6: 결제 대상 주소 불일치 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.005, category="gemini", wallet_balance=5.0,
        recipient_address="SOME_OTHER_ADDRESS", request_id="req-6",
    )))
    print()

    print("=== 테스트 7: 중복 결제 (같은 request_id 재요청) ===")
    engine = make_engine()
    dup_request = PaymentRequest(
        amount=0.005, category="gemini", wallet_balance=5.0,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-7",
    )
    print(evaluate_and_commit(engine, dup_request))
    print(evaluate_and_commit(engine, dup_request))
    print()

    print("=== 테스트 8: AI 반복 실패 ===")
    engine = make_engine()
    print(evaluate_and_commit(engine, PaymentRequest(
        amount=0.005, category="gemini", wallet_balance=5.0,
        ai_consecutive_failures=3, recipient_address=OFFICIAL_ADDRESS, request_id="req-8",
    )))
    print()

    print("=== 테스트 9: 분당 요청 과다 (레이트리밋) ===")
    engine = make_engine(max_calls_per_minute=5)
    for i in range(1, 8):
        r = evaluate_and_commit(engine, PaymentRequest(
            amount=0.005, category="gemini", wallet_balance=5.0,
            recipient_address=OFFICIAL_ADDRESS, request_id=f"req-rate-{i}",
        ))
        print(f"{i}번째: approved={r['approved']}, checks={r['policy_check']}")
    print()

    print("=== 테스트 10: 일일 한도 초과 시뮬레이션 ===")
    engine = make_engine(max_calls_per_minute=1000)  # 레이트리밋이 아니라 일일한도만 보기 위해 넉넉히 설정
    for i in range(1, 15):
        r = evaluate_and_commit(engine, PaymentRequest(
            amount=0.8, category="gemini", wallet_balance=100.0,
            recipient_address=OFFICIAL_ADDRESS, request_id=f"req-daily-{i}",
        ))
        if not r["approved"]:
            print(f"{i}번째 호출에서 거부됨: {r['reason']}")
            break
        else:
            print(f"{i}번째 호출 승인, 누적: {r['spent_today_after']}")

    print()
    print("=== 테스트 11 (버그 재현): Layer1 승인 + Layer2 거부 시 지출이 이중으로 안 깎이는지 ===")
    engine = make_engine()
    layer1_only = engine.evaluate(PaymentRequest(
        amount=0.005, category="gemini", wallet_balance=5.0,
        recipient_address=OFFICIAL_ADDRESS, request_id="req-11",
    ))
    print("Layer1 평가 직후 (아직 커밋 안 함):", layer1_only["approved"], "spent_today:", engine.tracker.get_spent_today())
    print("-> Layer2가 거부했다고 가정하고 commit()을 호출하지 않으면:")
    print("   spent_today_after:", engine.tracker.get_spent_today(), "(0.0이어야 정상. 이전엔 여기서 0.005가 이미 깎여 있었음)")
