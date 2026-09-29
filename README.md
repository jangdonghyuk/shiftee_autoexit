# Shiftee 자동 퇴근처리

시프티(shiftee.io) 출퇴근기록에서 **오늘 출근했지만 퇴근이 찍히지 않은 직원**의 퇴근시간을 `18:00`으로 자동 저장합니다.
이미 퇴근이 찍힌 직원은 건드리지 않습니다.

> **운영 방식 변경 (2026-09-29)**
> 로컬 Windows 작업 스케줄러로 돌리던 임시 운영을 종료하고 **ECS Fargate 로 복귀**했습니다.
> 로컬 예약(`shiftee-local-*`)은 전부 삭제됐습니다. 평상시 운영에 로컬 실행은 필요하지 않습니다.

---

## 현재 구성 (ECS Fargate)

| 항목 | 값 |
|---|---|
| 클러스터 | `shiftee-cluster` |
| 작업 정의 | `shiftee-exit-task:3` (1 vCPU / 2 GB) |
| 이미지 | `632852507243.dkr.ecr.ap-northeast-2.amazonaws.com/shiftee-exit:latest` |
| 비밀값 | Secrets Manager `shiftee/exit` → `SHIFTEE_EMAIL`, `SHIFTEE_PW` |
| 실행 역할 | `shiftee-exit-exec-role` (ECR pull, Secrets 읽기) |
| 작업 역할 | `shiftee-exit-task-role` (SES 발송 전용) |
| 보안그룹 | `shiftee-exit-task-sg` (인바운드 없음, 아웃바운드만) |
| 정기 실행 | `shiftee-exit-schedule` — 평일 **17:59** (Asia/Seoul) |
| 백업 실행 | `shiftee-exit-schedule-backup` — 평일 **18:10** (Asia/Seoul) |
| 스케줄러 역할 | `shiftee-exit-scheduler-role` |
| 결과 메일 | SES → `sharpspear@bizcube-sol.com`, `help@bizcube-sol.com` |
| 실행 로그 | CloudWatch Logs `/ecs/shiftee-exit-task` |

### 실행 시각을 17:59로 잡은 이유

처리에 1분 30초~2분이 걸립니다. 17:58에 시작하면 마지막 직원이 17:59:5x에 저장돼 18:00 직전에 끝납니다.
17:59에 시작하면 18:00을 넘겨 끝납니다. **저장되는 값은 실행 시각과 무관하게 항상 `18:00` 고정**입니다.

### 백업 실행(18:10)이 있는 이유

인앱 재시도는 *직원 단위* 실패만 막습니다. 실행 전체가 죽는 경우는 막지 못합니다.
2026-08-04에 로그인 직후 페이지 로딩이 멈춰 **그날 0명이 처리**됐고, 당시 백업 실행이 없어 구제되지 못했습니다.
18:10 실행은 17:59가 통째로 실패했을 때만 의미가 있고, 정상 처리된 날에는 **"대상 없음" 메일 1통**만 추가로 옵니다.

---

## 처리 규칙

1. 시프티 로그인 → 출퇴근 목록 페이지 이동 → 표가 렌더될 때까지 대기
2. **오늘 날짜** 행 중 퇴근시간이 비어 있는 직원을 대상으로 선정
3. 직원별로 모달을 열어 "현재 근무중" 해제 → 퇴근시간 `18:00` 입력 → 저장
4. **저장 검증**: 목록 행에 `- 18:00` 이 실제로 반영됐는지 다시 읽어 확인. 확인될 때만 성공 처리
5. 실패한 직원만 모아 **최대 3회 재시도** (총 4회). 재시도 전 페이지를 새로고침해 stale element 상태를 리셋
6. 재시도 시 이미 퇴근이 찍혀 있으면 직전 시도의 성공으로 간주하고 다시 처리하지 않음
7. 결과를 HTML 메일로 발송

### 제외 대상

- **구민성** (`EXCLUDE_EMPLOYEES`) — 최고 관리자라 이 계정 권한으로는 편집 모달이 열리지 않습니다.
  자동 처리가 불가능하므로 매일 **시프티에서 직접 18:00으로 수정**해야 합니다. 메일에 `🔒 제외`로 표시됩니다.
- 휴가자는 별도 규칙이 없습니다. 출근 기록이 없으면 목록에 잡히지 않아 자연히 제외됩니다.

### 공휴일

**자동 제외 규칙이 없습니다.** 평일이면 공휴일에도 실행됩니다.
출근자가 없으면 아무것도 처리하지 않고 "대상 없음" 메일만 발송되므로 부작용은 없습니다.

### 결과 메일

제목만 봐도 상태를 알 수 있습니다.

