#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ROBITA 논문 2.4 '시스템 동작 및 평가' 측정 스크립트.

실제 프로젝트 코드(app/)를 그대로 import 해서 시뮬레이터를 돌린다.
LLM 키를 비워 규칙 모드로 실행한다(LLM 판단 정확도는 eval_llm.py 로 따로 잰다).

  E1  자동 재배치 A/B    재배치 ON/OFF × 4개 시간대 × N개 시드
  E2  충돌 시나리오       9종 × N회 · 오케스트레이터 판단(화면 표시) + 실제 동작
  E3  운영 시나리오       정상 배차 / 수요 불균형 / 배터리 부족 / 사고 · 기대 조치 수행률
  E4  배차 판단           무작위 호출 배정 결과(운행 중 차량 선택 여부, 점수식 vs 최근접)
  E5  고객 AI 판단(규칙)  testsets.RIDE / INQ 에 대한 키워드 규칙 정확도

사용:  python evaluation/robita_eval.py [프로젝트 폴더] [--seeds 20] [--out result.json]
       (프로젝트 폴더를 생략하면 이 파일의 위치에서 app/ 폴더를 찾아 자동으로 정한다)

시뮬레이션 조건
  - 시계: 실시간 모드를 끄고 시나리오 시각에 고정(수요 조건 고정), 요일은 수요일
  - 도로 경로: 카카오 길찾기 대신 격자형(L자) 경로로 근사 → 키 없이도 실제 주행 시간 반영
  - 그 밖의 규칙(배차 예산, 충전 임계치, 재배치 주기 등)은 코드 그대로
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import statistics as st
import sys
import time

# ── 0. 규칙 모드 고정 + 프로젝트 import ─────────────────────────────
for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "KAKAO_REST_KEY"):
    os.environ[k] = ""

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_root(arg):
    """프로젝트 폴더(app/ 가 있는 곳)를 찾는다. 인자 → 이 파일 위치 → 상위 폴더 순."""
    for cand in ([arg] if arg else []) + [HERE, os.path.dirname(HERE)]:
        if cand and os.path.isdir(os.path.join(cand, "app")):
            return os.path.abspath(cand)
    sys.exit("app 폴더를 찾지 못했습니다. ROBITA 프로젝트 폴더 경로를 인자로 주세요.")


ap = argparse.ArgumentParser()
ap.add_argument("root", nargs="?", default=None)
ap.add_argument("--seeds", type=int, default=20)
ap.add_argument("--out", default=os.path.join(HERE, "result.json"))
ap.add_argument("--only", default="")          # 예: --only E1,E2
ARGS = ap.parse_args()
ARGS.root = _find_root(ARGS.root)
sys.path.insert(0, ARGS.root)
sys.path.insert(1, HERE)

from app import area, routing                     # noqa: E402
from app import simulation as S                   # noqa: E402
from app.llm import assignment, incident          # noqa: E402
import app.main as M                              # noqa: E402
from app import rider as R                        # noqa: E402
from app import ops as OPS, support as SUP        # noqa: E402
import testsets                                   # noqa: E402

KM_LAT = 111.32


def manhattan_km(lat1, lng1, lat2, lng2):
    return (abs(lat2 - lat1) * KM_LAT
            + abs(lng2 - lng1) * KM_LAT * math.cos(math.radians((lat1 + lat2) / 2)))


# ── 1. 도로 경로 근사(L자 격자 경로) ───────────────────────────────
def _grid_route(o, d, *a, **k):
    la1, ln1, la2, ln2 = o["lat"], o["lng"], d["lat"], d["lng"]
    # 방향을 섞기 위해 좌표 해시로 꺾는 순서를 정한다(결정적)
    mid = (la2, ln1) if (int(la1 * 1e4) + int(ln2 * 1e4)) % 2 == 0 else (la1, ln2)
    pts = [{"lat": la1, "lng": ln1}, {"lat": mid[0], "lng": mid[1]},
           {"lat": la2, "lng": ln2}]
    dist = manhattan_km(la1, ln1, la2, ln2)
    return {"points": pts, "distance_km": round(dist, 2),
            "eta_min": max(1, round(dist / 24 * 60)), "taxi_fare": None}


routing.available = lambda: True
routing.cached_route = _grid_route
routing.road_route = _grid_route
routing.prefetch = lambda *a, **k: None
routing.route_options = lambda o, d: {"recommend": _grid_route(o, d), "free": _grid_route(o, d)}

# ── 2. 시계·요일 고정 ──────────────────────────────────────────────
DOW = 2  # 수요일(평일, 금요일·주말 특수 패턴 제외)
S.FleetSimulator.current_dow = lambda self: DOW


def make_fleet(seed: int, hour: float) -> "S.FleetSimulator":
    S._wall_minutes = lambda: hour * 60.0          # 생성 시점 시각도 고정
    f = S.FleetSimulator(seed=seed)
    f.time_realtime = False
    f.time_paused = True
    f.sim_minutes = hour * 60.0
    # 다른 모듈이 참조하는 전역 fleet 을 이 인스턴스로 교체
    for mod in (assignment, incident, M, R, OPS, SUP):
        mod.fleet = f
    return f


