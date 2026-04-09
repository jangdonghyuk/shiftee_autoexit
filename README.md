# Shiftee 자동 퇴근처리

시프티(shiftee.io) 출퇴근기록에서 오늘 출근한 직원들의 퇴근시간을 18:00으로 자동 설정하는 프로그램입니다.

이미 퇴근처리된 직원은 자동으로 건너뜁니다.

---

## 사전 준비

### 1. Python 설치

- https://www.python.org/downloads/ 에서 Python 3.10 이상 설치
- 설치 시 **"Add Python to PATH"** 반드시 체크

### 2. Chrome 브라우저

- 최신 버전의 Chrome이 설치되어 있어야 합니다
- ChromeDriver는 자동으로 다운로드됩니다

---

## 설치 방법

### 1. 라이브러리 설치

터미널(명령 프롬프트)을 열고, 이 폴더로 이동한 뒤 아래 명령어를 실행합니다.

```
pip install -r requirements.txt
```

### 2. 환경변수 파일 생성

이 폴더에 `.env` 파일을 만들고 아래 내용을 입력합니다.

```
SHIFTEE_EMAIL=여기에시프티이메일입력
SHIFTEE_PW=여기에시프티비밀번호입력
```

---

## 실행 방법

```
python login.py
```

실행하면 자동으로 진행됩니다.

1. 시프티 로그인
2. 출퇴근 목록 페이지 이동
3. 오늘 출근한 직원 목록 확인
4. 이미 퇴근처리된 직원은 SKIP
5. 나머지 직원 퇴근시간 18:00 설정 후 저장
6. 완료 후 브라우저 자동 종료

---

## 로그

실행 기록은 `logs/` 폴더에 날짜별로 저장됩니다.

```
logs/2026-04-09.log
logs/2026-04-10.log
```

에러가 발생하면 해당 로그 파일을 확인하세요.

---

## ECS Fargate 자동 실행 (월~금 18:00)

`fargate/` 폴더에 자동 실행용 코드가 있습니다. 아래 순서로 AWS 콘솔에서 설정합니다.

### 1. SES (이메일 발송)

1. AWS 콘솔 > SES > Verified identities
2. 발신용 이메일 주소 인증 (Create identity > Email address)
3. 수신자 이메일도 인증 (SES 샌드박스 모드에서는 수신자도 인증 필요)
4. 프로덕션 사용 시 SES 샌드박스 해제 요청

### 2. ECR (Docker 이미지 저장소)

1. AWS 콘솔 > ECR > Create repository
2. 이름: `shiftee-exit`
3. 로컬에서 Docker 이미지 빌드 및 push:

```bash
cd fargate

# ECR 로그인 (리전/계정ID 본인 것으로 변경)
aws ecr get-login-password --region ap-northeast-2 | docker login --username AWS --password-stdin {계정ID}.dkr.ecr.ap-northeast-2.amazonaws.com

# 빌드 & push
docker build -t shiftee-exit .
docker tag shiftee-exit:latest {계정ID}.dkr.ecr.ap-northeast-2.amazonaws.com/shiftee-exit:latest
docker push {계정ID}.dkr.ecr.ap-northeast-2.amazonaws.com/shiftee-exit:latest
```

### 3. ECS Cluster

1. AWS 콘솔 > ECS > Create cluster
2. 이름: `shiftee-cluster`
3. Infrastructure: **AWS Fargate** 선택

### 4. IAM Role (Task용)

1. IAM > Roles > Create role
2. Trusted entity: Elastic Container Service > Elastic Container Service Task
3. 정책 추가: `AmazonSESFullAccess`
4. 이름: `shiftee-task-role`

### 5. Task Definition

1. ECS > Task definitions > Create new task definition
2. 설정:
   - Family: `shiftee-exit-task`
   - Launch type: Fargate
   - CPU: 0.5 vCPU / Memory: 1 GB
   - Task role: `shiftee-task-role`
   - Container:
     - Image: `{계정ID}.dkr.ecr.ap-northeast-2.amazonaws.com/shiftee-exit:latest`
     - 환경변수:
       - `SHIFTEE_EMAIL` = 시프티 로그인 이메일
       - `SHIFTEE_PW` = 시프티 로그인 비밀번호
       - `SES_SENDER` = SES 인증된 발신 이메일
       - `EMAIL_RECIPIENTS` = 수신자 이메일 (쉼표 구분)
       - `AWS_REGION` = ap-northeast-2

### 6. EventBridge 스케줄

1. EventBridge > Rules > Create rule
2. 이름: `shiftee-exit-schedule`
3. Schedule pattern: `cron(0 9 ? * MON-FRI *)` (UTC 09:00 = KST 18:00)
4. Target: ECS task
   - Cluster: `shiftee-cluster`
   - Task definition: `shiftee-exit-task`
   - Launch type: Fargate
   - Subnets / Security group: VPC 기본값 사용, 아웃바운드 허용

### 7. 테스트

현재 `fargate/main.py`는 **장동혁만 대상** (테스트 모드)입니다.

```bash
# 로컬 Docker 테스트
docker build -t shiftee-exit ./fargate
docker run --rm \
  -e SHIFTEE_EMAIL=이메일 \
  -e SHIFTEE_PW=비밀번호 \
  -e SES_SENDER=발신이메일 \
  -e EMAIL_RECIPIENTS=수신이메일 \
  -e AWS_REGION=ap-northeast-2 \
  -e AWS_ACCESS_KEY_ID=키 \
  -e AWS_SECRET_ACCESS_KEY=시크릿 \
  shiftee-exit
```

테스트 확인 후 전직원으로 전환:
- `fargate/main.py`에서 `TARGET_EMPLOYEES = []` 로 변경
- Docker 이미지 다시 빌드 & push

---

## 참고사항

- `.env` 파일은 git에 올라가지 않습니다 (보안)
- `logs/` 폴더도 git에 올라가지 않습니다
- 퇴근시간이 이미 찍혀있는 직원은 자동으로 건너뜁니다
- 에러가 발생한 직원은 건너뛰고 다음 직원으로 계속 진행합니다
- Fargate 실행 로그는 AWS CloudWatch Logs에서 확인 가능합니다
- 실행 결과는 이메일로도 발송됩니다