| 상황 | 제목 |
|---|---|
| 정상 완료 | `[Shiftee] ✅ 퇴근처리 완료 성공 N·스킵 N - 날짜` |
| 일부 실패 | `[Shiftee] ⚠️ 퇴근처리 성공 N·실패 N - 날짜` |
| 처리 대상 없음 | `[Shiftee] 퇴근처리 · 대상 없음 - 날짜` |
| 실행 중단 | `[Shiftee] ⚠️ 퇴근처리 중단 - 날짜` |
| 시험 모드 | `[Shiftee] 🧪 시험(저장 안 함) · …` |

본문에는 성공/스킵/실패 집계, 시도 횟수, 직원 이름, **실패 사유**, 제외 목록이 들어갑니다.

---

## 이미지 갱신 (코드 배포)

> ⚠️ **`fargate/deploy.sh` 는 더 이상 쓰지 않습니다.** 로컬 AWS 액세스 키를 전제로 하는데, 2026-08 키 유출 사고 이후 **계정에 액세스 키가 없습니다.**
> 마찬가지로 `create_schedules.py` 도 키 기반이라 사용하지 않습니다. 스케줄은 콘솔에서 관리합니다.

배포는 **zip → S3 → CodeBuild** 경로로, CodeBuild 서비스 역할 권한으로 ECR 에 push 합니다.

**1. 로컬에서 `fargate/` 폴더를 zip 으로 압축** (`Dockerfile`, `main.py`, `requirements.txt`, `buildspec.yml` 포함)

```powershell
Compress-Archive -Path fargate\* -DestinationPath shiftee-src.zip -Force
```

**2. S3 업로드 + 빌드 시작** — AWS CloudShell 에서 실행

```bash
aws s3 cp shiftee-src.zip s3://shiftee-build-src-632852507243/shiftee-src.zip

aws codebuild start-build \
  --project-name shiftee-exit-build \
  --region ap-northeast-2
```

**3. 빌드 결과 확인**

```bash
aws codebuild list-builds-for-project --project-name shiftee-exit-build \
  --region ap-northeast-2 --max-items 1

aws ecr describe-images --repository-name shiftee-exit --region ap-northeast-2 \
  --image-ids imageTag=latest \
  --query "imageDetails[0].{pushedAt:imagePushedAt,digest:imageDigest}" --output table
```

작업 정의가 `:latest` 태그를 참조하므로 **태스크 정의를 수정할 필요가 없습니다.** 다음 실행부터 새 이미지가 적용됩니다.

---

## 시험 실행 (DRY_RUN)

`DRY_RUN=1` 이면 **로그인 · 대상 계산 · 메일 발송까지만** 하고 `18:00` 저장은 하지 않습니다.
메일 제목에 `🧪 시험(저장 안 함)` 이 붙어 실제 처리와 구분됩니다.

CloudShell 에서:

```bash
aws ecs run-task \
  --cluster shiftee-cluster \
  --task-definition shiftee-exit-task:3 \
  --launch-type FARGATE \
  --region ap-northeast-2 \
  --network-configuration 'awsvpcConfiguration={subnets=[SUBNET_ID],securityGroups=[SG_ID],assignPublicIp=ENABLED}' \
  --overrides '{"containerOverrides":[{"name":"CONTAINER_NAME","environment":[{"name":"DRY_RUN","value":"1"}]}]}'
```

`SUBNET_ID` · `SG_ID` · `CONTAINER_NAME` 은 스케줄과 작업 정의에서 확인합니다.

```bash
aws scheduler get-schedule --name shiftee-exit-schedule --region ap-northeast-2 \
  --query "Target.EcsParameters.NetworkConfiguration.awsvpcConfiguration"

aws ecs describe-task-definition --task-definition shiftee-exit-task:3 \
  --region ap-northeast-2 --query "taskDefinition.containerDefinitions[0].name"
```

> **`assignPublicIp=ENABLED` 는 필수입니다.** 꺼져 있으면 시프티·SES 에 접속하지 못해 조용히 실패합니다.

---

## 로컬 대체 실행 (`login.py`)

ECS 가 실패했거나 AWS 를 쓸 수 없을 때의 **수동 대체 수단**입니다. 평상시에는 쓰지 않습니다.
`fargate/main.py` 의 함수를 그대로 `import` 하므로 처리 로직이 갈라지지 않습니다.
차이는 **브라우저 표시 실행**, **SES 대신 Gmail SMTP**, **터미널 출력 + `logs/날짜.log` 기록**뿐입니다.

`.env` 파일 (git 에 올라가지 않음):

```
SHIFTEE_EMAIL=시프티 로그인 이메일
SHIFTEE_PW=시프티 로그인 비밀번호
SENDER_EMAIL=발신 Gmail 주소
GOOGLE_APP_PASSWORD=Gmail 앱 비밀번호
RECIPIENT_EMAIL=수신 주소 (쉼표로 여러 명)
```