def hotspots(f):
    out = []
    for name, w in f.current_hotspots():
        a = next((x for x in area.ANCHORS if x[0] == name), None)
        if a:
            out.append((name, a[1], a[2], float(w)))
    return out


def idle_ok(v) -> bool:
    """호출을 받을 수 있는 빈 차량 = 대기 중이거나 빈 차로 재배치 이동 중,
    사고 아님·배터리 30% 이상·세단(배차 후보 조건과 같다)."""
    return ((v.status == S.STATUS_WAITING
             or (v.status == S.STATUS_DRIVING and v.repositioning))
            and not v.incident and v.car_type != "SUV" and v.battery >= 30)


def occupied(v) -> bool:
    """승객을 태웠거나 태우러 가는 차량."""
    return v.status in (S.STATUS_DRIVING, S.STATUS_PICKUP) and not v.repositioning


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return st.pstdev(xs) if len(xs) > 1 else 0.0


def r(x, n=2):
    return None if x is None else round(x, n)


# ══════════════════════════════════════════════════════════════════
# E1. 자동 재배치 A/B
# ══════════════════════════════════════════════════════════════════
BANDS = [("출근(08시)", 8), ("주간(13시)", 13), ("퇴근(18시)", 18), ("심야(23시)", 23)]
WARMUP, TICKS, EVERY, NREQ = 300, 1200, 10, 40


def shortage(f, coords):
    """수요 대비 공급 부족률.
    수요(need)는 대시보드 demand_map 과 같은 식, 공급은 '핫스팟 반경 0.9km 안에 실제로 있는
    대기 차량'만 센다(이동 중인 재배치 차량은 제외 → 보수적)."""
    d = f.current_demand()
    total_w = sum(c[3] for c in coords)
    fleet_n = sum(1 for v in f.vehicles.values() if v.car_type != "SUV")
    pool = fleet_n * S.REBALANCE_FRACTION * max(0.25, d)
    need = {c[0]: pool * c[3] / total_w for c in coords}
    sup = {c[0]: 0 for c in coords}
    for v in f.vehicles.values():
        if v.status == S.STATUS_WAITING and not v.incident and v.car_type != "SUV":
            best = min(coords, key=lambda c: S._haversine_km(v.lat, v.lng, c[1], c[2]))
            if S._haversine_km(v.lat, v.lng, best[1], best[2]) <= S.REBALANCE_RADIUS_KM:
                sup[best[0]] += 1
    short = sum(max(0.0, need[k] - sup[k]) for k in need)
    spots = sum(1 for k in need if need[k] - sup[k] >= 1.0)
    return short / sum(need.values()), spots


def pickup_eta(f, origins):
    """각 호출 지점에서 가장 가까운 배차 가능 빈 차량까지의 예상 도착 시간(분, 24km/h)."""
    idle = [v for v in f.vehicles.values() if idle_ok(v)]
    out = []
    for o in origins:
        if not idle:
            out.append(None)
            continue
        km = min(manhattan_km(o[1], o[2], v.lat, v.lng) for v in idle)
        out.append(km / 24.0 * 60.0)
    return out


def e1_run(seed, hour, rebalance_on):
    f = make_fleet(seed, hour)
    if not rebalance_on:
        f._rebalance_hotspots = lambda: None
    rec = {"km": 0.0, "n": 0}
    orig = f._send_idle_to

    def counted(v, name, lat, lng, zone):
        if f._tick_count > WARMUP:                # 측정 구간만 집계
            rec["km"] += manhattan_km(v.lat, v.lng, lat, lng)
            rec["n"] += 1
        return orig(v, name, lat, lng, zone)

    f._send_idle_to = counted
    coords = hotspots(f)
    req_rng = random.Random(seed * 7919 + hour)     # ON/OFF 가 같은 호출 표본을 쓰도록
    sr, sp, ef, eu, idle_n, unserved = [], [], [], [], [], 0
    trips0 = None
    for t in range(1, TICKS + 1):
        f.tick()
        if t == WARMUP:
            trips0 = f.session_trips
        if t > WARMUP and t % EVERY == 0:
            a, b = shortage(f, coords)
            sr.append(a)
            sp.append(b)
            o_fc = req_rng.choices(coords, weights=[c[3] for c in coords], k=NREQ)
            o_un = req_rng.choices(area.ANCHORS, k=NREQ)
            e_fc = pickup_eta(f, o_fc)
            e_un = pickup_eta(f, o_un)
            unserved += sum(1 for x in e_fc if x is None)
            ef += [x for x in e_fc if x is not None]
            eu += [x for x in e_un if x is not None]
            idle_n.append(sum(1 for v in f.vehicles.values() if idle_ok(v)))
    hours = (TICKS - WARMUP) * S.settings.tick_seconds / 3600.0
    n_cars = sum(1 for v in f.vehicles.values() if v.car_type != "SUV")
    return {
        "short_rate": mean(sr), "deficit_spots": mean(sp),
        "eta_forecast": mean(ef), "eta_uniform": mean(eu),
        "within5_forecast": (sum(1 for x in ef if x <= 5.0) / len(ef)) if ef else None,
        "unserved_rate": unserved / (len(sr) * NREQ),
        "idle": mean(idle_n),
        "trips": f.session_trips - (trips0 or 0),
        "repos_moves": rec["n"],
        "empty_km_per_car_h": rec["km"] / n_cars / hours,
    }


