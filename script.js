// "use strict": 자바스크립트를 좀 더 엄격한 규칙으로 실행하게 하는 선언.
// 예를 들어 선언 안 한 변수에 값을 넣으면(오타 등) 조용히 넘어가지 않고 바로 에러를 내준다.
"use strict";


/*
    Team JAC AutoPay Agent

    실제 백엔드 주소의 엔드포인트가 준비되면
    설정창에 백엔드 주소를 입력합니다.

    현재 사용 엔드포인트

    GET  /health
    POST /execute
*/


/*
    ── 이 파일 읽을 때 헷갈리기 쉬운 JS 문법 요약 ──

    const x = ...       "x라는 이름에 값을 한 번 붙이고, 다시는 다른 값으로 안 바꾼다"는 선언.
                         (파이썬의 그냥 변수 대입과 비슷하지만, 재대입을 문법으로 막아준다)

    () => { ... }        "화살표 함수". function(){...}을 짧게 쓰는 문법. 이 파일 전체에서
                         이벤트 콜백(버튼 눌렀을 때 실행할 함수)으로 계속 나온다.

    `문자열 ${값}`        "템플릿 리터럴". 문자열 안에 변수 값을 바로 끼워넣는 문법.
                         파이썬의 f"문자열 {값}" 이랑 똑같은 역할.

    async / await        "이 함수는 기다리는(비동기) 작업이 있다"는 표시(async)와,
                         "그 작업이 끝날 때까지 여기서 기다려라"는 표시(await).
                         파이썬의 async/await와 개념이 동일하다.

    obj?.prop            "옵셔널 체이닝". obj가 null/undefined면 에러 안 내고 그냥
                         undefined를 돌려준다. obj.prop이라고만 쓰면 obj가 없을 때 에러남.

    a ?? b               "널리시 코얼레싱". a가 null 또는 undefined일 때만 b를 쓴다
                         (a가 0이나 빈 문자열이면 a를 그대로 씀 — a || b 와는 미묘하게 다름).

    { ...obj }            "스프레드(전개)". 객체나 배열의 내용물을 풀어서 새 객체/배열에
                         복사해 넣는 문법. { ...a, ...b }는 a와 b를 합친 새 객체를 만든다.

    const { x, y } = obj  "구조분해할당". obj.x, obj.y를 각각 x, y라는 이름으로 한 번에 꺼낸다.
*/


/* =========================
   화면 요소 가져오기
========================= */

// document.getElementById("아이디"): index.html에 있는 그 id의 태그를 찾아서
// 자바스크립트 객체로 돌려준다. 아래는 전부 "이 화면 요소를 나중에 조작하려고
// 미리 이름 붙여서 저장해두는" 반복 패턴이다 (하나하나 다른 로직이 있는 게 아님).
const goalInput =
    document.getElementById("goalInput");

const characterCount =
    document.getElementById("characterCount");

const inputMessage =
    document.getElementById("inputMessage");

const startButton =
    document.getElementById("startButton");

const startButtonText =
    document.getElementById("startButtonText");


const emptyProcess =
    document.getElementById("emptyProcess");

const processList =
    document.getElementById("processList");

const processBadge =
    document.getElementById("processBadge");

// document.querySelectorAll(".process-step"): 그 CSS 클래스를 가진 요소를 "전부" 찾는다
// (getElementById는 하나만, querySelectorAll은 여러 개를 돌려줌). 근데 그 결과가
// 진짜 배열이 아니라 배열 비슷한 객체(NodeList)라서, 대괄호 안에 [...그것]으로
// "스프레드"해서 진짜 배열로 바꿔준다 — 그래야 .forEach, .find 같은 배열 메서드를 자유롭게 씀.
const processSteps =
    [...document.querySelectorAll(".process-step")];


const successResult =
    document.getElementById("successResult");

const rejectedResult =
    document.getElementById("rejectedResult");

const errorResult =
    document.getElementById("errorResult");


const resultAnswer =
    document.getElementById("resultAnswer");

const resultService =
    document.getElementById("resultService");

const resultAmount =
    document.getElementById("resultAmount");

const resultPaymentStatus =
    document.getElementById("resultPaymentStatus");

const resultRequestId =
    document.getElementById("resultRequestId");

const transactionSignature =
    document.getElementById("transactionSignature");

const policyCheckList =
    document.getElementById("policyCheckList");


const rejectionReason =
    document.getElementById("rejectionReason");

const errorMessage =
    document.getElementById("errorMessage");

const retryButton =
    document.getElementById("retryButton");

const copyTransactionButton =
    document.getElementById("copyTransactionButton");


/* 설정창 */

const settingsButton =
    document.getElementById("settingsButton");

const openSettingsFromPolicy =
    document.getElementById("openSettingsFromPolicy");

const settingsOverlay =
    document.getElementById("settingsOverlay");

const closeSettingsButton =
    document.getElementById("closeSettingsButton");

const cancelSettingsButton =
    document.getElementById("cancelSettingsButton");

const saveSettingsButton =
    document.getElementById("saveSettingsButton");


const backendUrlInput =
    document.getElementById("backendUrlInput");

const testApiButton =
    document.getElementById("testApiButton");

const apiConnectionStatus =
    document.getElementById("apiConnectionStatus");


const autoExecuteToggle =
    document.getElementById("autoExecuteToggle");

