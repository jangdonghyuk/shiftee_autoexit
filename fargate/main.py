import time
import os
import re
import html
import logging
import boto3
from datetime import datetime, timezone, timedelta
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
# 자동 처리 제외 직원 (최고 관리자 등 — 이 계정 권한으로 편집 모달이 안 열려 처리 불가)
EXCLUDE_EMPLOYEES = ["구민성"]

# 1차 전체 실행 후, 실패자에 대해 최대 MAX_RETRY회 재시도 (총 MAX_RETRY+1회 시도)
MAX_RETRY = 3

# 시험 모드 (2026-09-29) — DRY_RUN=1 이면 로그인·대상 계산·메일까지만 하고 18:00 저장은 하지 않는다.
# ECS 수동 run-task 의 environment override 로만 켠다. 예약 실행(스케줄)에는 넣지 않는다.
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# 페이지 이동 1회당 최대 대기 시간(초)
PAGE_LOAD_TIMEOUT = 60
# 출퇴근 표가 안 그려지면 목록을 다시 불러오는 최대 횟수
TABLE_LOAD_TRIES = 3

KST = timezone(timedelta(hours=9))

# === 로그 설정 (stdout → CloudWatch Logs) ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_today_str():
    now = datetime.now(KST)
    return f"{now.month}/{now.day:02d}"


def send_email(subject, html_body, text_body):
    """AWS SES로 결과 이메일 발송 (HTML + 텍스트 폴백)"""
    try:
        ses = boto3.client("ses", region_name=AWS_REGION)
        ses.send_email(
            Source=SES_SENDER,
            Destination={"ToAddresses": EMAIL_RECIPIENTS},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        log.info(f"이메일 발송 완료: {', '.join(EMAIL_RECIPIENTS)}")
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


def scan_table(driver, today_str):
    """현재 테이블을 스캔해 (오늘 출근자 리스트, 이미 퇴근 찍힌 이름 set) 반환"""
    rows = driver.find_elements(By.CSS_SELECTOR, "tbody tr")
    today_employees = []
    skip_set = set()

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
            # 근무시간 "09:53 - 18:00" 패턴이면 이미 퇴근 처리됨
            if re.match(r'\d{2}:\d{2}\s*-\s*\d{2}:\d{2}', text):
                has_clock_out = True

        if is_today and name not in today_employees:
            today_employees.append(name)
            if has_clock_out:
                skip_set.add(name)

    return today_employees, skip_set


def wait_for_table(driver, timeout=30):
    """직원 목록 표가 실제로 그려질 때까지 대기.
    (Angular가 표를 나중에 그리므로, 표가 비어있는 상태로 스캔해
     '출근자 0명'으로 잘못 판단하는 것을 방지)"""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.find_elements(
                By.CSS_SELECTOR, "tbody tr td span.font-weight-bold")) > 0
        )
        return True
    except Exception:
        log.warning("표 로딩 대기 시간 초과 — 그대로 진행")
        return False


def open_attendance_list(driver, wait):
    """출퇴근 목록을 열고 표가 그려질 때까지 대기. 안 그려지면 다시 불러와 최대 TABLE_LOAD_TRIES번.
    (2026-10-20 17:59 실행: 표가 30초 안에 안 그려졌는데 빈 표를 읽어 '출근 0명 → 대상 없음' 메일이 나갔고,
     10분 뒤 백업 실행에서 4명 처리됨 — 빈 표를 '대상 없음'으로 판정하면 안 된다)"""
    for i in range(1, TABLE_LOAD_TRIES + 1):
        driver.get(TARGET_URL)
        wait.until(lambda d: "attendances" in d.current_url)
        log.info(f"출퇴근 목록 페이지 도착 ({i}/{TABLE_LOAD_TRIES})")
        if wait_for_table(driver):
            return True
    return False


def log_table_snapshot(driver, today_str):
    """표 상태를 로그에 남긴다 — 줄 수와 앞 3줄 글자(출근 0명이 나오면 원인을 로그로 바로 보려고)."""
    rows = driver.find_elements(By.CSS_SELECTOR, "tbody tr")
    log.info(f"표 줄 수 {len(rows)} / 오늘 날짜 문자열 '{today_str}'")
    for row in rows[:3]:
        texts = [s.text.strip() for s in row.find_elements(By.CSS_SELECTOR, "td span") if s.text.strip()]
        log.info(f"  표 앞줄: {' | '.join(texts)[:150]}")


