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

# collections.deque: 양쪽 끝에서 추가/삭제가 빠른 리스트 비슷한 자료구조.
# "최근 1분간의 요청 시간들"처럼 앞에서 오래된 것부터 계속 빼내야 하는 경우에
# 일반 list보다 훨씬 빠르다 (list.pop(0)은 느리지만 deque.popleft()는 빠름).
from collections import deque
# dataclass: "필드 이름과 타입만 나열하면" __init__ 같은 반복 코드를 자동으로
# 만들어주는 데코레이터. field(default_factory=...)는 기본값을 "미리 만들어둔 값"이
# 아니라 "필요할 때마다 새로 만드는 함수"로 지정하는 보조 도구
# (리스트/딕셔너리처럼 여러 객체가 기본값을 공유하면 안 되는 타입에 필요).
from dataclasses import dataclass, field
# date: 오늘 날짜(연-월-일)만 다룰 때. datetime: 날짜+시각까지 다룰 때.
from datetime import date, datetime
# time.monotonic(): "항상 증가하기만 하는 시계"에서 현재 값을 초 단위로 가져온다.
# 시스템 시각(예: date.today())과 달리 사용자가 시계를 바꿔도 영향받지 않아서,
# "1분 이내에 몇 번 왔는지" 같은 시간 간격 계산엔 이게 더 안전하다.
from time import monotonic


