"""가민 커넥트 -> Apps Script 웹앱(구글 시트) 전송 (v2).

보내는 데이터
  - activities: 모든 운동 (HIIT, 러닝 등) + 심박 존, 유산소/무산소 효과, 훈련 부하
  - wellness  : 일별 수면, HRV, 안정시 심박, 바디배터리, 스트레스, 걸음수, 훈련 준비도

필요한 환경변수 (GitHub Secrets):
  GARMIN_EMAIL, GARMIN_PASSWORD, WEBAPP_URL, API_KEY
선택: DAYS (며칠 전까지 가져올지, 기본 7)

로그에는 건수와 상태만 출력합니다 (공개 저장소에서도 데이터가 노출되지 않게).
"""
import datetime as dt
import os
import sys
import time

import requests
from garminconnect import Garmin

EMAIL = os.environ["GARMIN_EMAIL"]
PASSWORD = os.environ["GARMIN_PASSWORD"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
API_KEY = os.environ["API_KEY"]
DAYS = int(os.environ.get("DAYS") or "7")
WELLNESS_DAYS = min(DAYS, 30)  # 건강 데이터는 날짜마다 여러 번 호출하므로 최대 30일


def safe(fn, *args):
    """호출이 실패해도 전체가 멈추지 않게 None 반환."""
    try:
        return fn(*args)
    except Exception:
        return None


def val(x):
    """None, 0 대신 빈칸으로 (시트에서 깔끔하게 보이도록)."""
    return x if x not in (None, 0, "") else ""


def build_activity(client, a):
    atype = (a.get("activityType") or {}).get("typeKey", "")
    zones = {1: "", 2: "", 3: "", 4: "", 5: ""}

    for item in safe(client.get_activity_hr_in_timezones, a["activityId"]) or []:
        try:
            zones[item["zoneNumber"]] = round(item.get("secsInZone") or 0)
        except Exception:
            pass

    return {
        "activityId": a["activityId"],
        "startTime": a.get("startTimeLocal", ""),
        "type": atype,
        "name": a.get("activityName", ""),
        "distanceKm": round((a.get("distance") or 0) / 1000, 2),
        "durationSec": round(a.get("duration") or 0),
        "avgHR": val(a.get("averageHR")),
        "maxHR": val(a.get("maxHR")),
        "elevationGainM": round(a.get("elevationGain") or 0),
        "calories": round(a.get("calories") or 0),
        "aerobicTE": val(a.get("aerobicTrainingEffect")),
        "z1Sec": zones[1],
        "z2Sec": zones[2],
        "z3Sec": zones[3],
        "z4Sec": zones[4],
        "z5Sec": zones[5],
        "anaerobicTE": val(a.get("anaerobicTrainingEffect")),
        "trainingLoad": val(round(a["activityTrainingLoad"]) if a.get("activityTrainingLoad") else None),
    }


def build_wellness(client, day):
    s = day.isoformat()
    stats = safe(client.get_stats, s) or {}
    sleep = safe(client.get_sleep_data, s) or {}
    hrv = safe(client.get_hrv_data, s) or {}
    ready = safe(client.get_training_readiness, s)

    dto = sleep.get("dailySleepDTO") or {}
    score = ((dto.get("sleepScores") or {}).get("overall") or {}).get("value")
    hs = hrv.get("hrvSummary") or {}
    if isinstance(ready, list):
        ready = ready[0] if ready else {}
    ready = ready or {}

    row = {
        "date": s,
        "sleepSec": val(dto.get("sleepTimeSeconds")),
        "deepSec": val(dto.get("deepSleepSeconds")),
        "lightSec": val(dto.get("lightSleepSeconds")),
        "remSec": val(dto.get("remSleepSeconds")),
        "awakeSec": val(dto.get("awakeSleepSeconds")),
        "sleepScore": val(score),
        "hrvLastNight": val(hs.get("lastNightAvg")),
        "hrvWeeklyAvg": val(hs.get("weeklyAvg")),
        "hrvStatus": hs.get("status") or "",
        "restingHR": val(stats.get("restingHeartRate")),
        "bodyBatteryHigh": val(stats.get("bodyBatteryHighestValue")),
        "bodyBatteryLow": val(stats.get("bodyBatteryLowestValue")),
        "avgStress": val(stats.get("averageStressLevel")),
        "steps": val(stats.get("totalSteps")),
        "trainingReadiness": val(ready.get("score")),
    }
    # 날짜 말고 채워진 값이 하나도 없으면 보내지 않음
    has_data = any(v != "" for k, v in row.items() if k != "date")
    return row if has_data else None


def post(sheet, rows):
    if not rows:
        print(f"[{sheet}] 전송할 데이터가 없습니다.")
        return True
    resp = requests.post(
        WEBAPP_URL, json={"key": API_KEY, "sheet": sheet, "rows": rows}, timeout=120
    )
    print(f"[{sheet}] 웹앱 응답:", resp.status_code, resp.text[:200])
    try:
        return resp.json().get("ok") is True
    except ValueError:
        return False


def main():
    client = Garmin(EMAIL, PASSWORD)
    client.login()
    print("가민 로그인 성공")

    end = dt.date.today()
    start = end - dt.timedelta(days=DAYS)

    # 1) 운동 기록
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    print(f"가져온 활동: {len(activities)}건 ({start} ~ {end})")
    types = {}
    for a in activities:
        t = (a.get("activityType") or {}).get("typeKey", "?")
        types[t] = types.get(t, 0) + 1
    print("활동 종류별 건수:", types)
    act_rows = [build_activity(client, a) for a in activities]
    ok_a = post("activities", act_rows)

    # 2) 일별 건강 데이터
    well_rows = []
    for i in range(WELLNESS_DAYS + 1):
        row = build_wellness(client, end - dt.timedelta(days=i))
        if row:
            well_rows.append(row)
        time.sleep(0.3)  # 호출 간격을 둬서 차단 위험을 줄임
    print(f"가져온 건강 데이터: {len(well_rows)}일")
    ok_w = post("wellness", well_rows)

    if not (ok_a and ok_w):
        sys.exit(1)


if __name__ == "__main__":
    main()
