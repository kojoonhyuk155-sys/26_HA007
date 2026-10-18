"""로보택시 플릿 시뮬레이션 엔진.

status: DRIVING 운행중 / WAITING 대기중 / CHARGING 충전중 / MAINTENANCE 점검중
battery_state: DANGER 30% 이하 / CAUTION 50% 이하 / NORMAL
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

from . import area
from . import routing
from .config import settings

KST = timezone(timedelta(hours=9))

STATUS_DRIVING = "DRIVING"
STATUS_PICKUP = "PICKUP"  # 손님을 태우러 이동 중(승객 탑승 전)
STATUS_WAITING = "WAITING"
STATUS_CHARGING = "CHARGING"
STATUS_MAINTENANCE = "MAINTENANCE"

STATUS_LABELS = {
    STATUS_DRIVING: "목적지 이동중",
    STATUS_PICKUP: "손님 맞이중",
    STATUS_WAITING: "대기중",
    STATUS_CHARGING: "충전중",
    STATUS_MAINTENANCE: "점검중",
}

# 도넛 차트 범례 색상
STATUS_COLORS = {
    STATUS_DRIVING: "#36b37e",
    STATUS_PICKUP: "#14b8a6",
    STATUS_WAITING: "#2f6fed",
    STATUS_CHARGING: "#f5b301",
    STATUS_MAINTENANCE: "#e5484d",
}

HANGUL = list("가나다라마바사아자하허호배국")

# 우측통행 표시 보정(미터)
RIGHT_OFFSET_M = 3.0

# 시간대별 수요 곡선(0~1, 24시각)
DEMAND_CURVE = [
    0.15, 0.10, 0.08, 0.07, 0.08, 0.15,  # 0~5시 (심야)
    0.35, 0.75, 1.00, 0.70, 0.50, 0.55,  # 6~11시 (8시 출근 피크)
    0.65, 0.55, 0.45, 0.45, 0.55, 0.80,  # 12~17시 (점심·퇴근 시작)
    1.00, 0.85, 0.60, 0.50, 0.40, 0.25,  # 18~23시 (18시 퇴근 피크)
]
SIM_MIN_PER_TICK = 1.0  # 수요 시계용 가상 시각
SIM_START_MIN = 7 * 60  # 시작 가상 시각 07:00

# 실제 시간 기준 운영
MOVE_ACCEL = 1.0  # 주행 속도 배수. 1.0 = 실제 속도(km/h 그대로)

# 배터리·시간 설정
DRAIN_PER_KM     = 2.5  # 주행 소모 (이동 km당 %)
IDLE_DRAIN_TICK  = 0.01  # 대기 중 소모(틱당 %)
CHARGE_MINUTES   = 10  # 충전 소요 시간(가상 분)
REPAIR_MINUTES   = 60  # 사고/고장 수리 소요 시간(가상 분)
TOW_SPEED_KMH    = 30  # 견인차 이동 속도(km/h)
AMBULANCE_SPEED_KMH = 48  # 119 구급차 속도(km/h)
AMBULANCE_ONSCENE_TICKS = 20  # 현장 응급처치 시간(틱)
HOSPITAL_NAME = "시화병원"  # 부상자 이송 병원(구급차 도착지)
# 주행 중 사고 확률 (차량·틱당)
ACCIDENT_PROB_PER_TICK = 0.00002
CHARGE_THRESHOLD = 30  # 대기 중 이 % 이하면 충전소로 이동
DEPOT_RADIUS_KM  = 0.6  # 충전소 집계 반경
SHOP_RADIUS_KM   = 0.6  # 정비소 집계 반경
ROUTE_WAIT_MAX   = 45  # 경로 대기 최대 틱수
SUV_COUNT        = 3  # 대형(SUV) 택시 대수
SUV_MIN_PAX      = 5  # 이 인원 이상이면 SUV 로 배차

# 대기 중 배회
CRUISE_SPEED_KMH = 24  # 배회 속도(km/h)
CRUISE_IDLE_MIN  = 8  # 배회 후 정차하는 최소 틱수(약 16초)
CRUISE_IDLE_MAX  = 45  # 배회 후 정차하는 최대 틱수(약 1.5분)
CRUISE_START_PROB = 0.08  # 정차가 끝난 뒤 새 배회를 시작할 틱당 확률
CRUISE_MIN_BATT  = 33  # 이 % 미만이면 배회하지 않음
CRUISE_RADIUS_KM = 2.0  # 배회 목적지로 고를 지점의 최대 거리

# 수요 -> 목표 운행 비율 매핑
ACTIVE_RATIO_MIN = 0.12  # 수요 최저(심야) 시 목표 운행 비율
ACTIVE_RATIO_MAX = 0.82  # 수요 최고(러시아워) 시 목표 운행 비율
DISPATCH_CATCHUP = 0.5  # 목표까지의 부족분을 이번 틱에 얼마나 메울지(0~1)

# 수요 핫스팟 사전배치
# 대기 택시를 수요 핫스팟으로 이동
REBALANCE_INTERVAL = 25  # 재배치 판단 주기(틱)
REBALANCE_RADIUS_KM = 0.9  # 핫스팟 '근처'로 인정하는 반경(km)
REBALANCE_FRACTION = 0.6  # 대기 택시 중 핫스팟에 배치할 비율(나머지는 자유 대기)
REBALANCE_BATCH = 3  # 핫스팟 1회 배치 최대 대수
AUTO_BRIEF_MIN_GAP = 60  # 자동 재배치 브리핑 최소 간격(틱)


def _now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# engine 코드값 -> 표시용 이름
_ENGINE_LABELS = {
    "openai": "ChatGPT", "gemini": "Gemini",
    "score": "점수식", "rule": "규칙기반", "none": "없음", "ai": "AI",
}


def _engine_label(code: str) -> str:
    return _ENGINE_LABELS.get(code, code or "규칙기반")


def _wall_minutes() -> float:
    """현재 실제 시각(KST)을 자정 기준 '분'으로 반환(초 포함 → 부드럽게 진행)."""
    now = datetime.now(KST)
    return now.hour * 60 + now.minute + now.second / 60.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# 요금 모델 (기본 3800원 + 1300원/km, 최소 4800원)
FARE_BASE = 3800
FARE_PER_KM = 1300
FARE_MIN = 4800


def estimate_fare(distance_km: float | None) -> int:
    d = max(0.0, distance_km or 0.0)
    return max(FARE_MIN, round((FARE_BASE + d * FARE_PER_KM) / 100) * 100)


@dataclass
class Vehicle:
    id: str
    plate: str
    lat: float
    lng: float
    battery: float
    status: str
    rating: float
    cleanliness: float
    car_type: str = "SEDAN"  # SEDAN(일반 4인승) | SUV(대형 6인승)
    seats: int = 4  # 최대 탑승 인원
    speed: float = 0.0
    heading: float = 0.0
    passenger: str | None = None
    note: str = ""
    incident: dict | None = None  # {"type","desc","ts"}
    dest_name: str | None = None
    dest_lat: float | None = None
    dest_lng: float | None = None
    trips: int = 0  # 이 차량 누적 운행 건수
    trip_origin: str | None = None  # 현재 운행의 출발지 이름
    trip_distance_km: float | None = None  # 현재 운행의 예상 거리(km)
    trip_pickup_name: str | None = None  # 손님 픽업 지점 이름(표시용)
    trip_dest_name: str | None = None  # 최종 목적지 이름(표시용)
    trip_dest_lat: float | None = None  # 픽업 후 향할 최종 목적지
    trip_dest_lng: float | None = None
    trip_path: list | None = None  # 픽업 후 이동할 도로 경로
    manual: bool = False  # 운영자 수동 호출로 배차되었는지
    via_final: dict | None = None  # customer stop: continue to the final destination
    trip_seq: int | None = None  # 수동 호출 시 trips_log 항목 번호
    path: list | None = None  # 도로 경로 좌표열
    path_i: int = 0  # 경로에서 다음으로 향할 점의 인덱스
    route_wait: int = 0  # 도로 경로 준비를 기다린 틱 수
    home_area: str = "정왕동"  # 소속 구역(정왕동/배곧동/거북섬)
    maint_ticks: int = 0  # (구) 점검 남은 틱
    depot_anchor: tuple | None = None  # 차고지/정비소 이동 시 경로 출발 지점(스냅용)
    charge_elapsed: float = 0.0  # 충전 경과(가상 분)
    charge_start_batt: float = 0.0  # 충전 시작 시 배터리(%)
    repair_elapsed: float = 0.0  # 수리 경과(가상 분)
    towing: bool = False  # 견인 중
    shop_name: str | None = None  # 입고/이송 대상 정비소 이름
    is_replacement: bool = False  # 사고 차량 대체용 임시 차량
    replacement_id: str | None = None  # (원 차량) 자신을 대체 중인 임시 차량 id
    remove_pending: bool = False  # (대체 차량) 원 차량 복귀 → 대기 시 제거 예정
    repositioning: bool = False  # 수요예측 사전배치로 다른 구역으로 이동 중
    cruising: bool = False  # 대기 중 배회
    cruise_idle: int = 0  # 배회 사이 정차 시간(틱)
    route_seq: int = 0  # 경로 변경 시 증가
    tow_wait: bool = False  # 사고 지점에서 견인차를 기다리는 중
    await_board: bool = False  # 승차 완료 대기 중
    updated_at: str = field(default_factory=_now_iso)

    @property
    def battery_state(self) -> str:
        if self.status == STATUS_CHARGING:
            return "CHARGING"
        if self.battery <= 30:
            return "DANGER"
        if self.battery <= 50:
            return "CAUTION"
        return "NORMAL"

    @property
    def marker_color(self) -> str:
        # 마커 색상 우선순위: 충전 > 위험 > 주의 > 정상
        bs = self.battery_state
        return {
            "CHARGING": "#36b37e",
            "DANGER": "#e5484d",
            "CAUTION": "#f5b301",
            "NORMAL": "#1b1f33",
        }[bs]

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("path", "path_i", "route_wait", "home_area", "maint_ticks",
                  "depot_anchor", "charge_elapsed", "charge_start_batt",
                  "repair_elapsed", "replacement_id", "remove_pending",
                  "repositioning", "cruising", "cruise_idle", "tow_wait",
                  "trip_dest_lat", "trip_dest_lng", "trip_path", "via_final"):
            d.pop(k, None)  # 내부용 필드는 전송 payload 에서 제외
        # 현재 따라가는 도로 경로 (지도 표시용)
        if self.path and self.path_i < len(self.path):
            rem = self.path[self.path_i:]
            d["route"] = ([{"lat": round(self.lat, 6), "lng": round(self.lng, 6)}] +
                          [{"lat": round(p[0], 6), "lng": round(p[1], 6)} for p in rem])
        else:
            d["route"] = []
        # 우측통행 표시 보정
        if self.speed > 0.5 and self.status in (STATUS_DRIVING, STATUS_PICKUP, STATUS_CHARGING):
            brg = math.radians(self.heading + 90.0)  # 진행 방향의 오른쪽(시계방향 90°)
            d["lat"] = self.lat + (RIGHT_OFFSET_M * math.cos(brg)) / 111320.0
            d["lng"] = self.lng + (RIGHT_OFFSET_M * math.sin(brg)) / (
                111320.0 * max(0.1, math.cos(math.radians(self.lat))))
        d["status_label"] = STATUS_LABELS[self.status]
        if self.status == STATUS_MAINTENANCE:
            d["status_label"] = ("견인 대기중" if self.tow_wait
                                 else "견인중" if self.towing else "수리중")
        d["battery_state"] = self.battery_state
        d["marker_color"] = self.marker_color
        d["battery"] = round(self.battery, 1)
        d["cleanliness"] = round(self.cleanliness, 0)
        d["speed"] = round(self.speed, 0)
        return d


@dataclass
class Ambulance:
    """부상 신고 시 출동하는 119 구급차. 소방서에서 사고 지점으로 갔다가 병원으로 이송한다."""
    id: str
    lat: float
    lng: float
    base_name: str  # 출동한 소방서(거점)
    base_lat: float
    base_lng: float
    target_id: str  # 사고 차량 id
    phase: str = "to_scene"  # to_scene -> onscene -> to_hospital
    onscene_ticks: int = 0
    hosp_name: str = ""  # 환자를 이송할 병원(도착지)
    hosp_lat: float = 0.0
    hosp_lng: float = 0.0
    path: list | None = None
    path_i: int = 0
    route_wait: int = 0
    heading: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "lat": round(self.lat, 6), "lng": round(self.lng, 6),
                "phase": self.phase, "target": self.target_id,
                "base": self.base_name, "heading": round(self.heading)}


@dataclass
class TowTruck:
    """정비소에서 출동해 사고 차량을 싣고 돌아오는 견인차."""
    id: str
    lat: float
    lng: float
    shop_name: str
    shop_lat: float
    shop_lng: float
    target_id: str
    phase: str = "to_vehicle"  # to_vehicle -> to_shop
    path: list | None = None
    path_i: int = 0
    route_wait: int = 0
    heading: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "lat": round(self.lat, 6), "lng": round(self.lng, 6),
                "phase": self.phase, "target": self.target_id,
                "shop": self.shop_name, "heading": round(self.heading)}


class FleetSimulator:
    def __init__(self, size: int | None = None, seed: int = 7):
        self.rng = random.Random(seed)
        self.size = size or settings.fleet_size
        self.vehicles: dict[str, Vehicle] = {}
        self.events: list[dict] = []  # 최근 이벤트 로그 (신규가 앞)
        self.started_at = time.time()
        self.sim_minutes = _wall_minutes()  # 가상 시계
        self.time_scale = 1.0  # 시간 배속(1=기본). 콘솔에서 조정
        self.time_paused = False  # 시간 고정
        self.time_realtime = True  # 실시간 모드

        # 수요 기반 운행비율 제어
        self._active_target = 0  # 현재 수요가 요구하는 목표 '운행 중' 대수
        self._dispatch_budget = 0  # 이번 틱에 새로 배차할 수 있는 대수(목표까지 부족분)

        # 세션 누적값 + 오늘 기준치
        self.session_trips = 0
        self.session_revenue = 0
        self.baseline_trips = 160  # 시뮬레이션 시작 전 오늘 이미 발생한 운행
        self.baseline_revenue = 1_950_000

        # 전일(어제) 동시간대 기준치
        self.yesterday_trips = 180
        self.yesterday_revenue = 2_170_000
        self.yesterday_registered = 47
        self.yesterday_active = 23

        # 금일 이용내역(운행 기록) 로그
        self.trips_log: list[dict] = []
        self._trip_seq = 0

        # 차량별 에이전트 활동 이력
        self.agent_events: dict[str, list[dict]] = {}
        self._agent_seq = 0
        self._tick_count = 0
        self.briefs: deque = deque(maxlen=8)  # 재배치 브리핑(자동/수동) 최근 기록
        self._brief_seq = 0
        self._last_auto_brief_tick = -1000  # 자동 브리핑 스로틀용
        self.forced_dow: int | None = None  # 콘솔에서 지정한 요일(None=실제 요일)
        self.repos_total = 0  # 재배치로 이동시킨 누적 대수(성과 지표)
        self._pending_replacements: list[str] = []  # 대체 차량 생성 예약(틱 루프 후 처리)
        self._replacement_seq = 0
        self.tows: dict[str, TowTruck] = {}  # 출동 중인 견인차
        self._tow_seq = 0
        self.ambulances: dict[str, Ambulance] = {}  # 출동 중인 119 구급차
        self._amb_seq = 0

        self._spawn()
        self._seed_history(14)  # 페이지가 비지 않도록 과거 완료 내역 일부 생성

    # 생성
    def _spawn(self) -> None:
        # 구역 비중대로 차량 배치 (정왕5:배곧3:거북2)
        area_pool: list[str] = []
        for area_name, w in area.AREA_WEIGHTS.items():
            area_pool += [area_name] * w
        self.rng.shuffle(area_pool)

        for i in range(1, self.size + 1):
            home = area_pool[(i - 1) % len(area_pool)]
            name, lat, lng = self.rng.choice(area.AREAS[home])
            plate = self._gen_plate()
            battery = self.rng.uniform(35, 100)
            roll = self.rng.random()
            if roll < 0.40:
                status = STATUS_DRIVING
            elif roll < 0.78:
                status = STATUS_WAITING
            elif roll < 0.90:
                status = STATUS_CHARGING
            else:
                status = STATUS_MAINTENANCE

            v = Vehicle(
                id=f"RT-{i:03d}",
                plate=plate,
                lat=lat + self.rng.uniform(-0.0025, 0.0025),
                lng=lng + self.rng.uniform(-0.0025, 0.0025),
                battery=battery,
                status=status,
                home_area=home,
                rating=round(self.rng.uniform(4.2, 5.0), 1),
                cleanliness=self.rng.uniform(55, 98),
            )
            if status == STATUS_DRIVING:
                self._start_random_trip(v, force=True)
                v.speed = self.rng.uniform(20, 55)
            elif status == STATUS_CHARGING:
                self._send_to_depot(v)
            elif status == STATUS_MAINTENANCE:
                shop = self.rng.choice(area.REPAIR_SHOPS)
                v.lat, v.lng = shop[1], shop[2]
                v.shop_name = shop[0]
                v.repair_elapsed = self.rng.uniform(0, 45)
                v.note = "수리 중"
            self.vehicles[v.id] = v

        # 대형(SUV) 3대 추가
        for j in range(1, SUV_COUNT + 1):
            home = list(area.AREA_WEIGHTS.keys())[0]
            name, lat, lng = self.rng.choice(area.AREAS[home])
            v = Vehicle(
                id=f"SUV-{j:02d}",
                plate=self._gen_plate(),
                lat=lat + self.rng.uniform(-0.0025, 0.0025),
                lng=lng + self.rng.uniform(-0.0025, 0.0025),
                battery=self.rng.uniform(60, 100),
                status=STATUS_WAITING,
                home_area=home,
                rating=round(self.rng.uniform(4.5, 5.0), 1),
                cleanliness=self.rng.uniform(80, 99),
                car_type="SUV",
                seats=6,
            )
            self.vehicles[v.id] = v
            # 초기 에이전트 활동 1건 기록
            if status == STATUS_DRIVING:
                self._agent_event(v.id, "배차 에이전트", "done", f"운행 배차 · {v.passenger}")
            elif status == STATUS_CHARGING:
                self._agent_event(v.id, "정비 오퍼레이션 에이전트", "active", "충전소 이동 · 배터리 보충 예정")
            elif status == STATUS_MAINTENANCE:
                self._agent_event(v.id, "정비 오퍼레이션 에이전트", "active", "정기 점검 진행 중")
            else:
                self._agent_event(v.id, "재배치 에이전트", "active", "수요 예측 기반 대기 지점 배치")

    def _gen_plate(self) -> str:
        return f"{self.rng.randint(10, 99)}{self.rng.choice(HANGUL)}{self.rng.randint(1000, 9999)}"

    def _pick_destination(self, anchors: list, pickup) -> tuple:
        """목적지를 '시각·요일별 수요 가중치'로 뽑는다."""
        cands = [a for a in anchors if a is not pickup] or list(anchors)
        h = int((self.sim_minutes / 60.0) % 24)
        attract = area.trip_attraction(h, self.current_dow())
        if not attract:
            return self.rng.choice(cands)
        weights = [max(0.15, attract.get(a[0], 1.0)) for a in cands]
        return self.rng.choices(cands, weights=weights, k=1)[0]

    def _start_random_trip(self, v: Vehicle, force: bool = False) -> bool:
        """소속 구역(home_area) 안에서 운행을 시작한다."""
        anchors = area.AREAS.get(v.home_area) or area.ANCHORS
        ranked = sorted(anchors, key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2]))
        start_anchor = ranked[0]  # 현재 위치에서 가장 가까운 (구역 내) 지점
        # 픽업 지점: 현재와 다른 지점(손님 맞이 이동이 보이도록)
        pickup = self.rng.choice(ranked[1:] or ranked)
        # 목적지는 시각·요일별 가중치로 선택
        d = self._pick_destination(anchors, pickup)
        p_dict = {"lat": pickup[1], "lng": pickup[2]}
        d_dict = {"lat": d[1], "lng": d[2]}
        trip_km = round(_haversine_km(pickup[1], pickup[2], d[1], d[2]), 2)

        if force:
            # 스폰: 이미 승객을 태우고 목적지로 이동 중(픽업 단계 생략)
            cached = routing.cached_route(p_dict, d_dict)
            v.trip_origin = v.trip_pickup_name = pickup[0]
            v.trip_dest_name, v.trip_dest_lat, v.trip_dest_lng = d[0], d[1], d[2]
            v.passenger = f"{pickup[0]} → {d[0]}"
            v.dest_name, v.dest_lat, v.dest_lng = d[0], d[1], d[2]
            if cached and cached.get("points"):
                v.lat, v.lng = pickup[1], pickup[2]
                v.path = [(p["lat"], p["lng"]) for p in cached["points"]]
                v.path_i = 0
                v.trip_distance_km = cached.get("distance_km") or trip_km
            else:
                v.path, v.path_i = None, 0
                if routing.available():
                    routing.prefetch(p_dict, d_dict)
                v.trip_distance_km = trip_km
            v.status = STATUS_DRIVING
            return True

        # 일반 배차
        sa_dict = {"lat": start_anchor[1], "lng": start_anchor[2]}
        if routing.available() and routing.cached_route(sa_dict, p_dict) is None:
            routing.prefetch(sa_dict, p_dict)
            routing.prefetch(p_dict, d_dict)  # 다음 구간(픽업→목적지)도 미리
            v.route_wait += 1
            if v.route_wait < 8:
                return False  # 경로 준비될 때까지 대기
        v.route_wait = 0
        v.trip_pickup_name = pickup[0]
        v.trip_dest_name, v.trip_dest_lat, v.trip_dest_lng = d[0], d[1], d[2]
        v.passenger = f"{pickup[0]} → {d[0]}"
        v.trip_distance_km = trip_km
        # 픽업 지점으로 이동
        v.dest_name, v.dest_lat, v.dest_lng = pickup[0], pickup[1], pickup[2]
        v.depot_anchor = (start_anchor[1], start_anchor[2])
        v.trip_path, v.path, v.path_i = None, None, 0
        self._try_load_depot_route(v)  # 픽업 경로 적용(가능하면)
        v.status = STATUS_PICKUP
        return True

    def _tick_pickup(self, v: Vehicle) -> None:
        """손님 맞이 이동: 픽업 지점까지 도로를 따라 이동. 도착하면 승객 탑승 후 목적지 이동(DRIVING)."""
        if v.await_board and v.path is None and v.dest_lat is not None \
                and _haversine_km(v.lat, v.lng, v.dest_lat, v.dest_lng) < 0.12:
            v.speed = 0  # 이미 도착
            return
        arrived = self._travel_to_facility(v, 38)
        if not arrived:
            return
        v.speed = 0
        if v.await_board:  # 고객 앱 호출 → 승차 완료 전까지 출발하지 않음
            v.note = "손님 탑승 대기"
            if not getattr(v, "_arrived_logged", False):
                v._arrived_logged = True
                self._log("info", v.id, f"{v.plate} 픽업 지점 도착 · 승객 탑승 대기")
                self._agent_event(v.id, "배차 에이전트", "active",
                                  "픽업 지점 도착 · 고객 승차 완료 대기 중")
            return
        self._start_trip_leg(v)  # 자동 호출은 즉시 탑승 → 목적지 출발

    def _try_load_depot_route(self, v: Vehicle) -> bool:
        """도로 경로를 확보해 적용한다."""
        if not routing.available() or v.dest_lat is None:
            return False
        dd = {"lat": v.dest_lat, "lng": v.dest_lng}

        # ① 현재 위치 → 목적지 (스냅 없음)
        cur = routing.cached_route({"lat": v.lat, "lng": v.lng}, dd)
        if cur and cur.get("points"):
            v.path = [(p["lat"], p["lng"]) for p in cur["points"]]
            v.path_i = 0
            v.route_seq += 1
            return True

        # ② 예열된 anchor 경로
        oa = getattr(v, "depot_anchor", None)
        if oa and _haversine_km(v.lat, v.lng, oa[0], oa[1]) <= 0.25:
            cached = routing.cached_route({"lat": oa[0], "lng": oa[1]}, dd)
            if cached and cached.get("points"):
                v.lat, v.lng = oa[0], oa[1]  # 250m 이내 미세 보정
                v.path = [(p["lat"], p["lng"]) for p in cached["points"]]
                v.path_i = 0
                v.route_seq += 1
                return True
            routing.prefetch({"lat": oa[0], "lng": oa[1]}, dd)

        # ③ 현재 위치 기준으로 경로 예약
        routing.prefetch({"lat": v.lat, "lng": v.lng}, dd)
        return False

    def _send_to_depot(self, v: Vehicle) -> None:
        # 가까운 anchor -> 가까운 차고지
        origin_anchor = min(
            area.ANCHORS, key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2])
        )
        depot = min(
            area.DEPOTS,
            key=lambda d: _haversine_km(origin_anchor[1], origin_anchor[2], d[1], d[2]),
        )
        v.dest_name, v.dest_lat, v.dest_lng = depot[0], depot[1], depot[2]
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        self._try_load_depot_route(v)  # 가능하면 즉시 도로 경로 적용

    def _send_to_shop(self, v: Vehicle) -> None:
        """사고/고장 차량을 가장 가까운 정비센터로 이송(견인). 차고지와 동일하게 도로 경로를 따른다."""
        origin_anchor = min(
            area.ANCHORS, key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2])
        )
        shop = min(
            area.REPAIR_SHOPS,
            key=lambda s: _haversine_km(origin_anchor[1], origin_anchor[2], s[1], s[2]),
        )
        v.dest_name, v.dest_lat, v.dest_lng = shop[0], shop[1], shop[2]
        v.shop_name = shop[0]
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        self._try_load_depot_route(v)

    def board(self, vehicle_id: str) -> dict:
        """고객이 '승차 완료'를 누름 → 픽업 지점에서 목적지로 출발한다."""
        v = self.vehicles.get(vehicle_id)
        if not v or v.status != STATUS_PICKUP:
            return {"ok": False, "msg": "탑승할 수 있는 상태가 아닙니다."}
        if v.dest_lat is not None and _haversine_km(v.lat, v.lng, v.dest_lat, v.dest_lng) > 0.12:
            return {"ok": False, "msg": "차량이 아직 도착하지 않았습니다."}
        v.await_board = False
        v._arrived_logged = False
        v.note = ""
        self._start_trip_leg(v)
        return {"ok": True, "plate": v.plate, "msg": "탑승 완료 · 목적지로 출발합니다."}

    def _start_trip_leg(self, v: Vehicle) -> None:
        """픽업 완료 → 목적지 구간 시작(경로·상태 설정)."""
        v.trip_origin = v.trip_pickup_name
        v.dest_name = v.trip_dest_name
        v.dest_lat, v.dest_lng = v.trip_dest_lat, v.trip_dest_lng
        if v.trip_path:
            v.path, v.path_i, v.trip_path = list(v.trip_path), 0, None
            v.route_seq += 1
        else:
            v.depot_anchor = (v.lat, v.lng)
            v.path, v.path_i, v.route_wait = None, 0, 0
            self._try_load_depot_route(v)
        v.status = STATUS_DRIVING
        v.speed = self.rng.uniform(18, 40)
        self._log("info", v.id, f"{v.plate} {v.trip_pickup_name}에서 승객 탑승 → {v.trip_dest_name} 이동")
        self._agent_event(v.id, "배차 에이전트", "active",
                          f"{v.trip_pickup_name} 승객 탑승 · {v.trip_dest_name} 목적지 이동 시작")

    def deliver_to(self, vehicle_id: str, lat: float, lng: float, note: str = "분실물 전달") -> dict:
        """대기 중인 차량을 지정 좌표로 보낸다(분실물 전달 등). 도로 경로를 따라 이동."""
        v = self.vehicles.get(vehicle_id)
        if not v:
            return {"ok": False, "msg": "차량을 찾을 수 없습니다."}
        if v.incident or v.status not in (STATUS_WAITING,):
            return {"ok": False, "msg": "지금은 보낼 수 없는 차량입니다."}
        origin_anchor = min(area.ANCHORS,
                            key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2]))
        v.cruising = False
        v.passenger = None
        v.dest_name = note
        v.dest_lat, v.dest_lng = lat, lng
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.path, v.path_i, v.route_wait = None, 0, 0
        v.repositioning = True  # 요금 없는 이동(사전배치와 동일 처리)
        v.note = note
        v.status = STATUS_DRIVING
        self._try_load_depot_route(v)
        dist = _haversine_km(v.lat, v.lng, lat, lng)
        eta = max(1, round(dist / 24 * 60))
        self._log("info", v.id, f"[{note}] {v.plate} 고객 위치로 이동 (약 {eta}분)")
        self._agent_event(v.id, "고객 지원 에이전트", "active",
                          f"{note} · 고객 위치로 차량 이동 시작(약 {eta}분)")
        return {"ok": True, "plate": v.plate, "eta_min": eta}

    def reposition(self, vehicle_id: str, zone: str) -> dict:
        """수요예측 사전배치: 대기 차량을 다른 구역으로 이동시킨다(도로 경로로 주행, 요금 없음)."""
        v = self.vehicles.get(vehicle_id)
        if not v or v.status != STATUS_WAITING or v.incident:
            return {"ok": False}
        anchors = area.AREAS.get(zone) or area.ANCHORS
        target = self.rng.choice(anchors)
        origin_anchor = min(area.ANCHORS,
                            key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2]))
        v.home_area = zone
        v.dest_name, v.dest_lat, v.dest_lng = target[0], target[1], target[2]
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.passenger = None
        v.cruising = False
        v.repositioning = True
        v.note = f"{zone} 사전배치 이동중"
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        v.status = STATUS_DRIVING
        self._try_load_depot_route(v)
        return {"ok": True, "vehicle": v.id, "plate": v.plate, "zone": zone,
                "dest": target[0]}

    # 수요 핫스팟 사전배치
    def current_hotspots(self) -> list[tuple[str, float]]:
        """현재 가상 시각·요일 기준 수요 핫스팟 목록 [(지점명, 가중치), ...]."""
        h = int((self.sim_minutes / 60.0) % 24)
        return area.hotspots_for_hour(h, self.current_dow())

    def _band(self, hh: int) -> str:
        """시간대 라벨(브리핑 문구용)."""
        return ("출근 시간대" if 7 <= hh <= 9 else "퇴근 시간대" if 17 <= hh <= 20 else
                "심야 유흥 시간대" if (hh >= 21 or hh <= 1) else
                "주간 시간대" if 10 <= hh <= 16 else "심야 시간대")

    def demand_map(self) -> list[dict]:
        """수요 히트맵 + 수요·공급 균형 데이터."""
        spots = self.current_hotspots()
        coords = []
        for name, w in spots:
            a = next((x for x in area.ANCHORS if x[0] == name), None)
            if a:
                coords.append((name, a[1], a[2], float(w)))
        if not coords:
            return []
        names = {c[0] for c in coords}

        supply = {c[0]: 0 for c in coords}
        for v in self.vehicles.values():
            if v.status == STATUS_WAITING and not v.incident:
                best, bd = None, 1e9
                for c in coords:
                    dd = _haversine_km(v.lat, v.lng, c[1], c[2])
                    if dd < bd:
                        best, bd = c, dd
                if best is not None and bd <= REBALANCE_RADIUS_KM:
                    supply[best[0]] += 1
            elif v.status == STATUS_DRIVING and v.repositioning and v.dest_name in names:
                supply[v.dest_name] += 1

        d = self.current_demand()
        total_w = sum(c[3] for c in coords) or 1.0
        max_w = max(c[3] for c in coords) or 1.0
        fleet_n = sum(1 for v in self.vehicles.values() if v.car_type != "SUV")
        pool = fleet_n * REBALANCE_FRACTION * max(0.25, d)
        out = []
        for name, lat, lng, w in coords:
            need = pool * w / total_w
            sup = supply[name]
            gap = sup - need
            out.append({
                "name": name, "lat": lat, "lng": lng, "weight": round(w, 2),
                "demand": round(need, 1), "supply": sup, "gap": round(gap, 1),
                "level": round(min(1.0, 0.35 + 0.65 * (w / max_w) * max(0.4, d)), 3),
                "status": "부족" if gap <= -1.0 else ("과잉" if gap >= 2.0 else "적정"),
            })
        out.sort(key=lambda x: x["gap"])
        return out

    def _add_brief(self, kind: str, text: str) -> None:
        """재배치 브리핑 한 건 기록(kind: 'auto' 자동 / 'manual' 관리자)."""
        self._brief_seq += 1
        hh = int(self.sim_minutes // 60) % 24
        mm = int(self.sim_minutes % 60)
        self.briefs.appendleft({"id": self._brief_seq, "kind": kind,
                                "time": f"{hh:02d}:{mm:02d}", "text": text})

    def _send_idle_to(self, v: "Vehicle", name: str, lat: float, lng: float, zone: str) -> None:
        """대기 택시 한 대를 특정 지점으로 사전배치(요금 없는 도로 주행 후 그 지점에서 대기)."""
        origin_anchor = min(area.ANCHORS, key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2]))
        v.home_area = zone
        v.dest_name, v.dest_lat, v.dest_lng = name, lat, lng
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.passenger = None
        v.cruising = False
        v.repositioning = True
        v.note = f"{name} 사전배치 이동중"
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        v.status = STATUS_DRIVING
        self.repos_total += 1
        self._try_load_depot_route(v)

    def repos_metrics(self) -> dict:
        """재배치 성과 지표."""
        spots = self.current_hotspots()
        coords = []
        for name, _w in spots:
            a = next((x for x in area.ANCHORS if x[0] == name), None)
            if a:
                coords.append((a[1], a[2]))
        idle = [v for v in self.vehicles.values()
                if v.status == STATUS_WAITING and not v.incident and v.car_type != "SUV"]
        if not coords or not idle:
            return {"before_min": None, "after_min": None, "saved_min": None,
                    "repositioned": self.repos_total, "covered": 0, "idle": len(idle)}

        after_d = [min(_haversine_km(v.lat, v.lng, c[0], c[1]) for c in coords) for v in idle]
        base = [min(_haversine_km(a[1], a[2], c[0], c[1]) for c in coords) for a in area.ANCHORS]
        to_min = lambda km: round(km / 24.0 * 60.0, 1)  # 도심 평균 24km/h
        before_min = to_min(sum(base) / len(base))
        after_min = to_min(sum(after_d) / len(after_d))
        return {
            "before_min": before_min,
            "after_min": after_min,
            "saved_min": round(max(0.0, before_min - after_min), 1),
            "repositioned": self.repos_total,
            "covered": sum(1 for x in after_d if x <= REBALANCE_RADIUS_KM),
            "idle": len(idle),
        }

    def _movable_idle(self, lat: float, lng: float, min_km: float = 0.0) -> list:
        """사전배치로 보낼 수 있는 대기 택시(대기·배터리 충분·재배치/사고 아님·SUV 제외)."""
        out = []
        for v in self.vehicles.values():
            if (v.status == STATUS_WAITING and not v.repositioning and not v.incident
                    and v.car_type != "SUV" and v.battery > CRUISE_MIN_BATT):
                if _haversine_km(v.lat, v.lng, lat, lng) > min_km:
                    out.append(v)
        return out

    def _rebalance_hotspots(self) -> None:
        """대기 택시 분포를 현재 핫스팟 가중치에 맞춰 조정한다."""
        spots = self.current_hotspots()
        if not spots:
            return
        coords = []
        for name, w in spots:
            a = next((x for x in area.ANCHORS if x[0] == name), None)
            if a:
                coords.append([name, a[1], a[2], float(w)])
        if not coords:
            return
        names = {c[0] for c in coords}

        waiting = [v for v in self.vehicles.values()
                   if v.status == STATUS_WAITING and not v.incident and v.car_type != "SUV"]

        # 공급 = 반경 내 대기 택시 + 재배치 중인 택시
        supply = {c[0]: 0 for c in coords}
        for v in waiting:
            best, bd = None, 1e9
            for c in coords:
                dd = _haversine_km(v.lat, v.lng, c[1], c[2])
                if dd < bd:
                    best, bd = c, dd
            if best is not None and bd <= REBALANCE_RADIUS_KM:
                supply[best[0]] += 1
        for v in self.vehicles.values():
            if v.status == STATUS_DRIVING and v.repositioning and v.dest_name in names:
                supply[v.dest_name] += 1

        idle_free = [v for v in waiting if not v.repositioning and v.battery > CRUISE_MIN_BATT]
        pool = len(idle_free) + sum(supply.values())  # 재배치 대상 총 풀
        total_w = sum(c[3] for c in coords) or 1.0
        budget = pool * REBALANCE_FRACTION
        desired = {c[0]: budget * c[3] / total_w for c in coords}

        deficit = sorted(([c, desired[c[0]] - supply[c[0]]] for c in coords),
                         key=lambda x: x[1], reverse=True)
        target, gap = deficit[0]
        if gap < 1.0:
            return  # 이미 충분히 배치됨(이동 중 포함)

        n = int(min(math.ceil(gap), REBALANCE_BATCH))
        movable = self._movable_idle(target[1], target[2], min_km=REBALANCE_RADIUS_KM)
        # 다른 핫스팟을 지키고 있는 차량은 빼 오지 않는다(핫스팟 간 맞교환 방지)
        movable = [v for v in movable
                   if all(_haversine_km(v.lat, v.lng, c[1], c[2]) > REBALANCE_RADIUS_KM
                          for c in coords)]
        if not movable:
            return
        movable.sort(key=lambda x: _haversine_km(x.lat, x.lng, target[1], target[2]))
        zone = area.ANCHOR_AREA.get(target[0])
        sent = []
        for v in movable[:n]:
            self._send_idle_to(v, target[0], target[1], target[2], zone or v.home_area)
            self._agent_event(v.id, "재배치 에이전트", "active", f"수요 예측 · {target[0]} 사전배치 이동")
            sent.append(v.plate)

        # 의미 있는 규모(2대 이상)만 자동 브리핑
        if len(sent) >= 2 and (self._tick_count - self._last_auto_brief_tick) >= AUTO_BRIEF_MIN_GAP:
            self._last_auto_brief_tick = self._tick_count
            hh = int(self.sim_minutes // 60) % 24
            d = round(self.current_demand() * 100)
            dn = area.DAY_NAMES[self.current_dow()]
            extra = ""
            if self.current_dow() == 4 and (hh >= 17 or hh <= 1):
                extra = " 회식 수요가 겹쳐"
            elif self.current_dow() >= 5 and 10 <= hh <= 20:
                extra = " 주말 나들이 수요가 몰려"
            self._add_brief("auto",
                            f"현재 {dn}요일 {self._band(hh)}로{extra} {target[0]} 수요가 높아 "
                            f"{sent[0]} 외 {len(sent) - 1}대를 자동 배차했습니다. (수요 {d}%)")

    def surge_to_point(self, point_name: str) -> dict:
        """[관제 버튼] 특정 지점(정왕역·오이도역 등)으로 대기 택시를 집중 배치한다."""
        a = next((x for x in area.ANCHORS if x[0] == point_name), None)
        if not a:
            return {"ok": False, "msg": f"'{point_name}' 지점을 찾을 수 없습니다."}
        name, lat, lng = a[0], a[1], a[2]
        d = self.current_demand()
        hh = int((self.sim_minutes / 60.0) % 24)
        spots = {n: w for n, w in self.current_hotspots()}
        is_hot = name in spots
        pf = 1.6 if is_hot else 1.0
        band = ("출근 피크" if 7 <= hh <= 9 else "퇴근 피크" if 17 <= hh <= 20 else
                "심야 유흥" if (hh >= 21 or hh <= 1) else "주간 생활" if 10 <= hh <= 16 else "심야")

        movable = self._movable_idle(lat, lng, min_km=0.4)  # 이미 근처인 차량은 제외
        avail = len(movable)
        if avail == 0:
            return {"ok": True, "count": 0, "point": name,
                    "reason": f"{hh:02d}시 {band}(수요 {round(d * 100)}%) · 대부분 운행 중이라 보낼 대기 차량이 없습니다."}
        # 수요·시간대 가중으로 배치 대수 결정
        n = max(1, min(avail, round(avail * (0.25 + 0.5 * d) * pf)))
        movable.sort(key=lambda v: _haversine_km(v.lat, v.lng, lat, lng))
        sent = []
        zone = area.ANCHOR_AREA.get(name, "정왕동")
        for v in movable[:n]:
            dist = _haversine_km(v.lat, v.lng, lat, lng)
            self._send_idle_to(v, name, lat, lng, zone)
            sent.append({
                "id": v.id, "plate": v.plate, "car_type": v.car_type,
                "battery": round(v.battery),
                "distance_km": round(dist, 2),
                "eta_min": max(1, round(dist / 24.0 * 60)),  # 도심 평균 24km/h
            })

        plates = [s["plate"] for s in sent]
        reason = (f"{hh:02d}시 {band} · 수요 {round(d * 100)}%"
                  + (f" · {name} 집중 시간대" if is_hot else "")
                  + f" → {len(sent)}대 배치")
        if sent:
            self._log("info", "", f"[집중배치] {name} · {reason} ({', '.join(plates)})")
            self._add_brief("manual",
                            f"관리자에 의해 {name}으로 {len(sent)}대를 배차했습니다. "
                            f"({area.DAY_NAMES[self.current_dow()]}요일 {hh:02d}시 {band}, 수요 {round(d * 100)}%)")
        return {"ok": True, "count": len(sent), "point": name, "reason": reason,
                "plates": plates, "vehicles": sent}

    def _travel_to_facility(self, v: Vehicle, speed_kmh: float) -> bool:
        """차고지/정비소로 도로 경로를 따라 이동. 경로가 없으면 제자리 대기하고(건물 관통 방지)."""
        if v.path is None and v.dest_lat is not None:
            at = _haversine_km(v.lat, v.lng, v.dest_lat, v.dest_lng) < 0.12
            if not at:
                if not self._try_load_depot_route(v):
                    v.route_wait += 1
                    if v.route_wait < 30:
                        v.speed = 0
                        return False  # 도로 경로 준비될 때까지 대기
                    v.lat, v.lng = v.dest_lat, v.dest_lng  # 오래 못 받으면 스냅
                    v.path = None
        step_km = speed_kmh / 3600 * settings.tick_seconds * MOVE_ACCEL
        v.speed = speed_kmh
        return self._move_toward(v, step_km)

    def _spawn_replacement(self, original_id: str) -> None:
        """사고/고장으로 수리에 들어간 차량을 대신할 임시 대체 차량을 즉시 투입한다."""
        orig = self.vehicles.get(original_id)
        if not orig or orig.replacement_id:
            return
        self._replacement_seq += 1
        home = orig.home_area
        anchors = area.AREAS.get(home) or area.ANCHORS
        a = self.rng.choice(anchors)
        vid = f"RPL-{self._replacement_seq:02d}"
        rep = Vehicle(
            id=vid,
            plate=self._gen_plate(),
            lat=a[1] + self.rng.uniform(-0.0015, 0.0015),
            lng=a[2] + self.rng.uniform(-0.0015, 0.0015),
            battery=self.rng.uniform(70, 100),
            status=STATUS_WAITING,
            rating=round(self.rng.uniform(4.5, 5.0), 1),
            cleanliness=self.rng.uniform(88, 99),
            home_area=home,
            is_replacement=True,
        )
        self.vehicles[vid] = rep
        orig.replacement_id = vid
        self._log("info", vid,
                  f"{rep.plate} 대체 차량 투입({home}) · {orig.plate} 수리 동안 운행")
        self._agent_event(vid, "플릿 오케스트레이터 에이전트", "active",
                          f"사고 차량({orig.plate}) 대체 투입 · {home} 운행 대기")

    def _log(self, level: str, vehicle_id: str, text: str) -> None:
        self.events.insert(
            0,
            {
                "ts": _now_iso(),
                "level": level,  # info / warn / danger
                "vehicle_id": vehicle_id,
                "text": text,
            },
        )
        del self.events[200:]  # 최근 200개만 유지

    def _agent_event(self, vehicle_id: str, agent: str, state: str, text: str,
                     detail: dict | None = None) -> None:
        """차량별 에이전트 활동 1건을 누적 기록한다(신규가 앞)."""
        lst = self.agent_events.setdefault(vehicle_id, [])
        self._agent_seq += 1
        ev = {
            "seq": self._agent_seq,
            "ts": _now_iso(),
            "time": _now_iso()[11:19],
            "agent": agent,
            "state": state,
            "text": text,
        }
        if detail is not None:
            ev["detail"] = detail
        lst.insert(0, ev)
        del lst[150:]  # 차량당 최근 150건 유지

    def _build_maint_detail(self, v: "Vehicle", kind: str) -> dict:
        """완료된 정비의 상세 내역(정비소·소요시간·항목·비용)을 구성한다."""
        shops = getattr(area, "REPAIR_SHOPS", None) or area.DEPOTS
        shop = min(shops, key=lambda s: _haversine_km(v.lat, v.lng, s[1], s[2]))
        rng = self.rng
        if kind == "사고 수리":
            pool = [("범퍼·외관 패널 복원", 180000), ("차체 정렬", 90000),
                    ("안전 센서 재보정", 60000), ("도장 보수", 70000),
                    ("타이어 교체", 120000), ("하부 손상 점검", 50000)]
            items = rng.sample(pool, k=rng.randint(2, 3)) + [("종합 안전 점검", 40000)]
            duration = rng.randint(50, 95)
            note = "보험 청구 접수 완료 · 안전 점검 통과 후 운행 복귀"
        elif kind == "방전/고장 정비":
            pool = [("배터리 셀 진단", 60000), ("급속 충전·셀 밸런싱", 45000),
                    ("구동계 점검", 55000), ("전장 계통 점검", 50000)]
            items = rng.sample(pool, k=rng.randint(2, 3))
            duration = rng.randint(30, 60)
            note = "배터리 상태 정상화 확인 후 운행 복귀"
        else:  # 정기 점검
            pool = [("타이어 공기압·마모 점검", 20000), ("브레이크 패드 점검", 35000),
                    ("배터리 셀 진단", 40000), ("센서·카메라 캘리브레이션", 30000),
                    ("소프트웨어 업데이트", 0), ("실내 청소·소독", 15000),
                    ("냉각수·소모품 점검", 25000)]
            items = rng.sample(pool, k=rng.randint(3, 4))
            duration = rng.randint(20, 45)
            note = "정기 점검 완료 · 운행 복귀"
        total = sum(c for _, c in items)
        return {
            "type": "maintenance",
            "kind": kind,
            "shop": shop[0],
            "duration_min": duration,
            "items": [{"name": n, "cost": c} for n, c in items],
            "total_cost": total,
            "after": {"battery": round(v.battery), "cleanliness": int(v.cleanliness)},
            "note": note,
            "completed_at": _now_iso(),
        }

    # 틱
    def real_minutes_per_tick(self) -> float:
        """이번 틱에 '실제로' 흐른 분. 충전·수리 등 운영 소요시간 계산에 사용한다."""
        scale = 1.0 if self.time_realtime else max(0.1, self.time_scale)
        return settings.tick_seconds / 60.0 * scale

    def tick(self) -> None:
        # 가상 시계 진행
        if self.time_realtime:
            self.sim_minutes = _wall_minutes()  # 현재 실제 시각(KST)을 그대로 반영
        elif not self.time_paused:
            self.sim_minutes = (self.sim_minutes + SIM_MIN_PER_TICK * self.time_scale) % 1440.0

        # 수요 기반 운행 비율 제어
        # 수요 목표와 현재 운행대수를 비교해 배차 예산 결정
        counts = self.status_counts()
        # 빈 차로 이동 중인 재배치 차량은 '운행 중'에서 제외(승객 수요 목표와 무관)
        repos = sum(1 for v in self.vehicles.values()
                    if v.status == STATUS_DRIVING and v.repositioning)
        active_now = counts[STATUS_DRIVING] - repos + counts[STATUS_PICKUP]
        self._active_target = self.target_active_count()
        gap = self._active_target - active_now
        self._dispatch_budget = math.ceil(gap * DISPATCH_CATCHUP) if gap > 0 else 0

        # 시간대별 수요 핫스팟으로 대기 택시 사전배치
        self._tick_count += 1
        if self._tick_count % REBALANCE_INTERVAL == 0:
            self._rebalance_hotspots()

        for v in self.vehicles.values():
            if v.status == STATUS_MAINTENANCE:
                self._tick_maintenance(v)
            elif v.status == STATUS_CHARGING:
                self._tick_charging(v)
            elif v.status == STATUS_PICKUP:
                self._tick_pickup(v)
            elif v.status == STATUS_DRIVING:
                self._tick_driving(v)
            elif v.status == STATUS_WAITING:
                self._tick_waiting(v)
            v.updated_at = _now_iso()

        self._tick_tows()  # 견인차 이동(사고 차량 → 정비소)
        self._tick_ambulances()  # 119 구급차 이동(부상 신고 시)

        # 루프 종료 후: 대체 차량 생성 / 임무 끝난 대체 차량 제거
        while self._pending_replacements:
            self._spawn_replacement(self._pending_replacements.pop(0))
        for rid in [vid for vid, v in self.vehicles.items()
                    if v.is_replacement and v.remove_pending and v.status == STATUS_WAITING]:
            rep = self.vehicles.pop(rid, None)
            if rep:
                self._log("info", rep.id, f"{rep.plate} 대체 차량 회수 · 운영 정상화")

    def _move_toward(self, v: Vehicle, step_km: float) -> bool:
        """도로 경로(path)를 따라서만 이동한다."""
        if v.path:
            return self._advance_path(v, step_km)
        if v.dest_lat is None:
            return False
        if _haversine_km(v.lat, v.lng, v.dest_lat, v.dest_lng) < 0.12:
            v.lat, v.lng = v.dest_lat, v.dest_lng
            return True
        # 경로 없음
        if self._try_load_depot_route(v):
            return self._advance_path(v, step_km)
        v.route_wait += 1
        if v.route_wait < ROUTE_WAIT_MAX:
            v.speed = 0
            return False  # 도로 경로 준비될 때까지 대기(이동 안 함)
        v.lat, v.lng = v.dest_lat, v.dest_lng  # 최후 수단: 스냅
        v.path = None
        v.route_wait = 0
        return True

    def _advance_path(self, v: Vehicle, step_km: float) -> bool:
        """도로 경로 좌표열(v.path)을 따라 step_km 만큼 전진. 경로 끝에 닿으면 True."""
        remaining = step_km
        while remaining > 1e-6 and v.path_i < len(v.path):
            tlat, tlng = v.path[v.path_i]
            d = _haversine_km(v.lat, v.lng, tlat, tlng)
            # 진행 방향은 매 구간마다 갱신
            if d > 1e-7:
                _k = math.cos(math.radians(v.lat))  # 경도 축 보정
                v.heading = math.degrees(math.atan2((tlng - v.lng) * _k, tlat - v.lat)) % 360
            if d <= remaining or d < 1e-6:
                v.lat, v.lng = tlat, tlng
                remaining -= max(d, 0.0)
                v.path_i += 1
            else:
                frac = remaining / d
                v.lat += (tlat - v.lat) * frac
                v.lng += (tlng - v.lng) * frac
                remaining = 0.0
        if v.path_i >= len(v.path):
            v.path = None
            v.path_i = 0
            return True
        return False

    def _tick_driving(self, v: Vehicle) -> None:
        # 사전배치 이동 (요금 없음)
        if v.repositioning:
            arrived = self._travel_to_facility(v, 34)
            if arrived:
                v.repositioning = False
                v.note = ""
                v.status = STATUS_WAITING
                v.speed = 0
            return
        v.speed = _clamp(v.speed + self.rng.uniform(-8, 8), 8, 65)
        step_km = v.speed / 3600 * settings.tick_seconds * MOVE_ACCEL  # 실제 속도(km/h) 기준
        before_lat, before_lng = v.lat, v.lng
        arrived = self._move_toward(v, step_km)
        moved = _haversine_km(before_lat, before_lng, v.lat, v.lng)
        # 경로 대기 중이면 소모·사고 없이 종료
        if not arrived and moved < 1e-4:
            v.speed = 0
            return
        # 주행 소모: 실제 이동 거리에 비례
        v.battery = _clamp(
            v.battery - min(moved, step_km * 1.2) * DRAIN_PER_KM * self.rng.uniform(0.9, 1.1),
            0, 100)
        v.cleanliness = _clamp(v.cleanliness - self.rng.uniform(0.1, 0.4), 0, 100)

        # 통합 관제 에이전트: 주행 중 모니터링 하트비트
        if self.rng.random() < 0.06:
            self._agent_event(v.id, "통합 관제 에이전트", "active",
                              f"실시간 모니터링 · 배터리 {v.battery:.0f}% · {int(v.speed)}km/h · "
                              f"({v.lat:.4f}, {v.lng:.4f})")

        # 사고 발생
        if self.rng.random() < ACCIDENT_PROB_PER_TICK:
            self._raise_incident(v, "ACCIDENT", "주행 중 접촉 사고 감지")
            return

        # 운행 중에는 배터리가 부족해도 목적지까지 완주

        if arrived:
            if v.via_final:
                final, v.via_final = v.via_final, None
                v.dest_name = v.trip_dest_name = final["name"]
                v.dest_lat = v.trip_dest_lat = final["lat"]
                v.dest_lng = v.trip_dest_lng = final["lng"]
                v.depot_anchor = (v.lat, v.lng)
                v.path, v.path_i, v.route_wait = None, 0, 0
                self._try_load_depot_route(v)
                self._log("info", v.id, f"{v.plate} 경유지 통과 → {final['name']}")
                return
            # 승객 하차 / 운행 완료
            self.session_trips += 1
            v.trips += 1
            fare = estimate_fare(v.trip_distance_km)
            self.session_revenue += fare
            if v.manual and v.trip_seq is not None:
                # 운영자 수동 호출 건 → 기존 '배차' 항목을 '완료'로 갱신
                for t in self.trips_log:
                    if t["seq"] == v.trip_seq:
                        t["status"] = "완료"
                        t["fare"] = fare
                        break
            else:
                self.record_trip(
                    plate=v.plate, vehicle_id=v.id,
                    origin=v.trip_origin or "—", destination=v.dest_name or "—",
                    distance_km=v.trip_distance_km, fare=fare,
                    eta_min=None, status="완료", source="자동",
                    engine="rule", rating=v.rating,
                )
            v.manual = False
            v.trip_seq = None
            v.trip_origin = None
            v.trip_distance_km = None
            v.path = None
            v.path_i = 0
            v.passenger = None
            v.status = STATUS_WAITING
            v.speed = 0
            self._log("info", v.id, f"{v.plate} 운행 완료 (요금 {fare:,}원)")
            self._agent_event(v.id, "배차 에이전트", "done",
                              f"운행 완료 · 요금 {fare:,}원 정산 · 대기 전환")

    def _tick_waiting(self, v: Vehicle) -> None:
        v.battery = _clamp(v.battery - IDLE_DRAIN_TICK, 0, 100)
        if v.battery <= CHARGE_THRESHOLD:
            v.cruising = False
            v.speed = 0
            v.status = STATUS_CHARGING
            self._send_to_depot(v)
            self._agent_event(v.id, "플릿 오케스트레이터 에이전트", "active",
                              f"대기 중 배터리 {v.battery:.0f}% → 충전 우선 재배치 결정")
            return

        # 대기 중 배회
        self._tick_cruise(v)

        # 대형(SUV)은 5인 이상 호출 전용
        if v.car_type == "SUV":
            return
        # 새 호출 수락 (배차 예산이 남아 있을 때만)
        if self._dispatch_budget <= 0:
            return
        if self._start_random_trip(v):  # 상태(PICKUP) 는 내부에서 설정
            self._dispatch_budget -= 1
            v.cruising = False  # 배차되면 배회 종료
            self._log("info", v.id, f"{v.plate} 신규 배차 → {v.passenger} (손님 맞이 이동)")
            self._agent_event(v.id, "배차 에이전트", "done",
                              f"신규 호출 수락 · {v.trip_pickup_name} 손님 맞이 이동 시작")

    def _tick_cruise(self, v: Vehicle) -> None:
        """대기 차량의 배회. 도로 경로가 있을 때만 이동하며(건물 관통 없음)."""
        # 배회 중
        if v.cruising:
            if v.path is None and not self._try_load_depot_route(v):
                v.route_wait += 1
                v.speed = 0
                if v.route_wait > 20:  # 경로를 못 받으면 배회 취소(스냅/직선 금지)
                    self._end_cruise(v)
                return
            before = (v.lat, v.lng)
            arrived = self._advance_path(v, CRUISE_SPEED_KMH / 3600 * settings.tick_seconds * MOVE_ACCEL)
            v.speed = CRUISE_SPEED_KMH
            moved = _haversine_km(before[0], before[1], v.lat, v.lng)
            v.battery = _clamp(v.battery - moved * DRAIN_PER_KM * 0.3, 0, 100)  # 저속 배회 → 소모 완화
            if arrived:
                self._end_cruise(v)
            return

        # 정차 중
        v.speed = 0
        if v.cruise_idle > 0:
            v.cruise_idle -= 1
            return
        if v.battery < CRUISE_MIN_BATT or not routing.available():
            return
        if self.rng.random() >= CRUISE_START_PROB:
            return
        self._start_cruise(v)

    def _start_cruise(self, v: Vehicle) -> None:
        """근처 지점 하나를 골라 도로를 따라 천천히 이동(대기 상태 유지)."""
        near = [a for a in area.ANCHORS
                if 0.2 < _haversine_km(v.lat, v.lng, a[1], a[2]) <= CRUISE_RADIUS_KM]
        if not near:
            v.cruise_idle = self.rng.randint(CRUISE_IDLE_MIN, CRUISE_IDLE_MAX)
            return
        target = self.rng.choice(near)
        origin_anchor = min(area.ANCHORS,
                            key=lambda a: _haversine_km(v.lat, v.lng, a[1], a[2]))
        v.dest_name = None  # 행선지 표시는 하지 않음(운행이 아님)
        v.dest_lat, v.dest_lng = target[1], target[2]
        v.depot_anchor = (origin_anchor[1], origin_anchor[2])
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        v.cruising = True

    def _end_cruise(self, v: Vehicle) -> None:
        v.cruising = False
        v.speed = 0
        v.path = None
        v.path_i = 0
        v.route_wait = 0
        v.dest_lat = v.dest_lng = None
        v.dest_name = None
        v.cruise_idle = self.rng.randint(CRUISE_IDLE_MIN, CRUISE_IDLE_MAX)

    def _tick_charging(self, v: Vehicle) -> None:
        # 차고지까지 이동 후 충전 시작
        arrived = self._travel_to_facility(v, 32)
        if not arrived:
            return
        v.speed = 0
        if v.charge_elapsed <= 0.0:  # 막 도착 → 충전 시작
            v.charge_start_batt = v.battery
            v.note = "충전 중"
        v.charge_elapsed += self.real_minutes_per_tick()
        # CHARGE_MINUTES 동안 완충까지 선형 상승
        frac = min(1.0, v.charge_elapsed / CHARGE_MINUTES)
        v.battery = _clamp(v.charge_start_batt + (100.0 - v.charge_start_batt) * frac, 0, 100)
        v.cleanliness = _clamp(v.cleanliness + 0.5, 0, 100)
        if v.charge_elapsed >= CHARGE_MINUTES:  # 충전 완료
            v.battery = 100.0
            v.charge_elapsed = 0.0
            v.status = STATUS_WAITING
            v.note = ""
            self._log("info", v.id, f"{v.plate} 충전 완료 (100%, {CHARGE_MINUTES}분 소요)")
            self._agent_event(v.id, "정비 오퍼레이션 에이전트", "done",
                              f"충전 완료(100%) · {CHARGE_MINUTES}분 소요 · 운행 복귀 가능")

    def _tick_maintenance(self, v: Vehicle) -> None:
        # 1-a. 견인차 대기
        if v.tow_wait:
            v.speed = 0
            return
        # 1-b. 견인 중
        if v.towing:
            v.speed = TOW_SPEED_KMH
            return

        # 2단계: 수리
        v.speed = 0
        v.repair_elapsed += self.real_minutes_per_tick()
        if v.repair_elapsed < REPAIR_MINUTES:
            return

        # 수리 완료
        v.cleanliness = self.rng.uniform(90, 99)
        v.battery = max(v.battery, 60.0)
        if v.incident:
            kind = "사고 수리" if v.incident["type"] == "ACCIDENT" else "고장 정비"
            detail = self._build_maint_detail(v, kind)
            self._log("info", v.id, f"{v.plate} {kind} 완료 → 운행 복귀")
            self._agent_event(
                v.id, "정비 오퍼레이션 에이전트", "done",
                f"{kind} 완료({REPAIR_MINUTES}분) · 차량 복귀 (정비소: {detail['shop']}, 총 {detail['total_cost']:,}원)",
                detail=detail,
            )
        v.incident = None
        v.note = ""
        v.shop_name = None
        v.repair_elapsed = 0.0
        v.tow_wait = False
        v.towing = False
        v.status = STATUS_WAITING
        # 이 차량을 대체하던 임시 차량은 한가해지면 제거하도록 표시
        if v.replacement_id and v.replacement_id in self.vehicles:
            rep = self.vehicles[v.replacement_id]
            rep.remove_pending = True
            self._log("info", rep.id, f"{rep.plate} 대체 임무 종료 → 원 차량({v.plate}) 복귀")
        v.replacement_id = None
        if v.battery < CHARGE_THRESHOLD:
            v.status = STATUS_CHARGING
            self._send_to_depot(v)

    def _raise_incident(self, v: Vehicle, kind: str, desc: str) -> None:
        v.incident = {"type": kind, "desc": desc, "ts": _now_iso()}
        v.status = STATUS_MAINTENANCE
        v.passenger = None
        v.speed = 0
        v.path = None
        v.path_i = 0
        v.cruising = False
        v.await_board = False
        v.repositioning = False
        v.towing = False  # 아직 견인 전
        v.tow_wait = True  # 견인차가 도착할 때까지 대기
        v.repair_elapsed = 0.0
        v.dest_name = v.dest_lat = v.dest_lng = None
        v.note = "사고 지점 대기 · 견인차 출동 중"
        self._dispatch_tow(v)  # 가장 가까운 정비소에서 견인차 출동
        level = "danger" if kind == "ACCIDENT" else "warn"
        label = "사고" if kind == "ACCIDENT" else "고장"
        self._log(level, v.id, f"[{label}] {v.plate} — {desc} · 견인차 출동 ({v.shop_name})")
        self._agent_event(v.id, "인시던트 대응 에이전트", "active",
                          f"{label} 접수 · {v.shop_name} 견인차 출동 · 사고 지점 대기")
        # 수리 동안 운영 차질 최소화
        if not v.is_replacement and not v.replacement_id:
            self._pending_replacements.append(v.id)

    def dispatch_ambulance(self, vehicle_id: str) -> dict:
        """부상 신고 → 119 구급차를 소방서에서 사고 지점으로 출동시키고, 이후 병원으로 이송한다."""
        v = self.vehicles.get(vehicle_id)
        if not v:
            return {"ok": False, "msg": "차량을 찾을 수 없습니다."}
        # 이미 출동한 구급차가 있으면 중복 출동 방지
        for a in self.ambulances.values():
            if a.target_id == vehicle_id:
                return {"ok": True, "already": True, "eta_min": 0,
                        "msg": "구급차가 이미 출동 중입니다."}
        station = area.FIRE_STATION  # 출동 거점(정왕 소방서)
        hosp = next((x for x in area.ANCHORS if x[0] == HOSPITAL_NAME), area.ANCHORS[0])  # 이송 병원
        self._amb_seq += 1
        a = Ambulance(id=f"AMB-{self._amb_seq:02d}", lat=station[1], lng=station[2],
                      base_name=station[0], base_lat=station[1], base_lng=station[2],
                      hosp_name=hosp[0], hosp_lat=hosp[1], hosp_lng=hosp[2],
                      target_id=v.id, phase="to_scene")
        self.ambulances[a.id] = a
        dist = _haversine_km(station[1], station[2], v.lat, v.lng)
        eta = max(1, round(dist / AMBULANCE_SPEED_KMH * 60))
        v.note = "119 구급차 출동 중"
        self._log("danger", v.id, f"[119] {v.plate} 부상 신고 · {station[0]}에서 구급차 출동 (약 {eta}분)")
        self._agent_event(v.id, "인시던트 대응 에이전트", "alert",
                          f"부상 신고 접수 · 119 구급차 출동 ({station[0]}) · 예상 {eta}분")
        return {"ok": True, "station": station[0], "hospital": hosp[0], "eta_min": eta}

    def _amb_route(self, a: Ambulance, dlat: float, dlng: float) -> bool:
        """구급차 도로 경로."""
        if not routing.available():
            return False
        o = {"lat": a.lat, "lng": a.lng}
        d = {"lat": dlat, "lng": dlng}
        cached = routing.cached_route(o, d)
        if cached and cached.get("points"):
            pts = [(p["lat"], p["lng"]) for p in cached["points"]]
            if not pts or _haversine_km(a.lat, a.lng, pts[0][0], pts[0][1]) > 0.03:
                pts.insert(0, (a.lat, a.lng))
            if _haversine_km(pts[-1][0], pts[-1][1], dlat, dlng) > 0.03:
                pts.append((dlat, dlng))
            a.path = pts
            a.path_i = 0
            return True
        routing.prefetch(o, d)
        return False

    def _advance_amb(self, a: Ambulance, dlat: float, dlng: float, step_km: float) -> bool:
        """구급차를 목적지로 이동. 도착하면 True."""
        if _haversine_km(a.lat, a.lng, dlat, dlng) < 0.08:
            a.lat, a.lng = dlat, dlng
            a.path = None
            return True
        if a.path is None:
            if not self._amb_route(a, dlat, dlng):
                a.route_wait += 1
                if a.route_wait < ROUTE_WAIT_MAX:
                    return False
                a.lat, a.lng = dlat, dlng  # 최후 수단
                a.route_wait = 0
                return True
            a.route_wait = 0
        remaining = step_km
        while remaining > 1e-6 and a.path and a.path_i < len(a.path):
            tlat, tlng = a.path[a.path_i]
            d = _haversine_km(a.lat, a.lng, tlat, tlng)
            if d > 1e-7:
                a.heading = math.degrees(math.atan2(
                    (tlng - a.lng) * math.cos(math.radians(a.lat)), tlat - a.lat)) % 360
            if d <= remaining or d < 1e-6:
                a.lat, a.lng = tlat, tlng
                remaining -= max(d, 0.0)
                a.path_i += 1
            else:
                frac = remaining / d
                a.lat += (tlat - a.lat) * frac
                a.lng += (tlng - a.lng) * frac
                remaining = 0.0
        if a.path and a.path_i >= len(a.path):
            a.path = None
            a.path_i = 0
            return _haversine_km(a.lat, a.lng, dlat, dlng) < 0.12
        return False

    def _tick_ambulances(self) -> None:
        """구급차 이동: 병원 → 사고 지점 → (현장 처치) → 병원."""
        step_km = AMBULANCE_SPEED_KMH / 3600 * settings.tick_seconds * MOVE_ACCEL
        for aid in list(self.ambulances.keys()):
            a = self.ambulances[aid]
            v = self.vehicles.get(a.target_id)

            if a.phase == "to_scene":
                tgt = (v.lat, v.lng) if v else (a.base_lat, a.base_lng)
                if self._advance_amb(a, tgt[0], tgt[1], step_km):
                    a.phase = "onscene"
                    a.onscene_ticks = 0
                    a.path = None
                    if v:
                        v.note = "119 현장 처치 중"
                        self._log("info", a.target_id, f"[119] 구급차 현장 도착 · 응급 처치 시작")
                        self._agent_event(a.target_id, "인시던트 대응 에이전트", "active",
                                          "119 구급차 현장 도착 · 응급 처치 중")

            elif a.phase == "onscene":
                a.onscene_ticks += 1
                if a.onscene_ticks >= AMBULANCE_ONSCENE_TICKS:
                    a.phase = "to_hospital"
                    a.path = None
                    if v:
                        v.note = "견인 대기 중"
                    self._log("info", a.target_id, f"[119] 환자 이송 시작 → {a.hosp_name}")
                    self._agent_event(a.target_id, "인시던트 대응 에이전트", "done",
                                      f"환자 이송 시작 → {a.hosp_name}")

            else:  # to_hospital
                if self._advance_amb(a, a.hosp_lat, a.hosp_lng, step_km):
                    self._log("info", a.target_id, f"[119] {a.hosp_name} 도착 · 이송 완료")
                    del self.ambulances[aid]

    def _dispatch_tow(self, v: Vehicle) -> None:
        """사고 차량에서 가장 가까운 정비소에서 견인차를 출동시킨다."""
        shop = min(area.REPAIR_SHOPS,
                   key=lambda s: _haversine_km(v.lat, v.lng, s[1], s[2]))
        v.shop_name = shop[0]
        self._tow_seq += 1
        t = TowTruck(id=f"TOW-{self._tow_seq:02d}", lat=shop[1], lng=shop[2],
                     shop_name=shop[0], shop_lat=shop[1], shop_lng=shop[2],
                     target_id=v.id, phase="to_vehicle")
        self.tows[t.id] = t

    def _tow_route(self, t: TowTruck, dlat: float, dlng: float) -> bool:
        """견인차의 도로 경로 확보. 경로는 반드시 '견인차의 현재 위치'에서 시작해야 한다."""
        if not routing.available():
            return False
        o = {"lat": t.lat, "lng": t.lng}
        d = {"lat": dlat, "lng": dlng}
        cached = routing.cached_route(o, d)
        if cached and cached.get("points"):
            pts = [(p["lat"], p["lng"]) for p in cached["points"]]
            # 경로 첫 점이 멀면 현재 위치를 앞에 붙임
            if not pts or _haversine_km(t.lat, t.lng, pts[0][0], pts[0][1]) > 0.03:
                pts.insert(0, (t.lat, t.lng))
            if _haversine_km(pts[-1][0], pts[-1][1], dlat, dlng) > 0.03:
                pts.append((dlat, dlng))
            t.path = pts
            t.path_i = 0
            return True
        routing.prefetch(o, d)
        return False

    def _advance_tow(self, t: TowTruck, dlat: float, dlng: float, step_km: float) -> bool:
        """견인차를 목적지로 이동. 도착하면 True."""
        if _haversine_km(t.lat, t.lng, dlat, dlng) < 0.08:
            t.lat, t.lng = dlat, dlng
            t.path = None
            return True
        if t.path is None:
            if not self._tow_route(t, dlat, dlng):
                t.route_wait += 1
                if t.route_wait < ROUTE_WAIT_MAX:
                    return False  # 경로 대기(제자리)
                t.lat, t.lng = dlat, dlng  # 최후 수단: 스냅
                t.route_wait = 0
                return True
            t.route_wait = 0
        # 경로 따라 전진
        remaining = step_km
        while remaining > 1e-6 and t.path and t.path_i < len(t.path):
            tlat, tlng = t.path[t.path_i]
            d = _haversine_km(t.lat, t.lng, tlat, tlng)
            if d <= remaining or d < 1e-6:
                t.lat, t.lng = tlat, tlng
                remaining -= max(d, 0.0)
                t.path_i += 1
            else:
                frac = remaining / d
                t.heading = math.degrees(math.atan2((tlng - t.lng) * math.cos(math.radians(t.lat)), tlat - t.lat)) % 360
                t.lat += (tlat - t.lat) * frac
                t.lng += (tlng - t.lng) * frac
                remaining = 0.0
        if t.path and t.path_i >= len(t.path):
            t.path = None
            t.path_i = 0
            return _haversine_km(t.lat, t.lng, dlat, dlng) < 0.12
        return False

    def _tick_tows(self) -> None:
        """견인차 이동. 사고 차량까지 갔다가, 차를 싣고 정비소로 돌아온다."""
        step_km = TOW_SPEED_KMH / 3600 * settings.tick_seconds * MOVE_ACCEL
        for tid in list(self.tows.keys()):
            t = self.tows[tid]
            v = self.vehicles.get(t.target_id)
            if not v or not v.incident:
                del self.tows[tid]  # 대상이 사라짐(취소 등)
                continue

            if t.phase == "to_vehicle":
                if self._advance_tow(t, v.lat, v.lng, step_km):
                    t.phase = "to_shop"
                    t.path = None
                    t.path_i = 0
                    v.tow_wait = False
                    v.towing = True  # 이제 실제로 견인 시작
                    v.note = "견인 중"
                    self._log("info", v.id, f"{v.plate} 견인차 도착 · {t.shop_name}(으)로 이송 시작")
                    self._agent_event(v.id, "인시던트 대응 에이전트", "active",
                                      f"견인차 도착 · {t.shop_name} 이송 시작")
            else:  # to_shop
                arrived = self._advance_tow(t, t.shop_lat, t.shop_lng, step_km)
                v.lat, v.lng = t.lat, t.lng
                v.heading = t.heading
                if arrived:
                    v.towing = False
                    v.tow_wait = False
                    v.speed = 0
                    v.repair_elapsed = 0.0
                    v.note = "수리 중"
                    self._log("info", v.id, f"{v.plate} 정비소 입고 ({t.shop_name}) · 수리 시작")
                    self._agent_event(v.id, "정비 오퍼레이션 에이전트", "active",
                                      f"{t.shop_name} 입고 완료 · 수리 시작(예상 {REPAIR_MINUTES}분)")
                    del self.tows[tid]

    # 조회/집계
    def snapshot(self) -> list[dict]:
        return [v.to_dict() for v in self.vehicles.values()]

    def get(self, vehicle_id: str) -> Vehicle | None:
        return self.vehicles.get(vehicle_id)

    def status_counts(self) -> dict[str, int]:
        counts = {
            STATUS_DRIVING: 0,
            STATUS_PICKUP: 0,
            STATUS_WAITING: 0,
            STATUS_CHARGING: 0,
            STATUS_MAINTENANCE: 0,
        }
        for v in self.vehicles.values():
            counts[v.status] += 1
        return counts

    def current_dow(self) -> int:
        """현재 요일(0=월 … 6=일). 콘솔에서 요일을 지정하면 그 요일을 따른다(시연용)."""
        if self.forced_dow is not None:
            return self.forced_dow
        return datetime.now(KST).weekday()

    def current_demand(self) -> float:
        """가상 시각 기준 현재 수요(0~1). 시각 사이는 선형 보간하고, 요일 배율을 곱한다."""
        h = (self.sim_minutes / 60.0) % 24.0
        i = int(h) % 24
        frac = h - int(h)
        d0 = DEMAND_CURVE[i]
        d1 = DEMAND_CURVE[(i + 1) % 24]
        base = d0 + (d1 - d0) * frac
        return max(0.0, min(1.0, base * area.DAY_FACTOR.get(self.current_dow(), 1.0)))

    def target_active_ratio(self) -> float:
        """현재 수요에 대응하는 목표 운행 비율(0~1)."""
        d = self.current_demand()
        return ACTIVE_RATIO_MIN + (ACTIVE_RATIO_MAX - ACTIVE_RATIO_MIN) * d

    def target_active_count(self) -> int:
        """목표 '운행 중'(운행+손님맞이) 대수."""
        base = sum(1 for v in self.vehicles.values() if v.car_type != "SUV")
        return int(round(base * self.target_active_ratio()))

    def clock_state(self) -> dict:
        """화면에 표시할 가상 시각 + 수요 정보."""
        total = int(self.sim_minutes) % 1440
        hh, mm = total // 60, total % 60
        d = self.current_demand()
        # 현재 운행 비율 vs 목표 비율
        cc = self.status_counts()
        active = cc[STATUS_DRIVING] + cc[STATUS_PICKUP]
        registered = len(self.vehicles) or 1
        target_ratio = self.target_active_ratio()
        if d >= 0.8:
            label, tag = "매우 높음", "러시아워"
        elif d >= 0.55:
            label, tag = "높음", ""
        elif d >= 0.35:
            label, tag = "보통", ""
        elif d >= 0.2:
            label, tag = "낮음", ""
        else:
            label, tag = "매우 낮음", "심야"
        dow = self.current_dow()
        hh24 = hh
        return {
            "sim_time": f"{hh:02d}:{mm:02d}",
            "hour": hh,
            "minute": mm,
            "dow": dow,
            "day_name": area.DAY_NAMES[dow],
            # 금·토 저녁~심야 = 회식/유흥 성수기(화면에 배지로 표시)
            "party_night": bool(dow in (4, 5) and (hh24 >= 17 or hh24 <= 1)),
            "demand": round(d, 3),
            "demand_pct": round(d * 100),
            "demand_label": label,
            "demand_tag": tag,
            "curve": DEMAND_CURVE,
            "active_count": active,
            "active_pct": round(active / registered * 100),
            "target_pct": round(target_ratio * 100),
            "scale": self.time_scale,
            "paused": self.time_paused,
            "realtime": self.time_realtime,
        }

    def time_control(self, action: str, value=None) -> dict:
        """콘솔 시간 제어. action."""
        if action == "set" and value is not None:
            self.sim_minutes = float(value) % 1440.0
            self.time_paused = True
            self.time_realtime = False
        elif action == "step" and value is not None:
            self.sim_minutes = (self.sim_minutes + float(value)) % 1440.0
            self.time_paused = True
            self.time_realtime = False
        elif action == "speed" and value is not None:
            self.time_scale = max(0.1, min(60.0, float(value)))
            self.time_paused = False
            self.time_realtime = False
        elif action == "realtime":
            self.time_scale = 1.0
            self.time_paused = False
            self.time_realtime = True
            self.sim_minutes = _wall_minutes()  # 지금 실제 시각으로 즉시 동기화
        elif action == "pause":
            self.time_paused = True
            self.time_realtime = False
        elif action == "dow":
            # 요일 지정 (0=월~6=일, None이면 실제 요일)
            if value is None or int(value) < 0:
                self.forced_dow = None
            else:
                self.forced_dow = int(value) % 7
        return self.clock_state()

    def depot_status(self) -> list[dict]:
        """충전소(차고지) 위치 + 현재 그곳에서 충전 중인 차량 수."""
        out = [{"name": d[0], "lat": d[1], "lng": d[2], "charging": 0}
               for d in area.DEPOTS]
        for v in self.vehicles.values():
            if v.status != STATUS_CHARGING:
                continue
            best_i, best_d = 0, 1e9
            for i, d in enumerate(area.DEPOTS):
                dist = _haversine_km(v.lat, v.lng, d[1], d[2])
                if dist < best_d:
                    best_d, best_i = dist, i
            if best_d <= DEPOT_RADIUS_KM:
                out[best_i]["charging"] += 1
        return out

    def shop_status(self) -> list[dict]:
        """정비센터 위치 + 현재 그곳에서 수리 중인 차량 수(견인 이송 중은 제외)."""
        out = [{"name": s[0], "lat": s[1], "lng": s[2], "repairing": 0}
               for s in area.REPAIR_SHOPS]
        for v in self.vehicles.values():
            if v.status != STATUS_MAINTENANCE or v.towing:
                continue  # 견인 이송 중인 차는 아직 입고 전이라 제외
            best_i, best_d = 0, 1e9
            for i, s in enumerate(area.REPAIR_SHOPS):
                dist = _haversine_km(v.lat, v.lng, s[1], s[2])
                if dist < best_d:
                    best_d, best_i = dist, i
            if best_d <= SHOP_RADIUS_KM:
                out[best_i]["repairing"] += 1
        return out

    def kpis(self) -> dict:
        counts = self.status_counts()
        active = counts[STATUS_DRIVING] + counts[STATUS_PICKUP]  # 운행+손님맞이 = 운행중
        registered = len(self.vehicles)
        trips = self.baseline_trips + self.session_trips
        revenue = self.baseline_revenue + self.session_revenue

        def delta(now, prev):
            diff = now - prev
            pct = (diff / prev * 100) if prev else 0.0
            return {"diff": diff, "pct": round(pct, 1)}

        return {
            "trips_today": trips,
            "revenue_today": revenue,
            "registered": registered,
            "active": active,
            "deltas": {
                "trips": delta(trips, self.yesterday_trips),
                "revenue": delta(revenue, self.yesterday_revenue),
                "registered": delta(registered, self.yesterday_registered),
                "active": delta(active, self.yesterday_active),
            },
            "status_counts": counts,
            "status_labels": STATUS_LABELS,
            "status_colors": STATUS_COLORS,
        }

    def alerts(self) -> dict:
        battery_alerts = []
        incident_alerts = []
        for v in self.vehicles.values():
            if v.incident:
                incident_alerts.append(
                    {
                        "vehicle_id": v.id,
                        "plate": v.plate,
                        "type": v.incident["type"],
                        "label": "사고" if v.incident["type"] == "ACCIDENT" else "방전/고장",
                        "desc": v.incident["desc"],
                        "ts": v.incident["ts"],
                    }
                )
            elif v.status != STATUS_CHARGING and v.battery_state in ("CAUTION", "DANGER"):
                battery_alerts.append(
                    {
                        "vehicle_id": v.id,
                        "plate": v.plate,
                        "battery": round(v.battery, 1),
                        "state": v.battery_state,
                        "label": "위험" if v.battery_state == "DANGER" else "주의",
                    }
                )
        battery_alerts.sort(key=lambda a: a["battery"])
        incident_alerts.sort(key=lambda a: a["ts"], reverse=True)
        return {
            "battery": battery_alerts,
            "incident": incident_alerts,
            "events": self.events[:30],
        }

    # 금일 이용내역(운행 기록)
    def trips_today(self) -> dict:
        rows = [dict(t) for t in self.trips_log]
        completed = [t for t in rows if t.get("status") == "완료"]
        revenue = sum(t.get("fare") or 0 for t in completed)
        ratings = [t["rating"] for t in completed if t.get("rating") is not None]
        return {"rows": rows, "summary": {
            "count": len(rows), "completed": len(completed),
            "dispatched": sum(t.get("status") == "배차" for t in rows),
            "cancelled": sum(t.get("status") == "취소" for t in rows),
            "revenue": revenue,
            "avg_fare": round(revenue / len(completed)) if completed else 0,
            "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
            "manual": sum(t.get("source") == "수동" for t in rows),
        }}

    def record_trip(self, *, plate: str, vehicle_id: str, origin: str,
                    destination: str, distance_km: float | None, fare: int | None,
                    eta_min: int | None = None, status: str = "완료",
                    source: str = "자동", engine: str = "rule",
                    rating: float | None = None) -> int:
        """운행/배차 1건을 이용내역 로그에 기록하고 항목 번호(seq)를 반환한다."""
        self._trip_seq += 1
        self.trips_log.insert(0, {
            "seq": self._trip_seq,
            "ts": _now_iso(),
            "time": _now_iso()[11:19],  # HH:MM:SS
            "vehicle_id": vehicle_id,
            "plate": plate,
            "origin": origin,
            "destination": destination,
            "distance_km": round(distance_km, 2) if distance_km is not None else None,
            "fare": fare,
            "eta_min": eta_min,
            "status": status,  # 배차 / 운행중 / 완료
            "source": source,  # 자동 / 수동
            "engine": engine,  # rule / gemini
            "rating": rating,
        })
        del self.trips_log[300:]
        return self._trip_seq

    def _seed_history(self, n: int) -> None:
        """앱 시작 시점에 '오늘 이미 발생한' 완료 운행을 일부 만들어 둔다(데모용)."""
        base = datetime.now(KST)
        for _ in range(n):
            o, d = self.rng.sample(area.ANCHORS, 2)
            dist = _haversine_km(o[1], o[2], d[1], d[2])
            fare = estimate_fare(dist)
            t = base - timedelta(minutes=self.rng.randint(5, 380))
            self._trip_seq += 1
            self.trips_log.append({
                "seq": self._trip_seq,
                "ts": t.strftime("%Y-%m-%d %H:%M:%S"),
                "time": t.strftime("%H:%M:%S"),
                "vehicle_id": f"RT-{self.rng.randint(1, self.size):03d}",
                "plate": self._gen_plate(),
                "origin": o[0],
                "destination": d[0],
                "distance_km": round(dist, 2),
                "fare": fare,
                "eta_min": max(1, round(dist / 24 * 60)),
                "status": "완료",
                "source": "자동",
                "engine": "rule",
                "rating": round(self.rng.uniform(4.0, 5.0), 1),
            })
        self.trips_log.sort(key=lambda r: r["ts"], reverse=True)

    def dispatch_manual(self, origin: dict, destination: dict,
                        assign_res: dict, route_res: dict,
                        source: str = "수동") -> dict | None:
        """운영자가 출발지·목적지를 지정해 호출한 건을 실제 차량에 배차한다."""
        cand = assign_res.get("assigned")
        if not cand:
            return None
        v = self.vehicles.get(cand["id"])
        if not v:
            return None
        v.cruising = False
        # 승차 완료를 누를 때까지 출발하지 않음
        v.await_board = str(source).startswith("고객앱")
        v._arrived_logged = False
        v.status = STATUS_PICKUP
        v.trip_pickup_name = origin.get("name", "출발지")
        v.trip_origin = v.trip_pickup_name
        v.trip_dest_name = destination.get("name", "목적지")
        v.trip_dest_lat, v.trip_dest_lng = destination["lat"], destination["lng"]
        v.passenger = f"{v.trip_pickup_name} → {v.trip_dest_name}"
        v.trip_distance_km = route_res.get("distance_km")
        v.speed = max(v.speed, self.rng.uniform(22, 42))
        v.incident = None
        v.manual = True
        # 1) 픽업 이동 대상 = 손님 위치
        v.dest_name = v.trip_pickup_name
        v.dest_lat, v.dest_lng = origin["lat"], origin["lng"]
        v.depot_anchor = None
        pickup_road = (routing.road_route({"lat": v.lat, "lng": v.lng}, origin)
                       if routing.available() else None)
        if pickup_road and pickup_road.get("points"):
            v.path = [(p["lat"], p["lng"]) for p in pickup_road["points"]]
            v.path_i = 0
        else:
            v.path, v.path_i = None, 0
        # 2) 목적지 구간 (픽업 후 적용)
        if route_res.get("road") and route_res.get("primary"):
            v.trip_path = [(p["lat"], p["lng"]) for p in route_res["primary"]]
        else:
            v.trip_path = None
        seq = self.record_trip(
            plate=v.plate, vehicle_id=v.id,
            origin=v.trip_pickup_name, destination=v.trip_dest_name,
            distance_km=route_res.get("distance_km"),
            fare=assign_res.get("est_fare"),
            eta_min=assign_res.get("eta_min"),
            status="배차", source=source,
            engine=assign_res.get("engine", "rule"),
            rating=v.rating,
        )
        v.trip_seq = seq
        label = "수동배차" if source == "수동" else "배차"
        self._log("info", v.id,
                  f"[{label}] 호출 {v.trip_origin}→{v.dest_name} · {v.plate} 배정 "
                  f"(ETA {assign_res.get('eta_min')}분)")
        self._agent_event(
            v.id, "배차 에이전트", "done",
            f"{'운영자 수동 호출' if source == '수동' else '자동'} 배차 · "
            f"{v.trip_origin}→{v.dest_name} · ETA {assign_res.get('eta_min')}분 "
            f"(엔진: {_engine_label(assign_res.get('engine', 'rule'))})")
        return {"seq": seq, "vehicle": v.to_dict()}

    # 수동 이벤트(관제)
    def _event_target(self, vehicle_id: str | None, statuses: tuple) -> Vehicle | None:
        """대상 차량 선택: id 지정 시 그 차량, 없으면 주어진 상태 중 무작위 1대."""
        if vehicle_id and vehicle_id in self.vehicles:
            return self.vehicles[vehicle_id]
        cands = [v for v in self.vehicles.values()
                 if v.status in statuses and not v.incident]
        return self.rng.choice(cands) if cands else None

    def event_accident(self, vehicle_id: str | None = None) -> dict:
        """사고 발생 → 견인 → 정비소 수리 → 대체 차량 투입."""
        v = self._event_target(vehicle_id,
                               (STATUS_DRIVING, STATUS_PICKUP, STATUS_WAITING))
        if v is None:
            return {"ok": False, "msg": "대상 차량이 없습니다."}
        if v.status == STATUS_MAINTENANCE:
            return {"ok": False, "msg": f"{v.plate}는 이미 정비/수리 중입니다."}
        self._raise_incident(v, "ACCIDENT", "주행 중 접촉 사고")
        return {"ok": True, "vehicle": v.id, "plate": v.plate,
                "msg": f"🚨 {v.plate} 사고 발생 · 견인 → {v.shop_name} · 대체 차량 투입"}

    def event_breakdown(self, vehicle_id: str | None = None) -> dict:
        """차량 고장 → 견인 → 정비소 수리 → 대체 차량 투입."""
        v = self._event_target(vehicle_id,
                               (STATUS_DRIVING, STATUS_PICKUP, STATUS_WAITING))
        if v is None:
            return {"ok": False, "msg": "대상 차량이 없습니다."}
        if v.status == STATUS_MAINTENANCE:
            return {"ok": False, "msg": f"{v.plate}는 이미 정비/수리 중입니다."}
        self._raise_incident(v, "BREAKDOWN", "구동계 고장")
        return {"ok": True, "vehicle": v.id, "plate": v.plate,
                "msg": f"🔧 {v.plate} 고장 발생 · 견인 → {v.shop_name}"}

    def event_charge(self, vehicle_id: str | None = None) -> dict:
        """충전소로 보내 충전 시작(10분 충전)."""
        v = self._event_target(vehicle_id,
                               (STATUS_WAITING, STATUS_DRIVING, STATUS_PICKUP))
        if v is None:
            return {"ok": False, "msg": "대상 차량이 없습니다."}
        if v.status in (STATUS_MAINTENANCE, STATUS_CHARGING):
            return {"ok": False, "msg": f"{v.plate}는 지금 {v.status_label} 상태입니다."}
        v.passenger = None
        v.charge_elapsed = 0.0
        v.status = STATUS_CHARGING
        self._send_to_depot(v)
        self._agent_event(v.id, "플릿 오케스트레이터 에이전트", "active",
                          "배터리 확보 · 충전소 이동 → 충전 시작")
        return {"ok": True, "vehicle": v.id, "plate": v.plate,
                "msg": f"⚡ {v.plate} 충전소({v.dest_name})로 이동 → 충전 시작"}

    def event_drain(self, vehicle_id: str | None = None, level: float = 12.0) -> dict:
        """배터리를 낮춰 방전 임박 상태로 만든다(대기 차량은 곧 자동 충전)."""
        v = self._event_target(vehicle_id,
                               (STATUS_WAITING, STATUS_DRIVING, STATUS_PICKUP))
        if v is None:
            return {"ok": False, "msg": "대상 차량이 없습니다."}
        v.battery = float(max(1.0, min(100.0, level)))
        self._log("warn", v.id, f"[배터리] {v.plate} 잔량 {v.battery:.0f}% · 급속 감소")
        return {"ok": True, "vehicle": v.id, "plate": v.plate,
                "msg": f"🪫 {v.plate} 배터리 {v.battery:.0f}%로 소모"}

    def event_apply(self, kind: str, vehicle_id: str | None = None) -> dict:
        """이벤트 종류별 디스패치."""
        fn = {
            "accident": self.event_accident,
            "breakdown": self.event_breakdown,
            "charge": self.event_charge,
            "drain": self.event_drain,
        }.get(kind)
        if not fn:
            return {"ok": False, "msg": f"알 수 없는 이벤트: {kind}"}
        return fn(vehicle_id)

    def vehicle_brief(self) -> list[dict]:
        """이벤트 페이지용 차량 간단 목록(id·번호판·상태·배터리)."""
        out = []
        for v in self.vehicles.values():
            out.append({"id": v.id, "plate": v.plate,
                        "status_label": v.to_dict()["status_label"],
                        "battery": round(v.battery, 0),
                        "is_replacement": v.is_replacement})
        out.sort(key=lambda x: x["id"])
        return out
        """금일 이용내역 + 요약 통계."""
        rows = self.trips_log
        completed = [t for t in rows if t["status"] == "완료"]
        total_fare = sum(t["fare"] for t in completed if t.get("fare"))
        ratings = [t["rating"] for t in completed if t.get("rating")]
        return {
            "rows": rows[:120],
            "summary": {
                "count": len(rows),
                "completed": len(completed),
                "dispatched": sum(1 for t in rows if t["status"] == "배차"),
                "manual": sum(1 for t in rows if t["source"] == "수동"),
                "revenue": total_fare,
                "avg_fare": round(total_fare / len(completed)) if completed else 0,
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0.0,
            },
        }

    def full_state(self) -> dict:
        """WebSocket으로 보낼 전체 스냅샷."""
        return {
            "type": "snapshot",
            "ts": _now_iso(),
            "vehicles": self.snapshot(),
            "kpis": self.kpis(),
            "alerts": self.alerts(),
            "clock": self.clock_state(),
            "depots": self.depot_status(),
            "shops": self.shop_status(),
            "tows": [t.to_dict() for t in self.tows.values()],
            "ambulances": [a.to_dict() for a in self.ambulances.values()],
            "briefs": list(self.briefs),
            "demand_map": self.demand_map(),
            "repos": self.repos_metrics(),
        }


# 앱 전역에서 공유하는 단일 시뮬레이터 인스턴스
fleet = FleetSimulator()
