"""
로컬 수동/예약 실행용 (AWS 없이 내 PC에서 직접 실행)

fargate/main.py 의 처리 로직과 이메일 템플릿을 그대로 가져다 쓴다.
(복사하지 않으므로 두 코드가 갈라질 일이 없음)

차이점:
  - 브라우저가 눈에 보이게 실행 (headless 아님)
  - 메일은 AWS SES 대신 Gmail SMTP 로 발송
  - logs/날짜.log 파일로도 기록

실행:  python login.py          (처리 대상 확인 후 엔터)
       python login.py --yes    (확인 없이 바로 실행 — 예약 실행용)

.env 필요 항목:
  SHIFTEE_EMAIL / SHIFTEE_PW / SENDER_EMAIL / GOOGLE_APP_PASSWORD / RECIPIENT_EMAIL
"""
import os
import sys
import time
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# .env 에서 시프티 계정 + Gmail 설정 로드
load_dotenv(os.path.join(BASE_DIR, ".env"))

# fargate/main.py 는 SES 환경변수를 필수로 요구하므로 더미로 채운다.
# (SES 는 쓰지 않고 Gmail SMTP 로 보내므로 실제로 사용되지 않음)
os.environ.setdefault("SES_SENDER", "local@local")
os.environ.setdefault("EMAIL_RECIPIENTS", "local@local")

if not os.environ.get("SHIFTEE_EMAIL") or not os.environ.get("SHIFTEE_PW"):
    sys.exit("[중단] .env 에 SHIFTEE_EMAIL / SHIFTEE_PW 가 없습니다.")

sys.path.insert(0, os.path.join(BASE_DIR, "fargate"))
import main as fg  # noqa: E402  (환경변수 세팅 후에 import 해야 함)

from selenium import webdriver  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402

# === 로그 파일 추가 (logs/2026-08-19.log) ===
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_fh = logging.FileHandler(
    os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log"),
    encoding="utf-8",
)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_fh)

log = fg.log


def send_mail_smtp(subject, html_body, text_body):
    """Gmail SMTP 로 결과 메일 발송 (HTML + 텍스트 폴백)"""
    sender = os.environ.get("SENDER_EMAIL")
    pw = os.environ.get("GOOGLE_APP_PASSWORD")
    to = [x.strip() for x in os.environ.get("RECIPIENT_EMAIL", "").split(",") if x.strip()]

    if not (sender and pw and to):
        log.warning("메일 설정(.env: SENDER_EMAIL/GOOGLE_APP_PASSWORD/RECIPIENT_EMAIL) 없음 — 발송 건너뜀")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(sender, pw)
            s.send_message(msg)
        log.info(f"메일 발송 완료: {', '.join(to)}")
        return True
    except Exception as e:
        log.error(f"메일 발송 실패: {e}")
        return False