def e1(seeds):
    out = {}
    for band, hour in BANDS:
        runs = {"OFF": [], "ON": []}
        for s in seeds:
            for mode in ("OFF", "ON"):
                runs[mode].append(e1_run(s, hour, mode == "ON"))
        row = {}
        for mode in ("OFF", "ON"):
            row[mode] = {k: {"mean": r(mean([x[k] for x in runs[mode]]), 4),
                             "sd": r(sd([x[k] for x in runs[mode]]), 4)}
                         for k in runs[mode][0]}
        off, on = row["OFF"], row["ON"]

        def red(k):
            a, b = off[k]["mean"], on[k]["mean"]
            return r(100 * (a - b) / a, 1) if a else None

        # 시드별로 짝지어 개선된 비율(ON 이 OFF 보다 좋은 시드 수)
        better = sum(1 for a, b in zip(runs["OFF"], runs["ON"]) if b["short_rate"] < a["short_rate"])
        better_eta = sum(1 for a, b in zip(runs["OFF"], runs["ON"])
                         if (b["eta_forecast"] or 0) < (a["eta_forecast"] or 0))
        row["개선"] = {
            "부족률 감소(%)": red("short_rate"),
            "부족 지점 수 감소(%)": red("deficit_spots"),
            "예측수요 픽업시간 단축(%)": red("eta_forecast"),
            "균등수요 픽업시간 단축(%)": red("eta_uniform"),
            "부족률 개선 시드": f"{better}/{len(seeds)}",
            "픽업시간 개선 시드": f"{better_eta}/{len(seeds)}",
        }
        out[band] = row
        print(f"  E1 {band}: 부족률 {off['short_rate']['mean']:.3f} → {on['short_rate']['mean']:.3f}"
              f" ({row['개선']['부족률 감소(%)']}% 감소), 픽업 {off['eta_forecast']['mean']:.2f} → "
              f"{on['eta_forecast']['mean']:.2f}분, 운행 {off['trips']['mean']:.1f} → {on['trips']['mean']:.1f}")
    # 4개 시간대 평균
    allr = {m: [] for m in ("OFF", "ON")}
    for band, _ in BANDS:
        for m in ("OFF", "ON"):
            allr[m].append(out[band][m])
    tot = {}
    for k in ("short_rate", "deficit_spots", "eta_forecast", "eta_uniform", "within5_forecast",
              "trips", "empty_km_per_car_h"):
        a = mean([x[k]["mean"] for x in allr["OFF"]])
        b = mean([x[k]["mean"] for x in allr["ON"]])
        tot[k] = {"OFF": r(a, 4), "ON": r(b, 4),
                  "변화(%)": r(100 * (b - a) / a, 1) if a else None}
    out["시간대 평균"] = tot
    return out


# ── E1-a. 수요 불균형 시나리오: 대기 차량이 수요 핫스팟 밖에 편중된 상황 ──
FAR = [a for a in area.ANCHORS
       if a[0] == "시화국가산업단지" or a in area.BAEGOT or a in area.OIDO]


def make_imbalance(seed):
    """13시(주간 상권 수요)에 대기 차량이 산업단지·배곧·오이도 쪽에 몰려 있는 상태를 만든다."""
    f = make_fleet(seed, 13)
    f._rebalance_hotspots = lambda: None            # 준비 구간은 재배치 없이 진행
    for _ in range(150):
        f.tick()
    rng = random.Random(seed * 31)
    for v in f.vehicles.values():
        if v.status == S.STATUS_WAITING and v.car_type != "SUV" and not v.incident:
            a = rng.choice(FAR)
            v.lat = a[1] + rng.uniform(-0.001, 0.001)
            v.lng = a[2] + rng.uniform(-0.001, 0.001)
            v.path, v.path_i, v.cruising = None, 0, False
            v.dest_name = v.dest_lat = v.dest_lng = None
    del f._rebalance_hotspots                         # 클래스 메서드(실제 재배치)로 복원
    return f


def score_pickup(f, origins):
    """배차 에이전트(assignment.assign, 점수식)가 실제로 고르는 차량까지의 픽업 시간(분)."""
    keep = assignment.fleet
    assignment.fleet = f
    try:
        out = []
        for o in origins:
            res = assignment.assign({"name": o[0], "lat": o[1], "lng": o[2]},
                                    {"name": "x", "lat": o[1] + 0.01, "lng": o[2]})
            ch = res.get("assigned")
            if not ch:
                out.append(None)
                continue
            v = f.vehicles[ch["id"]]
            out.append(manhattan_km(o[1], o[2], v.lat, v.lng) / 24.0 * 60.0)
        return out
    finally:
        assignment.fleet = keep


SIM_PICKUPS = area.AREAS["정왕동"]   # 시뮬레이터가 실제로 픽업 지점을 뽑는 범위(균등)