def verify_clock_out(driver, today_str, emp_name, timeout=6):
    """저장 후 해당 직원 행에 '- 18:00' 퇴근시간이 실제로 반영됐는지 확인.
    (모달이 안 열렸거나 저장이 안 됐는데 에러 없이 넘어가는 '거짓 성공' 방지)"""
    pat = re.compile(r'\d{2}:\d{2}\s*-\s*18:00')
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for row in driver.find_elements(By.CSS_SELECTOR, "tbody tr"):
                name_el = row.find_elements(By.CSS_SELECTOR, "td span.font-weight-bold")
                if not (name_el and name_el[0].text.strip() == emp_name):
                    continue
                spans = row.find_elements(By.CSS_SELECTOR, "td span")
                texts = [s.text for s in spans]
                if any(today_str in t for t in texts) and any(pat.search(t) for t in texts):
                    return True
        except Exception:
            pass  # 테이블 갱신 중 stale 등은 무시하고 재확인
        time.sleep(1)
    return False


def close_modal(driver):
    """열려있는 수정 모달이 있으면 닫기 (실패 후 다음 직원으로 넘어가기 위함)"""
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


def process_one(driver, wait, today_str, emp_name):
    """직원 1명의 퇴근시간을 18:00으로 설정·저장. 실패 시 예외 발생."""
    # 행을 매번 새로 찾아 stale 윈도우 최소화
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
        raise RuntimeError("오늘 출근 행을 찾지 못함")

    target_row.find_element(By.CSS_SELECTOR, "td span.font-weight-bold").click()
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
    time.sleep(3)

    # 저장 반영 검증: 행에 '- 18:00'이 실제로 찍혔는지 확인 (거짓 성공 방지)
    if not verify_clock_out(driver, today_str, emp_name):
        raise RuntimeError("저장 후 18:00 퇴근시간이 확인되지 않음 (모달 미오픈/저장 실패)")