def main():
    auto_yes = "--yes" in sys.argv

    now = datetime.now()
    today_str = fg.get_today_str()
    today_date = now.strftime("%Y-%m-%d")
    weekday = "월화수목금토일"[now.weekday()]
    run_time = now.strftime("%H:%M")

    success_list, skip_list, exclude_list = [], [], []
    fail_errors = {}
    attempts_used = 0
    had_targets = False
    fatal_error = None
    driver = None

    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--lang=ko-KR")
        # 로딩 멈춤 방지 (fargate 와 동일 설정)
        options.page_load_strategy = "eager"

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(fg.PAGE_LOAD_TIMEOUT)
        wait = WebDriverWait(driver, 20)

        # === 로그인 ===
        driver.get("https://shiftee.io/ko/accounts/login")
        log.info("shiftee 로그인 페이지 로딩 완료")

        email_input = wait.until(
            EC.presence_of_element_located((By.ID, "email-address"))
        )
        email_input.clear()
        email_input.send_keys(os.environ["SHIFTEE_EMAIL"])

        pw_input = driver.find_element(By.ID, "password")
        pw_input.clear()
        pw_input.send_keys(os.environ["SHIFTEE_PW"])

        driver.find_element(
            By.CSS_SELECTOR, "button.btn.btn-primary.btn-block"
        ).click()
        log.info("로그인 시도")

        wait.until(lambda d: "shiftee.io/app" in d.current_url)
        log.info("로그인 성공")

        # === 출퇴근 목록 페이지 ===
        driver.get(fg.TARGET_URL)
        wait.until(lambda d: "attendances" in d.current_url)
        log.info("출퇴근 목록 페이지 도착")
        fg.wait_for_table(driver)
        time.sleep(5)

        # === 오늘 출근자 필터링 (fargate 와 동일 규칙) ===
        today_employees, skip_set = fg.scan_table(driver, today_str)
        targets = fg.TARGET_EMPLOYEES if fg.TARGET_EMPLOYEES else today_employees

        process_list = [e for e in targets
                        if e in today_employees and e not in skip_set
                        and e not in fg.EXCLUDE_EMPLOYEES]
        skip_list = [e for e in targets
                     if e in skip_set and e not in fg.EXCLUDE_EMPLOYEES]
        exclude_list = [e for e in targets if e in fg.EXCLUDE_EMPLOYEES
                        and e in today_employees and e not in skip_set]
        had_targets = bool(process_list)

        print("\n" + "=" * 46)
        print(f"  오늘({today_str}) 출근: {len(today_employees)}명")
        print(f"  처리 대상 : {len(process_list)}명  {', '.join(process_list) or '-'}")
        print(f"  이미 퇴근 : {len(skip_list)}명  {', '.join(skip_list) or '-'}")
        print(f"  제외      : {len(exclude_list)}명  {', '.join(exclude_list) or '-'}")
        print("=" * 46)
        log.info(f"처리 대상 {len(process_list)}명 / SKIP {len(skip_list)}명 / 제외 {len(exclude_list)}명")

        if not had_targets:
            print("\n처리할 직원이 없습니다.\n")
        else:
            if not auto_yes:
                try:
                    input("\n위 '처리 대상'의 퇴근시간을 18:00으로 저장합니다. 엔터=진행 / Ctrl+C=취소 > ")
                except KeyboardInterrupt:
                    print("\n취소했습니다. (메일 발송 안 함)\n")
                    return

            # === 재시도 루프 (fargate 와 동일: 1차 + 최대 MAX_RETRY회) ===
            remaining = list(process_list)
            attempt = 0
            while remaining and attempt < (fg.MAX_RETRY + 1):
                attempt += 1
                attempts_used = attempt

                if attempt > 1:
                    log.info(f"=== 재시도 {attempt - 1}/{fg.MAX_RETRY} : 실패 {len(remaining)}명 재처리 ===")
                    driver.get(fg.TARGET_URL)
                    wait.until(lambda d: "attendances" in d.current_url)
                    fg.wait_for_table(driver)
                    time.sleep(5)
                    # 이미 퇴근 찍힌 사람은 직전 시도에서 사실상 성공
                    _, skip_now = fg.scan_table(driver, today_str)
                    for e in [x for x in remaining if x in skip_now]:
                        log.info(f"  {e} 재확인 결과 이미 퇴근처리됨 → 성공 처리")
                        success_list.append(e)
                        fail_errors.pop(e, None)
                    remaining = [e for e in remaining if e not in skip_now]

                log.info(f"[시도 {attempt}] 처리 대상 {len(remaining)}명")
                next_remaining = []
                for idx, emp_name in enumerate(remaining):
                    log.info(f"  [{idx + 1}/{len(remaining)}] {emp_name} 처리 시작")
                    try:
                        fg.process_one(driver, wait, today_str, emp_name)
                        log.info(f"  {emp_name} 저장완료")
                        success_list.append(emp_name)
                        fail_errors.pop(emp_name, None)
                    except Exception as e:
                        msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
                        log.error(f"  {emp_name} 에러: {msg}")
                        fail_errors[emp_name] = msg
                        next_remaining.append(emp_name)
                        fg.close_modal(driver)
                        time.sleep(2)
                remaining = next_remaining

    except Exception as e:
        fatal_error = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        log.error(f"치명적 에러: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    # === 결과 출력 + 메일 발송 ===
    fail_list = list(fail_errors.keys())
    print("\n" + "=" * 46)
    if fatal_error:
        print(f"  [중단] {fatal_error}")
    print(f"  성공 {len(success_list)}명 / 실패 {len(fail_list)}명"
          f" / 스킵 {len(skip_list)}명  (시도 {attempts_used}회)")
    if success_list:
        print(f"  성공: {', '.join(success_list)}")
    if exclude_list:
        print(f"  제외(수동 처리 필요): {', '.join(exclude_list)}")
    if fail_list:
        print("  실패:")
        for n in fail_list:
            print(f"    - {n}: {fail_errors[n]}")
    print("=" * 46 + "\n")

    subject, html_body, text_body = fg.build_email(
        today_date, weekday, run_time,
        success_list, skip_list, fail_list, fail_errors,
        exclude_list, attempts_used, had_targets, fatal_error,
    )
    send_mail_smtp(subject, html_body, text_body)


if __name__ == "__main__":
    main()