def e1_imbalance(seeds, minutes=30):
    rows = []
    for s in seeds:
        on = make_imbalance(s)
        off = copy.deepcopy(on)
        off._rebalance_hotspots = lambda: None        # 같은 상태에서 재배치만 끈 비교군
        coords = hotspots(on)
        s0 = shortage(on, coords)[0]
        req = random.Random(s * 101)
        rec = {k: {"short": [], "eta": [], "eta_score": [], "eta_sim": []} for k in ("ON", "OFF")}
        t_on, t_off = on.session_trips, off.session_trips
        r_on = on.repos_total
        for t in range(1, minutes * 30 + 1):           # 30틱 = 1분(틱 2초)
            on.tick()
            off.tick()
            if t % 30 == 0:
                o = req.choices(coords, weights=[c[3] for c in coords], k=NREQ)
                o2 = req.choices(SIM_PICKUPS, k=NREQ)
                for k, x in (("ON", on), ("OFF", off)):
                    rec[k]["short"].append(shortage(x, coords)[0])
                    rec[k]["eta"].append(mean([y for y in pickup_eta(x, o) if y is not None]))
                    rec[k]["eta_score"].append(mean([y for y in score_pickup(x, o) if y is not None]))
                    rec[k]["eta_sim"].append(mean([y for y in pickup_eta(x, o2) if y is not None]))
        rows.append({
            "s0": s0,
            "short10": {k: mean(rec[k]["short"][:10]) for k in rec},
            "short30": {k: mean(rec[k]["short"]) for k in rec},
            "eta10": {k: mean(rec[k]["eta"][:10]) for k in rec},
            "eta30": {k: mean(rec[k]["eta"]) for k in rec},
            "eta_score30": {k: mean(rec[k]["eta_score"]) for k in rec},
            "eta_sim30": {k: mean(rec[k]["eta_sim"]) for k in rec},
            "trips": {"ON": on.session_trips - t_on, "OFF": off.session_trips - t_off},
            "moves": on.repos_total - r_on,
        })

    def agg(key, k):
        return mean([x[key][k] for x in rows])

    def pct(a, b):
        return r(100 * (a - b) / a, 1) if a else None

    out = {"시행": len(rows), "시작 부족률": r(mean([x["s0"] for x in rows]), 3)}
    for key, lab in (("short10", "부족률 첫 10분"), ("short30", "부족률 30분"),
                     ("eta10", "예상 픽업(분) 첫 10분"), ("eta30", "예상 픽업(분) 30분"),
                     ("eta_score30", "점수식 배차 픽업(분) 30분"),
                     ("eta_sim30", "시뮬레이터 픽업분포 기준 픽업(분) 30분")):
        a, b = agg(key, "OFF"), agg(key, "ON")
        better = sum(1 for x in rows if x[key]["ON"] < x[key]["OFF"])
        diffs = [x[key]["OFF"] - x[key]["ON"] for x in rows]
        half = (2.093 if len(diffs) == 20 else 2.262) * st.stdev(diffs) / math.sqrt(len(diffs))
        out[lab] = {"재배치 미적용": r(a, 3), "재배치 적용": r(b, 3), "감소(%)": pct(a, b),
                    "적용이 더 나은 시행": f"{better}/{len(rows)}",
                    "감소폭 95% CI": [r(mean(diffs) - half, 3), r(mean(diffs) + half, 3)],
                    "표준편차(미적용/적용)": [r(sd([x[key]["OFF"] for x in rows]), 3),
                                        r(sd([x[key]["ON"] for x in rows]), 3)]}
    out["완료 운행(30분)"] = {"재배치 미적용": r(agg("trips", "OFF"), 1),
                          "재배치 적용": r(agg("trips", "ON"), 1)}
    out["재배치 이동 횟수(30분)"] = r(mean([x["moves"] for x in rows]), 1)
    print(f"  E1a 편중: 부족률(30분) {out['부족률 30분']}  픽업(첫10분) {out['예상 픽업(분) 첫 10분']}")
    print(f"       운행 {out['완료 운행(30분)']}  이동 {out['재배치 이동 횟수(30분)']}")
    return out


# ══════════════════════════════════════════════════════════════════
# 공통: 오케스트레이터 판단(화면 표시) 읽기
# ══════════════════════════════════════════════════════════════════
def orch_decision(v) -> str:
    logs = M._build_agent_log_core(v, use_llm=False)
    orch = next(x for x in logs if x["key"] == "orchestrator")
    text = next(s["text"] for s in orch["steps"] if s["label"] == "실행")
    for key, lab in (("인시던트", "인시던트 대응"), ("정비 전환", "정비"),
                     ("현재 배차 유지", "배차 유지"), ("충전", "충전")):
        if text.startswith(key):
            return lab
    return "재배치"


def pick(f, rng, pred):
    c = [v for v in f.vehicles.values() if pred(v)]
    return rng.choice(c) if c else None


def call_at(lat, lng, rng):
    d = rng.choice(area.ANCHORS)
    return ({"name": "호출지점", "lat": lat, "lng": lng},
            {"name": d[0], "lat": d[1], "lng": d[2]})


def assigned_id(origin, dest):
    res = assignment.assign(origin, dest)
    return (res.get("assigned") or {}).get("id")


def deficit_target(f):
    """대시보드 기준 가장 부족한 핫스팟."""
    dm = f.demand_map()
    return dm[0] if dm else None


def rebalance_target(f):
    """재배치 에이전트가 지금 차량을 보낼 핫스팟(복사본에서 한 번 실행해 확인)."""
    g = copy.deepcopy(f)
    before = {k for k, v in g.vehicles.items() if v.repositioning}
    g._rebalance_hotspots()
    sent = [v for k, v in g.vehicles.items() if v.repositioning and k not in before]
    if not sent:
        return None
    a = next(x for x in area.ANCHORS if x[0] == sent[0].dest_name)
    return {"name": a[0], "lat": a[1], "lng": a[2]}