def main():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    # 느린 리소스(분석 스크립트·웹소켓 등) 하나 때문에 페이지 로딩이 '완료'되지 않아
    # ChromeDriver가 모든 명령에 응답을 멈추는 것을 방지 (DOM 준비되면 진행)
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(options=options)
    # 페이지 이동이 무한 대기하지 않도록 상한을 둔다 (초과 시 TimeoutException)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    wait = WebDriverWait(driver, 20)

    now_kst = datetime.now(KST)
    today_str = get_today_str()
    today_date = now_kst.strftime("%Y-%m-%d")
    weekday = "월화수목금토일"[now_kst.weekday()]
    run_time = now_kst.strftime("%H:%M")

    success_list = []
    skip_list = []
    exclude_list = []
    fail_errors = {}      # 이름 -> 마지막 에러 메시지 (남아있는 = 최종 실패)
    attempts_used = 0
    had_targets = False
    fatal_error = None
    dry_run_names = None  # 시험 모드일 때만 목록(처리했을 대상)

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
        if not open_attendance_list(driver, wait):
            raise RuntimeError(
                f"출퇴근 표가 {TABLE_LOAD_TRIES}번 모두 안 그려짐 — 출근자를 읽지 못해 처리하지 않음")
        time.sleep(5)

        # === 오늘 출근자 필터링 ===
        log_table_snapshot(driver, today_str)
        today_employees, skip_set = scan_table(driver, today_str)
        targets = TARGET_EMPLOYEES if TARGET_EMPLOYEES else today_employees

        process_list = [e for e in targets if e in today_employees and e not in skip_set and e not in EXCLUDE_EMPLOYEES]
        skip_list = [e for e in targets if e in skip_set and e not in EXCLUDE_EMPLOYEES]
        exclude_list = [e for e in targets if e in EXCLUDE_EMPLOYEES and e in today_employees and e not in skip_set]
        had_targets = bool(process_list)

        log.info(f"오늘 날짜: {today_str} / 출근 {len(today_employees)}명")
        log.info(f"처리 대상: {len(process_list)}명 / SKIP(이미 퇴근): {len(skip_list)}명 / 제외: {len(exclude_list)}명")
        if exclude_list:
            log.info(f"제외(수동 처리): {', '.join(exclude_list)}")

        if DRY_RUN:
            dry_run_names = list(process_list)
            log.info(f"[시험 모드] 저장하지 않음 — 처리했을 대상 {len(dry_run_names)}명: {', '.join(dry_run_names)}")
            process_list = []

        # === 재시도 루프 (1차 + 최대 MAX_RETRY회 재시도) ===
        remaining = list(process_list)
        attempt = 0
        while remaining and attempt < (MAX_RETRY + 1):
            attempt += 1
            attempts_used = attempt

            if attempt > 1:
                log.info(f"=== 재시도 {attempt - 1}/{MAX_RETRY} : 실패 {len(remaining)}명 재처리 ===")
                # 페이지 새로고침으로 stale element 상태를 리셋
                driver.get(TARGET_URL)
                wait.until(lambda d: "attendances" in d.current_url)
                wait_for_table(driver)
                time.sleep(5)
                # 새로고침 후 이미 퇴근 찍힌 사람은 직전 시도에서 사실상 성공 → 성공 처리
                _, skip_now = scan_table(driver, today_str)
                recovered = [e for e in remaining if e in skip_now]
                for e in recovered:
                    log.info(f"  {e} 재확인 결과 이미 퇴근처리됨 → 성공 처리")
                    success_list.append(e)
                    fail_errors.pop(e, None)
                remaining = [e for e in remaining if e not in skip_now]

            log.info(f"[시도 {attempt}] 처리 대상 {len(remaining)}명")
            next_remaining = []
            for idx, emp_name in enumerate(remaining):
                log.info(f"  [{idx + 1}/{len(remaining)}] {emp_name} 처리 시작")
                try:
                    process_one(driver, wait, today_str, emp_name)
                    log.info(f"  {emp_name} 저장완료")
                    success_list.append(emp_name)
                    fail_errors.pop(emp_name, None)
                except Exception as e:
                    msg = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
                    log.error(f"  {emp_name} 에러: {e}")
                    fail_errors[emp_name] = msg
                    next_remaining.append(emp_name)
                    close_modal(driver)
                    time.sleep(2)
            remaining = next_remaining

        fail_list = list(fail_errors.keys())
        log.info(
            f"완료! 성공 {len(success_list)} / 실패 {len(fail_list)} / SKIP {len(skip_list)} "
            f"(시도 {attempts_used}회)"
        )
        if fail_list:
            log.warning(f"실패 목록: {', '.join(fail_list)}")

    except Exception as e:
        fatal_error = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        log.error(f"치명적 에러: {e}")
        # 세션이 이미 멎은 상태에서 driver.current_url을 다시 부르면
        # 또 120초씩 대기하게 되므로 호출하지 않는다.
    finally:
        try:
            driver.quit()
        except Exception as e:
            log.warning(f"브라우저 종료 실패(무시): {e}")

    # === 결과 이메일 발송 ===
    fail_list = list(fail_errors.keys())
    subject, html_body, text_body = build_email(
        today_date, weekday, run_time,
        success_list, skip_list, fail_list, fail_errors,
        exclude_list, attempts_used, had_targets, fatal_error,
        dry_run_names=dry_run_names,
    )
    send_email(subject, html_body, text_body)


