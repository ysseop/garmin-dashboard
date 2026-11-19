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
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.8-flash"
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite"]  # 혼잡할 때 대신 쓸 모델
sys.stdout.reconfigure(line_buffering=True)  # 로그가 실시간으로 찍히게
WELLNESS_DAYS = min(DAYS, 30)  # 건강 데이터는 날짜마다 여러 번 호출하므로 최대 30일
KST = dt.timezone(dt.timedelta(hours=9))


def today_kst():
    """GitHub 서버는 UTC 시계라서, 한국 날짜로 '오늘'을 계산 (07:30 KST = 전날 22:30 UTC)."""
    return dt.datetime.now(KST).date()


def safe(fn, *args):
    """호출이 실패해도 전체가 멈추지 않게 None 반환."""
    try:
        return fn(*args)
    except Exception:
        return None


def val(x):
    """None, 0 대신 빈칸으로 (시트에서 깔끔하게 보이도록)."""
    return x if x not in (None, 0, "") else ""


def norm_time(s):
    """'2026-11-18 6:26:55'처럼 자릿수가 들쭉날쭉해도 '2026-11-18 06:26:55'로 맞춤."""
    s = str(s or "").replace("T", " ").split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return s


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
        "startTime": norm_time(a.get("startTimeLocal")),
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


def pick_device(m):
    """가민은 기기별로 값을 줌 -> 주 기기(primaryTrainingDevice) 값을 고름."""
    if not isinstance(m, dict) or not m:
        return {}
    vals = [v for v in m.values() if isinstance(v, dict)]
    for v in vals:
        if v.get("primaryTrainingDevice"):
            return v
    return vals[0] if vals else {}


def num(x, nd=1):
    try:
        return round(float(x), nd) if nd else int(round(float(x)))
    except (TypeError, ValueError):
        return None