def place_near(v, spot, km=1.05, bearing=0.0):
    """핫스팟에서 km 만큼 떨어진 곳(재배치 대상이 되는 최소 거리 0.9km 바로 밖)에 차량을 둔다."""
    dlat = km / KM_LAT * math.cos(bearing)
    dlng = km / (KM_LAT * math.cos(math.radians(spot["lat"]))) * math.sin(bearing)
    v.lat, v.lng = spot["lat"] + dlat, spot["lng"] + dlng
    v.path, v.path_i, v.cruising, v.cruise_idle = None, 0, False, 999


def would_rebalance(f, vid, mutate):
    """반사실 확인: 제약 조건만 풀었을 때 이 차량이 재배치되는가? (복사본에서 실행)"""
    g = copy.deepcopy(f)
    mutate(g.vehicles[vid])
    g._rebalance_hotspots()
    return g.vehicles[vid].repositioning


def would_assign(f, vid, mutate, origin, dest):
    """반사실 확인: 제약 조건만 풀었을 때 이 차량이 배정되는가?"""
    g = copy.deepcopy(f)
    mutate(g.vehicles[vid])
    assignment.fleet = g
    try:
        return assigned_id(origin, dest) == vid
    finally:
        assignment.fleet = f


# ══════════════════════════════════════════════════════════════════
# E2. 충돌 시나리오(에이전트 요청이 같은 차량에 겹치는 경우)
#     정책: 인시던트 대응 → 정비 → 기존 배차 유지 → 충전 → 재배치
# ══════════════════════════════════════════════════════════════════
def _passenger_car(v):
    return (v.status == S.STATUS_DRIVING and v.passenger and not v.repositioning
            and v.car_type != "SUV")


def c1(seed):  # 인시던트(사고) vs 기존 배차 유지
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: _passenger_car(x) and x.battery > 50)
    f.event_accident(v.id)
    lab = orch_decision(v)
    f.tick()
    o, d = call_at(v.lat, v.lng, rng)
    beh = (v.status == S.STATUS_MAINTENANCE and v.incident and v.passenger is None
           and any(t.target_id == v.id for t in f.tows.values())
           and bool(v.replacement_id) and assigned_id(o, d) != v.id)
    return "인시던트 대응", lab, beh, True


def c2(seed):  # 인시던트(고장) vs 충전
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    v.battery = 22.0
    f.event_breakdown(v.id)
    lab = orch_decision(v)
    for _ in range(3):
        f.tick()
    beh = v.status == S.STATUS_MAINTENANCE and v.incident is not None
    return "인시던트 대응", lab, beh, True


def c3(seed):  # 인시던트 vs 신규 배차
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    v.battery, v.cleanliness = 95.0, 95.0  # 제약이 없다면 확실히 1순위가 되도록
    o, d = call_at(v.lat, v.lng, rng)
    f.event_accident(v.id)
    valid = would_assign(f, v.id, lambda x: (setattr(x, "incident", None),
                                             setattr(x, "status", S.STATUS_WAITING)), o, d)
    lab = orch_decision(v)
    beh = assigned_id(o, d) != v.id
    return "인시던트 대응", lab, beh, valid


def c4(seed):  # 인시던트 vs 재배치
    f = make_fleet(seed, 13); rng = random.Random(seed)
    spot = rebalance_target(f)
    if spot is None:
        return None, None, None, False
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    v.battery = 80.0
    place_near(v, spot, bearing=rng.uniform(0, 6.28))
    f.event_accident(v.id)
    valid = would_rebalance(f, v.id, lambda x: (setattr(x, "incident", None),
                                                setattr(x, "status", S.STATUS_WAITING)))
    lab = orch_decision(v)
    f._rebalance_hotspots()
    beh = not v.repositioning and v.status == S.STATUS_MAINTENANCE
    return "인시던트 대응", lab, beh, valid


def c5(seed):  # 기존 배차 유지 vs 충전 (승객 탑승 중 배터리 위험)
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, _passenger_car)
    v.battery = 25.0
    lab = orch_decision(v)
    trip, dest = v.passenger, v.trip_dest_name
    kept = True
    dropped = False
    for _ in range(1500):
        f.tick()
        if v.passenger is None:              # 하차(운행 완료)
            dropped = v.status in (S.STATUS_WAITING, S.STATUS_CHARGING)
            break
        if v.passenger != trip or v.status != S.STATUS_DRIVING or v.dest_name != dest:
            kept = False                     # 운행 도중 다른 조치로 전환됨
            break
    charged = False
    if dropped:
        for _ in range(3):
            f.tick()
            if v.status == S.STATUS_CHARGING:
                charged = True
                break
    beh = kept and dropped and charged
    return "배차 유지", lab, beh, True