const perTransactionLimit =
    document.getElementById("perTransactionLimit");

const dailyLimit =
    document.getElementById("dailyLimit");


const allowGemini =
    document.getElementById("allowGemini");


const connectWalletButton =
    document.getElementById("connectWalletButton");

const walletBadge =
    document.getElementById("walletBadge");

const walletBadgeText =
    document.getElementById("walletBadgeText");

const settingsWalletStatus =
    document.getElementById("settingsWalletStatus");

const settingsWalletAddress =
    document.getElementById("settingsWalletAddress");

const settingsWalletBalance =
    document.getElementById("settingsWalletBalance");


const summaryPerTransaction =
    document.getElementById("summaryPerTransaction");

const summaryDailyLimit =
    document.getElementById("summaryDailyLimit");

const summaryServices =
    document.getElementById("summaryServices");

const summaryPaymentMethod =
    document.getElementById("summaryPaymentMethod");


/* =========================
   앱 상태
========================= */

// appState: 이 화면이 지금 어떤 상태인지(실행중인지, 지갑이 연결됐는지 등)를 전부
// 모아놓은 하나의 객체(딕셔너리 비슷한 것). 이 파일의 함수들이 서로 정보를 주고받을 때
// 전역 변수처럼 이 appState를 통해서 주고받는다.
const appState = {
    running: false,

    walletConnected: false,

    walletPublicKey: "",

    walletBalance: null,

    currentRequestId: "",

    currentTransactionSignature: "",

    settings: {
        // ["a","b"].includes(x): 배열 안에 x가 있는지 true/false로 확인 (파이썬의 "x in [a,b]").
        // 그 뒤의 "조건 ? A : B"는 삼항연산자 — 조건이 참이면 A, 거짓이면 B.
        // 즉 "지금 로컬(개발용)에서 열었으면 로컬 서버 주소를, 아니면 배포된 주소를 써라"는 뜻.
        backendUrl:
            ["localhost", "127.0.0.1"].includes(
                window.location.hostname
            )
                ? "http://localhost:3000"
                : "https://jac-autopay-api.onrender.com",

        autoExecute: true,

        perTransactionLimit: 2,

        dailyLimit: 10,

        allowedServices: {
            gemini: true
        },

        paymentMethod: "phantom"
    }
};


/* =========================
   입력창
========================= */

// addEventListener("이벤트이름", 콜백함수): "이 요소에서 이 이벤트(input=타이핑,
// click=클릭 등)가 발생하면 이 함수를 실행해라"고 등록하는 것. 두 번째 인자로 준
// () => {...}가 화살표 함수(익명 콜백)다 — 미리 이름 붙일 필요 없이 그 자리에서 정의.
goalInput.addEventListener("input", () => {
    characterCount.textContent =
        `${goalInput.value.length} / 500`;

    clearInputMessage();
});


/* =========================
   작업 시작 버튼
========================= */

startButton.addEventListener(
    "click",
    handleStart
);


// 함수 이름 앞의 async: 이 함수 안에서 await(기다리는 작업)를 쓸 거라는 표시.
async function handleStart() {
    if (appState.running) {
        return;
    }

    const goal =
        goalInput.value.trim();

    if (!validateGoal(goal)) {
        return;
    }

    if (!appState.settings.autoExecute) {
        showInputMessage(
            "AI 자동 실행이 꺼져 있습니다. 설정에서 AI 자동 실행을 켜 주세요."
        );

        return;
    }

    if (!appState.settings.backendUrl) {
        showSystemError(
            "백엔드 서버가 설정되지 않았습니다. 설정에서 백엔드 서버 주소를 입력해 주세요."
        );

        openSettings();

        return;
    }

    resetExecutionScreen();

    appState.running = true;

    startButton.disabled = true;
    startButtonText.textContent =
        "작업 실행 중...";

    appState.currentRequestId =
        createRequestId();

    // try/catch/finally: try 안의 코드를 실행하다가 에러(throw)가 나면 catch로 건너뛰고,
    // 성공하든 실패하든 finally는 무조건 마지막에 실행된다. 파이썬의 try/except/finally와
    // 하는 역할이 완전히 같다. 여기서는 "버튼 다시 눌러도 되게 되돌리는 처리(finally)"를
    // 성공/실패 상관없이 항상 해주려고 이 구조를 씀.
    try {
        await executeUserRequest(goal);

    } catch (error) {
        console.error(error);

        showSystemError(
            error.message ||
            "작업을 실행하는 중 알 수 없는 오류가 발생했습니다."
        );

    } finally {
        appState.running = false;

        startButton.disabled = false;

        startButtonText.textContent =
            "다시 실행하기";
    }
}


/* =========================
   실제 백엔드 요청
========================= */

