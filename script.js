"use strict";


/*
    Team JAC AutoPay Agent

    실제 백엔드 주소의 엔드포인트가 준비되면
    설정창에 백엔드 주소를 입력합니다.

    현재 사용 엔드포인트

    GET  /health
    POST /execute
*/


/* =========================
   화면 요소 가져오기
========================= */

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

const processSteps =
    [...document.querySelectorAll(".process-step")];

const chainProgress =
    document.getElementById("chainProgress");

const chainProgressText =
    document.getElementById("chainProgressText");


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

const appState = {
    running: false,

    walletConnected: false,

    walletPublicKey: "",

    walletBalance: null,

    currentRequestId: "",

    currentTransactionSignature: "",

    settings: {
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
   실제 백엔드 요청 (다단계 계획 자동 진행)
========================= */

/*
    목표가 뭉툭하면 백엔드(main.py)가 첫 호출에서 이를 구체적인 단계
    리스트(plan_steps)로 분해하고, 각 단계가 충족됐는지(plan_step_status)를
    돌려준다. 전부 충족(task_complete=true)될 때까지 next_prompt로
    자동으로 다음 결제+호출을 이어간다 (demo_chain.py와 동일한 방식).

    안전장치: MAX_CHAIN_STEPS에 도달하면 강제로 멈춘다.
*/
const MAX_CHAIN_STEPS = 5;

async function executeUserRequest(goal) {
    const backendUrl =
        normalizeBackendUrl(
            appState.settings.backendUrl
        );

    let prompt = goal;
    let planSteps = null;
    let planStepStatus = null;

    const stepAnswers = [];
    let totalAmount = 0;
    let lastData = null;

    for (
        let stepNumber = 1;
        stepNumber <= MAX_CHAIN_STEPS;
        stepNumber += 1
    ) {
        updateChainProgress(
            stepNumber,
            planSteps,
            planStepStatus
        );

        const data = await runSingleChainCall(
            backendUrl,
            prompt,
            planSteps,
            planStepStatus,
            stepNumber
        );

        if (data === null) {
            // runSingleChainCall이 이미 거절/오류 화면을 띄우고 중단한 경우
            return;
        }

        lastData = data;
        totalAmount += Number(data.amount ?? 0);

        stepAnswers.push({
            stepNumber,
            answer:
                data.answer ||
                "실행 결과가 없습니다."
        });

        planSteps =
            data.plan_steps || planSteps;

        planStepStatus =
            data.plan_step_status || planStepStatus;

        updateChainProgress(
            stepNumber,
            planSteps,
            planStepStatus
        );

        if (data.task_complete) {
            completeAllProcessSteps();

            showSuccessResult(
                lastData,
                stepAnswers,
                totalAmount
            );

            return;
        }

        if (!data.next_prompt) {
            // task_complete=false인데 next_prompt가 없으면 안전하게 중단한다.
            completeAllProcessSteps();

            showSuccessResult(
                lastData,
                stepAnswers,
                totalAmount
            );

            return;
        }

        prompt = data.next_prompt;
    }

    // 안전장치: 최대 단계 수에 도달해 강제로 종료한다.
    completeAllProcessSteps();

    showSuccessResult(
        lastData,
        stepAnswers,
        totalAmount
    );
}


/*
    체인의 한 단계(결제 1회 + /execute 호출 1회)를 처리한다.
    거절되거나 오류가 나면 해당 결과 화면을 직접 띄우고 null을 반환한다
    (호출부가 루프를 멈추라는 신호).
*/
async function runSingleChainCall(
    backendUrl,
    prompt,
    planSteps,
    planStepStatus,
    stepNumber
) {
    resetProcessStepsForNextChainStep();

    prepareProcess();

    processBadge.textContent =
        planSteps && planSteps.length > 1
            ? `${stepNumber}/${planSteps.length}단계 진행 중`
            : "실행 중";

    processBadge.className =
        "process-badge running";

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

            showSystemError(
                stepNumber > 1
                    ? `${stepNumber}단계 결제 중 오류: ${error.message || "지갑 결제에 실패했습니다."}`
                    : error.message || "지갑 결제에 실패했습니다."
            );

            return null;
        }
    }

    // 매 단계마다 새 request_id를 써야 한다 (같은 id를 재사용하면 중복 결제로 거절됨).
    appState.currentRequestId =
        createRequestId();

    const payload = {
        prompt,

        request_id:
            appState.currentRequestId,

        plan_steps: planSteps,

        plan_step_status: planStepStatus,

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

        showSystemError(
            "백엔드 서버에 연결할 수 없습니다. 서버 주소와 실행 상태를 확인해 주세요."
        );

        return null;
    }


    let data;

    try {
        data = await response.json();

    } catch (error) {
        failCurrentProcessStep();

        showSystemError(
            "서버 응답 형식이 올바르지 않습니다."
        );

        return null;
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
            showRejectedResult(
                stepNumber > 1
                    ? `${stepNumber}단계에서 거부됨: ${message}`
                    : message
            );

            return null;
        }

        showSystemError(message);

        return null;
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

    if (
        data.approved === false ||
        data.status === "rejected"
    ) {
        const rejectedStep =
            getRejectedStepIndex(data);

        failProcessStep(
            rejectedStep,
            "거절"
        );

        showRejectedResult(
            stepNumber > 1
                ? `${stepNumber}단계에서 거부됨: ${data.reason || "정책 검사에서 요청이 거절되었습니다."}`
                : data.reason || "정책 검사에서 요청이 거절되었습니다."
        );

        return null;
    }

    return data;
}