def c6(seed):  # 기존 배차 유지 vs 신규 배차 (운행 중 차량 위치에서 호출)
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, _passenger_car)
    v.battery, v.cleanliness = 95.0, 95.0  # 제약이 없다면 확실히 1순위가 되도록
    o, d = call_at(v.lat, v.lng, rng)
    valid = would_assign(f, v.id, lambda x: (setattr(x, "status", S.STATUS_WAITING),
                                             setattr(x, "passenger", None)), o, d)
    lab = orch_decision(v)
    beh = assigned_id(o, d) != v.id
    return "배차 유지", lab, beh, valid


def c7(seed):  # 기존 배차 유지 vs 재배치
    f = make_fleet(seed, 13); rng = random.Random(seed)
    spot = rebalance_target(f)
    if spot is None:
        return None, None, None, False
    v = pick(f, rng, lambda x: _passenger_car(x) and x.battery > 50)
    place_near(v, spot, bearing=rng.uniform(0, 6.28))
    valid = would_rebalance(f, v.id, lambda x: (setattr(x, "status", S.STATUS_WAITING),
                                                setattr(x, "passenger", None)))
    lab = orch_decision(v)
    before = v.passenger
    f._rebalance_hotspots()
    beh = (not v.repositioning) and v.passenger == before
    return "배차 유지", lab, beh, valid


def c8(seed):  # 충전 vs 신규 배차 (충전 중 차량 위치에서 호출)
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    f.event_charge(v.id)
    v.battery, v.cleanliness = 95.0, 95.0  # 충전 거의 완료 · 제약이 없다면 1순위
    o, d = call_at(v.lat, v.lng, rng)
    valid = would_assign(f, v.id, lambda x: setattr(x, "status", S.STATUS_WAITING), o, d)
    lab = orch_decision(v)
    beh = assigned_id(o, d) != v.id
    return "충전", lab, beh, valid


def c9(seed):  # 충전 vs 재배치 (배터리 위험 대기 차량)
    f = make_fleet(seed, 13); rng = random.Random(seed)
    spot = rebalance_target(f)
    if spot is None:
        return None, None, None, False
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    place_near(v, spot, bearing=rng.uniform(0, 6.28))
    v.battery = 25.0
    valid = would_rebalance(f, v.id, lambda x: setattr(x, "battery", 80.0))
    lab = orch_decision(v)
    f._rebalance_hotspots()
    moved = v.repositioning
    f.tick()
    beh = (not moved) and v.status == S.STATUS_CHARGING
    return "충전", lab, beh, valid


CONFLICTS = [
    ("C1 사고 ↔ 기존 배차(승객 탑승)", c1),
    ("C2 고장 ↔ 충전(배터리 위험)", c2),
    ("C3 사고 ↔ 신규 배차", c3),
    ("C4 사고 ↔ 재배치", c4),
    ("C5 기존 배차 ↔ 충전(탑승 중 배터리 위험)", c5),
    ("C6 기존 배차 ↔ 신규 배차", c6),
    ("C7 기존 배차 ↔ 재배치", c7),
    ("C8 충전 ↔ 신규 배차", c8),
    ("C9 충전 ↔ 재배치", c9),
]


def e2(seeds):
    out, tot = {}, {"n": 0, "label": 0, "beh": 0, "both": 0, "invalid": 0}
    for name, fn in CONFLICTS:
        rows = []
        invalid = 0
        cand = iter(range(seeds[0], seeds[0] + 10_000))
        while len(rows) < len(seeds):
            s = next(cand)
            exp, lab, beh, valid = fn(s)
            if not valid:                 # 충돌이 성립하지 않는 배치 → 다음 시드로 교체
                invalid += 1
                continue
            rows.append((exp, lab, bool(beh)))
        tot["invalid"] += invalid
        n = len(rows)
        lab_ok = sum(1 for e, l, b in rows if l == e)
        beh_ok = sum(1 for e, l, b in rows if b)
        both = sum(1 for e, l, b in rows if l == e and b)
        labels = {}
        for e, l, b in rows:
            labels[l] = labels.get(l, 0) + 1
        out[name] = {"기대 조치": rows[0][0], "표시 판단 일치": f"{lab_ok}/{n}",
                     "실제 동작 일치": f"{beh_ok}/{n}", "둘 다 일치": f"{both}/{n}",
                     "표시 판단 분포": labels, "교체된 배치": invalid}
        tot["n"] += n; tot["label"] += lab_ok; tot["beh"] += beh_ok; tot["both"] += both
        print(f"  E2 {name}: 판단 {lab_ok}/{n} · 동작 {beh_ok}/{n} · 표시 {labels} · 교체 {invalid}")
    out["합계"] = {"시나리오 수": tot["n"],
                 "표시 판단 일치율(%)": r(100 * tot["label"] / tot["n"], 1),
                 "실제 동작 일치율(%)": r(100 * tot["beh"] / tot["n"], 1),
                 "둘 다 일치(%)": r(100 * tot["both"] / tot["n"], 1),
                 "충돌 불성립으로 교체한 배치": tot["invalid"]}
    return out


# ══════════════════════════════════════════════════════════════════
# E3. 운영 시나리오 4종 — 기대 조치 수행 여부
# ══════════════════════════════════════════════════════════════════
def s_normal(seed):
    """정상 운행: 호출 → 배차 가능한 차량(대기·빈 차, 배터리 30%↑, 사고 아님) 배정."""
    f = make_fleet(seed, 13); rng = random.Random(seed)
    for _ in range(200):
        f.tick()
    ok = 0
    n = 10
    coords = hotspots(f)
    for _ in range(n):
        o = rng.choices(coords, weights=[c[3] for c in coords])[0]
        org, dst = call_at(o[1], o[2], rng)
        vid = assigned_id(org, dst)
        v = f.vehicles.get(vid) if vid else None
        if v and not occupied(v) and not v.incident and v.battery >= 30:
            ok += 1
    return ok, n