def build_email(today_date, weekday, run_time, success_list, skip_list,
                fail_list, fail_errors, exclude_list, attempts_used, had_targets, fatal_error,
                dry_run_names=None):
    """결과를 예쁜 HTML 이메일(+텍스트 폴백)로 구성"""
    s, k, f = len(success_list), len(skip_list), len(fail_list)

    # --- 제목 ---
    if fatal_error:
        subject = f"[Shiftee] ⚠️ 퇴근처리 중단 - {today_date}"
    elif not had_targets:
        subject = f"[Shiftee] 퇴근처리 · 대상 없음 - {today_date}"
    elif f > 0:
        subject = f"[Shiftee] ⚠️ 퇴근처리 성공 {s}·실패 {f} - {today_date}"
    else:
        subject = f"[Shiftee] ✅ 퇴근처리 완료 성공 {s}·스킵 {k} - {today_date}"

    # --- 상단 배너 ---
    if fatal_error:
        banner = _banner("#dc2626", "#fef2f2",
                         "실행 중 오류로 중단되었습니다.",
                         html.escape(fatal_error))
    elif not had_targets:
        banner = _banner("#2563eb", "#eff6ff",
                         "이번 실행에서 처리할 직원이 없습니다.",
                         "전원 이미 퇴근 처리되었거나 출근자가 없습니다.")
    elif f > 0:
        banner = _banner("#d97706", "#fffbeb",
                         "일부 직원 처리에 실패했습니다.",
                         f"{MAX_RETRY}회 재시도 후에도 남은 {f}명입니다. 아래 실패 목록을 확인하세요.")
    else:
        banner = _banner("#16a34a", "#f0fdf4",
                         "모든 대상 직원의 퇴근처리가 완료되었습니다.",
                         f"성공 {s}명 · 스킵 {k}명")

    # --- 백업 실행(18:10)에서 처리한 사람이 있으면 = 17:59 실행이 놓친 것. 평소 성공 메일과 구분되게 (2026-10-21) ---
    if run_time >= "18:05" and s > 0 and not fatal_error:
        subject = subject.replace("[Shiftee] ", "[Shiftee] 🔁 백업 실행 · ", 1)
        banner = _banner("#d97706", "#fffbeb",
                         f"백업 실행 — 17:59 실행에서 빠진 {s}명을 처리했습니다.",
                         "17:59 실행 메일(중단·대상 없음 등)과 CloudWatch 로그를 확인하세요.").replace("<div style=\"background:", "<div style=\"margin-bottom:10px; background:", 1) + banner

    # --- 시험 모드: 제목·배너를 바꿔 실제 처리와 헷갈리지 않게 한다 ---
    if dry_run_names is not None:
        if fatal_error:
            subject = subject.replace("[Shiftee] ", "[Shiftee] 🧪 시험(저장 안 함) · ", 1)
        else:
            subject = f"[Shiftee] 🧪 시험(저장 안 함) · 대상 {len(dry_run_names)}명 - {today_date}"
            banner = _banner("#7c3aed", "#f5f3ff",
                             "시험 모드 — 저장하지 않았습니다.",
                             f"실제 실행이었다면 퇴근 처리했을 직원 {len(dry_run_names)}명: "
                             + html.escape(", ".join(dry_run_names) or "없음"))

    # --- 통계 박스 ---
    stats = (
        '<table width="100%" cellspacing="8" cellpadding="0" '
        'style="border-collapse:separate; margin-top:4px;"><tr>'
        + _stat(s, "성공", "#16a34a", "#f0fdf4")
        + _stat(k, "스킵", "#6b7280", "#f3f4f6")
        + _stat(f, "실패", "#dc2626", "#fef2f2")
        + "</tr></table>"
    )

    # --- 시도 횟수 (실제 처리 대상이 있었을 때만 표시) ---
    if not had_targets:
        attempt_note = "처리 대상 없음"
        attempt_html = ""
    elif attempts_used <= 1:
        attempt_note = "처리 시도: 1회"
        attempt_html = (
            f'<div style="margin-top:14px; font-size:12px; color:#9ca3af;">{attempt_note}</div>'
        )
    else:
        attempt_note = f"처리 시도: {attempts_used}회 (재시도 {attempts_used - 1}회 포함)"
        attempt_html = (
            f'<div style="margin-top:14px; font-size:12px; color:#9ca3af;">{attempt_note}</div>'
        )

    # --- 목록 섹션 ---
    sections = ""
    if success_list:
        sections += _section("✅ 성공", "#16a34a",
                              _chips(success_list, "#dcfce7", "#166534"))
    if skip_list:
        sections += _section("⏭️ 스킵 (이미 퇴근)", "#6b7280",
                             _chips(skip_list, "#f3f4f6", "#4b5563"))
    if fail_list:
        rows = ""
        for n in fail_list:
            err = html.escape(fail_errors.get(n, ""))
            rows += (
                '<div style="padding:8px 12px; background:#fef2f2; '
                'border-left:3px solid #ef4444; border-radius:4px; margin-bottom:6px;">'
                f'<b style="color:#b91c1c;">{html.escape(n)}</b>'
                f'<div style="color:#991b1b; font-size:12px; margin-top:2px;">{err}</div>'
                "</div>"
            )
        sections += _section("❌ 실패", "#dc2626", rows)
    if exclude_list:
        sections += _section("🔒 제외 (권한 등 · 수동 처리 필요)", "#7c3aed",
                             _chips(exclude_list, "#ede9fe", "#5b21b6"))

    html_body = f"""<!DOCTYPE html>
<html lang="ko"><body style="margin:0; padding:0; background:#f4f5f7;">
  <div style="font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif; background:#f4f5f7; padding:24px;">
    <div style="max-width:560px; margin:0 auto; background:#ffffff; border-radius:14px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,0.08);">
      <div style="background:#1f2937; padding:22px 24px;">
        <div style="color:#ffffff; font-size:18px; font-weight:700;">🕡 Shiftee 자동 퇴근처리</div>
        <div style="color:#9ca3af; font-size:13px; margin-top:5px;">{today_date} ({weekday}) · 실행 {run_time} KST</div>
      </div>
      <div style="padding:24px;">
        {banner}
        {stats}
        {attempt_html}
        {sections}
      </div>
      <div style="padding:14px 24px; background:#f9fafb; color:#9ca3af; font-size:12px; text-align:center;">
        본 메일은 자동 발송되었습니다.
      </div>
    </div>
  </div>
</body></html>"""

    # --- 텍스트 폴백 ---
    lines = [
        f"Shiftee 자동 퇴근처리 - {today_date} ({weekday}) 실행 {run_time} KST",
        "",
        f"성공 {s} / 스킵 {k} / 실패 {f}  ({attempt_note})",
        "",
    ]
    if fatal_error:
        lines.append(f"[중단] {fatal_error}")
    if success_list:
        lines.append(f"성공: {', '.join(success_list)}")
    if skip_list:
        lines.append(f"스킵: {', '.join(skip_list)}")
    if fail_list:
        lines.append("실패:")
        for n in fail_list:
            lines.append(f"  - {n}: {fail_errors.get(n, '')}")
    if exclude_list:
        lines.append(f"제외(수동 처리): {', '.join(exclude_list)}")
    if not had_targets and not exclude_list and not fatal_error:
        lines.append("처리할 직원이 없습니다 (전원 완료 또는 출근자 없음).")
    if run_time >= "18:05" and s > 0 and not fatal_error:
        lines.insert(0, f"[백업 실행] 17:59 실행에서 빠진 {s}명을 처리함 — 17:59 메일·로그 확인")
    if dry_run_names is not None:
        lines.insert(0, f"[시험 모드 · 저장 안 함] 처리했을 대상 {len(dry_run_names)}명: {', '.join(dry_run_names) or '없음'}")
    text_body = "\n".join(lines)

    return subject, html_body, text_body