# ----------------------------
# 1. 정책 설정 (정보보안 친구가 넘겨준 스펙으로 교체할 부분)
# ----------------------------
# @dataclass를 클래스 위에 붙이면, 아래 나열한 필드들로 자동으로
# PolicyConfig(daily_limit=10.0, per_tx_limit=1.0, ...) 같은 생성자를 만들어준다.
# "필드이름: 타입 = 기본값" 형태로 쓰면 그 필드가 기본값을 가진다.
@dataclass
class PolicyConfig:
    daily_limit: float = 10.0          # 일일 총 한도 (USDC 기준)
    per_tx_limit: float = 1.0          # 건당 한도
    # list나 dict를 기본값으로 바로 "= []"처럼 쓰면 파이썬에서 위험하다(모든 인스턴스가
    # 같은 리스트를 공유해버림). 그래서 field(default_factory=lambda: [...])로 "이 함수를
    # 호출해서 매번 새 리스트를 만들어라"라고 지정한다. lambda는 이름 없는 한 줄짜리 함수.
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
    # 기본값이 없는 필드(amount, category)는 반드시 값을 넣어줘야 하고,
    # dataclass에서는 기본값 없는 필드를 기본값 있는 필드보다 먼저 써야 한다.
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
# 여기부터는 일반 클래스(class SpendTracker:). dataclass가 아니라서 __init__을
# 직접 손으로 써준다. self는 "지금 만들어지고 있는(또는 쓰이고 있는) 이 객체 자신"을
# 가리키는 관례적인 이름 — 파이썬 메서드는 항상 첫 번째 인자로 self를 받는다.
class SpendTracker:
    def __init__(self):
        # self._today처럼 밑줄(_)로 시작하는 이름은 "이 클래스 내부에서만 쓰는
        # 값이니 바깥에서 직접 건드리지 마라"는 관례적인 표시(강제되진 않음).
        self._today = date.today()
        self._spent_today = 0.0
        self._calls_today = 0

    def _reset_if_new_day(self):
        # 이 메서드가 호출된 시점의 날짜가 마지막으로 기록한 날짜랑 다르면
        # (자정을 넘겨서 하루가 바뀌었으면) 지출/횟수를 0으로 리셋한다.
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
        self._call_timestamps = deque()   # 최근 요청들의 "시각" 기록 (분당 과다 요청 체크용)
        self._seen_requests = {}          # {request_id: 처음 본 시각} (중복 요청 체크용)

    def _prune(self, now: float):
        """오래돼서 더 이상 의미 없는 기록들을 지운다 (메모리가 계속 쌓이지 않게)."""
        # deque의 맨 왼쪽(가장 오래된 것)이 시간 창(rate_window)보다 오래됐으면 계속 버린다.
        while self._call_timestamps and now - self._call_timestamps[0] > self._rate_window:
            self._call_timestamps.popleft()
        # 딕셔너리 컴프리헨션 비슷한 리스트 컴프리헨션: "for rid, ts in ... 를 돌면서
        # 조건을 만족하는 rid만 모아 새 리스트를 만들어라"는 한 줄 문법.
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
        # 생성자에서 넘겨받은 세 객체를 self.xxx에 저장해서, 이 클래스의 다른
        # 메서드(evaluate, commit 등)에서도 self.config처럼 계속 꺼내 쓸 수 있게 한다.
        self.config = config
        self.tracker = tracker
        self.guard = guard

    def evaluate(self, request: PaymentRequest) -> dict:
        # 조건 7: 중복 결제 - 다른 체크보다 먼저 걸러서 지출 상태 오염을 막는다
        if self.guard.is_duplicate(request.request_id):
            # 여기서 바로 return하면 아래 코드는 실행되지 않고 함수가 즉시 끝난다
            # ("조기 반환"). 중복이면 다른 조건을 더 볼 필요도 없으니 바로 거부.
            return self._result(
                approved=False,
                request=request,
                checks=["duplicate_request_fail"],
                reasons=["이미 처리된 요청입니다. 중복 결제를 방지하기 위해 거부되었습니다."],
            )

        checks = []      # 통과/실패한 조건 이름들을 순서대로 쌓는 리스트 (예: "category_ok")
        reasons = []     # 거부된 이유 문장들을 쌓는 리스트
        approved = True  # 하나라도 실패하면 False로 바뀔 최종 승인 여부

        # 함수 안에 함수를 정의하는 것(중첩 함수, 클로저)이다. fail/ok는 evaluate()
        # 밖에서는 못 쓰고, 이 evaluate() 호출 동안만 존재한다. 매번 "approved = False;
        # checks.append(...); reasons.append(...)" 세 줄을 반복해서 쓰는 대신
        # fail("코드", "메시지") 한 줄로 줄이려고 만든 것.
        def fail(code: str, message: str):
            # nonlocal: "이 approved는 바깥(evaluate 함수)에 있는 그 변수를 그대로
            # 가리켜서 수정하겠다"는 선언. 이게 없으면 파이썬은 fail() 함수 안에
            # 새로운 지역변수 approved를 만들려다가 에러를 낸다.
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

        # 일일 누적 한도 — "지금까지 쓴 돈 + 이번 요청 금액"이 한도를 넘는지 미리 계산해본다.
        # (아직 tracker에 실제로 기록하는 게 아니라, "만약 승인하면 얼마가 될지" 예측만 함)
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
            # " / ".join(reasons): 리스트 안의 문자열들을 " / "로 이어붙여 한 문장으로 만듦.
            # 예: ["a", "b"] -> "a / b"
            "reason": " / ".join(reasons),
            "policy_check": checks,
            "timestamp": datetime.now().isoformat(),  # ISO 8601 형식의 문자열로 변환
            "spent_today_after": self.tracker.get_spent_today(),
        }


# ----------------------------
# 6. 테스트 케이스 (Day1 검증용)
# 각 시나리오는 독립된 엔진을 써서 조건 하나만 격리해서 검증한다.
# (분당 레이트리밋/일일누적처럼 상태를 공유해야 하는 시나리오만 예외)
#
# if __name__ == "__main__": 은 "이 파일을 다른 곳에서 import할 땐 실행되지 않고,
# `python policy_engine.py`처럼 직접 실행했을 때만 아래 코드가 돈다"는 뜻의 관용구.
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
        # 매 테스트마다 이 함수를 다시 불러서, 이전 테스트의 지출/요청 기록이
        # 섞이지 않는 "깨끗한" SpendTracker/RequestGuard를 새로 만든다.
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