def s_imbalance(seed):
    """수요 불균형: 대기 차량이 핫스팟 밖에 몰린 상태 → 재배치 1주기(25틱) 안에
    부족한 핫스팟으로 대기 차량 이동을 시작하면 성공."""
    f = make_imbalance(seed)
    short = {x["name"] for x in f.demand_map() if x["gap"] < 0}
    for _ in range(S.REBALANCE_INTERVAL):
        f.tick()
        if any(v.repositioning and v.dest_name in short for v in f.vehicles.values()):
            return 1, 1
    return 0, 1


def s_battery(seed):
    """배터리 부족: 대기 차량 배터리 20% → 다음 틱 충전 전환 → 충전 완료 후 대기 복귀."""
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: x.status == S.STATUS_WAITING and x.car_type != "SUV")
    f.event_drain(v.id, level=20.0)
    f.tick()
    to_charge = v.status == S.STATUS_CHARGING
    back = False
    for _ in range(2000):
        f.tick()
        if v.status == S.STATUS_WAITING and v.battery >= 99.9:
            back = True
            break
    return int(to_charge and back), 1


def s_accident(seed):
    """사고: 견인차 출동 → 대체 차량 투입 → 배차·재배치 제외 → 수리 후 운행 복귀."""
    f = make_fleet(seed, 13); rng = random.Random(seed)
    v = pick(f, rng, lambda x: _passenger_car(x))
    f.event_accident(v.id)
    f.tick()
    tow = any(t.target_id == v.id for t in f.tows.values())
    rep = bool(v.replacement_id) and v.replacement_id in f.vehicles
    o, d = call_at(v.lat, v.lng, rng)
    excluded = assigned_id(o, d) != v.id
    back = False
    for _ in range(3000):
        f.tick()
        if v.incident is None and v.status in (S.STATUS_WAITING, S.STATUS_CHARGING):
            back = True
            break
    return int(tow and rep and excluded and back), 1


def e3(seeds):
    out = {}
    for name, fn in (("정상 운행(배차)", s_normal), ("수요 불균형(재배치)", s_imbalance),
                     ("배터리 부족(충전)", s_battery), ("차량 사고(인시던트)", s_accident)):
        ok = n = 0
        for s in seeds:
            a, b = fn(s)
            ok += a; n += b
        out[name] = {"성공": ok, "시행": n, "수행률(%)": r(100 * ok / n, 1)}
        print(f"  E3 {name}: {ok}/{n}")
    return out


# ══════════════════════════════════════════════════════════════════
# E4. 배차 판단 — 무작위 호출
# ══════════════════════════════════════════════════════════════════
def e4(seeds):
    res = {"calls": 0, "occupied": 0, "agree_nearest": 0, "none": 0,
           "eta_score": [], "eta_near": [], "bat_score": [], "bat_near": [],
           "low_bat_score": 0, "low_bat_near": 0}
    by_band = {}
    for band, hour in BANDS:
        bc = bo = bn_ = 0
        for s in seeds:
            f = make_fleet(s, hour); rng = random.Random(s + 101)
            coords = hotspots(f) or [(a[0], a[1], a[2], 1.0) for a in area.ANCHORS]
            for t in range(1, 601):
                f.tick()
                if t >= 200 and t % 50 == 0:
                    for _ in range(5):
                        o = rng.choices(coords, weights=[c[3] for c in coords])[0]
                        org, dst = call_at(o[1], o[2], rng)
                        res_a = assignment.assign(org, dst)
                        ch = res_a.get("assigned")
                        if not ch:
                            res["none"] += 1; bn_ += 1
                            continue
                        v = f.vehicles[ch["id"]]
                        res["calls"] += 1; bc += 1
                        if occupied(v):
                            res["occupied"] += 1; bo += 1
                        # 비교: 빈 차량만 놓고 점수식 vs 최근접
                        cands = [c for c in assignment._candidates(org["lat"], org["lng"], limit=100)
                                 if not occupied(f.vehicles[c["id"]])][:8]
                        if len(cands) >= 1:
                            for c in cands:
                                c["score"] = assignment._score_parts(c)["total"]
                            bs = max(cands, key=lambda c: c["score"])
                            bn = min(cands, key=lambda c: (c["eta_min"], c["distance_km"]))
                            res["agree_nearest"] += int(bs["id"] == bn["id"])
                            res["eta_score"].append(bs["eta_min"]); res["eta_near"].append(bn["eta_min"])
                            res["bat_score"].append(bs["battery"]); res["bat_near"].append(bn["battery"])
                            res["low_bat_score"] += int(bs["battery"] < 50)
                            res["low_bat_near"] += int(bn["battery"] < 50)
        by_band[band] = {"배정된 호출": bc, "운행 중 차량 배정": bo,
                         "비율(%)": r(100 * bo / bc, 1) if bc else None,
                         "배정 불가(빈 차 없음)": bn_}
        print(f"  E4 {band}: 운행 중 차량 배정 {bo}/{bc} · 배정 불가 {bn_}")
    n = len(res["eta_score"])
    return {
        "배정된 호출 수": res["calls"], "배정 불가": res["none"],
        "운행 중(승객) 차량 배정 비율(%)": r(100 * res["occupied"] / res["calls"], 1),
        "시간대별": by_band,
        "빈 차량 기준 비교": {
            "비교 호출": n,
            "점수식·최근접 동일 선택(%)": r(100 * res["agree_nearest"] / n, 1),
            "평균 ETA(분) 점수식": r(mean(res["eta_score"])),
            "평균 ETA(분) 최근접": r(mean(res["eta_near"])),
            "평균 배터리(%) 점수식": r(mean(res["bat_score"]), 1),
            "평균 배터리(%) 최근접": r(mean(res["bat_near"]), 1),
            "배터리 50% 미만 배정(%) 점수식": r(100 * res["low_bat_score"] / n, 1),
            "배터리 50% 미만 배정(%) 최근접": r(100 * res["low_bat_near"] / n, 1),
        },
    }