```bash
pip install -r requirements.txt

python login.py          # 처리 대상을 보여주고 엔터를 기다림
python login.py --yes    # 확인 없이 바로 실행
```

**주의:** 스크립트는 **실행하는 그 날짜**의 기록만 처리합니다. 지난 날짜는 되돌려 고칠 수 없으므로 시프티에서 직접 수정해야 합니다.

---

## 코드 구조

| 파일 | 설명 |
|---|---|
| `fargate/main.py` | 처리 로직 본체. ECS 에서 실행되며 `login.py` 도 이 파일을 import 함 |
| `fargate/Dockerfile` | Chrome + Python 이미지 |
| `fargate/buildspec.yml` | CodeBuild 빌드 정의 (ECR push) |
| `fargate/requirements.txt` | `selenium`, `boto3` |
| `login.py` | 로컬 대체 실행 (Gmail SMTP, 브라우저 표시) |
| `fargate/deploy.sh` | **사용 중지** — 로컬 액세스 키 전제 |
| `create_schedules.py` | **사용 중지** — 로컬 액세스 키 전제 |

`fargate/main.py` 주요 상수:

| 이름 | 기본값 | 의미 |
|---|---|---|
| `TARGET_EMPLOYEES` | `[]` | 비어 있으면 오늘 출근한 전직원 대상 |
| `EXCLUDE_EMPLOYEES` | `["구민성"]` | 자동 처리에서 제외할 직원 |
| `MAX_RETRY` | `3` | 1차 실행 후 실패자 재시도 횟수 |
| `PAGE_LOAD_TIMEOUT` | `60` | 페이지 이동 1회당 최대 대기(초) |
| `DRY_RUN` | 환경변수 | `1` 이면 저장하지 않음 |

---

## 장애 이력과 대응

**1. `stale element reference` (2026-06-04)**
저장할 때마다 Angular 테이블이 다시 그려지면서 잡아둔 행 참조가 무효화 → 일부 직원 실패.
→ **재시도 3회 + 재시도 전 새로고침**으로 해결. 이후 재발 없음.

**2. 거짓 성공 (2026-06-10 발견)**
최고 관리자는 클릭해도 모달이 열리지 않는데, 코드가 *요소 존재 여부*만 확인해 예외 없이 통과하고 성공으로 기록.
→ **저장 후 `- 18:00` 반영 검증** 추가 + 해당 직원 **제외 목록** 등록.

**3. `chromedriver ReadTimeout` (2026-08-04)**
로그인 직후 페이지 로딩이 끝나지 않아 ChromeDriver 가 모든 명령에 응답 중단.
Chrome 기본값 `pageLoadStrategy=normal` 은 로딩 완료까지 명령을 대기시키므로, 리소스 하나만 안 끝나도 전체가 멈춥니다.
25분간 헛돌다 **0명 처리**로 종료.
→ `page_load_strategy="eager"`, `set_page_load_timeout(60)`, `wait_for_table()`, 예외 처리 중 `current_url` 재호출 제거.
CPU·메모리 문제가 아니었습니다.

---

## 운영 메모

- **액세스 키를 만들지 않습니다.** 2026-08 키 유출로 Bedrock 부정사용 사고가 있었고 계정에 키가 0개입니다.
  AWS 작업은 **콘솔 + MFA** 또는 **CloudShell** 에서 합니다. 서버는 역할 권한을 씁니다.
- 비밀값은 Secrets Manager `shiftee/exit` 에 있습니다. **작업 정의에 평문 비밀번호를 넣지 않습니다.**
  (평문 환경변수를 쓰던 옛 작업 정의 1·2 는 2026-09-29에 삭제했습니다.)
- 시프티 비밀번호를 교체하면 **Secrets Manager `shiftee/exit` 의 `SHIFTEE_PW` 만 갱신**하면 됩니다. 이미지 재빌드 불필요.
- 실행 로그는 CloudWatch Logs `/ecs/shiftee-exit-task` 에서 확인합니다.
- `.env`, `logs/` 는 git 에 올라가지 않습니다.

---

## 남은 일

- [ ] **옛 IAM 역할 정리** — `shiftee-task-role`(SES 전체 권한), `Amazon_EventBridge_Scheduler_ECS_3449002de7`.
      며칠간 안정 확인 후 삭제.
- [ ] **시프티 비밀번호 교체** — 과거 작업 정의에 평문으로 존재했으므로 노출 가능성이 있습니다.
      교체 후 Secrets Manager `shiftee/exit` 의 `SHIFTEE_PW` 갱신.
- [ ] **공휴일 자동 제외** — 현재 없음. 필요하면 별도 설계 필요.