async function executeUserRequest(goal) {
    const backendUrl =
        normalizeBackendUrl(
            appState.settings.backendUrl
        );

    prepareProcess();

    activateProcessStep(0);

    let transactionSignature = null;

    /*
        지갑이 연결되어 있고 결제 방식이 "Phantom Wallet"이면
        실제로 devnet USDC를 서명해서 보낸다.
        (Agent Wallet 방식은 아직 미구현이라 기존 데모 경로로 처리한다.)
    */
    const shouldPayForReal =
        appState.walletConnected &&
        appState.settings.paymentMethod === "phantom";

    completeProcessStep(0);

    activateProcessStep(1);

    /*
        가격은 /config에서 이미 정해져 있으므로 확인 자체는 즉시 끝난다.
        실제 서명·전송을 기다리는 동안에는 4번(결제) 단계를 진행 중으로 보여준다.
    */
    completeProcessStep(1);

    if (shouldPayForReal) {
        activateProcessStep(3);

        try {
            transactionSignature =
                await payForGeminiCall(backendUrl);

            completeProcessStep(3);

        } catch (error) {
            console.error(error);

            failProcessStep(3, "결제 실패");

            throw new Error(
                error.message ||
                "지갑 결제에 실패했습니다."
            );
        }
    }

    const payload = {
        prompt: goal,

        task_plan: goal,

        request_id:
            appState.currentRequestId,

        wallet: {
            connected:
                appState.walletConnected,

            public_key:
                appState.walletPublicKey,

            balance:
                appState.walletBalance
        },

        transaction_signature:
            transactionSignature
    };

    let response;

    // fetch(url, 옵션): 브라우저 내장 함수로, 다른 서버에 HTTP 요청을 보낸다
    // (파이썬의 httpx.post()랑 같은 역할). await를 붙였으니 응답이 올 때까지 여기서 멈춰있다가,
    // 응답이 오면 다음 줄로 넘어간다. JSON.stringify(payload)는 자바스크립트 객체를
    // JSON 텍스트로 바꾸는 함수 (파이썬 json.dumps()와 동일).
    try {
        response = await fetch(
            `${backendUrl}/execute`,
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify(payload)
            }
        );

    } catch (error) {
        failProcessStep(0, "연결 실패");

        throw new Error(
            "백엔드 서버에 연결할 수 없습니다. 서버 주소와 실행 상태를 확인해 주세요."
        );
    }


    let data;

    try {
        data = await response.json();

    } catch (error) {
        failCurrentProcessStep();

        throw new Error(
            "서버 응답 형식이 올바르지 않습니다."
        );
    }


    if (!response.ok) {
        failCurrentProcessStep();

        const message =
            data.reason ||
            data.detail ||
            data.message ||
            `서버 요청이 실패했습니다. HTTP ${response.status}`;

        if (
            response.status === 403 ||
            data.approved === false ||
            data.status === "rejected"
        ) {
            showRejectedResult(message);
            return;
        }

        throw new Error(message);
    }


    /*
        백엔드가 현재 진행 단계를 배열로 보내면
        해당 단계를 화면에 표시합니다.

        예시:
        completed_steps: [
            "request_analysis",
            "price_check",
            "policy_check"
        ]
    */

    updateStepsFromResponse(
        data.completed_steps
    );


    /*
        approved가 false이면
        결제를 진행하지 않고 거절 사유를 표시합니다.
    */

    if (data.approved === false) {
        const rejectedStep =
            getRejectedStepIndex(data);

        failProcessStep(
            rejectedStep,
            "거절"
        );

        showRejectedResult(
            data.reason ||
            "정책 검사에서 요청이 거절되었습니다."
        );

        return;
    }


    if (data.status === "rejected") {
        const rejectedStep =
            getRejectedStepIndex(data);

        failProcessStep(
            rejectedStep,
            "거절"
        );

        showRejectedResult(
            data.reason ||
            "정책 검사에서 요청이 거절되었습니다."
        );

        return;
    }


    completeAllProcessSteps();

    showSuccessResult(data);
}


/* =========================
   입력값 검사
========================= */

function validateGoal(goal) {
    clearInputMessage();

    if (!goal) {
        showInputMessage(
            "실행할 작업 목표를 입력해 주세요."
        );

        goalInput.focus();

        return false;
    }

    if (goal.length < 5) {
        showInputMessage(
            "목표가 너무 짧습니다. 필요한 작업을 조금 더 구체적으로 입력해 주세요."
        );

        goalInput.focus();

        return false;
    }

    return true;
}


function showInputMessage(message) {
    inputMessage.textContent = message;

    inputMessage.classList.add("error");
}


function clearInputMessage() {
    inputMessage.textContent = "";

    inputMessage.classList.remove("error");
}


/* =========================
   진행 화면
========================= */

function resetExecutionScreen() {
    hideAllResults();

    emptyProcess.classList.add("hidden");

    processList.classList.remove("hidden");

    processSteps.forEach((step) => {
        step.classList.remove(
            "active",
            "completed",
            "failed"
        );

        const status =
            step.querySelector(".step-status");

        status.textContent = "대기";
    });

    processBadge.textContent =
        "실행 준비";

    processBadge.className =
        "process-badge waiting";
}


function prepareProcess() {
    emptyProcess.classList.add("hidden");

    processList.classList.remove("hidden");

    processBadge.textContent =
        "실행 중";

    processBadge.className =
        "process-badge running";
}


function activateProcessStep(index) {
    processSteps.forEach(
        (step, stepIndex) => {
            if (
                stepIndex !== index &&
                step.classList.contains("active")
            ) {
                step.classList.remove("active");
            }
        }
    );

    const step =
        processSteps[index];

    if (!step) {
        return;
    }

    step.classList.remove(
        "completed",
        "failed"
    );

    step.classList.add("active");

    step.querySelector(
        ".step-status"
    ).textContent = "진행 중";
}


function completeProcessStep(index) {
    const step =
        processSteps[index];

    if (!step) {
        return;
    }

    step.classList.remove(
        "active",
        "failed"
    );

    step.classList.add("completed");

    step.querySelector(
        ".step-status"
    ).textContent = "완료";
}