# ══════════════════════════════════════════════════════════════════
# E5. 고객 AI 판단 — 규칙(키워드) 모드 정확도
# ══════════════════════════════════════════════════════════════════
def norm_tool(d: dict):
    tool = str(d.get("tool") or "none")
    if tool == "out_of_area":
        tool = "none"
    arg = None
    if tool in ("change_destination", "add_stop"):
        arg = d.get("place")
    elif tool in ("set_aircon", "set_speed"):
        arg = d.get("mode")
    return tool, arg


def e5():
    svc = R.RiderService()
    detail, agg = [], {"E": [0, 0], "I": [0, 0]}
    for typ, text, tool, arg in testsets.RIDE:
        got = norm_tool(svc._agent_fallback(text))
        ok = got == (tool, arg)
        agg[typ][0] += int(ok); agg[typ][1] += 1
        detail.append({"유형": typ, "발화": text, "정답": [tool, arg], "규칙": list(got), "일치": ok})
    ride = {"전체(%)": r(100 * (agg["E"][0] + agg["I"][0]) / (agg["E"][1] + agg["I"][1]), 1),
            "명시적(%)": r(100 * agg["E"][0] / agg["E"][1], 1),
            "간접·복합(%)": r(100 * agg["I"][0] / agg["I"][1], 1),
            "건수": agg["E"][1] + agg["I"][1]}

    inq_d, ag2 = [], {"E": [0, 0], "I": [0, 0]}
    tp = fp = fn_ = tn = 0
    for typ, text, need in testsets.INQ:
        res = svc.post_ride_inquiry("eval", text)
        got = bool(res.get("need_agent"))
        lost = res.get("action") == "lost"
        ok = (got == need) and not lost
        ag2[typ][0] += int(ok); ag2[typ][1] += 1
        tp += int(need and got); fn_ += int(need and not got)
        fp += int(not need and got); tn += int(not need and not got)
        inq_d.append({"유형": typ, "문의": text, "정답": need, "규칙": got, "분실물오분류": lost, "일치": ok})
    inq = {"전체(%)": r(100 * (ag2["E"][0] + ag2["I"][0]) / len(testsets.INQ), 1),
           "명시적(%)": r(100 * ag2["E"][0] / ag2["E"][1], 1),
           "간접·복합(%)": r(100 * ag2["I"][0] / ag2["I"][1], 1),
           "상담 필요 문의 연결률(재현율, %)": r(100 * tp / (tp + fn_), 1),
           "불필요 연결률(%)": r(100 * fp / (fp + tn), 1),
           "건수": len(testsets.INQ)}
    print(f"  E5 탑승 중 요청(규칙): {ride}")
    print(f"  E5 이용 후 문의(규칙): {inq}")
    return {"탑승 중 요청 기능 선택": ride, "이용 후 문의 상담 연결 판단": inq,
            "상세": {"ride": detail, "inq": inq_d}}


# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    seeds = list(range(1, ARGS.seeds + 1))
    only = set(x.strip() for x in ARGS.only.split(",") if x.strip())
    RES = {"조건": {"프로젝트": os.path.abspath(ARGS.root), "시드": seeds, "요일": "수",
                   "경로": "L자 격자 근사", "틱(초)": S.settings.tick_seconds,
                   "E1 측정": f"{WARMUP}틱 이후 {TICKS - WARMUP}틱, {EVERY}틱마다"}}
    t0 = time.time()
    for key, fn in (("E1", lambda: {"편중 시나리오": e1_imbalance(seeds),
                                    "정상 상태(참고)": e1(seeds)}),
                    ("E2", lambda: e2(seeds)),
                    ("E3", lambda: e3(list(range(1, 2 * ARGS.seeds + 1)))),
                    ("E4", lambda: e4(seeds)), ("E5", e5)):
        if only and key not in only:
            continue
        print(f"[{key}] 시작 ({time.time() - t0:.0f}s)")
        RES[key] = fn()
    with open(ARGS.out, "w", encoding="utf-8") as fp:
        json.dump(RES, fp, ensure_ascii=False, indent=2)
    print(f"완료 {time.time() - t0:.0f}s → {ARGS.out}")