def training_fields(client, s):
    """VO2max, 훈련 상태, 급성/만성 부하, 4주 부하 균형(무산소/고강도 유산소/저강도 유산소)."""
    ts = safe(client.get_training_status, s) or {}
    vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic")) or {}
    st = pick_device((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    ac = st.get("acuteTrainingLoadDTO") or {}
    lb = pick_device((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap"))
    ratio = ac.get("dailyAcuteChronicWorkloadRatio")
    if ratio is None and ac.get("acwrPercent") is not None:
        ratio = ac["acwrPercent"] / 100
    return {
        "vo2max": val(num(vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"))),
        "trainingStatus": st.get("trainingStatusFeedbackPhrase") or "",
        "acuteLoad": val(num(ac.get("dailyTrainingLoadAcute"), 0)),
        "chronicLoad": val(num(ac.get("dailyTrainingLoadChronic"), 0)),
        "acwr": val(num(ratio, 2)),
        "acwrStatus": ac.get("acwrStatus") or "",
        "loadAnaerobic": val(num(lb.get("monthlyLoadAnaerobic"), 0)),
        "loadAnaerobicMin": val(num(lb.get("monthlyLoadAnaerobicTargetMin"), 0)),
        "loadAnaerobicMax": val(num(lb.get("monthlyLoadAnaerobicTargetMax"), 0)),
        "loadHighAerobic": val(num(lb.get("monthlyLoadAerobicHigh"), 0)),
        "loadHighAerobicMin": val(num(lb.get("monthlyLoadAerobicHighTargetMin"), 0)),
        "loadHighAerobicMax": val(num(lb.get("monthlyLoadAerobicHighTargetMax"), 0)),
        "loadLowAerobic": val(num(lb.get("monthlyLoadAerobicLow"), 0)),
        "loadLowAerobicMin": val(num(lb.get("monthlyLoadAerobicLowTargetMin"), 0)),
        "loadLowAerobicMax": val(num(lb.get("monthlyLoadAerobicLowTargetMax"), 0)),
        "loadFeedback": lb.get("trainingBalanceFeedbackPhrase") or "",
    }


def build_wellness(client, day, with_training=True):
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
    if with_training:
        row.update(training_fields(client, s))
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


def coach(act_rows, well_rows):
    """가장 최근 운동일을 Gemini로 리뷰해서 coaching 시트에 저장 (키가 없으면 건너뜀)."""
    if not GEMINI_KEY:
        print("[coaching] GEMINI_API_KEY 없음 - 건너뜀")
        return
    if not act_rows:
        return
    latest = max(str(r["startTime"])[:10] for r in act_rows)
    if (today_kst() - dt.date.fromisoformat(latest)).days > 1:
        print("[coaching] 최근 1일 내 운동 없음 - 건너뜀")
        return
    todays = [r for r in act_rows if str(r["startTime"])[:10] == latest]
    ids = ",".join(sorted(str(r["activityId"]) for r in todays))

    try:  # 같은 운동에 이미 코칭이 있으면 다시 만들지 않음 (API 비용 절약)
        got = requests.get(WEBAPP_URL, params={"key": API_KEY}, timeout=60).json()
        for c in got.get("coaching") or []:
            if str(c.get("date"))[:10] == latest and str(c.get("activityIds")) == ids:
                print("[coaching] 이미 생성됨 - 건너뜀")
                return
    except Exception:
        pass

    def hi_of(r):
        return ((r["z4Sec"] or 0) + (r["z5Sec"] or 0)) / 60

    def grade(r):
        """강한 날: 무산소TE 3.0 이상 또는 존4~5 10분 이상 / 쉬운 날: 무산소TE 2.0 미만이고 존4~5 10분 미만."""
        an = r["anaerobicTE"]
        if an in (None, ""):
            return "-"
        if float(an) >= 3 or hi_of(r) >= 10:
            return "강한 날"
        return "쉬운 날" if float(an) < 2 else "애매한 날"

    def line(r):
        return (f"{str(r['startTime'])[:10]} {r['name']}({r['type']}) [{grade(r)}, 존4~5 {round(hi_of(r))}분] "
                f"{round((r['durationSec'] or 0)/60)}분, "
                f"평균심박 {r['avgHR'] or '-'}, 최대 {r['maxHR'] or '-'}, 유산소TE {r['aerobicTE'] or '-'}, "
                f"무산소TE {r['anaerobicTE'] or '-'}, 부하 {r['trainingLoad'] or '-'}, "
                f"존(초) {r['z1Sec']}/{r['z2Sec']}/{r['z3Sec']}/{r['z4Sec']}/{r['z5Sec']}, {r['calories']}kcal")

    cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
    recent = [r for r in act_rows if cutoff <= str(r["startTime"])[:10] < latest]
    well = sorted(well_rows, key=lambda w: w["date"])[-4:]
    wl = "\n".join(
        f"{w['date']} 수면 {round((w['sleepSec'] or 0)/3600, 1) if w['sleepSec'] != '' else '-'}시간 점수 {w['sleepScore'] or '-'}, "
        f"HRV {w['hrvLastNight'] or '-'}({w['hrvStatus'] or '-'}), 안정시심박 {w['restingHR'] or '-'}, "
        f"바디배터리 {w['bodyBatteryHigh'] or '-'}/{w['bodyBatteryLow'] or '-'}, 훈련준비도 {w['trainingReadiness'] or '-'}"
        for w in well)
    tw = next((w for w in sorted(well_rows, key=lambda w: w["date"], reverse=True)
               if w.get("acwr") not in (None, "") or w.get("loadAnaerobic") not in (None, "")), {})
    g = lambda k: tw.get(k) if tw.get(k) not in (None, "") else "-"
    tl = (f"VO2max {g('vo2max')}, 훈련상태 {g('trainingStatus')}, 급성/만성 부하 {g('acuteLoad')}/{g('chronicLoad')} "
          f"(비율 {g('acwr')}, {g('acwrStatus')})\n"
          f"4주 부하 균형: 무산소 {g('loadAnaerobic')} (목표 {g('loadAnaerobicMin')}~{g('loadAnaerobicMax')}), "
          f"고강도 유산소 {g('loadHighAerobic')} (목표 {g('loadHighAerobicMin')}~{g('loadHighAerobicMax')}), "
          f"저강도 유산소 {g('loadLowAerobic')} (목표 {g('loadLowAerobicMin')}~{g('loadLowAerobicMax')}), "
          f"가민 피드백 {g('loadFeedback')}") if tw else "기록 없음"
    week = [r for r in act_rows if str(r["startTime"])[:10] > (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()]
    hi_min = round(sum((r["z4Sec"] or 0) + (r["z5Sec"] or 0) for r in week) / 60)
    prompt = (
        "너는 고강도 인터벌 트레이닝(HIIT, 하이록스 포함)을 주로 하는 아마추어 운동선수의 코치야. "
        "목표는 더 건강해지고 운동능력(VO2max, 무산소 능력, 회복력)을 키우는 것.\n"
        "판단 기준: 강한 날 = 무산소TE 3.0 이상 또는 존4~5 10분 이상, 쉬운 날 = 무산소TE 2.0 미만이고 존4~5 10분 미만, "
        "그 사이는 '애매한 날'. 각 운동의 등급은 데이터의 [ ] 안에 계산해 두었으니 그대로 써. "
        "존4~5를 오래 유지한 날은 무산소TE가 낮아도 고강도 유산소 자극이 충분한 좋은 세션이야. "
        "2026-11-19부터 심박 존 기준을 바로잡아서(최대 심박 190, 젖산역치 168), 그 이전 기록의 존4~5 시간은 실제보다 적게 나와 있어. 강한 HIIT는 주 2~3회, 고강도 연속은 2일까지, 주 1~2회는 존2 40분 이상, "
        "주간 존4~5 합계 20~60분, 급성/만성 부하 비율 0.8~1.5, 수면 7시간 이상, 가민 4주 부하 균형은 세 항목 모두 목표 범위 안.\n"
        "아래 데이터를 보고 한국어로 간결하게 리뷰해줘. 형식:\n"
        "(1) 오늘 세션 등급(강한 날/쉬운 날/애매한 날)과 한 줄 평가\n"
        "(2) 잘한 점\n"
        "(3) 아쉬운 점과 '이렇게 했어야 했다'는 구체적 행동\n"
        "(4) 이번 주 균형 점검 (부하 균형, 부하 비율, 존4~5 시간 중 기준을 벗어난 것)\n"
        "(5) 내일 처방: 운동 종류, 시간, 목표 심박 또는 강도 (회복 데이터 반영)\n"
        "전체 10줄 이내, 숫자를 근거로 말하고, 의학적 진단은 하지 말고 데이터에 없는 건 추측하지 마. "
        "마크다운 굵게(**) 기호는 쓰지 마.\n\n"
        f"[오늘({latest}) 운동]\n" + "\n".join(line(r) for r in todays) +
        "\n\n[직전 7일 운동]\n" + ("\n".join(line(r) for r in recent) or "기록 없음") +
        f"\n\n[최근 7일 존4~5 합계] {hi_min}분" +
        "\n\n[가민 훈련 지표]\n" + tl +
        "\n\n[최근 건강 데이터]\n" + (wl or "기록 없음"))

    # 첫 모델이 혼잡(503/429)하거나 느리면 다음 모델로 넘어감
    models = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]
    headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
    resp = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                # 생각(thinking)을 짧게 해서 응답을 빨리 받음 (8줄 리뷰엔 충분)
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}}}
        for _ in range(2):
            t0 = time.time()
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=(10, 90))
            except requests.exceptions.Timeout:
                print(f"[coaching] {model} 응답 시간 초과 ({round(time.time()-t0)}초)")
                resp = None
                break
            print(f"[coaching] {model} 응답 {resp.status_code} ({round(time.time()-t0)}초)")
            if resp.status_code == 400 and "generationConfig" in body:  # 설정 미지원 모델이면 빼고 재시도
                body.pop("generationConfig")
                continue
            break
        if resp is not None and resp.status_code == 200:
            break
        if resp is not None and resp.status_code not in (429, 500, 503, 404):
            break  # 키 오류 등은 다른 모델로 바꿔도 안 되므로 중단
    if resp is None:
        print("[coaching] 모든 모델 응답 없음 - 다음 동기화 때 다시 시도")
        return
    if resp.status_code != 200:
        print("[coaching] API 오류:", resp.status_code, resp.text[:150])
        return
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError, ValueError):
        print("[coaching] 응답을 읽지 못함")
        return
    if text:
        post("coaching", [{"date": latest, "activityIds": ids, "text": text}])
        print("[coaching] 저장 완료")


def main():
    client = Garmin(EMAIL, PASSWORD)
    client.login()
    print("가민 로그인 성공")

    end = today_kst()
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
    for k in ("vo2max", "acwr", "loadAnaerobic", "loadLowAerobicMin", "trainingStatus"):  # 값은 출력하지 않음
        print(f"  훈련 지표 {k}: {sum(1 for w in well_rows if w.get(k) not in (None, ''))}일")
    ok_w = post("wellness", well_rows)

    try:
        coach(act_rows, well_rows)
    except Exception as e:
        print("[coaching] 건너뜀:", type(e).__name__, str(e)[:150])

    if not (ok_a and ok_w):
        sys.exit(1)


if __name__ == "__main__":
    main()