function resetProcessStepsForNextChainStep() {
    processSteps.forEach((step) => {
        step.classList.remove(
            "active",
            "completed",
            "failed"
        );

        step.querySelector(
            ".step-status"
        ).textContent = "대기";
    });
}


function updateChainProgress(
    stepNumber,
    planSteps,
    planStepStatus
) {
    if (!planSteps || planSteps.length <= 1) {
        chainProgress.classList.add("hidden");
        return;
    }

    chainProgress.classList.remove("hidden");

    chainProgressText.textContent =
        planSteps
            .map((step, index) => {
                const done =
                    planStepStatus &&
                    planStepStatus[index];

                return `${done ? "✓" : "○"} ${index + 1}. ${step}`;
            })
            .join("\n");
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

    chainProgress.classList.add("hidden");
    chainProgressText.textContent = "";

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

function showSuccessResult(
    data,
    stepAnswers = null,
    totalAmount = null
) {
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
        totalAmount !== null
            ? totalAmount
            : Number(
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


    /*
        여러 단계에 걸쳐 이어진 체인이면(stepAnswers.length > 1)
        각 단계 답변을 번호와 함께 이어붙여서 보여준다.
        단일 단계면 기존처럼 답변만 그대로 보여준다.
    */
    const answer =
        stepAnswers && stepAnswers.length > 1
            ? stepAnswers
                .map(
                    (item) =>
                        `[${item.stepNumber}단계]\n${item.answer}`
                )
                .join("\n\n")
            : (stepAnswers && stepAnswers[0]?.answer) ||
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
let _solanaLibrariesPromise = null;

function loadSolanaLibraries() {
    if (!_solanaLibrariesPromise) {
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

    const { web3, splToken } =
        await loadSolanaLibraries();

    const connection = new web3.Connection(
        config.rpc_url,
        "confirmed"
    );

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

    const amountRaw =
        Math.round(
            config.price_usd *
            10 ** config.decimals
        );

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

    const {
        blockhash,
        lastValidBlockHeight
    } = await connection.getLatestBlockhash();

    transaction.recentBlockhash = blockhash;
    transaction.feePayer = payer;

    const { signature } =
        await provider.signAndSendTransaction(
            transaction
        );

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
