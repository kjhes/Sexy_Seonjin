# Pay.sh CLI 연동 검증

Google Cloud × Solana Foundation이 만든 공식 결제 CLI([solana-foundation/pay](https://github.com/solana-foundation/pay))로
우리 x402 결제 서버(`main.py`)를 실제 devnet 결제까지 검증한 기록.

## ⚠️ README에 낚이지 말 것

`solana-foundation/pay`의 공식 README는 `npm install -g @solana/pay`로 설치하라고 되어 있는데,
**그 npm 패키지는 완전히 다른(구버전) "Solana Pay" QR결제 라이브러리다.** 실제 CLI는 GitHub Releases의
프리빌트 바이너리로 받아야 한다.

## 설치 (Windows)

```bash
# 최신 릴리스에서 Windows용 바이너리 받기 (버전은 바뀔 수 있음, 릴리스 페이지 확인)
curl -sL -o pay-win.zip https://github.com/solana-foundation/pay/releases/download/pay-v0.26.0/pay-x86_64-pc-windows-msvc.zip
curl -sL -o sha256sums.txt https://github.com/solana-foundation/pay/releases/download/pay-v0.26.0/sha256sums.txt

# 체크섬 검증 (권장)
sha256sum pay-win.zip
grep x86_64-pc-windows-msvc.zip sha256sums.txt   # 위 해시랑 일치하는지 눈으로 대조

# 압축 풀면 pay.exe 하나 나옴. 이 폴더(tools/paysh/)에 두면 됨 (.gitignore에 이미 등록됨 — 커밋 안 됨)
```

macOS는 `brew install pay`, Linux는 릴리스의 `pay-x86_64-unknown-linux-gnu.tar.gz` 사용.

## 계정 설정 (헤드리스, Windows Hello 팝업 없이)

`pay setup`은 기본적으로 OS 생체인증(Windows Hello 등) 팝업을 띄우는데, 터미널/CI 환경에선 그게
안 되므로 `--backend file`로 대신 로컬 파일에 키를 저장한다. 이미 갖고 있는 Solana 키페어 JSON
(예: `demo_wallet.json`, 표준 64바이트 배열 형식)을 그대로 가져올 수 있다:

```bash
./pay.exe account import <계정이름> <키페어.json 경로> --backend file
./pay.exe account default <계정이름>   # 기본 계정으로 지정하면 매번 --account 안 붙여도 됨
```

계정마다 **네트워크별로 별도 키가 자동 생성**된다 (`account list`로 확인하면 `mainnet:`/`devnet:`
버킷이 따로 뜸). 우리 서버가 Solana Devnet만 쓰므로, `devnet:` 버킷에 뜬 주소로 devnet SOL·USDC를
받아야 실제 결제가 된다.

## 실제 검증 결과

우리 서버(`main.py`)는 x402 프로토콜을 표준대로 구현하고 있어서, **서버 코드를 하나도 안 고치고**
Pay.sh 공식 CLI가 바로 결제할 수 있었다:

```bash
./pay.exe curl -X POST http://localhost:3000/api/gemini \
  -H "Content-Type: application/json" \
  -d '{"prompt":"국내 가족 여행지 3곳을 추천해줘.","task_plan":"국내 가족 여행지 3곳 추천"}'
```

- `pay`가 402 응답에서 `network: solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`(devnet)을 정확히 인식
- 우리 서버와 동일한 공식 facilitator(`x402.org`)로 결제 검증 성공
- 최종 승인 후 실제 devnet USDC가 온체인에서 이동함을 잔액 조회로 확인
  (결제 지갑 0.035 → 0.03 USDC, 수신 지갑 → +0.005 USDC)
- 정책 엔진(`policy_server.py`) 16개 조건 전부 통과, 진짜 Gemini 응답까지 정상 수신

결론: **Pay.sh는 별도 프로토콜이 아니라 x402를 감싼 CLI라서, x402를 표준대로 구현한 우리 서버와
서버 측 변경 없이 곧바로 상호운용된다.**
