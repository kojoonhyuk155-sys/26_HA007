"""FastAPI 앱 본체.

실행: uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import random
import json
import time
import threading

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import auth, area, routing
from .config import settings, BASE_DIR
from .rider import rider
from .ops import ops
from .support import support
from .simulation import (
    fleet,
    estimate_fare,
    _haversine_km,
    STATUS_LABELS,
    STATUS_DRIVING,
    STATUS_WAITING,
    STATUS_CHARGING,
    STATUS_MAINTENANCE,
)
from .llm import assignment, risk, route, incident
from .llm import llm as llmsvc

app = FastAPI(title="로보택시 통합 관제 플랫폼")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 8)
# 콘솔 전용 공개 엔드포인트용 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Jinja2 캐시 키 버그 회피용
try:
    templates.env.cache = None
    templates.env.auto_reload = True
except Exception:
    pass

# 정적 파일 캐시 무력화 토큰
ASSET_VER = str(int(time.time()))
try:
    templates.env.globals["asset_ver"] = ASSET_VER
except Exception:
    pass


# 인증 헬퍼
def current_user(request: Request) -> dict | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    return {
        "id": uid,
        "name": request.session.get("user_name", ""),
        "email": request.session.get("user_email", ""),
        "role": request.session.get("user_role", "operator"),
    }


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# WebSocket 연결 관리
class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        dead = []
        data = json.dumps(message, ensure_ascii=False)
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()

# 에이전트 로그 캐시
_AGENT_LOG_CACHE: dict[str, tuple] = {}
_AGENT_LOG_TTL = 60.0  # 초
_AGENT_REFRESH_LOCK = threading.Lock()
_AGENT_REFRESHING: set[str] = set()  # 백그라운드 GPT 갱신 진행 중인 차량(중복 방지)


# 백그라운드 시뮬레이션
async def _sim_loop():
    """주기적으로 플릿을 갱신하고, 가끔 자동 호출을 발생시켜 배차 모델을 돌린다."""
    tick_count = 0
    while True:
        try:
            fleet.tick()
            await asyncio.to_thread(rider.process_reservations)
            tick_count += 1
            # 약 12틱마다 자동 호출 1건
            if tick_count % 12 == 0:
                asyncio.create_task(asyncio.to_thread(_auto_call))
            await manager.broadcast(fleet.full_state())
        except Exception as exc:
            print(f"[sim_loop] 오류: {exc}")
        await asyncio.sleep(settings.tick_seconds)


async def _ops_loop():
    """자율 관제 엔진."""
    await asyncio.sleep(3)  # 시작 직후 초기 데이터 확보
    while True:
        try:
            await asyncio.to_thread(ops.run)
        except Exception as exc:
            print(f"[ops_loop] 오류: {exc}")
        await asyncio.sleep(18)  # 약 18초마다 갱신


def _auto_call():
    try:
        a, b = random.sample(area.ANCHORS, 2)
        origin = {"name": a[0], "lat": a[1], "lng": a[2]}
        destination = {"name": b[0], "lat": b[1], "lng": b[2]}
        res = assignment.assign(origin, destination)
        if res.get("assigned"):
            c = res["assigned"]
            fleet._log(
                "info",
                c["id"],
                f"[배차] 호출 {origin['name']}→{destination['name']} · {c['plate']} 배정 (ETA {res['eta_min']}분)",
            )
    except Exception as exc:
        print(f"[auto_call] 배차 로그 생성 실패(무시): {exc}")


@app.on_event("startup")
async def _startup():
    auth.init_db()
    asyncio.create_task(_sim_loop())
    asyncio.create_task(_ops_loop())  # 자율 관제 엔진
    # 길찾기 프리페치 워커 시작
    routing.start_worker()
    if routing.available():
        warmed = 0
        # 충전소·정비센터 경로 먼저 예열
        for a in area.ANCHORS:
            dep = min(area.DEPOTS, key=lambda d: _haversine_km(a[1], a[2], d[1], d[2]))
            routing.prefetch({"lat": a[1], "lng": a[2]}, {"lat": dep[1], "lng": dep[2]})
            shop = min(area.REPAIR_SHOPS, key=lambda s: _haversine_km(a[1], a[2], s[1], s[2]))
            routing.prefetch({"lat": a[1], "lng": a[2]}, {"lat": shop[1], "lng": shop[2]})
            warmed += 2
        # 2) 같은 구역 안 지점쌍 경로(일반 운행용)
        for area_name, anchors_in_area in area.AREAS.items():
            for a in anchors_in_area:
                for b in anchors_in_area:
                    if a is not b:
                        routing.prefetch({"lat": a[1], "lng": a[2]},
                                         {"lat": b[1], "lng": b[2]})
                        warmed += 1
        print(f"  - 길찾기(도로경로): 사용 · 지점쌍 {warmed}개 백그라운드 준비 중(충전소 우선)")
    else:
        print("  - 길찾기(도로경로): 미설정(직선 경로) — .env 의 KAKAO_REST_KEY 확인")
    print("=" * 60)
    print(" 로보택시 통합 관제 플랫폼 시작")
    print(f"  - 플릿 차량 수 : {fleet.size}")
    print(f"  - 갱신 주기    : {settings.tick_seconds}s")
    print(f"  - LLM          : {settings.llm_label}")
    print("  - 기본 관리자  : admin@robotaxi.local / admin1234")
    print("=" * 60)


# 페이지
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    user = auth.authenticate(email, password)
    if not user:
        return templates.TemplateResponse(request, 
            "login.html",
            {"request": request, "error": "이메일 또는 비밀번호가 올바르지 않습니다."},
            status_code=401,
        )
    request.session["user_id"] = user["id"]
    request.session["user_name"] = user["name"]
    request.session["user_email"] = user["email"]
    request.session["user_role"] = user["role"]
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(request, 
        "signup.html", {"request": request, "error": None, "success": None}
    )


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    if password != password2:
        return templates.TemplateResponse(request, 
            "signup.html",
            {"request": request, "error": "비밀번호가 일치하지 않습니다.", "success": None},
            status_code=400,
        )
    ok, msg = auth.create_user(email, name, password)
    if not ok:
        return templates.TemplateResponse(request, 
            "signup.html",
            {"request": request, "error": msg, "success": None},
            status_code=400,
        )
    return templates.TemplateResponse(request, 
        "login.html", {"request": request, "error": None, "notice": msg}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, 
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "kakao_key": settings.kakao_js_key,
            "center": {"lat": area.CENTER_LAT, "lng": area.CENTER_LNG},
            "llm_enabled": settings.llm_enabled,
            "model": settings.llm_model,
            "llm_name": settings.llm_name,
        },
    )


@app.get("/monitor/{vehicle_id}", response_class=HTMLResponse)
async def monitor(request: Request, vehicle_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    v = fleet.get(vehicle_id)
    if not v:
        raise HTTPException(status_code=404, detail="차량을 찾을 수 없습니다.")
    return templates.TemplateResponse(request, 
        "monitor.html",
        {
            "request": request,
            "user": user,
            "kakao_key": settings.kakao_js_key,
            "vehicle_id": vehicle_id,
            "llm_enabled": settings.llm_enabled,
            "model": settings.llm_model,
            "llm_name": settings.llm_name,
        },
    )


@app.get("/call", response_class=HTMLResponse)
async def call_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, 
        "call.html",
        {
            "request": request,
            "user": user,
            "kakao_key": settings.kakao_js_key,
            "center": {"lat": area.CENTER_LAT, "lng": area.CENTER_LNG},
            "llm_enabled": settings.llm_enabled,
            "model": settings.llm_model,
            "llm_name": settings.llm_name,
            "area_name": area.AREA_NAME,
        },
    )


@app.get("/trips", response_class=HTMLResponse)
async def trips_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, 
        "trips.html",
        {
            "request": request,
            "user": user,
            "llm_enabled": settings.llm_enabled,
            "model": settings.llm_model,
            "llm_name": settings.llm_name,
            "area_name": area.AREA_NAME,
        },
    )


@app.get("/control", response_class=HTMLResponse)
async def control_page():
    """독립 '데모 제어' 페이지."""
    return FileResponse(str(BASE_DIR / "static" / "demo_control.html"))


@app.get("/rider", response_class=HTMLResponse)
async def rider_page(request: Request):
    """고객용 택시 호출 앱. 로그인 없이 열린다."""
    return templates.TemplateResponse(request, "rider.html", {
        "request": request,
        "kakao_js_key": settings.kakao_js_key,
        "llm_enabled": settings.llm_enabled,
        "llm_name": settings.llm_name,
        "area_name": area.AREA_NAME,
        "map_center": {"lat": area.CENTER_LAT, "lng": area.CENTER_LNG},
    })


@app.post("/api/rider/message")
async def api_rider_message(request: Request):
    """고객 챗봇 대화. body={session, text}. LLM 호출이 있을 수 있어 스레드에서 실행."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    sid = str(body.get("session", "")).strip() or "anon"
    text = str(body.get("text", ""))
    res = await asyncio.to_thread(rider.message, sid, text)
    if res.get("ride"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/reset")
async def api_rider_reset(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    sid = str(body.get("session", "")).strip() or "anon"
    return rider.reset(sid)


@app.get("/api/rider/track")
async def api_rider_track(session: str = "anon"):
    """배차된 차량의 실시간 위치·상태·ETA(고객 앱 지도용)."""
    return rider.track(session)


@app.get("/api/rider/route_options")
async def api_rider_route_options(session: str = "anon"):
    """카카오모빌리티 실시간 경로 옵션(실시간 추천 vs 무료 우선)."""
    return rider.route_options(session)


@app.get("/api/rider/route_path")
async def api_rider_route_path(session: str = "anon"):
    """경로 카드 미니맵용."""
    return rider.route_path(session)


@app.get("/api/rider/places")
async def api_rider_places():
    """앱의 출발지/도착지 선택 목록."""
    return {"places": rider.places()}


@app.get("/api/rider/vehicles")
async def api_rider_vehicles():
    """고객 앱 '실시간 위치' 지도용."""
    out = []
    for v in fleet.vehicles.values():
        d = v.to_dict()
        out.append({
            "id": v.id, "plate": v.plate,
            "lat": d["lat"], "lng": d["lng"],
            "battery": round(v.battery),
            "status": v.status,
            "status_label": d["status_label"],
            "incident": bool(v.incident),
            "car_type": getattr(v, "car_type", "SEDAN"),
        })
    return {"vehicles": out}


@app.post("/api/rider/quote")
async def api_rider_quote(request: Request):
    """예상 요금·소요시간(결제 확인 화면). body={origin, destination, via?}."""
    body = await _json(request)
    return await asyncio.to_thread(
        rider.quote, str(body.get("origin", "")), str(body.get("destination", "")),
        str(body.get("via", "") or ""))


@app.post("/api/rider/book")
async def api_rider_book(request: Request):
    """앱에서 직접 배차(호출하기 버튼). body={session, origin, destination, via?, passengers?}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = await asyncio.to_thread(
        rider.book, sid, str(body.get("origin", "")), str(body.get("destination", "")),
        str(body.get("via", "") or ""), int(body.get("passengers", 1) or 1),
        str(body.get("payment_method", "신한 9441")), str(body.get("request", "")))
    if res.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.get("/api/rider/state")
async def api_rider_state(session: str = "anon"):
    return rider.state(session)


@app.post("/api/rider/reserve")
async def api_rider_reserve(request: Request):
    b = await _json(request)
    return await asyncio.to_thread(
        rider.reserve, str(b.get("session", "")).strip() or "anon",
        str(b.get("origin", "")), str(b.get("destination", "")), str(b.get("when", "")),
        str(b.get("via", "")), int(b.get("passengers", 1) or 1),
        str(b.get("payment_method", "신한 9441")), str(b.get("request", "")))


@app.post("/api/rider/reservation/cancel")
async def api_rider_reservation_cancel(request: Request):
    b = await _json(request)
    return rider.cancel_reservation(str(b.get("session", "")).strip() or "anon")


@app.post("/api/rider/route")
async def api_rider_select_route(request: Request):
    b = await _json(request)
    result = await asyncio.to_thread(rider.select_route,
        str(b.get("session", "")).strip() or "anon", str(b.get("kind", "recommend")))
    if result.get("ok"):
        await manager.broadcast(fleet.full_state())
    return result


@app.post("/api/rider/board")
async def api_rider_board(request: Request):
    """승차 완료 → 목적지로 출발. body={session}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = rider.board(sid)
    if res.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/request")
async def api_rider_request(request: Request):
    """탑승 중 요청(에어컨/음악 등)."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    text = str(body.get("text", ""))
    return await asyncio.to_thread(rider.ride_request, sid, text)


# 상담사 연결(고객 ↔ 관리자)
@app.post("/api/support/request")
async def api_support_request(request: Request):
    """고객이 '상담사연결'을 누름. body={session, plate?, dest?}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    return support.request(sid, {"plate": body.get("plate"), "dest": body.get("dest")})


@app.post("/api/support/send")
async def api_support_send(request: Request):
    """고객이 상담 메시지 전송. body={session, text}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    return support.user_send(sid, str(body.get("text", "")))


@app.get("/api/support/poll")
async def api_support_poll(session: str = "anon", after: float = 0.0):
    """고객 앱 폴링."""
    return support.user_poll(session, after)


@app.post("/api/support/close")
async def api_support_close(request: Request):
    body = await _json(request)
    return support.user_close(str(body.get("session", "")).strip() or "anon")


# 관리자(대시보드) 측
@app.get("/api/support/rooms")
async def api_support_rooms(request: Request):
    """대기·진행 중인 상담 목록."""
    require_user(request)
    return {"rooms": support.rooms_list()}


@app.get("/api/support/history")
async def api_support_history(request: Request, session: str = ""):
    require_user(request)
    return support.admin_history(session)


@app.post("/api/support/reply")
async def api_support_reply(request: Request):
    """상담사가 답장. body={session, text}."""
    user = require_user(request)
    body = await _json(request)
    sid = str(body.get("session", "")).strip()
    return support.admin_send(sid, str(body.get("text", "")), user.get("name", "관제사"))


@app.post("/api/support/accept")
async def api_support_accept(request: Request):
    user = require_user(request)
    body = await _json(request)
    return support.accept(str(body.get("session", "")).strip(), user.get("name", "관제사"))


@app.post("/api/support/end")
async def api_support_end(request: Request):
    require_user(request)
    body = await _json(request)
    return support.admin_close(str(body.get("session", "")).strip())


@app.get("/api/rider/last-trip")
async def api_rider_last_trip(session: str = "anon"):
    """최근 이용 기록(이용 후 문의용)."""
    return rider.last_trip(session)


@app.post("/api/rider/lost-item")
async def api_rider_lost_item(request: Request):
    """분실물 접수."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    item = str(body.get("item", "") or "분실물")
    res = await asyncio.to_thread(rider.report_lost_item, sid, item)
    if res.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/inquiry")
async def api_rider_inquiry(request: Request):
    """이용 후 문의(마이페이지) 자유 입력."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    text = str(body.get("text", ""))
    res = await asyncio.to_thread(rider.post_ride_inquiry, sid, text)
    if res.get("ok") and res.get("mode") == "deliver":
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/passengers")
async def api_rider_passengers(request: Request):
    """탑승 인원 변경."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = await asyncio.to_thread(rider.change_passengers, sid, int(body.get("passengers", 1) or 1))
    if res.get("ok") and res.get("changed"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/injury")
async def api_rider_injury(request: Request):
    """부상 신고 → 119 구급차 출동. body={session}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = rider.report_injury(sid)
    if res.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/reassign")
async def api_rider_reassign(request: Request):
    """사고 후 차량 재배치. body={session}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = await asyncio.to_thread(rider.reassign, sid)
    if res.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return res


@app.post("/api/rider/complete")
async def api_rider_complete(request: Request):
    """운행 종료 → 이용 상세(영수증). body={session, free?}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = rider.complete(sid, bool(body.get("free", False)))
    try:
        await manager.broadcast(fleet.full_state())
    except Exception:
        pass
    return res


@app.post("/api/rider/cancel")
async def api_rider_cancel(request: Request):
    """호출 취소. body={session}."""
    body = await _json(request)
    sid = str(body.get("session", "")).strip() or "anon"
    res = rider.cancel(sid)
    try:
        await manager.broadcast(fleet.full_state())
    except Exception:
        pass
    return res


@app.get("/api/ops")
async def api_ops(request: Request):
    """자율 관제 엔진 결과(브리핑·권장조치·이상징후·사전배치·구역 수급)."""
    require_user(request)
    return ops.state


@app.get("/mapcheck", response_class=HTMLResponse)
async def mapcheck_page(request: Request):
    """카카오맵이 안 뜨는 원인을 자동 진단하는 페이지(로그인 불필요)."""
    return templates.TemplateResponse(request, "mapcheck.html", {
        "request": request,
        "kakao_js_key": settings.kakao_js_key,
    })


@app.get("/console", response_class=HTMLResponse)
async def console_page():
    """독립 '이벤트 관제 콘솔'."""
    return FileResponse(str(BASE_DIR / "static" / "console.html"))


# WebSocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # 세션 쿠키로 인증
    if not ws.session.get("user_id"):
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        # 접속 즉시 현재 상태 1회 전송
        await ws.send_text(json.dumps(fleet.full_state(), ensure_ascii=False))
        while True:
            # 클라이언트 메시지는 keep-alive 용도로만 받음
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# API
@app.get("/api/config")
async def api_config(request: Request):
    require_user(request)
    return {"llm_enabled": settings.llm_enabled, "model": settings.llm_model, "provider": settings.llm_name}


@app.get("/api/state")
async def api_state(request: Request):
    require_user(request)
    return fleet.full_state()


@app.get("/api/places")
async def api_places(request: Request):
    """택시 호출 화면의 출발지/목적지 선택용."""
    require_user(request)
    return {
        "area": area.AREA_NAME,
        "center": {"lat": area.CENTER_LAT, "lng": area.CENTER_LNG},
        "places": [{"name": n, "lat": la, "lng": lo} for (n, la, lo) in area.ANCHORS],
        "depots": [{"name": n, "lat": la, "lng": lo} for (n, la, lo) in area.DEPOTS],
    }


@app.get("/api/trips")
async def api_trips(request: Request):
    """금일 이용내역(운행 기록) + 요약 통계."""
    require_user(request)
    return fleet.trips_today()


@app.get("/api/vehicle/{vehicle_id}")
async def api_vehicle(request: Request, vehicle_id: str):
    require_user(request)
    v = fleet.get(vehicle_id)
    if not v:
        raise HTTPException(status_code=404, detail="차량을 찾을 수 없습니다.")
    agent_log = await asyncio.to_thread(build_agent_log, vehicle_id)
    return {
        "vehicle": v.to_dict(),
        "agent_log": agent_log,
        "agent_history": fleet.agent_events.get(vehicle_id, []),
        "history": _trip_history(v),
    }


@app.post("/api/llm/assign")
async def api_assign(request: Request):
    require_user(request)
    body = await request.json()
    origin = body.get("origin")
    destination = body.get("destination")
    if not origin or not destination:
        raise HTTPException(status_code=400, detail="origin/destination 필요")
    return await asyncio.to_thread(assignment.assign, origin, destination, body.get("demand", "보통"))


@app.post("/api/llm/route")
async def api_route(request: Request):
    require_user(request)
    body = await request.json()
    origin = body.get("origin")
    destination = body.get("destination")
    if not origin or not destination:
        raise HTTPException(status_code=400, detail="origin/destination 필요")
    return await asyncio.to_thread(route.find_route, origin, destination, body.get("traffic", "보통"))


@app.post("/api/llm/risk/{vehicle_id}")
async def api_risk(request: Request, vehicle_id: str):
    require_user(request)
    v = fleet.get(vehicle_id)
    if not v:
        raise HTTPException(status_code=404, detail="차량을 찾을 수 없습니다.")
    return await asyncio.to_thread(risk.analyze, v)


@app.post("/api/simulate/call")
async def api_simulate_call(request: Request):
    """택시 호출 → 배정+경로 모델 실행 후, 실제 차량을 배차해 출발시킨다."""
    require_user(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    manual = bool(body.get("origin") and body.get("destination"))
    if manual:
        origin, destination = body["origin"], body["destination"]
    else:
        a, b = random.sample(area.ANCHORS, 2)
        origin = {"name": a[0], "lat": a[1], "lng": a[2]}
        destination = {"name": b[0], "lat": b[1], "lng": b[2]}

    assign_res = await asyncio.to_thread(
        assignment.assign, origin, destination, body.get("demand", "보통"))
    # 외부 API 호출은 스레드에서
    route_res = await asyncio.to_thread(
        route.find_route, origin, destination, body.get("traffic", "보통"), True
    )

    dispatch = None
    if assign_res.get("assigned"):
        dispatch = fleet.dispatch_manual(
            origin, destination, assign_res, route_res,
            source="수동" if manual else "자동",
        )
    return {"origin": origin, "destination": destination,
            "assignment": assign_res, "route": route_res,
            "dispatch": dispatch, "manual": manual}


@app.get("/api/console/vehicles")
async def api_console_vehicles():
    """이벤트 콘솔용 차량 목록 + 현재 가상 시각(공개."""
    return {"vehicles": fleet.vehicle_brief(), "clock": fleet.clock_state()}


@app.post("/api/console/time")
async def api_console_time(request: Request):
    """콘솔 시간 제어: 시간 조정/배속/실시간. body={action, value?}."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    action = str(body.get("action", "")).strip()
    value = body.get("value")
    clock = fleet.time_control(action, value)
    try:
        await manager.broadcast(fleet.full_state())
    except Exception:
        pass
    return {"ok": True, "clock": clock}


@app.post("/api/console/surge")
async def api_console_surge(request: Request):
    """[관제 버튼] 특정 지점으로 대기 택시 집중 배치. body={point}. 대수는 현재 수요·시간대 근거로 자동 산정."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    point = str(body.get("point", "정왕역")).strip() or "정왕역"
    result = fleet.surge_to_point(point)
    # 즉시 반영 상태를 대시보드로 푸시
    if result.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return result


@app.post("/api/console/event")
async def api_console_event(request: Request):
    """관제 콘솔 이벤트 발생: 사고/고장/충전/방전. body={kind, vehicle_id?}."""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    kind = str(body.get("kind", "")).strip()
    vehicle_id = body.get("vehicle_id") or None
    result = fleet.event_apply(kind, vehicle_id)
    # 즉시 반영된 상태를 모든 대시보드로 푸시
    if result.get("ok"):
        try:
            await manager.broadcast(fleet.full_state())
        except Exception:
            pass
    return result


# 에이전트 진행 로그 빌더
def _trip_history(v) -> list[dict]:
    """차량 운행 기록(데모용 합성). 실제로는 DB에서 조회."""
    rng = random.Random(v.id)
    rows = []
    for i in range(6):
        a, b = rng.sample(area.ANCHORS, 2)
        dist = _haversine_km(a[1], a[2], b[1], b[2])
        rows.append(
            {
                "time": f"{rng.randint(8,21):02d}:{rng.randint(0,59):02d}",
                "route": f"{a[0]} → {b[0]}",
                "fare": estimate_fare(dist),
                "rating": round(rng.uniform(4.0, 5.0), 1),
            }
        )
    return rows


def build_agent_log(vehicle_id: str) -> list[dict]:
    """모니터 에이전트 로그. 즉시 반환하고 GPT 보강은 백그라운드."""
    v = fleet.get(vehicle_id)
    if not v:
        return []
    cache_key = (v.status, bool(v.incident), v.passenger or "")
    cached = _AGENT_LOG_CACHE.get(vehicle_id)
    if cached:
        if cached[0] == cache_key and (time.time() - cached[1] < _AGENT_LOG_TTL):
            return cached[2]  # 최신 캐시 → 즉시 반환
        _schedule_agent_refresh(vehicle_id)  # 상태변경/만료 → 백그라운드 GPT 갱신
        return cached[2]  # 이전 로그라도 즉시 반환(대기 없음)
    # 첫 조회는 즉시 생성, GPT 갱신은 백그라운드
    logs = _build_agent_log_core(v, use_llm=False)
    _AGENT_LOG_CACHE[vehicle_id] = (cache_key, time.time(), logs)
    _schedule_agent_refresh(vehicle_id)
    return logs


def _schedule_agent_refresh(vehicle_id: str) -> None:
    """백그라운드 스레드로 GPT 보강 로그를 계산해 캐시에 반영(차량별 중복 실행 방지)."""
    with _AGENT_REFRESH_LOCK:
        if vehicle_id in _AGENT_REFRESHING:
            return
        _AGENT_REFRESHING.add(vehicle_id)

    def _work():
        try:
            v = fleet.get(vehicle_id)
            if v:
                key = (v.status, bool(v.incident), v.passenger or "")
                logs = _build_agent_log_core(v, use_llm=True)
                _AGENT_LOG_CACHE[vehicle_id] = (key, time.time(), logs)
        except Exception:
            pass
        finally:
            with _AGENT_REFRESH_LOCK:
                _AGENT_REFRESHING.discard(vehicle_id)

    threading.Thread(target=_work, daemon=True).start()


def _build_agent_log_core(v, use_llm: bool = True) -> list[dict]:
    """7개 에이전트 진행 로그를 구성한다."""
    vd = v.to_dict()
    risk_res = risk.analyze(v, use_llm=use_llm)

    # 현재 운행/이동의 경로 모델 호출 (목적지가 있으면)
    route_res = None
    if v.dest_lat is not None:
        route_res = route.find_route(
            {"name": "현위치", "lat": v.lat, "lng": v.lng},
            {"name": v.dest_name or "목적지", "lat": v.dest_lat, "lng": v.dest_lng},
            traffic=random.choice(["원활", "보통", "혼잡"]),
            use_llm=use_llm,
        )

    counts = fleet.status_counts()

    def step(label, text):
        return {"label": label, "text": text}

    logs = []

    # 1) 재배치 에이전트
    logs.append({
        "key": "rebalance",
        "name": "재배치 에이전트",
        "state": "active" if v.status == STATUS_WAITING else "idle",
        "steps": [
            step("입력", "시간·지역별 수요 예측 및 공차 분포"),
            step("판단", "수요 집중 지역 대비 현재 위치의 공차 리스크 평가"),
            step("실행", f"대기 차량 {counts[STATUS_WAITING]}대 기준 사전 이동/대기 지점 최적화"),
            step("결과", "공차 주행 최소화를 위한 권장 대기 지점 산출"),
        ],
    })

    # 2) 배차 에이전트
    if v.passenger:
        assign_steps = [
            step("입력", f"호출 매칭 — {v.passenger}"),
            step("판단", "ETA·배터리·청결·운영 우선순위 종합 점수화"),
            step("실행", f"{v.plate} 배정 · 현재 속도 {int(v.speed)}km/h"),
            step("결과", (f"ETA {route_res['eta_min']}분, 거리 {route_res['distance_km']}km"
                          if route_res else "운행 중")),
        ]
        state = "done"
    else:
        assign_steps = [
            step("입력", "신규 호출 대기"),
            step("판단", "가용 차량 후보군 모니터링"),
            step("실행", "현재 배정된 호출 없음"),
            step("결과", "대기 상태 유지"),
        ]
        state = "idle"
    logs.append({"key": "dispatch", "name": "배차 에이전트", "state": state, "steps": assign_steps})

    # 3) 인시던트 대응 에이전트
    if v.incident:
        label = "사고" if v.incident["type"] == "ACCIDENT" else "방전/고장"
        if v.incident["type"] == "ACCIDENT":
            # 접촉 사고 대응 절차 생성
            resp = incident.respond(v, use_llm=use_llm)
            inc_steps = [step(s["label"], s["text"]) for s in resp["steps"]]
        else:
            inc_steps = [
                step("입력", f"이벤트 감지 — {label}"),
                step("판단", f"위험 등급 '{risk_res['grade']}' · 점수 {risk_res['score']}"),
                step("실행", "안전 정차 + 견인/충전 차량 요청 시퀀스 개시"),
                step("결과", v.incident["desc"] + " · 타임라인 자동 기록"),
            ]
        logs.append({
            "key": "incident",
            "name": "인시던트 대응 에이전트",
            "state": "alert",
            "steps": inc_steps,
        })
    else:
        logs.append({
            "key": "incident",
            "name": "인시던트 대응 에이전트",
            "state": "idle",
            "steps": [
                step("입력", "사고·고장 이벤트 모니터링"),
                step("판단", "이상 이벤트 없음"),
                step("실행", "대응 대기"),
                step("결과", "정상 운행 중"),
            ],
        })

    # 4) 통합 관제 에이전트
    logs.append({
        "key": "control",
        "name": "통합 관제 에이전트",
        "state": "active",
        "steps": [
            step("입력", "실시간 위치/상태/배터리 스트리밍"),
            step("판단", f"상태 '{vd['status_label']}' · 배터리 {vd['battery']}%"),
            step("실행", f"지도 기반 모니터링 · 위치 ({v.lat:.4f}, {v.lng:.4f})"),
            step("결과", f"운행 {counts[STATUS_DRIVING]} / 대기 {counts[STATUS_WAITING]} / "
                         f"충전 {counts[STATUS_CHARGING]} / 점검 {counts[STATUS_MAINTENANCE]}"),
        ],
    })

    # 5) 정비 인텔리전스 에이전트
    logs.append({
        "key": "maint_intel",
        "name": "정비 인텔리전스 에이전트",
        "state": "alert" if risk_res["score"] >= 45 else "active",
        "steps": [
            step("입력", "센서·주행 데이터 (온도/진동/배터리)"),
            step("판단", f"위험 점수 {risk_res['score']} · 등급 {risk_res['grade']}"),
            step("실행", "이상 탐지 및 고장 리스크·잔여수명 추정"),
            step("결과", risk_res["recommendation"]),
        ],
    })

    # 6) 정비 오퍼레이션 에이전트
    if v.status == STATUS_MAINTENANCE or risk_res["immediate_action"]:
        logs.append({
            "key": "maint_ops",
            "name": "정비 오퍼레이션 에이전트",
            "state": "active",
            "steps": [
                step("입력", "정비 필요 판단 결과 수신"),
                step("판단", "정비소·일정·부품 가용성 확인"),
                step("실행", "워크오더 발행 및 작업 배정"),
                step("결과", "완료 검증 후 차량 복귀 절차 관리"),
            ],
        })
    else:
        logs.append({
            "key": "maint_ops",
            "name": "정비 오퍼레이션 에이전트",
            "state": "idle",
            "steps": [
                step("입력", "워크오더 대기"),
                step("판단", "정비 필요 항목 없음"),
                step("실행", "예정 작업 없음"),
                step("결과", "정상 가동"),
            ],
        })

    # 7) 플릿 오케스트레이터 에이전트
    #    우선순위: 인시던트 대응 → 정비 → 기존 배차 유지 → 충전 → 재배치
    if v.incident:
        priority = "인시던트 대응 최우선 — 안전조치/견인 배차 우선 실행"
    elif risk_res["immediate_action"]:
        priority = "정비 전환 권고 — 관제 확인 후 정비 오퍼레이션 실행"
    elif v.passenger:
        priority = "현재 배차 유지 — 경로 모델 ETA 기준 정상 진행"
        if v.battery_state == "DANGER":
            priority += " · 하차 후 충전소 이동"
    elif v.status == STATUS_CHARGING:
        priority = "충전 진행 — 완충 후 운행 복귀"
    elif v.battery_state == "DANGER":
        priority = "충전 우선 — 인근 충전소 이동"
    else:
        priority = "수요 예측 기반 재배치 권고 — 공차 최소화"
        if v.battery_state == "CAUTION":
            priority += " · 배터리 주의"
    logs.append({
        "key": "orchestrator",
        "name": "플릿 오케스트레이터 에이전트",
        "state": "active",
        "steps": [
            step("입력", "전 에이전트 판단 결과 수집"),
            step("판단", "정책 기반 우선순위 적용 및 충돌 해소"),
            step("실행", priority),
            step("결과", f"엔진: {llmsvc.engine_label(risk_res['engine'])} · 최종 실행안 확정"),
        ],
    })

    return logs