def _banner(border, bg, title, desc):
    return (
        f'<div style="background:{bg}; border-left:4px solid {border}; '
        'border-radius:6px; padding:14px 16px;">'
        f'<div style="color:{border}; font-weight:700; font-size:14px;">{title}</div>'
        f'<div style="color:#6b7280; font-size:13px; margin-top:4px;">{desc}</div>'
        "</div>"
    )


def _stat(num, label, color, bg):
    return (
        f'<td style="width:33%; text-align:center; padding:16px 6px; '
        f'background:{bg}; border-radius:10px;">'
        f'<div style="font-size:28px; font-weight:800; color:{color}; line-height:1;">{num}</div>'
        f'<div style="font-size:12px; color:#6b7280; margin-top:6px;">{label}</div>'
        "</td>"
    )


def _section(title, color, body_html):
    return (
        '<div style="margin-top:20px;">'
        f'<div style="font-size:13px; font-weight:700; color:{color}; margin-bottom:8px;">{title}</div>'
        f'<div style="font-size:14px; color:#374151; line-height:1.7;">{body_html}</div>'
        "</div>"
    )


def _chips(names, bg, fg):
    return " ".join(
        f'<span style="display:inline-block; background:{bg}; color:{fg}; '
        f'border-radius:6px; padding:3px 10px; margin:2px 3px; font-size:13px;">{html.escape(n)}</span>'
        for n in names
    )


if __name__ == "__main__":
    main()
