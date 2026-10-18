"""고객용 택시 호출 챗봇."""
from __future__ import annotations

import time
from datetime import datetime
from threading import RLock

from . import area
from . import routing
from .llm import assignment, route as route_mod, llm
from .simulation import (
    fleet, estimate_fare, _haversine_km, KST, SUV_MIN_PAX,
    STATUS_PICKUP, STATUS_DRIVING, STATUS_WAITING, STATUS_MAINTENANCE,
)

# 목적지 후보 = 서비스 지역 주요 지점
DEST_ANCHORS = area.ANCHORS  # [(name, lat, lng), ...]
DEFAULT_PICKUP = {"name": "정왕역", "lat": 37.351816, "lng": 126.742844}

_YES = ("네", "예", "응", "어", "그래", "좋아", "호출", "불러", "colcall",
        "ㅇㅇ", "ok", "오케", "콜", "가자", "부탁", "출발", "go")
_NO = ("아니", "취소", "안할", "노", "no", "됐", "그만", "안돼", "안 해")

# 데모용 빠른선택(구역별 대표 목적지)
QUICK_DESTS = [
    "한국공학대학교", "정왕역", "성담스퀘어",
    "시화병원", "시흥중앙도서관", "정왕전통시장",
]


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").lower()


class RiderService:
    def __init__(self) -> None:
        self.lock = RLock()
        self.reservations: dict[str, dict] = {}
        self.receipts: dict[str, list] = {}
        self.sessions: dict[str, dict] = {}
        self.last_trips: dict[str, dict] = {}  # 이용 후 문의(분실물)용 최근 이용 기록
        self.lost_cases: dict[str, dict] = {}  # 진행 중인 분실물 처리 건

    # 세션
    def _sess(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s:
            s = {
                "state": "await_dest",  # await_dest -> confirm -> riding -> done
                "pickup": dict(DEFAULT_PICKUP),
                "dest": None,
                "vehicle_id": None,
                "fare": None,
                "phase": None,  # pickup -> onboard -> incident -> arrived
                "passengers": 1,
                "started_at": None,
                "created": time.time(),
            }
            self.sessions[sid] = s
        return s

    def reset(self, sid: str) -> dict:
        self.sessions.pop(sid, None)
        return {"reply": "새로 시작할게요! 어디로 모실까요? 🚕", "chips": self._dest_chips(),
                "state": "await_dest"}

    def _dest_chips(self) -> list[str]:
        return list(QUICK_DESTS)

    # 목적지 매칭
    def _match_dest(self, text: str) -> dict | None:
        t = _norm(text)
        if not t:
            return None
        for name, la, ln in DEST_ANCHORS:  # 부분 일치(공백 무시)
            k = _norm(name)
            if k and (k in t or t in k):
                return {"name": name, "lat": la, "lng": ln}
        for name, la, ln in DEST_ANCHORS:  # 토큰 일치(예: '웨이브파크')
            for w in name.replace("(", " ").replace(")", " ").split():
                if len(w) >= 2 and _norm(w) in t:
                    return {"name": name, "lat": la, "lng": ln}
        return None

    def _llm_understand(self, text: str) -> tuple[dict | None, str | None]:
        """LLM으로 목적지 이해 + 친근한 답변 생성. 실패하면 (None, None)."""
        if not llm.available():
            return None, None
        names = [n for n, _, _ in DEST_ANCHORS]
        system = (
            "너는 경기도 시흥시 로보택시의 고객 호출 챗봇이야. 친근한 반말이 아닌 정중한 존댓말로 짧게 답해. "
            "사용자가 가려는 '목적지'를 아래 목록에서 정확히 하나 골라야 해. 목록에 없으면 dest는 null. "
            "목적지 목록: " + ", ".join(names) + ". "
            "반드시 JSON만 출력."
        )
        hint = '{"dest": "목록에 있는 정확한 이름 또는 null", "reply": "사용자에게 할 정중한 한국어 1~2문장"}'
        try:
            res = llm.generate_json(system, {"message": text}, hint)
        except Exception:
            res = None
        if not res:
            return None, None
        dest_name = res.get("dest")
        reply = res.get("reply")
        match = None
        if dest_name:
            dn = _norm(str(dest_name))
            for name, la, ln in DEST_ANCHORS:
                if _norm(name) == dn:
                    match = {"name": name, "lat": la, "lng": ln}
                    break
            if match is None:  # LLM이 살짝 다르게 써도 부분매칭 구제
                match = self._match_dest(str(dest_name))
        return match, reply

    # 대화
    def message(self, sid: str, text: str) -> dict:
        s = self._sess(sid)
        text = (text or "").strip()

        # 이미 배차되어 운행 중이면 상태 안내
        if s["state"] == "riding":
            tr = self.track(sid)
            ph = tr.get("phase")
            if ph == "arrived":
                s["state"] = "done"
                return {"reply": "목적지에 도착했어요. 이용해 주셔서 감사합니다! 🙌 새로 부르시려면 아래 목적지를 골라주세요.",
                        "chips": self._dest_chips(), "state": "done"}
            veh = tr.get("vehicle", {})
            if ph == "onboard":
                return {"reply": f"목적지로 이동 중이에요. 예상 {tr.get('eta_min')}분 남았어요. 편안한 여정 되세요 😊",
                        "ride": self._ride_card(s), "state": "riding"}
            return {"reply": f"{veh.get('plate','차량')}이(가) 오고 있어요. 약 {tr.get('eta_min')}분 후 도착 예정이에요!",
                    "ride": self._ride_card(s), "state": "riding"}

        # 확인 단계
        if s["state"] == "confirm":
            if any(w in text for w in _NO):
                s["state"] = "await_dest"
                s["dest"] = None
                return {"reply": "네, 취소했어요. 어디로 모실까요?", "chips": self._dest_chips(),
                        "state": "await_dest"}
            m = self._match_dest(text)  # 확인 중 새 목적지를 말하면 교체
            if m and (not s["dest"] or m["name"] != s["dest"]["name"]):
                s["dest"] = m
                return self._confirm_msg(s)
            if any(w in text for w in _YES) or text == "":
                return self._dispatch(s)
            return {"reply": "호출하시려면 ‘네’를, 목적지를 바꾸시려면 목적지를 말씀해 주세요.",
                    "chips": ["네, 호출할게요", "아니오"], "state": "confirm"}

        # 목적지 단계
        m = self._match_dest(text)
        lead = None
        if not m:
            m, lead = self._llm_understand(text)
        if not m:
            return {"reply": lead or "어디로 가시나요? 목적지를 말씀해 주시거나 아래에서 골라주세요! 🗺️",
                    "chips": self._dest_chips(), "state": "await_dest"}
        s["dest"] = m
        return self._confirm_msg(s, lead=lead)

    def _confirm_msg(self, s: dict, lead: str | None = None) -> dict:
        dist = _haversine_km(s["pickup"]["lat"], s["pickup"]["lng"],
                             s["dest"]["lat"], s["dest"]["lng"]) * 1.3  # 도로거리 근사
        fare = estimate_fare(dist)
        s["fare"] = fare
        s["state"] = "confirm"
        head = (lead + " ") if lead else ""
        msg = (f"{head}‘{s['dest']['name']}’까지 모실게요. 출발은 현재 위치({s['pickup']['name']}), "
               f"예상 요금 약 {fare:,}원이에요. 호출할까요?")
        return {"reply": msg, "chips": ["네, 호출할게요", "아니오"], "state": "confirm"}

    def _dispatch(self, s: dict) -> dict:
        origin, dest = s["pickup"], s["dest"]
        pax = int(s.get("passengers", 1) or 1)
        assign_res = assignment.assign(origin, dest, "보통", passengers=pax)
        if not assign_res.get("assigned"):
            s["state"] = "await_dest"
            msg = ("죄송해요, 지금은 배차 가능한 대형(SUV) 차량이 없어요 😢 잠시 후 다시 시도해 주세요."
                   if pax >= 5 else
                   "죄송해요, 지금은 배차 가능한 차량이 없어요 😢 잠시 후 다시 시도해 주세요.")
            return {"reply": msg, "chips": self._dest_chips(), "state": "await_dest"}
        try:
            route_res = route_mod.find_route(origin, dest, "보통", True)
        except Exception:
            route_res = {"road": False, "primary": None, "distance_km": None}
        fleet.dispatch_manual(origin, dest, assign_res, route_res, source="고객앱")
        a = assign_res["assigned"]
        s["vehicle_id"] = a["id"]
        s["state"] = "riding"
        s["phase"] = "pickup"
        eta = assign_res.get("eta_min")
        return {"reply": f"배차 완료! {a['plate']} 차량이 약 {eta}분 후 도착해요. 곧 손님께 갑니다 🚕",
                "ride": self._ride_card(s), "state": "riding"}

    # 앱 연동 API
    def places(self) -> list[dict]:
        """앱의 출발지/도착지 선택 목록(서비스 지역 주요 지점)."""
        return [{"name": n, "lat": la, "lng": ln} for n, la, ln in DEST_ANCHORS]

    def _find_place(self, name: str) -> dict | None:
        return self._match_dest(name or "")

    def quote(self, origin_name: str, dest_name: str, via_name: str = "") -> dict:
        """예상 요금·소요시간 계산(결제 확인 화면용). 배차하지 않는다."""
        o = self._find_place(origin_name)
        d = self._find_place(dest_name)
        if not o or not d:
            return {"ok": False, "msg": "출발지 또는 도착지를 찾을 수 없습니다."}
        km = _haversine_km(o["lat"], o["lng"], d["lat"], d["lng"])
        via = self._find_place(via_name) if via_name else None
        if via:  # 경유지가 있으면 출발→경유→도착
            km = (_haversine_km(o["lat"], o["lng"], via["lat"], via["lng"])
                  + _haversine_km(via["lat"], via["lng"], d["lat"], d["lng"]))
        km *= 1.3  # 도로 거리 근사
        fare = estimate_fare(km)
        eta = max(1, round(km / 24 * 60))
        return {"ok": True, "origin": o, "dest": d, "via": via,
                "distance_km": round(km, 2), "eta_min": eta, "fare": fare}

    def book(self, sid: str, origin_name: str, dest_name: str,
             via_name: str = "", passengers: int = 1,
             payment_method: str = "신한 9441", request_text: str = "") -> dict:
        with self.lock:
            return self._book(sid, origin_name, dest_name, via_name, passengers,
                              payment_method, request_text)

    def _book(self, sid: str, origin_name: str, dest_name: str,
              via_name: str, passengers: int, payment_method: str, request_text: str) -> dict:
        if self.sessions.get(sid, {}).get("vehicle_id"):
            return {"ok": False, "msg": "이미 이용 중인 차량이 있습니다."}
        if not 1 <= passengers <= 6:
            return {"ok": False, "msg": "탑승 인원은 1~6명입니다."}
        """앱에서 출발지·도착지를 직접 지정해 실제 배차한다(챗봇 없이 호출 버튼용)."""
        q = self.quote(origin_name, dest_name, via_name)
        if not q.get("ok"):
            return {"ok": False, "msg": q.get("msg", "요청을 처리할 수 없습니다.")}
        s = self._sess(sid)
        s["pickup"] = q["origin"]
        s["dest"] = q["dest"]
        s["fare"] = q["fare"]
        s["passengers"] = max(1, int(passengers or 1))
        s["started_at"] = datetime.now(KST).strftime("%H:%M")
        s["payment_method"] = payment_method
        s["request"] = request_text[:1000]
        s["via"] = q.get("via")
        res = self._dispatch(s)
        if s.get("state") != "riding":
            return {"ok": False, "msg": res.get("reply", "배차 가능한 차량이 없습니다.")}
        card = res.get("ride", {})
        veh = fleet.vehicles.get(s.get("vehicle_id") or "")
        ctype = getattr(veh, "car_type", "SEDAN") if veh else "SEDAN"
        if veh:
            s["trip_seq"] = veh.trip_seq
            veh.trip_distance_km = q["distance_km"]
            for trip in fleet.trips_log:
                if trip["seq"] == veh.trip_seq:
                    trip.update(fare=q["fare"], distance_km=q["distance_km"], payment_method=payment_method,
                                passengers=passengers, request=request_text[:1000], via=via_name)
        if veh and q.get("via"):
            via = q["via"]
            # The pickup leg stays intact. The first passenger leg goes to the stop.
            veh.trip_dest_name = via["name"]
            veh.trip_dest_lat, veh.trip_dest_lng = via["lat"], via["lng"]
            veh.trip_path = None
            s["via_pending"] = q["dest"]
            veh.via_final = q["dest"]
        if veh and request_text:
            fleet._log("info", veh.id, f"[고객 요청] {request_text[:1000]}")
        return {"ok": True, "plate": card.get("plate"), "rating": card.get("rating"),
                "fare": s.get("fare"), "eta_min": q["eta_min"],
                "dest_name": q["dest"]["name"], "origin_name": q["origin"]["name"],
                "car_type": ctype, "seats": getattr(veh, "seats", 4) if veh else 4,
                "msg": res.get("reply")}

    def reassign(self, sid: str) -> dict:
        """사고 후 '차량 재배치'."""
        s = self.sessions.get(sid)
        if not s or not s.get("dest"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        old = fleet.vehicles.get(s.get("vehicle_id") or "")
        # 사고 차량의 현재 위치를 새 출발지로
        if old:
            s["pickup"] = {"name": "사고 지점", "lat": old.lat, "lng": old.lng}
        pax = int(s.get("passengers", 1) or 1)
        assign_res = assignment.assign(s["pickup"], s["dest"], "보통", passengers=pax)
        if not assign_res.get("assigned"):
            return {"ok": False, "msg": "지금은 배차 가능한 차량이 없어요. 잠시 후 다시 시도해 주세요."}
        try:
            route_res = route_mod.find_route(s["pickup"], s["dest"], "보통", True)
        except Exception:
            route_res = {"road": False, "primary": None, "distance_km": None}
        fleet.dispatch_manual(s["pickup"], s["dest"], assign_res, route_res, source="고객앱(재배차)")
        a = assign_res["assigned"]
        s["vehicle_id"] = a["id"]
        s["phase"] = "pickup"
        s["state"] = "riding"
        s["boarded"] = False
        s["free"] = True
        replacement = fleet.vehicles.get(a["id"])
        if replacement:
            s["trip_seq"] = replacement.trip_seq
            if s.get("via_pending") and s.get("via"):
                stop = s["via"]
                replacement.trip_dest_name = stop["name"]
                replacement.trip_dest_lat, replacement.trip_dest_lng = stop["lat"], stop["lng"]
                replacement.trip_path = None
                replacement.via_final = s["dest"]
        return {"ok": True, "plate": a["plate"], "eta_min": assign_res.get("eta_min"),
                "msg": f"재배차가 완료되었습니다. {a['plate']} 차량이 약 {assign_res.get('eta_min')}분 후 도착합니다."}

    def complete(self, sid: str, free: bool = False) -> dict:
        """운행 종료 → 이용 상세(영수증) 반환. free=True 면 사고 구간 무료 처리."""
        s = self.sessions.get(sid)
        if not s:
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        v = fleet.vehicles.get(s.get("vehicle_id") or "")
        free = bool(free or s.get("free"))
        fare = 0 if free else int(s.get("fare") or 0)
        now = datetime.now(KST)
        start = s.get("started_at") or now.strftime("%H:%M")
        receipt = {
            "ok": True,
            "date": now.strftime("%y.%m.%d"),
            "origin": s["pickup"]["name"] if s.get("pickup") else "-",
            "dest": s["dest"]["name"] if s.get("dest") else "-",
            "time_range": f"{start} - {now.strftime('%H:%M')}",
            "plate": v.plate if v else "-",
            "brand": "ROBITA",
            "driver": "ROBITA 자율주행",
            "meter_fare": int(s.get("fare") or 0),
            "fare": fare,
            "free": free,
            "payment_method": s.get("payment_method", "신한 9441"),
            "paid_at": now.strftime("%y.%m.%d %H:%M"),
            "passengers": s.get("passengers", 1),
        }
        # 차량을 대기로 되돌리고 세션 종료
        if v and not v.incident:
            v.passenger = None
            v.via_final = None
            v.await_board = False
            v.path = None
            v.path_i = 0
            v.status = STATUS_WAITING
        # 이용 후 문의(분실물 등)를 위해 최근 이용 기록을 남긴다
        self.last_trips[sid] = {
            "vehicle_id": (v.id if v else None),
            "plate": (v.plate if v else "-"),
            "origin": receipt["origin"],
            "dest": receipt["dest"],
            "dest_lat": (s.get("dest") or {}).get("lat"),
            "dest_lng": (s.get("dest") or {}).get("lng"),
            "time_range": receipt["time_range"],
            "date": receipt["date"],
            "ended_at": time.time(),
        }
        for trip in fleet.trips_log:
            if trip["seq"] == s.get("trip_seq"):
                trip.update(status="완료", fare=fare, payment_method=receipt["payment_method"])
        self.receipts.setdefault(sid, []).insert(0, receipt)
        self.sessions.pop(sid, None)
        return receipt

    def reserve(self, sid: str, origin: str, destination: str, when: str,
                via: str = "", passengers: int = 1, payment_method: str = "신한 9441",
                request_text: str = "") -> dict:
        try:
            due = datetime.fromisoformat(when)
            if due.tzinfo is None:
                due = due.replace(tzinfo=KST)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "예약 시간을 확인해 주세요."}
        if due <= datetime.now(KST):
            return {"ok": False, "msg": "미래의 시간을 선택해 주세요."}
        q = self.quote(origin, destination, via)
        if not q.get("ok"):
            return q
        if not 1 <= passengers <= 6:
            return {"ok": False, "msg": "탑승 인원은 1~6명입니다."}
        with self.lock:
            if self.sessions.get(sid, {}).get("vehicle_id"):
                return {"ok": False, "msg": "현재 운행 종료 후 예약해 주세요."}
            if self.reservations.get(sid, {}).get("status") in ("pending", "dispatching"):
                return {"ok": False, "msg": "이미 접수된 예약이 있습니다."}
            r = {"origin": origin, "destination": destination, "via": via,
                 "when": due.isoformat(), "when_label": due.astimezone(KST).strftime("%m/%d %H:%M"),
                 "passengers": passengers, "payment_method": payment_method,
                 "request": request_text[:1000], "fare": q["fare"], "status": "pending"}
            self.reservations[sid] = r
            fleet._log("info", "", f"[고객 예약] {origin}→{destination} · {r['when_label']}")
            return {"ok": True, "reservation": dict(r)}

    def cancel_reservation(self, sid: str) -> dict:
        with self.lock:
            r = self.reservations.get(sid)
            if not r or r["status"] != "pending":
                return {"ok": False, "msg": "취소 가능한 예약이 없습니다."}
            r["status"] = "cancelled"
            fleet._log("info", "", "[고객 예약] 예약 취소")
            return {"ok": True}

    def process_reservations(self) -> None:
        with self.lock:
            for sid, r in self.reservations.items():
                if r["status"] != "pending" or datetime.fromisoformat(r["when"]) > datetime.now(KST):
                    continue
                r["status"] = "dispatching"
                result = self.book(sid, r["origin"], r["destination"], r["via"],
                                   r["passengers"], r["payment_method"], r["request"])
                r["status"] = "dispatched" if result.get("ok") else "failed"
                r["msg"] = result.get("msg", "")
                r["plate"] = result.get("plate", "")

    def state(self, sid: str) -> dict:
        with self.lock:
            r = self.reservations.get(sid)
            return {"reservation": dict(r) if r else None,
                    "trips": list(self.receipts.get(sid, []))}

    def select_route(self, sid: str, kind: str) -> dict:
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        v = fleet.vehicles.get(s["vehicle_id"])
        if not v or v.incident:
            return {"ok": False, "msg": "현재 경로를 변경할 수 없습니다."}
        if not routing.available():
            return {"ok": False, "msg": "도로 경로 변경은 길찾기 키 설정 후 이용할 수 있어요. 현재 경로를 유지합니다."}
        destination = s.get("via") if s.get("via_pending") else s["dest"]
        origin = {"lat": v.lat, "lng": v.lng} if s.get("phase") == "onboard" else s["pickup"]
        rr = routing.road_route(origin, destination, avoid="toll" if kind == "free" else "")
        if not rr or not rr.get("points"):
            return {"ok": False, "msg": "도로 경로를 조회하지 못했어요. 현재 경로를 유지합니다."}
        pts = [(q["lat"], q["lng"]) for q in rr["points"]]
        if s.get("phase") == "onboard":
            v.path, v.path_i = pts, 0
        else:
            v.trip_path = pts
        s["route_kind"] = kind
        fleet._log("info", v.id, f"[고객 경로] {'무료 우선' if kind == 'free' else '추천'} 경로 적용")
        return {"ok": True, "eta_min": rr.get("eta_min", 1)}

    def board(self, sid: str) -> dict:
        """고객이 '승차 완료'를 누름 → 차량이 목적지로 출발한다."""
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "배차된 차량이 없습니다."}
        res = fleet.board(s["vehicle_id"])
        if res.get("ok"):
            s["phase"] = "onboard"
            s["boarded"] = True
        return res

    def change_passengers(self, sid: str, passengers: int) -> dict:
        """탑승 인원 변경. 5명 이상이면 SUV로 교체한다."""
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "배차된 차량이 없습니다."}
        pax = max(1, min(6, int(passengers or 1)))
        v = fleet.vehicles.get(s["vehicle_id"])
        if not v:
            return {"ok": False, "msg": "차량을 찾을 수 없습니다."}
        if v.status != STATUS_PICKUP:
            s["passengers"] = pax
            return {"ok": True, "changed": False, "passengers": pax,
                    "plate": v.plate, "car_type": v.car_type,
                    "msg": f"{pax}명으로 변경했습니다. (운행 중에는 차량 교체가 어렵습니다)"}

        need_suv = pax >= SUV_MIN_PAX
        cur_suv = (v.car_type == "SUV")
        s["passengers"] = pax
        if need_suv == cur_suv:
            return {"ok": True, "changed": False, "passengers": pax,
                    "plate": v.plate, "car_type": v.car_type,
                    "msg": f"{pax}명으로 변경했습니다."}

        # 차량 교체
        assign_res = assignment.assign(s["pickup"], s["dest"], "보통", passengers=pax)
        if not assign_res.get("assigned"):
            kind = "대형(SUV)" if need_suv else "일반"
            return {"ok": False, "msg": f"지금은 배차 가능한 {kind} 차량이 없어요."}
        v.passenger = None
        v.path = None
        v.path_i = 0
        v.await_board = False
        v.status = STATUS_WAITING
        try:
            route_res = route_mod.find_route(s["pickup"], s["dest"], "보통", True)
        except Exception:
            route_res = {"road": False, "primary": None, "distance_km": None}
        fleet.dispatch_manual(s["pickup"], s["dest"], assign_res, route_res, source="고객앱(차량변경)")
        a = assign_res["assigned"]
        s["vehicle_id"] = a["id"]
        s["phase"] = "pickup"
        nv = fleet.vehicles.get(a["id"])
        ctype = getattr(nv, "car_type", "SEDAN")
        label = f"대형(SUV · {getattr(nv, 'seats', 6)}인승)" if ctype == "SUV" else "일반"
        return {"ok": True, "changed": True, "passengers": pax,
                "plate": a["plate"], "car_type": ctype, "eta_min": assign_res.get("eta_min"),
                "msg": f"{pax}명 탑승에 맞춰 {label} 차량 {a['plate']}(으)로 변경되었습니다. "
                       f"약 {assign_res.get('eta_min')}분 후 도착합니다."}

    def change_destination(self, sid: str, dest_name: str) -> dict:
        """운행 중 목적지 변경."""
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        d = self._find_place(dest_name)
        if not d:
            return {"ok": False, "msg": f"‘{dest_name}’ 는 서비스 지역에서 찾을 수 없어요."}
        v = fleet.vehicles.get(s["vehicle_id"])
        if not v or v.incident:
            return {"ok": False, "msg": "지금은 목적지를 변경할 수 없어요."}
        if d["name"] == (s.get("dest") or {}).get("name"):
            return {"ok": False, "msg": f"이미 {d['name']}(으)로 가고 있어요."}

        old_name = (s.get("dest") or {}).get("name", "-")
        # 현재 위치 → 새 목적지로 요금·시간 재계산
        km = _haversine_km(v.lat, v.lng, d["lat"], d["lng"]) * 1.3
        eta = max(1, round(km / 24 * 60))
        extra = estimate_fare(km)

        # 실제 차량 경로 갱신
        v.dest_name, v.dest_lat, v.dest_lng = d["name"], d["lat"], d["lng"]
        v.trip_dest_name, v.trip_dest_lat, v.trip_dest_lng = d["name"], d["lat"], d["lng"]
        v.depot_anchor = (v.lat, v.lng)
        v.path, v.path_i, v.route_wait = None, 0, 0
        fleet._try_load_depot_route(v)

        s["dest"] = d
        s["fare"] = (s.get("fare") or 0) + extra // 3  # 변경분 일부 반영
        fleet._log("info", v.id, f"[목적지 변경] {v.plate} {old_name} → {d['name']} (AI 에이전트)")
        fleet._agent_event(v.id, "최적 경로 탐색 에이전트", "done",
                           f"승객 요청으로 목적지 변경: {old_name} → {d['name']} · 재경로 산출 완료")
        return {"ok": True, "dest": d["name"], "old": old_name,
                "eta_min": eta, "fare": s["fare"],
                "msg": f"목적지를 {d['name']}(으)로 변경했어요. 약 {eta}분 후 도착 예정입니다."}

    def add_stop(self, sid: str, place_name: str) -> dict:
        """경유지 추가."""
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        p = self._find_place(place_name)
        if not p:
            return {"ok": False, "msg": f"‘{place_name}’ 는 서비스 지역에서 찾을 수 없어요."}
        v = fleet.vehicles.get(s["vehicle_id"])
        if not v or v.incident:
            return {"ok": False, "msg": "지금은 경유지를 추가할 수 없어요."}
        final = s.get("dest")
        s["via_pending"] = final  # 경유 후 돌아갈 최종 목적지
        v.via_final = final
        v.dest_name, v.dest_lat, v.dest_lng = p["name"], p["lat"], p["lng"]
        v.trip_dest_name, v.trip_dest_lat, v.trip_dest_lng = p["name"], p["lat"], p["lng"]
        v.depot_anchor = (v.lat, v.lng)
        v.path, v.path_i, v.route_wait = None, 0, 0
        fleet._try_load_depot_route(v)
        fleet._log("info", v.id, f"[경유지 추가] {v.plate} → {p['name']} 경유 후 {final['name']}")
        return {"ok": True, "stop": p["name"], "final": final["name"],
                "msg": f"{p['name']}에 먼저 들렀다가 {final['name']}(으)로 모실게요."}

    # 탑승 중 AI 에이전트
    # AI 에이전트가 호출할 도구 목록
    TOOLS = """
- change_destination(place): 목적지를 바꾼다. 승객이 "거기 말고 OO로 가줘", "목적지 바꿔줘" 라고 할 때.
- add_stop(place): 경유지를 추가한다. "가는 길에 OO 들러줘" 라고 할 때.
- change_route(): 경로를 바꾼다. "길이 막혀", "다른 길로", "돌아가줘" 라고 할 때.
- set_aircon(mode): 냉난방. mode = cool | heat | off.
- play_music(): 음악을 켠다.
- open_window(): 창문을 연다/환기.
- set_speed(mode): 속도. mode = slow | normal.
- stop_vehicle(): 잠시 정차한다.
- call_support(): 상담사를 연결한다.
- eta(): 남은 시간·목적지를 알려준다.
- none(): 위 어디에도 해당하지 않는 잡담·문의.
""".strip()

    def _agent_decide(self, text: str, ctx: dict) -> dict | None:
        """GPT 가 승객 발화를 이해해 실행할 도구와 인자를 결정한다."""
        if not llm.available():
            return None
        places = [n for n, _, _ in DEST_ANCHORS]
        system = (
            "너는 자율주행 로보택시 '로비타'를 실제로 조종하는 AI 에이전트다. "
            "승객의 말을 듣고 아래 도구 중 정확히 하나를 골라 실행한다.\n\n"
            f"[사용 가능한 도구]\n{self.TOOLS}\n\n"
            f"[서비스 지역 장소 목록] {', '.join(places)}\n\n"
            "규칙:\n"
            "1. place 인자는 반드시 위 장소 목록에 있는 정확한 이름이어야 한다. 목록에 없으면 none 을 쓰고 안내하라.\n"
            "2. reply 는 실제로 그 동작을 수행한 것처럼 정중한 존댓말 1~2문장으로 짧게 쓴다.\n"
            "3. 목적지 변경과 경유지 추가를 혼동하지 마라. '대신/말고' → change_destination, '들러서/거쳐서' → add_stop.\n"
            "4. 반드시 JSON 만 출력한다."
        )
        hint = ('{"tool": "change_destination", "place": "정왕역", "mode": null, '
                '"reply": "목적지를 정왕역으로 변경할게요."}')
        try:
            return llm.generate_json(system, {"승객_발화": text, "현재_운행": ctx}, hint)
        except Exception:
            return None

    _OUT_OF_AREA = ("강남", "서울역", "인천", "부산", "홍대", "여의도", "판교", "수원")

    def _agent_fallback(self, text: str) -> dict:
        """LLM 이 없을 때 키워드로 도구를 고른다(동작은 동일)."""
        t = (text or "").replace(" ", "")
        place = self._match_dest(text)
        if not place and any(k in t for k in self._OUT_OF_AREA):
            return {"tool": "out_of_area"}
        if place and any(k in t for k in ("말고", "대신", "바꿔", "변경", "로가", "으로가")):
            return {"tool": "change_destination", "place": place["name"]}
        if place and any(k in t for k in ("들러", "들렀", "거쳐", "경유")):
            return {"tool": "add_stop", "place": place["name"]}
        if place:
            return {"tool": "change_destination", "place": place["name"]}
        for keys, tool, mode in [
            (("에어컨", "냉방", "시원", "더워", "덥"), "set_aircon", "cool"),
            (("히터", "난방", "따뜻", "추워", "춥"), "set_aircon", "heat"),
            (("음악", "노래", "라디오"), "play_music", None),
            (("창문", "환기", "바람"), "open_window", None),
            (("막히", "다른길", "우회", "돌아", "경로"), "change_route", None),
            (("천천", "느리게"), "set_speed", "slow"),
            (("세워", "정차", "잠깐"), "stop_vehicle", None),
            (("상담사", "사람", "직원"), "call_support", None),
            (("얼마나", "언제", "몇분", "도착"), "eta", None),
        ]:
            if any(k in t for k in keys):
                return {"tool": tool, "mode": mode}
        return {"tool": "none"}

    def ride_request(self, sid: str, text: str) -> dict:
        """탑승 중 승객 요청."""
        s = self.sessions.get(sid) or {}
        v = fleet.vehicles.get(s.get("vehicle_id") or "")
        ctx = {
            "차량": (v.plate if v else "-"),
            "현재_목적지": (s.get("dest") or {}).get("name", "-"),
            "탑승인원": s.get("passengers", 1),
        }
        decided = self._agent_decide(text, ctx)
        engine = "ai"
        if not decided or not decided.get("tool"):
            decided = self._agent_fallback(text)
            engine = "rule"

        tool = str(decided.get("tool", "none"))
        place = decided.get("place")
        mode = decided.get("mode")
        ai_reply = decided.get("reply")

        # 실제 실행
        result = {"ok": True, "action": tool, "engine": engine}
        if tool == "change_destination" and place:
            r = self.change_destination(sid, str(place))
            result.update(r)
            result["reply"] = r["msg"] if not r["ok"] else (ai_reply or r["msg"])
            result["action"] = "change_destination" if r["ok"] else "none"
            if r["ok"]:
                result["reply"] = f"{r['msg']}"
        elif tool == "add_stop" and place:
            r = self.add_stop(sid, str(place))
            result.update(r)
            result["reply"] = r["msg"] if not r["ok"] else (ai_reply or r["msg"])
            result["action"] = "add_stop" if r["ok"] else "none"
        elif tool == "change_route":
            result["reply"] = ai_reply or "경로 옵션을 확인해 드릴게요."
            result["action"] = "route"
        elif tool == "set_aircon":
            m = {"cool": "에어컨을 켜고 실내 온도를 22도로 맞췄어요 ❄️",
                 "heat": "히터를 켰어요. 곧 따뜻해집니다 ♨️",
                 "off": "냉난방을 껐어요."}.get(str(mode), "실내 온도를 조절했어요.")
            result["reply"] = ai_reply or m
        elif tool == "play_music":
            result["reply"] = ai_reply or "잔잔한 음악을 재생할게요 🎵"
        elif tool == "open_window":
            result["reply"] = ai_reply or "창문을 살짝 내려 환기할게요 🌬️"
        elif tool == "set_speed":
            if v:
                v.speed = max(12.0, v.speed * (0.7 if mode == "slow" else 1.0))
            result["reply"] = ai_reply or "속도를 조절했어요. 안전 운행을 최우선으로 합니다."
        elif tool == "stop_vehicle":
            if v:
                v.speed = 0
            result["reply"] = ai_reply or "안전한 곳을 찾아 잠시 정차하겠습니다."
        elif tool == "call_support":
            result["reply"] = ai_reply or "상담사를 연결해 드릴게요."
            result["action"] = "support"
        elif tool == "eta":
            tr = self.track(sid)
            if tr.get("active"):
                result["reply"] = (f"{tr['dest']['name']}까지 약 {tr['eta_min']}분 남았어요.")
            else:
                result["reply"] = ai_reply or "현재 운행 정보를 불러올 수 없어요."
        elif tool == "out_of_area":
            names = ", ".join([n for n, _, _ in DEST_ANCHORS][:4])
            result["reply"] = ("죄송해요, 현재 서비스 지역(시흥 정왕동) 밖이라 갈 수 없어요.\n"
                               f"가능한 목적지: {names} 등")
            result["action"] = "none"
        else:
            result["reply"] = ai_reply or "요청을 접수했어요. 도움이 필요하시면 언제든 말씀해 주세요."
            result["action"] = "none"
        return result

    # 이용 후 문의(분실물)
    def last_trip(self, sid: str) -> dict:
        """가장 최근 이용 기록(분실물 문의용)."""
        t = self.last_trips.get(sid)
        if not t:
            return {"ok": False, "msg": "최근 이용 내역이 없습니다."}
        v = fleet.vehicles.get(t.get("vehicle_id") or "")
        busy = bool(v and v.status in (STATUS_PICKUP, STATUS_DRIVING))
        return {"ok": True, "plate": t["plate"], "dest": t["dest"],
                "date": t["date"], "time_range": t["time_range"],
                "vehicle_exists": bool(v),
                "vehicle_busy": busy,
                "vehicle_status": (v.to_dict()["status_label"] if v else "-")}

    def report_lost_item(self, sid: str, item: str = "분실물") -> dict:
        """분실물 접수."""
        t = self.last_trips.get(sid)
        if not t:
            return {"ok": False, "msg": "최근 이용 내역이 없어 분실물 조회가 어려워요. 상담사를 연결해 드릴까요?",
                    "need_agent": True}
        v = fleet.vehicles.get(t.get("vehicle_id") or "")
        if not v:
            return {"ok": False, "need_agent": True,
                    "msg": "해당 차량 정보를 찾을 수 없어요. 상담사를 연결해 드릴게요."}

        # 고객 위치 = 하차 지점(목적지)
        clat = t.get("dest_lat")
        clng = t.get("dest_lng")
        if clat is None or clng is None:
            return {"ok": False, "need_agent": True,
                    "msg": "하차 위치를 확인할 수 없어요. 상담사를 연결해 드릴게요."}

        busy = v.status in (STATUS_PICKUP, STATUS_DRIVING)
        # 사고/정비 중이면 사람이 처리해야 함
        if v.incident or v.status == STATUS_MAINTENANCE:
            return {"ok": False, "need_agent": True,
                    "msg": f"{v.plate} 차량이 현재 정비 중이라 확인이 어려워요. 상담사를 연결해 드릴게요."}

        if not busy:
            # ① 택시가 한가함 → 고객 위치로 차량을 보낸다
            r = fleet.deliver_to(v.id, clat, clng, note=f"{item} 전달")
            if not r.get("ok"):
                return {"ok": False, "need_agent": True,
                        "msg": "지금 차량을 보낼 수 없어요. 상담사를 연결해 드릴게요."}
            self.lost_cases[sid] = {"mode": "deliver", "vehicle_id": v.id,
                                    "plate": v.plate, "item": item,
                                    "lat": clat, "lng": clng}
            return {"ok": True, "mode": "deliver", "plate": v.plate,
                    "eta_min": r["eta_min"], "place": t["dest"],
                    "msg": (f"{v.plate} 차량이 현재 운행하고 있지 않아,\n"
                            f"{item}을(를) 실은 채로 {t['dest']}(하차 지점)으로 보내드릴게요.\n"
                            f"약 {r['eta_min']}분 후 도착합니다.")}

        # ② 택시가 운행 중 → 가까운 정비센터에 맡긴다
        shop = min(area.REPAIR_SHOPS, key=lambda s: _haversine_km(clat, clng, s[1], s[2]))
        dist = _haversine_km(clat, clng, shop[1], shop[2])
        self.lost_cases[sid] = {"mode": "shop", "vehicle_id": v.id,
                                "plate": v.plate, "item": item, "shop": shop[0]}
        fleet._log("info", v.id, f"[분실물] {v.plate} 운행 중 → {shop[0]}에 {item} 보관 예정")
        fleet._agent_event(v.id, "고객 지원 에이전트", "done",
                           f"분실물 접수 · 운행 종료 후 {shop[0]} 보관 처리")
        return {"ok": True, "mode": "shop", "plate": v.plate, "shop": shop[0],
                "distance_km": round(dist, 2),
                "msg": (f"{v.plate} 차량이 현재 다른 손님을 모시고 운행 중이에요.\n"
                        f"운행이 끝나는 대로 {item}을(를) 가장 가까운 "
                        f"{shop[0]}(약 {dist:.1f}km)에 맡겨 둘게요.\n"
                        f"방문하셔서 수령하실 수 있어요.")}

    def post_ride_inquiry(self, sid: str, text: str) -> dict:
        """이용 후 문의(마이페이지 > 문의하기)의 자유 입력을 처리한다."""
        t = (text or "").replace(" ", "")
        lost_words = ("분실", "두고", "놓고", "잃어", "잊어", "지갑", "휴대폰", "핸드폰",
                      "가방", "우산", "이어폰", "카드", "찾아")
        if any(k in t for k in lost_words):
            # 물건 이름 추정
            item = next((w for w in ("지갑", "휴대폰", "핸드폰", "가방", "우산", "이어폰", "카드")
                         if w in t), "분실물")
            return {**self.report_lost_item(sid, item), "action": "lost"}

        # 해결 불가면 상담사 연결 권유
        if llm.available():
            system = (
                "너는 로보택시 '로비타'의 이용 후 고객지원 AI다. 이미 운행이 끝난 고객의 문의를 처리한다. "
                "요금·영수증·사고 보상·서비스 이용 방법 등 일반 안내는 직접 답한다. "
                "환불 승인, 결제 오류 정정, 개인정보 변경, 법적 분쟁, 보상 금액 확정처럼 "
                "사람이 확인해야 하는 문의는 need_agent 를 true 로 하고 상담사 연결을 권한다. "
                "정중한 존댓말 1~2문장. JSON 만 출력."
            )
            hint = '{"need_agent": false, "reply": "정중한 한국어 1~2문장"}'
            try:
                res = llm.generate_json(system, {"문의": text}, hint)
            except Exception:
                res = None
            if res and res.get("reply"):
                return {"ok": True, "engine": "ai", "action": "answer",
                        "need_agent": bool(res.get("need_agent")),
                        "msg": str(res["reply"])}

        # 규칙 기반 폴백
        if any(k in t for k in ("환불", "결제오류", "이중결제", "취소해", "보상금액", "개인정보")):
            return {"ok": False, "need_agent": True, "engine": "rule", "action": "answer",
                    "msg": "확인이 필요한 문의예요. 상담사를 연결해 드릴게요."}
        if any(k in t for k in ("요금", "얼마")):
            return {"ok": True, "need_agent": False, "engine": "rule", "action": "answer",
                    "msg": "요금은 기본요금에 이동 거리와 예상 소요 시간을 반영해 계산돼요."}
        if any(k in t for k in ("영수증", "내역")):
            return {"ok": True, "need_agent": False, "engine": "rule", "action": "answer",
                    "msg": "마이페이지 > 이용 기록에서 지난 운행 내역을 확인하실 수 있어요."}
        return {"ok": False, "need_agent": True, "engine": "rule", "action": "answer",
                "msg": "제가 바로 처리하기 어려운 문의예요. 상담사를 연결해 드릴까요?"}

    def report_injury(self, sid: str) -> dict:
        """탑승자 부상 신고 → 119 구급차 출동."""
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        return fleet.dispatch_ambulance(s["vehicle_id"])

    def cancel(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            self.sessions.pop(sid, None)
            return {"ok": True, "msg": "취소되었습니다."}
        v = fleet.vehicles.get(s["vehicle_id"])
        if v and not v.incident:
            v.passenger = None
            v.via_final = None
            v.await_board = False
            v.path = None
            v.path_i = 0
            v.status = STATUS_WAITING
            fleet._log("info", v.id, f"{v.plate} 고객앱 호출 취소 · 대기 복귀")
        for trip in fleet.trips_log:
            if trip["seq"] == s.get("trip_seq"):
                trip.update(status="취소", fare=0)
        self.sessions.pop(sid, None)
        return {"ok": True, "msg": "호출이 취소되었습니다."}

    # 추적
    def _ride_card(self, s: dict) -> dict:
        v = fleet.vehicles.get(s["vehicle_id"]) if s.get("vehicle_id") else None
        card = {"plate": v.plate if v else "-", "rating": v.rating if v else None,
                "fare": s.get("fare"), "dest_name": s["dest"]["name"] if s.get("dest") else ""}
        return card

    def route_options(self, sid: str) -> dict:
        """픽업->목적지 경로를 카카오모빌리티로 조회한다."""
        s = self.sessions.get(sid)
        if not s or not s.get("dest"):
            return {"ok": False, "msg": "진행 중인 운행이 없습니다."}
        o, d = s["pickup"], s["dest"]
        opts = routing.route_options(o, d) if routing.available() else {"recommend": None, "free": None}

        # 직선거리 기반 폴백치
        base_km = round(_haversine_km(o["lat"], o["lng"], d["lat"], d["lng"]) * 1.3, 2)
        base_eta = max(1, round(base_km / 24 * 60))

        def _opt(res, add_min=0, add_km=0.0):
            if res:
                return {"eta_min": res["eta_min"], "distance_km": res["distance_km"],
                        "taxi_fare": res.get("taxi_fare"), "source": "kakao"}
            return {"eta_min": base_eta + add_min, "distance_km": round(base_km + add_km, 2),
                    "taxi_fare": None, "source": "estimate"}

        recommend = _opt(opts.get("recommend"))
        free = _opt(opts.get("free"), add_min=5, add_km=0.6)  # 유료도로 회피 → 보통 더 돌아감
        # 무료 우선이 추천보다 빠르거나 같게 나오면
        if free["eta_min"] <= recommend["eta_min"]:
            free["eta_min"] = recommend["eta_min"] + 3
        engine = "kakao" if (recommend["source"] == "kakao" or free["source"] == "kakao") else "estimate"
        return {"ok": True, "recommend": recommend, "free": free, "engine": engine,
                "origin": o["name"], "dest": d["name"]}

    def route_path(self, sid: str) -> dict:
        """경로 카드 미니맵용."""
        s = self.sessions.get(sid)
        if not s or not s.get("dest"):
            return {"ok": False}
        o, d = s["pickup"], s["dest"]
        pts = None
        if routing.available():
            rr = routing.road_route(o, d)
            if rr and rr.get("points"):
                pts = rr["points"]
        if not pts:
            pts = [{"lat": o["lat"], "lng": o["lng"]}, {"lat": d["lat"], "lng": d["lng"]}]
        return {
            "ok": True,
            "points": pts,
            "origin": {"lat": o["lat"], "lng": o["lng"], "name": o.get("name", "출발")},
            "dest": {"lat": d["lat"], "lng": d["lng"], "name": d.get("name", "도착")},
        }

    def track(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s or not s.get("vehicle_id"):
            return {"active": False}
        v = fleet.vehicles.get(s["vehicle_id"])
        if not v:
            return {"active": False}
        vd = v.to_dict()
        # Stop transitions happen in the simulator, independent of rider polling.
        if s.get("via_pending") and not getattr(v, "via_final", None):
            s.pop("via_pending", None)

        if v.incident:
            s["phase"] = "incident"  # 사고/고장 → 앱 사고 대응 시나리오
        elif s.get("phase") not in ("arrived",):
            if v.status == STATUS_PICKUP:
                s["phase"] = "pickup"
            elif v.status == STATUS_DRIVING:
                s["phase"] = "onboard"
            elif s.get("phase") == "incident":
                pass  # 사고 처리 중
            else:
                # 픽업/운행이 아니면(대기 등) 목적지 근처면 도착으로 간주
                near = _haversine_km(v.lat, v.lng, s["dest"]["lat"], s["dest"]["lng"]) < 0.35
                if near or v.status == STATUS_WAITING:
                    s["phase"] = "arrived"
        target = s["pickup"] if s.get("phase") == "pickup" else s["dest"]
        dist = _haversine_km(v.lat, v.lng, target["lat"], target["lng"])
        eta = max(1, round(dist / 24 * 60)) if s.get("phase") in ("pickup", "onboard") else 0
        phase_label = {"pickup": "차량이 오고 있어요",
                       "onboard": "목적지로 이동 중",
                       "incident": "사고 대응 중",
                       "arrived": "도착 완료"}.get(s.get("phase"), "")
        return {
            "active": True,
            "phase": s.get("phase"),
            "phase_label": phase_label,
            "eta_min": eta,
            "fare": s.get("fare"),
            "pickup": s["pickup"],
            "dest": s["dest"],
            "passengers": s.get("passengers", 1),
            "started_at": s.get("started_at"),
            "boarded": bool(s.get("boarded")),
            "free": bool(s.get("free")),
            "payment_method": s.get("payment_method", "신한 9441"),
            "via": s.get("via"),
            "request": s.get("request", ""),
            # 픽업 지점에 정차했을 때만 승차 안내
            "awaiting_board": bool(getattr(v, "await_board", False)
                                   and v.status == STATUS_PICKUP
                                   and v.speed < 0.5
                                   and _haversine_km(v.lat, v.lng,
                                                     s["pickup"]["lat"], s["pickup"]["lng"]) < 0.12),
            # 사고/고장 발생 시 앱의 사고 대응 시나리오를 켜기 위한 정보
            "incident": ({"type": v.incident["type"], "desc": v.incident["desc"]}
                         if v.incident else None),
            "vehicle": {
                "id": v.id, "plate": v.plate, "rating": v.rating,
                "lat": vd["lat"], "lng": vd["lng"],
                "status_label": vd["status_label"],
                "towing": vd.get("towing", False),
                "route": vd.get("route", []),
                "car_type": getattr(v, "car_type", "SEDAN"),
                "seats": getattr(v, "seats", 4),
            },
        }


rider = RiderService()
