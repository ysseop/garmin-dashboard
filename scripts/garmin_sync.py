"""가민 커넥트 활동 -> Apps Script 웹앱(구글 시트) 전송.

필요한 환경변수 (GitHub Secrets):
  GARMIN_EMAIL, GARMIN_PASSWORD, WEBAPP_URL, API_KEY
선택: DAYS (며칠 전까지 가져올지, 기본 7)

로그에는 건수와 상태만 출력합니다 (공개 저장소에서도 데이터가 노출되지 않게).
"""
import datetime as dt
import os
import sys

import requests
from garminconnect import Garmin

EMAIL = os.environ["GARMIN_EMAIL"]
PASSWORD = os.environ["GARMIN_PASSWORD"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
API_KEY = os.environ["API_KEY"]
DAYS = int(os.environ.get("DAYS") or "7")

# 심박 존 시간을 추가로 가져올 활동 종류 (API 호출을 아끼기 위해 러닝만)
RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running"}


def build_row(client, a):
    atype = (a.get("activityType") or {}).get("typeKey", "")
    zones = {1: "", 2: "", 3: "", 4: "", 5: ""}

    if atype in RUN_TYPES:
        try:
            for item in client.get_activity_hr_in_timezones(a["activityId"]):
                zones[item["zoneNumber"]] = round(item.get("secsInZone") or 0)
        except Exception:
            pass  # 존 데이터가 없어도 나머지는 저장

    distance_m = a.get("distance") or 0
    return {
        "activityId": a["activityId"],
        "startTime": a.get("startTimeLocal", ""),
        "type": atype,
        "name": a.get("activityName", ""),
        "distanceKm": round(distance_m / 1000, 2),
        "durationSec": round(a.get("duration") or 0),
        "avgHR": a.get("averageHR") or "",
        "maxHR": a.get("maxHR") or "",
        "elevationGainM": round(a.get("elevationGain") or 0),
        "calories": round(a.get("calories") or 0),
        "aerobicTE": a.get("aerobicTrainingEffect") or "",
        "z1Sec": zones[1],
        "z2Sec": zones[2],
        "z3Sec": zones[3],
        "z4Sec": zones[4],
        "z5Sec": zones[5],
    }


def main():
    client = Garmin(EMAIL, PASSWORD)
    client.login()
    print("가민 로그인 성공")

    end = dt.date.today()
    start = end - dt.timedelta(days=DAYS)
    activities = client.get_activities_by_date(start.isoformat(), end.isoformat())
    print(f"가져온 활동: {len(activities)}건 ({start} ~ {end})")

    rows = [build_row(client, a) for a in activities]
    if not rows:
        print("전송할 활동이 없습니다.")
        return

    resp = requests.post(WEBAPP_URL, json={"key": API_KEY, "rows": rows}, timeout=120)
    print("웹앱 응답:", resp.status_code, resp.text[:200])
    try:
        ok = resp.json().get("ok") is True
    except ValueError:
        ok = False
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
