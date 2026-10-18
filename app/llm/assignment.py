"""차량 배정 모델."""

from __future__ import annotations

import math

from . import llm as llmsvc
from ..simulation import fleet, estimate_fare, STATUS_WAITING, STATUS_DRIVING


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _eta_minutes(dist_km: float) -> int:
    avg_kmh = 24.0  # 도심 평균 속도 가정
    return max(1, round(dist_km / avg_kmh * 60))


def _candidates(origin_lat: float, origin_lng: float, limit: int = 8,
                passengers: int = 1) -> list[dict]:
    """배차 후보. passengers 가 5명 이상이면 대형(SUV)만, 그 미만이면 일반(세단)만 고른다."""
    need_suv = passengers >= 5
    cands = []
    for v in fleet.vehicles.values():
        # 배차 가능 차량 추리기
        if v.incident:
            continue
        if v.status not in (STATUS_WAITING, STATUS_DRIVING):
            continue
        if v.status == STATUS_DRIVING and not v.repositioning:
            continue  # 승객 탑승 중인 차량은 제외(기존 배차 유지)
        if v.battery < 30:
            continue
        is_suv = getattr(v, "car_type", "SEDAN") == "SUV"
        if need_suv != is_suv:
            continue  # 5인 이상은 SUV, 4인 이하는 세단
        dist = _haversine_km(origin_lat, origin_lng, v.lat, v.lng)
        eta = _eta_minutes(dist)
        cands.append(
            {
                "id": v.id,
                "plate": v.plate,
                "status": v.status,
                "battery": round(v.battery, 1),
                "cleanliness": round(v.cleanliness, 0),
                "distance_km": round(dist, 2),
                "eta_min": eta,
            }
        )
    cands.sort(key=lambda c: c["eta_min"])
    return cands[:limit]


# 배정 점수 가중치(합 = 1.0)
W_ETA, W_BATT, W_CLEAN = 0.50, 0.30, 0.20
_ETA_REF_MIN = 30.0  # ETA 정규화 기준


def _score_parts(c: dict) -> dict:
    """후보 차량의 정규화 점수(각 0~1)와 가중 총점을 계산한다."""
    eta_s = max(0.0, 1.0 - c["eta_min"] / _ETA_REF_MIN)  # ETA 낮을수록 1
    bat_s = max(0.0, min(1.0, c["battery"] / 100.0))  # 배터리 높을수록 1
    cln_s = max(0.0, min(1.0, c["cleanliness"] / 100.0))  # 청결 높을수록 1
    total = W_ETA * eta_s + W_BATT * bat_s + W_CLEAN * cln_s
    return {"eta_s": round(eta_s, 2), "bat_s": round(bat_s, 2),
            "cln_s": round(cln_s, 2), "total": round(total, 3)}


def assign(origin: dict, destination: dict, predicted_demand: str = "보통",
           passengers: int = 1) -> dict:
    """origin/destination: {"name","lat","lng"}."""
    cands = _candidates(origin["lat"], origin["lng"], passengers=passengers)
    if not cands:
        return {
            "assigned": None,
            "reason": "현재 배차 가능한 차량이 없습니다. 인근 차량 재배치가 필요합니다.",
            "eta_min": None,
            "alternatives": [],
            "engine": "none",
        }

    # 목적지까지 거리/요금 추정
    trip_km = _haversine_km(
        origin["lat"], origin["lng"], destination["lat"], destination["lng"]
    )
    est_fare = estimate_fare(trip_km)

    # 점수 계산식으로 최적 차량 선택
    for c in cands:
        c["score_parts"] = _score_parts(c)
        c["score"] = c["score_parts"]["total"]
    cands.sort(key=lambda c: c["score"], reverse=True)  # 총점 높은 순
    chosen = cands[0]
    sp = chosen["score_parts"]
    # 사람이 읽는 계산식 (UI 표시용)
    reason = (
        f"배정점수 = {W_ETA:.2f}·ETA점수 + {W_BATT:.2f}·배터리점수 + {W_CLEAN:.2f}·청결점수<br>"
        f"{chosen['plate']} (ETA {chosen['eta_min']}분 · 배터리 {chosen['battery']}% · 청결 {int(chosen['cleanliness'])}점)<br>"
        f"= {W_ETA:.2f}×{sp['eta_s']:.2f} + {W_BATT:.2f}×{sp['bat_s']:.2f} + {W_CLEAN:.2f}×{sp['cln_s']:.2f} "
        f"= <b>{sp['total']:.3f}점</b> &nbsp;(후보 {len(cands)}대 중 1위)"
    )
    engine = "score"

    alternatives = [c for c in cands if c["id"] != chosen["id"]][:3]
    return {
        "assigned": chosen,
        "reason": reason,
        "score": chosen["score"],
        "weights": {"ETA": W_ETA, "battery": W_BATT, "clean": W_CLEAN},
        "eta_min": chosen["eta_min"],
        "trip_km": round(trip_km, 2),
        "est_fare": est_fare,
        "alternatives": alternatives,
        "engine": engine,
    }