function failProcessStep(
    index,
    statusText = "실패"
) {
    const step =
        processSteps[index];

    if (!step) {
        return;
    }

    step.classList.remove(
        "active",
        "completed"
    );

    step.classList.add("failed");

    step.querySelector(
        ".step-status"
    ).textContent = statusText;
}


function failCurrentProcessStep() {
    const activeStep =
        processSteps.find(
            (step) =>
                step.classList.contains("active")
        );

    if (!activeStep) {
        return;
    }

    activeStep.classList.remove("active");

    activeStep.classList.add("failed");

    activeStep.querySelector(
        ".step-status"
    ).textContent = "실패";
}


function completeAllProcessSteps() {
    processSteps.forEach((step) => {
        step.classList.remove(
            "active",
            "failed"
        );

        step.classList.add("completed");

        step.querySelector(
            ".step-status"
        ).textContent = "완료";
    });
}


/* =========================
   백엔드 단계 응답 처리
========================= */

function updateStepsFromResponse(
    completedSteps
) {
    if (!Array.isArray(completedSteps)) {
        return;
    }

    const stepIndexes = {
        request_analysis: 0,
        price_check: 1,
        policy_check: 2,
        payment: 3,
        onchain_confirmation: 4,
        api_execution: 5,
        result_delivery: 6
    };

    completedSteps.forEach(
        (stepName) => {
            const index =
                stepIndexes[stepName];

            if (index !== undefined) {
                completeProcessStep(index);
            }
        }
    );
}


function getRejectedStepIndex(data) {
    const stage =
        data.rejected_stage ||
        data.failed_stage ||
        "policy_check";

    const indexes = {
        request_analysis: 0,
        price_check: 1,
        policy_check: 2,
        payment: 3,
        onchain_confirmation: 4,
        api_execution: 5,
        result_delivery: 6
    };

    return indexes[stage] ?? 2;
}


/* =========================
   성공 결과
========================= */

