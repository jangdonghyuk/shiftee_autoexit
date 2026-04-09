import time
import os
import re
import logging
import boto3
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# === 환경변수 (Fargate Task Definition에서 주입) ===
SHIFTEE_EMAIL = os.environ["SHIFTEE_EMAIL"]
SHIFTEE_PW = os.environ["SHIFTEE_PW"]
SES_SENDER = os.environ["SES_SENDER"]
EMAIL_RECIPIENTS = [e.strip() for e in os.environ["EMAIL_RECIPIENTS"].split(",")]
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

TARGET_URL = "https://shiftee.io/app/companies/503273/manager/attendances/list"
# 빈 리스트 = 전직원 대상
TARGET_EMPLOYEES = []

# === 로그 설정 (stdout → CloudWatch Logs) ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_today_str():
    now = datetime.now()
    return f"{now.month}/{now.day:02d}"


def send_email(subject, body):
    """AWS SES로 결과 이메일 발송"""
    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        ses.send_email(
            Source=SES_SENDER,
            Destination={"ToAddresses": EMAIL_RECIPIENTS},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
        log.info(f"이메일 발송 완료: {', '.join(EMAIL_RECIPIENTS)}")
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


def main():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 20)

    today_str = get_today_str()
    today_date = datetime.now().strftime("%Y-%m-%d")

    success_list = []
    fail_list = []
    skip_list = []

    try:
        # === 로그인 ===
        driver.get("https://shiftee.io/ko/accounts/login")
        log.info("shiftee 로그인 페이지 로딩 완료")

        email_input = wait.until(
            EC.presence_of_element_located((By.ID, "email-address"))
        )
        email_input.clear()
        email_input.send_keys(SHIFTEE_EMAIL)

        pw_input = driver.find_element(By.ID, "password")
        pw_input.clear()
        pw_input.send_keys(SHIFTEE_PW)

        driver.find_element(
            By.CSS_SELECTOR, "button.btn.btn-primary.btn-block"
        ).click()
        log.info("로그인 시도")

        wait.until(lambda d: "shiftee.io/app" in d.current_url)
        log.info("로그인 성공")

        # === 출퇴근 목록 페이지 이동 ===
        driver.get(TARGET_URL)
        wait.until(lambda d: "attendances" in d.current_url)
        log.info("출퇴근 목록 페이지 도착")
        time.sleep(5)

        # === 오늘 날짜 기준 직원 필터링 ===
        rows = driver.find_elements(By.CSS_SELECTOR, "tbody tr")
        today_employees = []
        today_rows = {}
        skip_employees = set()

        for row in rows:
            name_el = row.find_elements(By.CSS_SELECTOR, "td span.font-weight-bold")
            if not name_el:
                continue

            name = name_el[0].text.strip()
            spans = row.find_elements(By.CSS_SELECTOR, "td span")

            is_today = False
            has_clock_out = False

            for span in spans:
                text = span.text.strip()
                if today_str in text:
                    is_today = True
                if re.match(r'\d{2}:\d{2}\s*-\s*\d{2}:\d{2}', text):
                    has_clock_out = True

            if is_today:
                today_employees.append(name)
                today_rows[name] = row
                if has_clock_out:
                    skip_employees.add(name)

        targets = TARGET_EMPLOYEES if TARGET_EMPLOYEES else today_employees

        log.info(f"오늘 날짜: {today_str}")
        log.info(f"오늘 출근한 직원 ({len(today_employees)}명):")
        for i, emp in enumerate(today_employees, 1):
            if emp in skip_employees:
                status = " [SKIP]"
            else:
                status = " ◀ 대상" if emp in targets else ""
            log.info(f"  {i}. {emp}{status}")

        process_list = [e for e in targets if e in today_rows and e not in skip_employees]
        skip_list = [e for e in targets if e in skip_employees]
        missing_list = [e for e in targets if e not in today_rows]

        log.info(f"처리: {len(process_list)}명 / SKIP: {len(skip_list)}명")

        if not process_list:
            log.info("처리할 직원이 없습니다.")
            # 이메일 발송 (처리 대상 없음)
            send_email(
                f"[Shiftee] 자동 퇴근처리 결과 - {today_date}",
                f"오늘 날짜: {today_str}\n\n처리할 직원이 없습니다.\n\nSKIP: {', '.join(skip_list) if skip_list else '없음'}\n미출근: {', '.join(missing_list) if missing_list else '없음'}",
            )
            return

        # === 직원별 자동 순차 처리 ===
        for idx, emp_name in enumerate(process_list):
            log.info(f"[{idx+1}/{len(process_list)}] {emp_name} 처리 시작")

            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "tbody tr")
                target_row = None
                for row in rows:
                    name_el = row.find_elements(By.CSS_SELECTOR, "td span.font-weight-bold")
                    if name_el and name_el[0].text.strip() == emp_name:
                        spans = row.find_elements(By.CSS_SELECTOR, "td span")
                        for span in spans:
                            if today_str in span.text:
                                target_row = row
                                break
                    if target_row:
                        break

                if not target_row:
                    log.warning(f"{emp_name} 행 못찾음 SKIP")
                    fail_list.append(emp_name)
                    continue

                target_row.find_element(
                    By.CSS_SELECTOR, "td span.font-weight-bold"
                ).click()
                time.sleep(2)

                # "현재 근무중" 체크박스 해제
                working_checkbox = wait.until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR,
                         ".sft-currently-working sft-form-checkbox input[type='checkbox']")
                    )
                )
                if working_checkbox.is_selected():
                    checkmark = driver.find_element(
                        By.CSS_SELECTOR,
                        ".sft-currently-working sft-form-checkbox .sft-checkmark"
                    )
                    checkmark.click()
                    time.sleep(1)

                # 퇴근시간 18:00 설정
                time.sleep(2)
                driver.execute_script("""
                    var picker = document.querySelector("sft-time-picker[formcontrolname='clock_out_time']");
                    var inputs = picker.querySelectorAll("input.bs-timepicker-field");
                    var hh = inputs[0];
                    var mm = inputs[1];
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    nativeInputValueSetter.call(hh, '18');
                    hh.dispatchEvent(new Event('input', { bubbles: true }));
                    hh.dispatchEvent(new Event('change', { bubbles: true }));
                    nativeInputValueSetter.call(mm, '00');
                    mm.dispatchEvent(new Event('input', { bubbles: true }));
                    mm.dispatchEvent(new Event('change', { bubbles: true }));
                """)
                time.sleep(1)

                # 저장
                modal_body = driver.find_element(By.CSS_SELECTOR, ".modal-body")
                driver.execute_script(
                    "arguments[0].scrollTop = arguments[0].scrollHeight;", modal_body
                )
                time.sleep(1)
                driver.execute_script("""
                    var btns = document.querySelectorAll('.modal-footer button');
                    for (var i = 0; i < btns.length; i++) {
                        if (btns[i].textContent.trim().includes('변경사항 저장')) {
                            btns[i].click();
                            break;
                        }
                    }
                """)
                log.info(f"{emp_name} 저장완료")
                success_list.append(emp_name)
                time.sleep(3)

            except Exception as e:
                log.error(f"{emp_name} 에러: {e}")
                fail_list.append(emp_name)
                try:
                    driver.execute_script("""
                        var btns = document.querySelectorAll('.modal-footer button');
                        for (var i = 0; i < btns.length; i++) {
                            if (btns[i].textContent.trim().includes('닫기')) {
                                btns[i].click();
                                break;
                            }
                        }
                    """)
                except Exception:
                    pass
                time.sleep(2)

        log.info(f"완료! 성공: {len(success_list)}명 / 실패: {len(fail_list)}명 / SKIP: {len(skip_list)}명")
        if fail_list:
            log.warning(f"실패 목록: {', '.join(fail_list)}")

    except Exception as e:
        log.error(f"에러 발생: {e}")
        fail_list.append(f"전체 에러: {e}")
        try:
            log.error(f"현재 URL: {driver.current_url}")
        except Exception:
            log.error("브라우저 세션 종료됨")
    finally:
        driver.quit()

    # === 결과 이메일 발송 ===
    body = f"""오늘 날짜: {today_str}

처리 결과:
  성공: {len(success_list)}명
  실패: {len(fail_list)}명
  SKIP: {len(skip_list)}명

성공 목록: {', '.join(success_list) if success_list else '없음'}
SKIP 목록: {', '.join(skip_list) if skip_list else '없음'}
실패 목록: {', '.join(fail_list) if fail_list else '없음'}"""

    send_email(
        f"[Shiftee] 자동 퇴근처리 결과 - {today_date}",
        body,
    )


if __name__ == "__main__":
    main()
