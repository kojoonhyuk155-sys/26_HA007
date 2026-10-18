# -*- coding: utf-8 -*-
"""고객 AI 판단 정확도 — LLM 모드 측정 (내 PC에서 실행).

ROBITA 프로젝트 폴더(app/ 와 .env 가 있는 곳)나 그 안의 evaluation/ 폴더에 두고 실행한다.
(시험 문장 72개가 이 파일 안에 들어 있다. testsets.py 는 필요 없다.)

    python evaluation/eval_llm.py            # 3회 반복(기본)
    python evaluation/eval_llm.py --repeat 1 # 빠르게 1회만

.env 의 OPENAI_API_KEY(또는 GEMINI_API_KEY)를 그대로 사용한다.
측정 대상은 앱이 실제로 쓰는 함수 그대로다.
  - 탑승 중 요청 : RiderService._agent_decide → 실패 시 _agent_fallback  (ride_request 와 같은 순서)
  - 이용 후 문의 : RiderService.post_ride_inquiry 의 need_agent
결과: 화면 출력 + eval_llm_result.json (문장별 정답/응답 포함)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import sys
import time
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("project", nargs="?", default=None)
ap.add_argument("--repeat", type=int, default=3)
args = ap.parse_args()
for _cand in ([args.project] if args.project else []) + [HERE, os.path.dirname(HERE)]:
    if _cand and os.path.isdir(os.path.join(_cand, "app")):
        args.project = os.path.abspath(_cand)
        break
else:
    sys.exit("app 폴더를 찾지 못했습니다. 이 파일을 ROBITA 폴더나 그 안의 evaluation 폴더에 두세요.")
sys.path.insert(0, args.project)

from app.config import settings          # noqa: E402  (.env 자동 로드)
from app.llm import llm                  # noqa: E402
from app.rider import RiderService       # noqa: E402

# ── 시험 문장(정답 포함) ─ robita_eval.py 의 testsets.py 와 같은 내용 ──
# (유형, 발화, 정답 도구, 정답 인자)  인자: 장소명 또는 mode, 없으면 None
RIDE = [
    # 목적지 변경
    ("E", "목적지를 오이도역으로 바꿔 주세요", "change_destination", "오이도역"),
    ("E", "배곧호수공원으로 가 주세요", "change_destination", "배곧호수공원"),
    ("I", "정왕역 말고 시화병원으로 가 주세요", "change_destination", "시화병원"),
    ("I", "생각해 보니 성담스퀘어에서 내리는 게 낫겠어요", "change_destination", "성담스퀘어"),
    # 경유지 추가
    ("E", "가는 길에 정왕전통시장 들러 주세요", "add_stop", "정왕전통시장"),
    ("E", "시흥중앙도서관 경유해서 가 주세요", "add_stop", "시흥중앙도서관"),
    ("I", "오이도역 들렀다가 정왕역으로 가 주세요", "add_stop", "오이도역"),
    ("I", "성담스퀘어에서 물건 하나만 사고 갈게요", "add_stop", "성담스퀘어"),
    # 경로 변경
    ("E", "다른 길로 가 주세요", "change_route", None),
    ("E", "경로 좀 바꿔 주세요", "change_route", None),
    ("I", "앞에 공사 중이라 계속 서 있는데 빠른 길 없을까요?", "change_route", None),
    ("I", "여기 매번 정체던데 피해서 갈 수 있어요?", "change_route", None),
    # 냉방
    ("E", "에어컨 좀 켜 주세요", "set_aircon", "cool"),
    ("E", "냉방 세게 해 주세요", "set_aircon", "cool"),
    ("I", "차 안이 좀 후덥지근하네요", "set_aircon", "cool"),
    ("I", "땀이 계속 나요", "set_aircon", "cool"),
    # 난방
    ("E", "히터 틀어 주세요", "set_aircon", "heat"),
    ("E", "난방 좀 올려 주세요", "set_aircon", "heat"),
    ("I", "좀 쌀쌀하네요", "set_aircon", "heat"),
    ("I", "손이 시려워요", "set_aircon", "heat"),
    # 음악
    ("E", "음악 틀어 주세요", "play_music", None),
    ("E", "라디오 켜 줄 수 있어요?", "play_music", None),
    ("I", "가는 동안 너무 조용한데 뭐라도 들을 수 있을까요?", "play_music", None),
    ("I", "잔잔한 재즈 듣고 싶어요", "play_music", None),
    # 창문·환기
    ("E", "창문 좀 열어 주세요", "open_window", None),
    ("E", "환기 좀 시켜 주세요", "open_window", None),
    ("I", "차 안 공기가 답답해요", "open_window", None),
    ("I", "바깥 공기 좀 쐬고 싶어요", "open_window", None),
    # 감속
    ("E", "천천히 가 주세요", "set_speed", "slow"),
    ("E", "속도 좀 줄여 주세요", "set_speed", "slow"),
    ("I", "너무 빨라서 무서워요", "set_speed", "slow"),
    ("I", "급정거가 많아서 불안해요, 여유 있게 가 주세요", "set_speed", "slow"),
    # 정차
    ("E", "잠깐 세워 주세요", "stop_vehicle", None),
    ("E", "저기서 잠시 정차해 주세요", "stop_vehicle", None),
    ("I", "속이 안 좋아서 차 좀 멈춰 주세요", "stop_vehicle", None),
    ("I", "갓길에 1분만 대 주실 수 있나요?", "stop_vehicle", None),
    # 상담사 연결
    ("E", "상담사 연결해 주세요", "call_support", None),
    ("E", "직원이랑 통화하고 싶어요", "call_support", None),
    ("I", "고객센터랑 이야기하고 싶어요", "call_support", None),
    ("I", "AI 말고 진짜 담당자한테 말하고 싶어요", "call_support", None),
    # 남은 시간
    ("E", "언제 도착해요?", "eta", None),
    ("E", "몇 분 남았어요?", "eta", None),
    ("I", "아직 멀었나요?", "eta", None),
    ("I", "약속이 7시인데 늦지 않게 갈 수 있을까요?", "eta", None),
    # 미실행(서비스 지역 밖·잡담)
    ("E", "강남역으로 가 주세요", "none", None),
    ("E", "인천공항까지 가 주세요", "none", None),
    ("I", "오늘 날씨 정말 좋네요", "none", None),
    ("I", "고마워요, 승차감이 좋네요", "none", None),
]

# (유형, 문의, 상담원 연결 필요 여부)
INQ = [
    # 상담원 연결 필요
    ("E", "환불 받고 싶어요", True),
    ("E", "이중 결제된 것 같아요", True),
    ("E", "개인정보를 수정하고 싶어요", True),
    ("E", "사고 보상금액을 확정해 주세요", True),
    ("E", "결제 오류가 났어요", True),
    ("E", "어제 예약 건 취소해 주시고 돈 돌려주세요", True),
    ("I", "요금이 두 번 빠져나갔어요", True),
    ("I", "영수증에 찍힌 금액이 실제 결제된 금액이랑 달라요", True),
    ("I", "사고 때문에 병원비가 나왔는데 얼마까지 보상되나요?", True),
    ("I", "제 전화번호가 바뀌었어요", True),
    ("I", "사고 처리 과정이 납득이 안 돼서 법적으로 대응하려고요", True),
    ("I", "지난번 운행 요금 결제가 실패했다고 떠요", True),
    # AI 직접 안내
    ("E", "요금은 어떻게 계산돼요?", False),
    ("E", "영수증은 어디서 볼 수 있어요?", False),
    ("E", "지난 이용 내역 확인하고 싶어요", False),
    ("E", "기본요금이 얼마예요?", False),
    ("E", "심야에도 요금이 같나요?", False),
    ("E", "이용 내역은 며칠까지 보관돼요?", False),
    ("I", "예약은 어떻게 하나요?", False),
    ("I", "서비스 지역이 어디까지예요?", False),
    ("I", "몇 시까지 운행해요?", False),
    ("I", "5명이 같이 타려면 어떻게 해야 돼요?", False),
    ("I", "사고가 나면 보상은 어떤 절차로 진행되나요?", False),
    ("I", "로보택시도 팁을 줘야 하나요?", False),
]

testsets = SimpleNamespace(RIDE=RIDE, INQ=INQ)

if not llm.available():
    sys.exit("LLM 키가 없습니다. .env 에 OPENAI_API_KEY 를 넣고 다시 실행하세요.")

MODEL = settings.openai_model if settings.llm_provider == "openai" else settings.gemini_model
print(f"LLM: {settings.llm_provider} / {MODEL} · 반복 {args.repeat}회")
CTX = {"차량": "12가3456", "현재_목적지": "정왕역", "탑승인원": 1}


def norm(d: dict):
    tool = str((d or {}).get("tool") or "none")
    if tool == "out_of_area":
        tool = "none"
    arg = None
    if tool in ("change_destination", "add_stop"):
        arg = (d or {}).get("place")
    elif tool in ("set_aircon", "set_speed"):
        arg = (d or {}).get("mode")
    return tool, arg


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


svc = RiderService()
runs = []
t0 = time.time()
for rep in range(1, args.repeat + 1):
    ride_rows, inq_rows = [], []
    for i, (typ, text, tool, arg) in enumerate(testsets.RIDE, 1):
        d = svc._agent_decide(text, CTX)
        engine = "llm"
        if not d or not d.get("tool"):
            d = svc._agent_fallback(text)
            engine = "rule(대체)"
        got = norm(d)
        ride_rows.append({"유형": typ, "발화": text, "정답": [tool, arg], "응답": list(got),
                          "엔진": engine, "일치": got == (tool, arg)})
        print(f"\r  [{rep}회차] 탑승 중 요청 {i}/{len(testsets.RIDE)}", end="", flush=True)
    for i, (typ, text, need) in enumerate(testsets.INQ, 1):
        res = svc.post_ride_inquiry(f"eval{rep}", text)
        got = bool(res.get("need_agent"))
        lost = res.get("action") == "lost"
        inq_rows.append({"유형": typ, "문의": text, "정답": need, "응답": got,
                         "엔진": res.get("engine", "-"), "분실물오분류": lost,
                         "일치": (got == need) and not lost})
        print(f"\r  [{rep}회차] 이용 후 문의 {i}/{len(testsets.INQ)}      ", end="", flush=True)
    print()

    def acc(rows, typ=None):
        rr = [x for x in rows if typ is None or x["유형"] == typ]
        return pct(sum(x["일치"] for x in rr), len(rr))

    need_rows = [x for x in inq_rows if x["정답"]]
    noneed_rows = [x for x in inq_rows if not x["정답"]]
    runs.append({
        "ride": {"전체": acc(ride_rows), "명시적": acc(ride_rows, "E"), "간접·복합": acc(ride_rows, "I"),
                 "LLM 응답 실패(규칙 대체)": sum(1 for x in ride_rows if x["엔진"] != "llm")},
        "inq": {"전체": acc(inq_rows), "명시적": acc(inq_rows, "E"), "간접·복합": acc(inq_rows, "I"),
                "상담 필요 문의 연결률": pct(sum(x["응답"] for x in need_rows), len(need_rows)),
                "불필요 연결률": pct(sum(x["응답"] for x in noneed_rows), len(noneed_rows)),
                "LLM 응답 실패(규칙 대체)": sum(1 for x in inq_rows if x["엔진"] != "ai")},
        "detail": {"ride": ride_rows, "inq": inq_rows},
    })


def summary(key, field):
    xs = [r[key][field] for r in runs if r[key][field] is not None]
    if not xs:
        return None
    return {"평균": round(st.mean(xs), 1), "최소": min(xs), "최대": max(xs)}


out = {"모델": f"{settings.llm_provider}/{MODEL}", "반복": args.repeat,
       "탑승 중 요청 기능 선택(48건)": {f: summary("ride", f) for f in ("전체", "명시적", "간접·복합")},
       "이용 후 문의 상담 연결 판단(24건)": {f: summary("inq", f) for f in
                                       ("전체", "명시적", "간접·복합", "상담 필요 문의 연결률", "불필요 연결률")},
       "LLM 응답 실패 횟수": {"탑승": [r["ride"]["LLM 응답 실패(규칙 대체)"] for r in runs],
                         "문의": [r["inq"]["LLM 응답 실패(규칙 대체)"] for r in runs]},
       "회차별": [{k: v for k, v in r.items() if k != "detail"} for r in runs],
       "상세(1회차)": runs[0]["detail"]}

with open(os.path.join(HERE, "eval_llm_result.json"), "w", encoding="utf-8") as fp:
    json.dump(out, fp, ensure_ascii=False, indent=2)

print(f"\n완료 ({time.time() - t0:.0f}초) — eval_llm_result.json 저장")
print("탑승 중 요청 기능 선택 정확도(%):", out["탑승 중 요청 기능 선택(48건)"])
print("이용 후 문의 상담 연결 판단 정확도(%):", out["이용 후 문의 상담 연결 판단(24건)"])
print("LLM 응답 실패(규칙으로 대체된 건수):", out["LLM 응답 실패 횟수"])
print("\n틀린 문장(1회차):")
for x in runs[0]["detail"]["ride"]:
    if not x["일치"]:
        print(f"  [탑승] {x['발화']}  정답 {x['정답']}  응답 {x['응답']}")
for x in runs[0]["detail"]["inq"]:
    if not x["일치"]:
        print(f"  [문의] {x['문의']}  정답 {x['정답']}  응답 {x['응답']}")