function showSuccessResult(data) {
    hideAllResults();

    successResult.classList.remove("hidden");

    processBadge.textContent =
        "실행 완료";

    processBadge.className =
        "process-badge success";


    const service =
        data.category ||
        data.service ||
        "확인되지 않음";


    const amount =
        Number(
            data.amount ??
            data.price ??
            0
        );


    const signature =
        data.transaction_signature ||
        data.transactionSignature ||
        "";


    appState.currentTransactionSignature =
        signature;


    const answer =
        data.result ||
        data.answer ||
        data.output ||
        "작업이 완료되었습니다.";

    resultAnswer.textContent = data.demo_mode
        ? `${answer}\n\n[DEMO] 정책 검사는 실행되었지만 USDC는 결제되지 않았습니다.`
        : answer;


    resultService.textContent =
        getServiceName(service);


    resultAmount.textContent =
        `${amount.toFixed(3)} USDC`;


    resultPaymentStatus.textContent =
        convertPaymentStatus(
            data.payment_status ||
            data.paymentStatus ||
            "confirmed"
        );


    resultRequestId.textContent =
        shortenText(
            data.request_id ||
            appState.currentRequestId,
            22
        );


    transactionSignature.textContent =
        signature
            ? shortenAddress(signature)
            : "트랜잭션 정보 없음";


    copyTransactionButton.disabled =
        !signature;


    renderPolicyChecks(
        data.policy_check || []
    );


    successResult.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


/* =========================
   정책 거절 결과
========================= */

function showRejectedResult(reason) {
    hideAllResults();

    rejectedResult.classList.remove("hidden");

    processBadge.textContent =
        "정책 거절";

    processBadge.className =
        "process-badge rejected";

    rejectionReason.textContent = reason;

    rejectedResult.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


/* =========================
   시스템 오류 결과
========================= */

function showSystemError(message) {
    hideAllResults();

    errorResult.classList.remove("hidden");

    processBadge.textContent =
        "실행 실패";

    processBadge.className =
        "process-badge error";

    errorMessage.textContent = message;

    failCurrentProcessStep();

    errorResult.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}


function hideAllResults() {
    successResult.classList.add("hidden");

    rejectedResult.classList.add("hidden");

    errorResult.classList.add("hidden");
}


/* =========================
   결과 버튼
========================= */

retryButton.addEventListener(
    "click",
    () => {
        goalInput.focus();

        window.scrollTo({
            top: 0,
            behavior: "smooth"
        });
    }
);


copyTransactionButton.addEventListener(
    "click",
    async () => {
        if (
            !appState.currentTransactionSignature
        ) {
            return;
        }

        try {
            await navigator.clipboard.writeText(
                appState.currentTransactionSignature
            );

            copyTransactionButton.textContent =
                "복사 완료";

            setTimeout(() => {
                copyTransactionButton.textContent =
                    "복사";
            }, 1500);

        } catch (error) {
            alert(
                "트랜잭션 서명을 복사하지 못했습니다."
            );
        }
    }
);


/* =========================
   설정창 열기와 닫기
========================= */

settingsButton.addEventListener(
    "click",
    openSettings
);

openSettingsFromPolicy.addEventListener(
    "click",
    openSettings
);

closeSettingsButton.addEventListener(
    "click",
    closeSettings
);

cancelSettingsButton.addEventListener(
    "click",
    closeSettings
);


settingsOverlay.addEventListener(
    "click",
    (event) => {
        if (
            event.target === settingsOverlay
        ) {
            closeSettings();
        }
    }
);


function openSettings() {
    backendUrlInput.value =
        appState.settings.backendUrl;

    autoExecuteToggle.checked =
        appState.settings.autoExecute;

    perTransactionLimit.value =
        appState.settings.perTransactionLimit;

    dailyLimit.value =
        appState.settings.dailyLimit;

    allowGemini.checked =
        appState.settings
            .allowedServices
            .gemini;


    const selectedPayment =
        document.querySelector(
            `input[name="paymentMethod"][value="${appState.settings.paymentMethod}"]`
        );

    if (selectedPayment) {
        selectedPayment.checked = true;
    }


    settingsOverlay.classList.remove(
        "hidden"
    );

    document.body.style.overflow =
        "hidden";
}


function closeSettings() {
    settingsOverlay.classList.add(
        "hidden"
    );

    document.body.style.overflow = "";
}


/* =========================
   설정 저장
========================= */

saveSettingsButton.addEventListener(
    "click",
    saveSettings
);


function saveSettings() {
    const oneTimeLimit =
        Number(
            perTransactionLimit.value
        );

    const oneDayLimit =
        Number(
            dailyLimit.value
        );


    if (
        Number.isNaN(oneTimeLimit) ||
        oneTimeLimit < 0
    ) {
        alert(
            "1회 결제 한도를 올바르게 입력해 주세요."
        );

        return;
    }


    if (
        Number.isNaN(oneDayLimit) ||
        oneDayLimit < 0
    ) {
        alert(
            "하루 결제 한도를 올바르게 입력해 주세요."
        );

        return;
    }


    if (
        oneDayLimit <
        oneTimeLimit
    ) {
        alert(
            "하루 결제 한도는 1회 결제 한도보다 작을 수 없습니다."
        );

        return;
    }


    const allowedServiceCount =
        [
            allowGemini.checked
        ].filter(Boolean).length;


    if (
        allowedServiceCount === 0
    ) {
        alert(
            "최소 한 개 이상의 서비스를 허용해야 합니다."
        );

        return;
    }


    const selectedPaymentMethod =
        document.querySelector(
            'input[name="paymentMethod"]:checked'
        )?.value || "phantom";


    appState.settings = {
        backendUrl:
            backendUrlInput.value.trim(),

        autoExecute:
            autoExecuteToggle.checked,

        perTransactionLimit:
            oneTimeLimit,

        dailyLimit:
            oneDayLimit,

        allowedServices: {
            gemini:
                allowGemini.checked
        },

        paymentMethod:
            selectedPaymentMethod
    };


    saveSettingsToLocalStorage();

    updateSettingsSummary();

    closeSettings();
}


/* =========================
   API 연결 테스트
========================= */

testApiButton.addEventListener(
    "click",
    testBackendConnection
);


async function testBackendConnection() {
    const backendUrl =
        normalizeBackendUrl(
            backendUrlInput.value.trim()
        );


    if (!backendUrl) {
        setApiStatus(
            "서버 주소를 입력해 주세요.",
            "warning"
        );

        return;
    }


    setApiStatus(
        "연결 확인 중...",
        "warning"
    );

    testApiButton.disabled = true;


    try {
        const response = await fetch(
            `${backendUrl}/health`,
            {
                method: "GET"
            }
        );


        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}`
            );
        }


        setApiStatus(
            "정상 연결됨",
            "success"
        );

    } catch (error) {
        setApiStatus(
            "연결 실패",
            "error"
        );

    } finally {
        testApiButton.disabled = false;
    }
}


function setApiStatus(
    message,
    type
) {
    apiConnectionStatus.textContent =
        message;

    apiConnectionStatus.className = "";

    if (type === "success") {
        apiConnectionStatus.classList.add(
            "success-status"
        );
    } else if (type === "error") {
        apiConnectionStatus.classList.add(
            "error-status"
        );
    } else {
        apiConnectionStatus.classList.add(
            "warning-text"
        );
    }
}


/* =========================
   Phantom 지갑 연결
========================= */

connectWalletButton.addEventListener(
    "click",
    connectPhantomWallet
);


async function connectPhantomWallet() {
    // window.phantom?.solana : Phantom 확장 프로그램이 설치돼 있으면 브라우저가
    // window.phantom.solana라는 객체를 자동으로 만들어준다. 옵셔널 체이닝(?.)을
    // 써서, 혹시 Phantom이 없어도(window.phantom이 undefined) 에러 없이
    // provider가 그냥 undefined가 되게 만든 것 — 그래서 바로 아래에서 안전하게
    // "!provider"로 설치 여부를 확인할 수 있다.
    const provider =
        window?.phantom?.solana;


    if (!provider) {
        alert(
            "Phantom 지갑 확장 프로그램을 찾을 수 없습니다. Phantom을 설치한 다음 시도해 주세요."
        );

        return;
    }


    connectWalletButton.disabled = true;

    connectWalletButton.textContent =
        "지갑 연결 중...";


    try {
        const response =
            await provider.connect();


        appState.walletConnected = true;

        appState.walletPublicKey =
            response.publicKey.toString();


        /*
            실제 USDC 잔액은 Solana RPC 또는
            백엔드 서버를 통해 조회해야 합니다.
        */

        appState.walletBalance =
            await requestWalletBalance();


        updateWalletDisplay();

    } catch (error) {
        console.error(error);

        alert(
            "지갑 연결이 취소되었거나 실패했습니다."
        );

    } finally {
        connectWalletButton.disabled = false;

        updateWalletButtonText();
    }
}


/* =========================
   실제 devnet USDC 결제 (Phantom 서명)
========================= */

/*
    @solana/web3.js, @solana/spl-token은 이 프로젝트에 번들러가 없어서
    esm.sh(CDN이 npm 패키지를 브라우저용 ES 모듈로 바로 변환해 주는 서비스)에서
    동적으로 불러온다. 버전을 고정해서 나중에 CDN 쪽이 업데이트돼도 깨지지 않게 한다.
    spl-token 쪽에 ?deps=...로 web3.js 버전을 강제로 맞춰줘야 두 모듈이 같은
    PublicKey/Transaction 클래스를 쓰게 되어(안 맞으면 instanceof 검사가 깨질 수 있음).
*/
// 이 함수를 여러 번 호출해도 패키지를 두 번 다운로드하지 않도록, 한 번 시작한
// "다운로드 중이라는 약속"(Promise)을 이 변수에 저장해두고 재사용한다.
let _solanaLibrariesPromise = null;

function loadSolanaLibraries() {
    if (!_solanaLibrariesPromise) {
        // import("주소"): 자바스크립트 코드를 "그 순간에" 인터넷에서 내려받아 실행하는
        // 문법 (동적 import). 파일 맨 위에 쓰는 보통의 import와 달리, 함수 안에서
        // 필요할 때만 불러올 수 있고, 결과가 Promise(비동기 값)로 나온다.
        // Promise.all([...]) : 여러 개의 Promise(여기선 두 패키지 다운로드)를 동시에
        // 시작해서, 전부 끝날 때까지 기다렸다가 결과를 배열로 모아준다.
        // .then(([web3, splToken]) => ...) : 다 끝나면 그 결과 배열을
        // [web3, splToken]으로 "구조분해"해서 이름 붙이고, 객체 { web3, splToken }로
        // (짧게 쓴 표현. { web3: web3, splToken: splToken }과 같은 뜻) 정리해서 돌려준다.
        _solanaLibrariesPromise = Promise.all([
            import(
                "https://esm.sh/@solana/web3.js@1.98.4"
            ),
            import(
                "https://esm.sh/@solana/spl-token@0.4.15?deps=@solana/web3.js@1.98.4"
            )
        ]).then(([web3, splToken]) => ({ web3, splToken }));
    }

    return _solanaLibrariesPromise;
}


/*
    실제로 devnet USDC를 우리 결제 서버의 수신 지갑으로 전송하는 트랜잭션을
    만들어 Phantom으로 서명·전송하고, 확정될 때까지 기다린 뒤 트랜잭션
    서명(signature)을 돌려준다. 이 값을 /execute에 그대로 실어 보내면
    백엔드가 온체인에서 직접 재검증한다 (main.py의 verify_onchain_usdc_payment).
*/
async function payForGeminiCall(backendUrl) {
    const provider =
        window?.phantom?.solana;

    if (!provider || !appState.walletPublicKey) {
        throw new Error(
            "Phantom 지갑이 연결되어 있지 않습니다. 먼저 지갑을 연결해 주세요."
        );
    }

    const configResponse = await fetch(
        `${backendUrl}/config`
    );

    if (!configResponse.ok) {
        throw new Error(
            "결제 설정 정보를 서버에서 가져오지 못했습니다."
        );
    }

    const config = await configResponse.json();

    // 방금 만든 loadSolanaLibraries()가 { web3, splToken } 객체를 돌려주는데,
    // 그걸 바로 두 개의 변수 web3, splToken으로 "구조분해"해서 꺼낸다.
    const { web3, splToken } =
        await loadSolanaLibraries();

    // "new 클래스이름(인자들)": 그 클래스의 새 인스턴스(객체)를 만드는 문법
    // (파이썬의 클래스이름(인자들)과 같은 역할. 파이썬엔 new가 따로 없을 뿐).
    // web3.Connection: 이 주소의 Solana RPC 노드에 접속하는 창구 하나를 만듦.
    const connection = new web3.Connection(
        config.rpc_url,
        "confirmed"
    );

    // web3.PublicKey: 문자열로 된 지갑/토큰 주소를 실제 "주소 객체"로 감싸는 것.
    // Solana 라이브러리 함수들은 문자열이 아니라 이 PublicKey 객체를 인자로 요구한다.
    const mint =
        new web3.PublicKey(config.usdc_mint);

    const recipient =
        new web3.PublicKey(
            config.recipient_address
        );

    const payer =
        new web3.PublicKey(
            appState.walletPublicKey
        );

    // getAssociatedTokenAddress: "이 지갑이 이 토큰(mint)을 보관하는 계좌 주소가
    // 어디인지" 계산해서 알려주는 함수. Solana에서 USDC 같은 토큰은 지갑 주소랑
    // 별도로, 토큰 종류마다 전용 보관함(ATA) 주소가 따로 있다.
    const senderAta =
        await splToken.getAssociatedTokenAddress(
            mint,
            payer
        );

    const recipientAta =
        await splToken.getAssociatedTokenAddress(
            mint,
            recipient
        );

    // USDC는 소수점 6자리라서, "0.005 달러"를 실제 전송 단위로 바꾸려면
    // 0.005 * 10^6 = 5000을 만들어야 한다. 10 ** config.decimals는 "10의 decimals제곱".
    const amountRaw =
        Math.round(
            config.price_usd *
            10 ** config.decimals
        );

    // 트랜잭션(거래) 객체를 하나 만든다. 아래에서 여기에 "무엇을 할지"를
    // 담은 명령어(instruction)들을 순서대로 추가해나간다.
    const transaction =
        new web3.Transaction();

    transaction.add(
        /*
            수신 지갑의 USDC 계좌(ATA)가 아직 없어도 여기서 같이 만들어 준다.
            이미 있으면 아무 일도 안 하는 안전한(idempotent) 명령이다.
        */
        splToken.createAssociatedTokenAccountIdempotentInstruction(
            payer,
            recipientAta,
            recipient,
            mint
        )
    );

    transaction.add(
        splToken.createTransferCheckedInstruction(
            senderAta,
            mint,
            recipientAta,
            payer,
            amountRaw,
            config.decimals
        )
    );

    // Solana 트랜잭션은 "최근 블록해시"를 반드시 포함해야 한다 (오래된 트랜잭션이
    // 나중에 뒤늦게 실행되는 걸 막는 안전장치). 그 값을 RPC에서 받아와서
    // 구조분해로 blockhash, lastValidBlockHeight 두 값을 한 번에 꺼낸다.
    const {
        blockhash,
        lastValidBlockHeight
    } = await connection.getLatestBlockhash();

    transaction.recentBlockhash = blockhash;
    transaction.feePayer = payer;

    // 여기서 실제로 Phantom 확장 프로그램에 "이 트랜잭션에 서명해서 네트워크로
    // 보내줘"라고 요청한다 — 이 순간 화면에 Phantom 승인 팝업이 뜬다.
    // { signature } = ... 도 구조분해: 반환된 객체에서 signature 값만 꺼낸다.
    const { signature } =
        await provider.signAndSendTransaction(
            transaction
        );

    // 블록체인에 제출됐다고 바로 끝난 게 아니라, "확정"(confirmed)될 때까지 좀
    // 기다려야 한다. 이 줄이 그 확정을 기다리는 부분.
    await connection.confirmTransaction(
        {
            signature,
            blockhash,
            lastValidBlockHeight
        },
        "confirmed"
    );

    return signature;
}


/* =========================
   지갑 잔액 조회
========================= */

async function requestWalletBalance() {
    if (!appState.settings.backendUrl) {
        return null;
    }


    const backendUrl =
        normalizeBackendUrl(
            appState.settings.backendUrl
        );


    // encodeURIComponent: 문자열 안에 URL에서 특별한 의미를 가지는 문자(&, ? 등)가
    // 있어도 안전하게 주소창에 넣을 수 있도록 이스케이프 처리해주는 내장 함수.
    try {
        const response = await fetch(
            `${backendUrl}/wallet/balance?address=${encodeURIComponent(
                appState.walletPublicKey
            )}`
        );


        if (!response.ok) {
            return null;
        }


        const data =
            await response.json();


        // a ?? b : a가 null/undefined면 b를 쓴다. 여기서는 백엔드가 usdc_balance라는
        // 이름으로 보내든 balance라는 이름으로 보내든 상관없이 값을 받으려는 방어 코드.
        const balance =
            Number(
                data.usdc_balance ??
                data.balance
            );


        return Number.isNaN(balance)
            ? null
            : balance;

    } catch (error) {
        console.warn(
            "지갑 잔액을 조회하지 못했습니다.",
            error
        );

        return null;
    }
}


function updateWalletDisplay() {
    if (appState.walletConnected) {
        walletBadge.className =
            "wallet-badge connected";

        walletBadgeText.textContent =
            "지갑 연결됨";

        settingsWalletStatus.textContent =
            "연결됨";

        settingsWalletAddress.textContent =
            shortenAddress(
                appState.walletPublicKey
            );


        if (
            typeof appState.walletBalance ===
            "number"
        ) {
            settingsWalletBalance.textContent =
                `${appState.walletBalance.toFixed(3)} USDC`;
        } else {
            settingsWalletBalance.textContent =
                "잔액 확인 필요";
        }

    } else {
        walletBadge.className =
            "wallet-badge disconnected";

        walletBadgeText.textContent =
            "지갑 연결 안 됨";

        settingsWalletStatus.textContent =
            "연결 안 됨";

        settingsWalletAddress.textContent =
            "-";

        settingsWalletBalance.textContent =
            "확인되지 않음";
    }


    updateWalletButtonText();
}


function updateWalletButtonText() {
    connectWalletButton.textContent =
        appState.walletConnected
            ? "지갑 연결 완료"
            : "Phantom 지갑 연결";
}


/* =========================
   설정 요약
========================= */

function updateSettingsSummary() {
    summaryPerTransaction.textContent =
        `${appState.settings.perTransactionLimit} USDC`;

    summaryDailyLimit.textContent =
        `${appState.settings.dailyLimit} USDC`;


    const services = [];


    if (
        appState.settings
            .allowedServices
            .gemini
    ) {
        services.push("Gemini");
    }


    summaryServices.textContent =
        services.join(", ");


    summaryPaymentMethod.textContent =
        appState.settings.paymentMethod ===
        "agent"
            ? "Agent Wallet"
            : "Phantom Wallet";
}


/* =========================
   로컬 저장
========================= */

function saveSettingsToLocalStorage() {
    localStorage.setItem(
        "jacAutoPaySettings",
        JSON.stringify(
            appState.settings
        )
    );
}


function loadSettingsFromLocalStorage() {
    const savedSettings =
        localStorage.getItem(
            "jacAutoPaySettings"
        );


    if (!savedSettings) {
        return;
    }


    try {
        const parsed =
            JSON.parse(savedSettings);


        // { ...a, ...b } : a의 모든 항목을 먼저 채워넣고, 그 위에 b의 항목들을
        // 덮어씌운 "새 객체"를 만든다. 즉 "기본 설정에, 저장해뒀던 값이 있으면
        // 그걸로 덮어써라"는 뜻. (원본 appState.settings 자체를 바꾸는 게 아니라
        // 완전히 새 객체를 만들어서 appState.settings에 다시 대입하는 것)
        appState.settings = {
            ...appState.settings,
            ...parsed,

            allowedServices: {
                ...appState.settings
                    .allowedServices,

                ...(
                    parsed.allowedServices ||
                    {}
                )
            }
        };

    } catch (error) {
        console.warn(
            "저장된 설정을 불러오지 못했습니다.",
            error
        );
    }
}


/* =========================
   정책 검사 목록
========================= */

function renderPolicyChecks(checks) {
    policyCheckList.innerHTML = "";


    if (
        !Array.isArray(checks) ||
        checks.length === 0
    ) {
        const chip =
            document.createElement("span");

        chip.className =
            "policy-chip";

        chip.textContent =
            "정책 검사 통과";

        policyCheckList.appendChild(chip);

        return;
    }


    checks.forEach((check) => {
        const chip =
            document.createElement("span");

        chip.className =
            "policy-chip";

        chip.textContent =
            convertPolicyName(check);

        policyCheckList.appendChild(chip);
    });
}


function convertPolicyName(check) {
    const names = {
        category_ok:
            "서비스 허용",

        api_registered_ok:
            "API 등록",

        wallet_connected_ok:
            "지갑 연결",

        balance_ok:
            "잔액 확인",

        recipient_ok:
            "수신 주소 확인",

        rate_limit_ok:
            "요청 횟수",

        ai_stability_ok:
            "AI 안정성",

        permission_ok:
            "접근 권한",

        infra_stable_ok:
            "인프라 상태",

        per_tx_limit_ok:
            "1회 한도",

        daily_limit_ok:
            "일일 한도",

        call_count_ok:
            "호출 횟수",

        in_scope_ok:
            "프로젝트 범위",

        prompt_injection_ok:
            "인젝션 검사",

        sensitive_info_ok:
            "민감정보 검사",

        goal_clear_ok:
            "목표 명확성"
    };


    return names[check] || check;
}


/* =========================
   보조 함수
========================= */

function createRequestId() {
    // crypto.randomUUID(): 브라우저가 기본 제공하는, 절대 안 겹치는 고유 ID 생성기
    // (파이썬의 uuid.uuid4()랑 같은 역할). 아주 오래된 브라우저는 이게 없을 수도
    // 있어서, typeof로 "진짜 함수인지" 확인하고 없으면 아래의 수동 생성 방식으로 대체한다.
    if (
        window.crypto &&
        typeof crypto.randomUUID ===
        "function"
    ) {
        return crypto.randomUUID();
    }


    return (
        "req-" +
        Date.now() +
        "-" +
        Math.random()
            .toString(16)
            .slice(2)
    );
}


function normalizeBackendUrl(url) {
    // /\/+$/ 는 정규표현식(regex): "문자열 맨 끝에 있는 슬래시(/) 하나 이상"을 뜻한다.
    // .replace(그 패턴, "")로 그 부분을 지워서, "http://a/"든 "http://a"든
    // 항상 끝에 슬래시 없는 형태로 통일시킨다.
    return url.replace(/\/+$/, "");
}


function getServiceName(service) {
    const names = {
        gemini: "Gemini"
    };


    return names[
        String(service).toLowerCase()
    ] || service;
}


function convertPaymentStatus(status) {
    const statuses = {
        confirmed: "결제 완료",
        completed: "결제 완료",
        success: "결제 완료",
        pending: "확인 중",
        failed: "결제 실패",
        demo_not_charged: "데모 모드 · 미결제",
        not_charged_demo: "데모 모드 · 미결제"
    };


    return statuses[
        String(status).toLowerCase()
    ] || status;
}


function shortenAddress(value) {
    if (!value) {
        return "-";
    }


    if (value.length <= 20) {
        return value;
    }


    return (
        value.slice(0, 10) +
        "..." +
        value.slice(-8)
    );
}


function shortenText(
    value,
    maximumLength
) {
    if (!value) {
        return "-";
    }


    if (
        value.length <=
        maximumLength
    ) {
        return value;
    }


    return (
        value.slice(
            0,
            maximumLength - 3
        ) + "..."
    );
}


/* =========================
   처음 실행
========================= */

function initializeApp() {
    loadSettingsFromLocalStorage();

    updateSettingsSummary();

    updateWalletDisplay();

    characterCount.textContent =
        `${goalInput.value.length} / 500`;
}


initializeApp();
