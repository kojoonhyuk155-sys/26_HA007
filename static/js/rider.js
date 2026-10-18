/* ROBITA 고객 앱 — 서버(FastAPI) 연동 전체 흐름
   홈 → 호출 → 결제 → 배정(실시간 추적) → 탑승 AI 채팅(경로선택/사고/재배차/보상/종료) → 영수증
   + 예약 / 알림 / 마이페이지 / 고객지원 채팅                                            */

const $ = (id) => document.getElementById(id);
const HOME_PLACE = "한국공학대학교";                  // 앱 기본 출발지
const CENTER = window.RIDER_CONFIG?.center || { lat: 37.340174, lng: 126.733593 };  // 한국공학대 좌표(기본 지도 중심)

/* 세션 */
let sid = localStorage.getItem("rider_sid");
if (!sid) { sid = (crypto.randomUUID ? crypto.randomUUID() : "r" + Date.now()); localStorage.setItem("rider_sid", sid); }

/* 상태 */
const S = {
  start: "", end: "", via: "", pax: 1, method: "신한 9441",
  request: "", reserveAt: "", places: [],
  fare: 0, etaMin: 0, plate: "", started: false, rideActive: false,
  reserved: false, chatMode: "ride",      // ride | support
  notis: [], trips: [],
};

/* ───────────────────────── 유틸 */
const api = {
  async get(u) { const r = await fetch(u); if (!r.ok) throw new Error("서버 연결 오류 (" + r.status + ")"); return r.json(); },
  async post(u, b) {
    const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {}) });
    if (!r.ok) throw new Error("서버 연결 오류 (" + r.status + ")"); return r.json();
  },
};
const won = (n) => Number(n || 0).toLocaleString("ko-KR") + "원";
const nowHM = () => new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });

function toast(msg, ms = 1800) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function addNoti(text, warn) {
  S.notis.unshift({ text, time: nowHM(), warn: !!warn });
  renderNotis(); saveLocal();
}
function renderNotis() {
  const box = $("noti-list");
  if (!S.notis.length) { box.innerHTML = '<div class="empty">새 알림이 없습니다.</div>'; return; }
  box.innerHTML = S.notis.map((n) =>
    `<div class="noti ${n.warn ? "warn" : ""}">${n.text}<div class="nt">${n.time}</div></div>`).join("");
}
function renderTrips() {
  const box = $("my-trips");
  if (!S.trips.length) { box.innerHTML = '<div class="empty">이용 내역이 없습니다.</div>'; return; }
  box.innerHTML = S.trips.map((t) =>
    `<div class="noti">${t.origin} → ${t.dest} · ${t.fare === 0 ? "무료" : won(t.fare)}
      <div class="nt">${t.date} ${t.time_range}</div></div>`).join("");
}

/* ───────────────────────── 화면 전환 */
const SCREENS = ["splash", "home", "call", "pay", "assign", "chat", "reserve", "rdone", "noti", "my"];
function show(name) {
  SCREENS.forEach((s) => { const el = $("sc-" + s); if (el) el.hidden = s !== name; });
  $("tabbar").hidden = (name === "splash");
  const map = { home: "home", noti: "noti", my: "my" };
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", map[name] === t.dataset.tab));
  if (name === "home") setTimeout(() => { relayout("home-map"); refreshFleet(); }, 60);
  if (name === "call") setTimeout(() => { relayout("call-map"); refreshFleet(); }, 60);
  if (name === "assign") setTimeout(() => relayout("assign-map"), 60);
  if (name === "chat" && !$("ride-map-wrap").hidden) setTimeout(() => relayout("chat-map"), 80);
  updateRideBanner();
}
document.querySelectorAll("[data-back]").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.back)));
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => show(t.dataset.tab)));

/* 이용 중인 택시 복귀 배너 — 운행/배차가 진행 중인데 다른 화면(홈 등)으로 나가면
   탭바 위에 배너를 띄우고, 누르면 이용 중인 택시 화면(운행 중이면 AI 채팅, 아니면 추적)으로 돌아간다. */
function updateRideBanner() {
  const el = $("ride-return");
  if (!el) return;
  const onRideScreen = !$("sc-chat").hidden || !$("sc-assign").hidden;
  el.hidden = !(S.rideActive && !onRideScreen);
}
$("ride-return").addEventListener("click", () => {
  show(S.started ? "chat" : "assign");
});

/* ───────────────────────── 팝업 */
function popup(title, items, onPick) {
  $("pop-title").textContent = title;
  $("pop-list").innerHTML = items.map((i) => `<button class="pop-item">${i}</button>`).join("");
  $("pop-list").querySelectorAll(".pop-item").forEach((b, i) =>
    b.addEventListener("click", () => { closePop(); onPick(items[i], i); }));
  $("dim").hidden = false; $("pop").hidden = false;
}
function closePop() { $("dim").hidden = true; $("pop").hidden = true; }
$("pop-close").addEventListener("click", closePop);
$("dim").addEventListener("click", closePop);

function setField(el, text) {
  let span = el.querySelector("span");
  if (!span) { span = document.createElement("span"); el.replaceChildren(span); }
  span.textContent = text;
  el.classList.add("filled");
}

/* ───────────────────────── 지도 */
const maps = {};
const MAP_IDS = ["home-map", "call-map", "assign-map", "chat-map"];
const fleetOv = {}, rideOv = {};
let kakaoReady = false, lastFleet = [], lastRide = null, fleetBusy = false;
const vehicleColors = {PICKUP:"#14b8a6",DRIVING:"#36b37e",WAITING:"#2f6fed",CHARGING:"#f5b301",MAINTENANCE:"#e5484d"};
function visible(id) { const el=$(id); return el && el.offsetWidth>0 && el.offsetHeight>0; }
function initMap(id) {
  if (!kakaoReady || maps[id] || !visible(id)) return;
  const el=$(id); el.classList.remove("fail"); el.replaceChildren();
  maps[id]=new kakao.maps.Map(el,{center:new kakao.maps.LatLng(CENTER.lat,CENTER.lng),level:5});
  kakao.maps.event.addListener(maps[id],"tilesloaded",()=>el.dataset.tiles="loaded");
  if (id==="home-map" || id==="call-map") drawFleet(id,lastFleet);
  else if(lastRide) drawRideOn(id,lastRide);
}
function relayout(id) {
  if (!visible(id) || !kakaoReady) return;
  initMap(id);
  const m=maps[id], c=m.getCenter(); m.relayout(); m.setCenter(c);
  if ((id==="assign-map" || id==="chat-map") && lastRide) drawRideOn(id,lastRide);
}
const mapSizes=new ResizeObserver(entries=>entries.forEach(e=>{
  if(e.contentRect.width && e.contentRect.height) requestAnimationFrame(()=>relayout(e.target.id));
}));
MAP_IDS.forEach(id=>mapSizes.observe($(id)));
function waitForKakao(tries=0) {
  if(kakaoReady) return;
  if(window.kakao?.maps && typeof kakao.maps.load==="function") {
    const timer=setTimeout(()=>{if(!kakaoReady) showMapError("timeout");},8000);
    kakao.maps.load(()=>{
      clearTimeout(timer); kakaoReady=true;
      MAP_IDS.forEach(id=>{if(visible(id)) relayout(id);});
      refreshFleet(); if(lastRide) drawRide(lastRide);
    });
    return;
  }
  if(window.__KAKAO_SDK__==="error" || tries>40){showMapError(window.__KAKAO_SDK__==="error"?"sdk":"timeout");return;}
  setTimeout(()=>waitForKakao(tries+1),200);
}
function showMapError(why) {
  const msg=why==="sdk"?"지도를 불러오지 못했어요. 인터넷 연결을 확인해 주세요.":
    "지도 연결을 확인해 주세요. 카카오 지도 웹 도메인 등록 주소: "+location.origin;
  MAP_IDS.forEach(id=>{
    if(maps[id]) return;
    const el=$(id);el.classList.add("fail");
    const text=document.createElement("span");text.textContent=msg;
    const retry=document.createElement("button");retry.textContent="다시 연결";retry.onclick=()=>location.reload();
    el.replaceChildren(text,retry);
  });
}
function fleetPin(v) {
  const el=document.createElement("div");el.className="fleet-marker";
  el.style.background=vehicleColors[v.status]||"#2f6fed";
  el.title=`${v.plate} · ${v.status_label} · 배터리 ${v.battery}%`;
  const suv=v.car_type==="SUV", suffix=v.battery<=30?"_red":v.battery<=50?"_yellow":"";
  const img=document.createElement("img");img.src=`/static/img/${suv?"suv":"car"}${suffix}.png`;img.alt=el.title;
  el.appendChild(img);return el;
}
function drawFleet(id,vehicles) {
  const m=maps[id];if(!m)return;
  const box=fleetOv[id]||(fleetOv[id]={}),seen=new Set();
  vehicles.forEach(v=>{
    seen.add(String(v.id));const pos=new kakao.maps.LatLng(v.lat,v.lng);
    if(!box[v.id]){box[v.id]=new kakao.maps.CustomOverlay({position:pos,content:fleetPin(v),zIndex:4});box[v.id].setMap(m);}
    else{box[v.id].setContent(fleetPin(v));box[v.id].setPosition(pos);}
  });
  Object.keys(box).forEach(id=>{if(!seen.has(id)){box[id].setMap(null);delete box[id];}});
}
async function refreshFleet() {
  if(fleetBusy) return; fleetBusy=true;
  try {
    const d=await api.get("/api/rider/vehicles");lastFleet=d.vehicles||[];
    $("fleet-driving").textContent=lastFleet.filter(v=>["PICKUP","DRIVING"].includes(v.status)).length;
    $("fleet-waiting").textContent=lastFleet.filter(v=>v.status==="WAITING").length;
    $("fleet-total").textContent=lastFleet.length;
    $("fleet-sync").textContent="관제 연동 · "+nowHM()+" 갱신";
    if(kakaoReady) ["home-map","call-map"].forEach(id=>{if(visible(id)){initMap(id);drawFleet(id,lastFleet);}});
  } catch(e) { $("fleet-sync").textContent="차량 연결 재시도 중…"; }
  finally {fleetBusy=false;}
}
function pin(txt,color) {
 const d=document.createElement("div");d.style.cssText=`background:${color};color:#fff;font-size:11px;font-weight:700;padding:4px 8px;border-radius:7px;white-space:nowrap`;
 d.textContent=txt;return d;
}
function carEl(suv) {
 const d=document.createElement("div");d.style.cssText="width:34px;height:34px;border:2px solid #2f6fed;background:white;border-radius:50%;display:grid;place-items:center;box-shadow:0 2px 6px #0003";
 const img=document.createElement("img");img.src=suv?"/static/img/suv.png":"/static/img/car.png";img.style.width="26px";img.alt="배정 차량";d.appendChild(img);return d;
}
function drawRideOn(mapId,tr) {
 if(!kakaoReady || !visible(mapId))return;
 if(!maps[mapId]){initMap(mapId);return;}
 const m=maps[mapId],o=rideOv[mapId]||(rideOv[mapId]={}),p=new kakao.maps.LatLng(tr.vehicle.lat,tr.vehicle.lng);
 const suv=tr.vehicle.car_type==="SUV";
 if(!o.car){o.car=new kakao.maps.CustomOverlay({zIndex:10,content:carEl(suv)});o.car.setMap(m);}
 if(o.plate!==tr.vehicle.plate){o.car.setContent(carEl(suv));o.plate=tr.vehicle.plate;}
 o.car.setPosition(p);
 for(const [key,point,label,color] of [["pick",tr.pickup,"출발","#36b37e"],["dest",tr.dest,"도착","#e5484d"]]){
  if(!o[key]){o[key]=new kakao.maps.CustomOverlay({zIndex:6,content:pin(label,color)});o[key].setMap(m);}
  o[key].setPosition(new kakao.maps.LatLng(point.lat,point.lng));
 }
 const r=tr.vehicle.route||[];
 if(r.length>=2){
  if(!o.line)o.line=new kakao.maps.Polyline({strokeWeight:5,strokeColor:"#2f6fed",strokeOpacity:.9});
  o.line.setPath(r.map(q=>new kakao.maps.LatLng(q.lat,q.lng)));o.line.setMap(m);
 }else if(o.line)o.line.setMap(null);
 m.setCenter(p);
}
function drawRide(tr) {
 lastRide=tr;
 ["assign-map","chat-map"].forEach(id=>{if(visible(id))drawRideOn(id,tr);});
 $("ride-status").textContent=(tr.phase_label||"운행 중")+(tr.eta_min?` · 약 ${tr.eta_min}분`:"");
}
function clearRide() {
 lastRide=null;
 Object.keys(rideOv).forEach(k=>{Object.values(rideOv[k]).forEach(o=>{if(o?.setMap)o.setMap(null);});delete rideOv[k];});
}
document.querySelectorAll("[data-center]").forEach(b=>b.onclick=()=>{
 const m=maps[b.dataset.center],c=S.placeCoords?.[S.start]||CENTER;
 if(m)m.setCenter(new kakao.maps.LatLng(c.lat,c.lng));
});

/* ───────────────────────── 장소/인원/결제수단 선택 */
async function loadPlaces() {
  const d = await api.get("/api/rider/places");
  S.places = (d.places || []).map((p) => p.name);
  S.placeCoords = {};
  (d.places || []).forEach((p) => { S.placeCoords[p.name] = { lat: p.lat, lng: p.lng }; });
  // 출발지는 항상 한국공학대로 시작(목록에 없으면 첫 번째 지점)
  if (S.places.length && !S.start) {
    S.start = S.places.includes(HOME_PLACE) ? HOME_PLACE : S.places[0];
    [$("h-start"), $("c-start"), $("r-start")].forEach((e) => e && setField(e, S.start));
    const c = S.placeCoords[S.start];
    if (c) { CENTER.lat = c.lat; CENTER.lng = c.lng; }   // 지도 중심을 출발지로
  }
}
function pickPlace(kind) {
  popup(kind === "end" ? "도착지 선택" : kind === "via" ? "경유지 선택" : "출발지 선택",
    kind === "via" ? ["경유지 없음", ...S.places] : S.places, (name) => {
      if (name === "경유지 없음") name = "";
      if (kind === "start") { S.start = name; [$("h-start"), $("c-start"), $("r-start")].forEach((e) => setField(e, name)); }
      if (kind === "end") { S.end = name; [$("h-end"), $("c-end"), $("r-end")].forEach((e) => setField(e, name)); }
      if (kind === "via") { S.via = name; [$("c-via"), $("r-via")].forEach((e) => setField(e, name || "경유지")); }
      if (kind === "start" && S.placeCoords[name]) { Object.assign(CENTER, S.placeCoords[name]); ["home-map","call-map"].forEach(id=>{ if(maps[id]) maps[id].setCenter(new kakao.maps.LatLng(CENTER.lat,CENTER.lng)); }); }
      refreshQuote();
    });
}
["h-start", "c-start", "r-start"].forEach((i) => $(i).addEventListener("click", () => pickPlace("start")));
["h-end", "c-end", "r-end"].forEach((i) => $(i).addEventListener("click", () => pickPlace("end")));
["c-via", "r-via"].forEach((i) => $(i).addEventListener("click", () => pickPlace("via")));

const PAX = ["1명", "2명", "3명", "4명", "5명", "6명"];
["c-pax", "r-pax"].forEach((i) => $(i).addEventListener("click", () =>
  popup("탑승 인원 선택", PAX, (v) => {
    S.pax = parseInt(v);
    [$("c-pax"), $("r-pax")].forEach((e) => setField(e, v));
    $("p-pax").querySelector("span").textContent = v;
  })));

const METHODS = ["신한 9441", "국민 3021", "카카오페이", "토스페이"];
["p-method", "r-method"].forEach((i) => $(i).addEventListener("click", () =>
  popup("결제 수단 선택", METHODS, (v) => {
    S.method = v; saveLocal();
    [$("p-method"), $("r-method")].forEach((e) => setField(e, v));
    [$("a-method"), $("rd-method")].forEach((e) => e && (e.textContent = v));
  })));
$("my-pay").addEventListener("click", () =>
  popup("결제 수단 관리", METHODS, (v) => { S.method = v; [$("p-method"), $("r-method")].forEach(e=>setField(e,v)); [$("a-method"),$("rd-method")].forEach(e=>e.textContent=v); toast(`기본 결제 수단이 ${v} 으로 변경되었습니다.`); saveLocal(); }));

/* 예약 시간 */
const SLOTS = [], SLOT_DATES = [];
(function buildSlots() {
  const d = new Date();
  for (let i = 1; i <= 6; i++) {
    const t = new Date(d.getTime() + i * 3600 * 1000);
    t.setMinutes(0,0,0); SLOT_DATES.push(t.toISOString());
    SLOTS.push(`${String(t.getMonth() + 1).padStart(2, "0")}/${String(t.getDate()).padStart(2, "0")} ${String(t.getHours()).padStart(2, "0")}:00`);
  }
})();
$("r-when").addEventListener("click", () =>
  popup("예약 날짜/시간 선택", SLOTS, (v, index) => { S.reserveAt = v; S.reserveISO=SLOT_DATES[index]; setField($("r-when"), v); }));
["h-time", "c-time"].forEach((i) => $(i).addEventListener("click", () =>
  popup("출발 시간", ["지금", "예약 시간 선택"], (v) => {
    if(v === "예약 시간 선택") { show("reserve"); $("r-when").click(); return; }
    [$("h-time"), $("c-time")].forEach((e) => setField(e, v));
    $("p-time").querySelector("span").textContent = v;
  })));

/* ───────────────────────── 요금 견적 */
async function refreshQuote() {
  if (!S.start || !S.end) return;
  const q = await api.post("/api/rider/quote", { origin: S.start, destination: S.end, via: S.via });
  if (!q.ok) { toast(q.msg || "요금 조회 실패"); return; }
  S.fare = q.fare; S.etaMin = q.eta_min;
  $("p-eta").textContent = q.eta_min + "분";
  $("p-fare").textContent = won(q.fare);
  $("r-eta").textContent = q.eta_min + "분";
  $("r-fare").textContent = won(q.fare);
}

/* ───────────────────────── 홈 → 호출 / 예약 */
$("go-call").addEventListener("click", () => {
  if (!S.end) { toast("도착지를 선택해 주세요."); return; }
  show("call"); refreshQuote();
});
$("go-reserve").addEventListener("click", () => { show(S.reserved ? "rdone" : "reserve"); refreshQuote(); });

$("c-next").addEventListener("click", () => {
  if (!S.start || !S.end) { toast("출발지와 도착지를 선택해 주세요."); return; }
  $("p-start").querySelector("span").textContent = S.start;
  $("p-end").querySelector("span").textContent = S.end;
  if (S.via) { $("p-via").hidden = false; $("p-via").querySelector("span").textContent = S.via; }
  $("p-pax").querySelector("span").textContent = S.pax + "명";
  $("p-method").querySelector("span").textContent = S.method;
  show("pay"); refreshQuote();
});

/* ───────────────────────── 결제 → 실제 배차 */
$("p-go").addEventListener("click", async () => {
  S.request = $("p-req").value.trim();
  $("p-go").disabled = true;
  toast("결제수단 확인 · 차량을 배정하고 있어요");
  show("assign");
  $("a-title").textContent = "차량 배정 중…";
  $("a-sub").textContent = "가까운 로보택시를 찾고 있어요";
  $("a-badge").textContent = "배정중"; $("a-badge").classList.remove("on");
  $("a-from").textContent = S.start; $("a-to").textContent = S.end;
  $("a-fare").textContent = won(S.fare);
  $("a-method").textContent = S.method;

  let b;
  try { b = await api.post("/api/rider/book", {
    session: sid, origin: S.start, destination: S.end, via: S.via, passengers: S.pax, payment_method: S.method, request: S.request,
  });
  } catch(e) { toast("배차 연결을 확인해 주세요."); show("pay"); return; }
  finally { $("p-go").disabled = false; }
  if (!b.ok) { toast(b.msg || "배차 가능한 차량이 없습니다."); show("pay"); return; }
  S.rideActive = true;
  S.plate = b.plate; S.fare = b.fare || S.fare; S.carType = b.car_type || "SEDAN";
  $("a-plate").textContent = b.plate;
  $("a-badge").textContent = "배정완료"; $("a-badge").classList.add("on");
  $("a-title").textContent = "차량이 곧 도착합니다";
  $("a-sub").textContent = "픽업을 준비하세요";
  $("a-fare").textContent = won(S.fare);
  // 대형(SUV) 배차 안내
  const isSuv = S.carType === "SUV";
  document.querySelector("#sc-assign .veh-desc").textContent =
    isSuv ? `ROBITA 대형택시(SUV · ${b.seats || 6}인승) · 완전 자율주행`
          : "ROBITA 차량 · 완전 자율주행";
  const aImg = document.querySelector("#sc-assign .veh-img");
  if (aImg) { aImg.src = isSuv ? "/static/img/suv.png" : "/static/img/car.png";
              aImg.classList.toggle("suv", isSuv); }
  if (isSuv) toast(`${S.pax}명 탑승 — 대형(SUV) 택시가 배차되었습니다.`);
  addNoti(`${b.plate}${isSuv ? " (SUV)" : ""} 배차 완료 · 약 ${b.eta_min}분 후 도착`);
  startPolling();
  S.rideActive = true; updateRideBanner();   // 이용 중 상태 시작 → 화면 이탈 시 복귀 배너 노출
});

/* ───────────────────────── 실시간 추적 */
let poll = null, incidentFired = false;
function startPolling() {
  if (poll) return;
  const tick = async () => {
    let tr;
    try { tr = await api.get("/api/rider/track?session=" + encodeURIComponent(sid)); }
    catch(e) { $("a-sub").textContent="차량 연결 재시도 중…"; return; }
    if (!tr.active) { S.rideActive = false; updateRideBanner(); stopPolling(); return; }
    S.rideActive=true; drawRide(tr);
    updateRideBanner();
    // 사고/고장은 탑승 전(픽업 중)이든 탑승 후든 항상 알린다.
    if (tr.incident && !incidentFired) {
      incidentFired = true;
      stopPolling();                        // 재배차/하차를 선택할 때까지 폴링 정지
      onAccident(tr);
      return;
    }
    if (tr.phase === "pickup") {
      const here = tr.eta_min <= 1 && (tr.vehicle.status_label || "").indexOf("맞이") >= 0;
      if (tr.awaiting_board) {
        $("a-title").textContent = "차량이 도착했습니다";
        $("a-sub").textContent = "‘차량 문 열기 · 탑승하기’를 눌러주세요";
        $("a-board").classList.add("pulse");
      } else {
        $("a-title").textContent = `${tr.vehicle.plate} 차량이 오고 있어요`;
        $("a-sub").textContent = `약 ${tr.eta_min}분 후 도착 예정`;
      }
    } else if (tr.phase === "onboard") {
      $("a-title").textContent = "목적지로 이동 중";
      $("a-sub").textContent = `약 ${tr.eta_min}분 남았어요`;
    } else if (tr.phase === "arrived") {
      $("a-title").textContent = "목적지에 도착했어요";
      $("a-sub").textContent = "이용해 주셔서 감사합니다";
      stopPolling();
      S.rideActive = false; updateRideBanner();   // 운행 종료 → 복귀 배너 숨김
      if (S.started) onArrived();          // 탑승했으면 결제·영수증까지 진행
      else { addNoti("차량이 목적지에 도착했습니다."); }
    }
  };
  tick(); poll = setInterval(tick, 2000);
}
function stopPolling() { if (poll) { clearInterval(poll); poll = null; } }

$("a-cancel").addEventListener("click", async () => {
  await api.post("/api/rider/cancel", { session: sid });
  stopPolling(); clearRide(); $("ride-map-wrap").hidden = true;
  resetTripState();
  addNoti("호출이 취소되었습니다.");
  toast("호출이 취소되었습니다.");
  show("home");
});

/* ───────────────────────── AI 채팅 엔진 */
function bubble(text, who = "bot") {
  const d = document.createElement("div");
  d.className = "bubble " + who; d.textContent = text;
  $("chat-log").appendChild(d); scrollChat(); return d;
}
function bubbleHTML(html, who = "bot") {
  const d = document.createElement("div");
  d.className = "bubble " + who; d.innerHTML = html;
  $("chat-log").appendChild(d); scrollChat(); return d;
}
function scrollChat() { const c = $("chat-log"); c.scrollTop = c.scrollHeight; }
function typing(on) {
  if (on) {
    if (typing._el) return;
    typing._el = document.createElement("div");
    typing._el.className = "typing"; typing._el.textContent = "입력 중…";
    $("chat-log").appendChild(typing._el); scrollChat();
  } else if (typing._el) { typing._el.remove(); typing._el = null; }
}
function choices(list) {
  const box = document.createElement("div");
  box.className = "choices";
  list.forEach(([label, fn, ghost]) => {
    const b = document.createElement("button");
    b.className = "choice" + (ghost ? " ghost" : "");
    b.textContent = label;
    b.addEventListener("click", () => { box.remove(); bubble(label, "me"); fn(); });
    box.appendChild(b);
  });
  $("chat-log").appendChild(box); scrollChat(); return box;
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function routeCard(label, min, sub) {
  return `<div class="rmap"><span class="tagm" style="left:10px;top:10px">${label} ${min}분</span>
    <svg width="100%" height="130" viewBox="0 0 300 130">
      <path d="M20,105 L70,95 L100,60 L150,70 L190,35 L250,30" stroke="#0B1335" stroke-width="4"
        fill="none" stroke-linecap="round"/>
      <circle cx="20" cy="105" r="6" fill="#fff" stroke="#0B1335" stroke-width="3"/>
      <circle cx="250" cy="30" r="6" fill="#fff" stroke="#0B1335" stroke-width="3"/>
    </svg>${sub ? `<span class="tagm" style="right:10px;bottom:10px">${sub}</span>` : ""}</div>`;
}

/* 경로 카드 — 실제 카카오 지도에 도로 경로를 그려 넣는다.
   카카오 SDK 준비 전이거나 경로를 못 받으면 위의 SVG 카드로 자연스럽게 폴백한다. */
let rcSeq = 0;
async function bubbleRouteMap(label, min, sub) {
  const id = "rcmap-" + (++rcSeq);
  // 지도 카드는 전용 말풍선(.map)에 담는다 — 빈 지도 div 가 flex 안에서 찌그러지지 않도록 확정 너비를 준다.
  const d = document.createElement("div");
  d.className = "bubble bot map";
  d.innerHTML = `<div class="rmap">
    <div class="rmap-canvas" id="${id}"></div>
    <span class="tagm" style="left:10px;top:10px">${label} ${min}분</span>
    ${sub ? `<span class="tagm" style="right:10px;bottom:10px">${sub}</span>` : ""}
  </div>`;
  $("chat-log").appendChild(d); scrollChat();
  let rp = null;
  try { rp = await api.get("/api/rider/route_path?session=" + encodeURIComponent(sid)); } catch (e) {}
  // 카카오 SDK가 아직 준비 전이면 잠깐 기다린다(최대 ~2초)
  for (let i = 0; i < 20 && !kakaoReady; i++) await wait(100);
  const el = document.getElementById(id);
  if (!el) return;
  if (!kakaoReady || !rp || !rp.ok || !rp.points || rp.points.length < 2) {
    console.warn("[rmap] SVG 폴백 — kakaoReady:", kakaoReady, "| route_path:", rp);
    if (el.parentElement) el.parentElement.outerHTML = routeCard(label, min, sub);  // 폴백(SVG)
    return;
  }
  renderRouteMiniMap(id, rp);
}

function rcDot(map, p, color) {
  const d = document.createElement("div");
  d.style.cssText = `width:12px;height:12px;border-radius:50%;background:${color};
    border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.4)`;
  const ov = new kakao.maps.CustomOverlay({ position: new kakao.maps.LatLng(p.lat, p.lng), content: d, zIndex: 6 });
  ov.setMap(map);
}

const rcMaps = {};   // 미니맵 인스턴스 보관(GC 방지 + 재계산용)
function renderRouteMiniMap(id, rp, tries = 0) {
  const el = document.getElementById(id);
  if (!el || !kakaoReady) return;
  if ((el.offsetWidth === 0 || el.offsetHeight === 0) && tries < 30) {
    setTimeout(() => renderRouteMiniMap(id, rp, tries + 1), 100);
    return;
  }
  if (el.offsetWidth === 0 || el.offsetHeight === 0) { console.warn("[rmap] 컨테이너 크기 0 —", id); return; }
  console.info("[rmap]", id, "컨테이너 크기:", el.offsetWidth + "x" + el.offsetHeight);

  kakao.maps.load(() => {
    try {
      const path = rp.points.map((p) => new kakao.maps.LatLng(p.lat, p.lng));
      // 경로 바운딩박스 중심 (setBounds 실패 대비 안전한 초기 중심)
      let minLa = 90, maxLa = -90, minLn = 180, maxLn = -180;
      rp.points.forEach((p) => {
        minLa = Math.min(minLa, p.lat); maxLa = Math.max(maxLa, p.lat);
        minLn = Math.min(minLn, p.lng); maxLn = Math.max(maxLn, p.lng);
      });
      const center = new kakao.maps.LatLng((minLa + maxLa) / 2, (minLn + maxLn) / 2);
      const m = new kakao.maps.Map(el, { center, level: 6 });
      rcMaps[id] = m;
      m.setDraggable(false);
      m.setZoomable(false);

      // 진단: 타일이 실제로 로드되는지 확인(안 찍히면 타일 요청 자체가 차단/실패)
      let tileCount = 0;
      kakao.maps.event.addListener(m, "tilesloaded", () => {
        tileCount++;
        if (tileCount <= 2) console.info("[rmap]", id, "✅ 타일 로드됨 #" + tileCount, "level:", m.getLevel());
      });

      const line = new kakao.maps.Polyline({ strokeWeight: 5, strokeColor: "#0B1335", strokeOpacity: 0.95 });
      line.setPath(path); line.setMap(m);
      rcDot(m, rp.origin, "#36B37E");
      rcDot(m, rp.dest, "#E5484D");

      const b = new kakao.maps.LatLngBounds();
      path.forEach((pt) => b.extend(pt));
      setTimeout(() => { m.relayout(); m.setBounds(b); }, 150);
      setTimeout(() => { m.relayout(); }, 550);
      console.info("[rmap] 미니맵 생성 완료", id, "점수:", path.length);
    } catch (e) {
      console.error("[rmap] 미니맵 생성 실패:", e);
    }
  });
}

/* 탑승 → AI 채팅 시작 (경로 선택 → 운행) */
$("a-board").addEventListener("click", async () => {
  // 서버에 '승차 완료' 전달 → 이때부터 차량이 목적지로 출발한다.
  const r = await api.post("/api/rider/board", { session: sid });
  if (!r.ok) { toast(r.msg || "아직 차량이 도착하지 않았어요."); return; }
  S.started = true; S.chatMode = "ride";
  $("chat-title").textContent = "로보택시 AI 채팅";
  $("chat-log").innerHTML = "";
  $("ride-map-wrap").hidden = false;      // 운행 중 실시간 위치 지도 표시
  show("chat");
  setTimeout(() => relayout("chat-map"), 120);
  startPolling();                          // 운행 중에도 위치 갱신 계속
  if (S.free) {
    // 사고 후 재배차 차량에 탑승 — 운행 재개
    bubble("운행이 재개되었습니다.\n목적지까지 안전하게 모실게요.");
    await bubbleRouteMap("출발", S.etaMin, S.end + " →");
    await wait(900);
    askCompensation();
    return;
  }
  bubble("로보택시 운행이 시작되었습니다!\n홍길동님, 환영합니다,\n목적지까지 안전하게 모실게요.");
  await wait(600);
  bubble("운행 중 언제든 말씀해 주세요.\n예) “여기 말고 정왕역으로 가줘”, “가는 길에 성담스퀘어 들러줘”,\n“에어컨 켜줘”, “얼마나 남았어?”");
  await wait(700);
  // 카카오모빌리티 실시간 경로 옵션(실시간 추천 vs 무료 우선=유료도로 회피) 조회
  let recMin = S.etaMin, freeMin = S.etaMin + 7, viaKakao = false;
  try {
    const ro = await api.get("/api/rider/route_options?session=" + encodeURIComponent(sid));
    if (ro && ro.ok) {
      recMin = ro.recommend.eta_min;
      freeMin = ro.free.eta_min;
      viaKakao = ro.engine === "kakao";
      S.recKm = ro.recommend.distance_km;
      S.freeKm = ro.free.distance_km;
    }
  } catch (e) { /* 실패 시 위 기본값 사용 */ }
  S.etaMin = recMin;                       // 추천 경로 ETA 기준으로 갱신
  if (!viaKakao) {
    bubble("현재 연결된 시뮬레이션 경로로 이동합니다. 도로 경로 비교는 길찾기 연결 후 이용할 수 있어요.");
    await bubbleRouteMap("이동 구간", recMin, "예상 경로");
    offerRouteChange(); return;
  }
  bubble("🛰️ 카카오 실시간 교통정보를 반영한 경로예요.");
  bubble("운행 경로를 선택해주세요.\n10초 내 미선택 시 추천 경로로 운행합니다.");
  await bubbleRouteMap("무료 우선", freeMin, "실시간 추천 " + recMin + "분");
  const box = choices([
    [`실시간추천 ${recMin}분`, () => pickRoute("실시간 추천", recMin)],
    [`무료 우선 ${freeMin}분`, () => pickRoute("무료 우선", freeMin), true],
  ]);
  setTimeout(() => { if (document.body.contains(box)) { box.remove(); pickRoute("실시간 추천", recMin); } }, 10000);
});

async function pickRoute(kind, etaMin) {
  const result=await api.post("/api/rider/route",{session:sid,kind:kind.includes("무료")?"free":"recommend"});
  if(!result.ok){bubble(result.msg);offerRouteChange();return;}
  etaMin=result.eta_min;
  S.route = kind;
  if (typeof etaMin === "number") S.etaMin = etaMin;   // 선택한 경로의 실제 ETA 반영
  await wait(500);
  bubble(`${kind} 경로로 운행을 시작합니다. (약 ${S.etaMin}분)`);
  await bubbleRouteMap("출발", S.etaMin, S.end + " →");
  addNoti(`${kind} 경로로 운행 시작 · 약 ${S.etaMin}분`);
  await wait(700);
  offerRouteChange();
}

/* 운행 중 언제든 경로를 변경할 수 있게 버튼을 띄운다. */
function offerRouteChange() {
  if (!S.started) return;
  choices([["경로 변경하기", suggestRouteChange, true]]);
}

/* 사고 발생 (서버에서 감지) */
async function onAccident(tr) {
  const kind = tr.incident && tr.incident.type === "BREAKDOWN" ? "차량 고장" : "충돌 사고";
  addNoti(`${kind}가 감지되었습니다.`, true);
  toast(`🚨 ${kind} 발생`);
  // 탑승 전이면 채팅 화면을 새로 열고, 탑승 중이면 기존 대화에 이어 붙인다.
  if (!S.started) { $("chat-log").innerHTML = ""; S.chatMode = "ride"; $("chat-title").textContent = "로보택시 AI 채팅"; }
  $("ride-map-wrap").hidden = false;
  show("chat");
  setTimeout(() => relayout("chat-map"), 120);
  await wait(400);
  if (S.started) {
    bubble(`${kind}가 감지되었습니다.\n탑승자님의 상태 확인을 요청드립니다.`);
    startInjuryCheck();
  } else {
    // 아직 탑승 전 — 배차된 차량에 문제가 생긴 상황
    bubble(`배차된 차량에 ${kind}가 발생했습니다.\n불편을 드려 죄송합니다.`);
    await wait(700);
    bubble("계속 이용하시겠습니까?\n새 차량을 다시 배정해드리거나,\n호출을 취소하실 수 있습니다.");
    choices([
      ["차량재배치", doReassign],
      ["호출취소", cancelFromChat, true],
    ]);
  }
}
async function cancelFromChat() {
  await api.post("/api/rider/cancel", { session: sid });
  stopPolling(); clearRide(); S.started = false; S.rideActive = false; incidentFired = false;
  await wait(400);
  bubble("호출이 취소되었습니다. 결제하신 금액은 전액 환불됩니다.");
  addNoti("호출 취소 · 전액 환불 예정");
  await wait(900);
  show("home");
}
/* 사고 후 부상 확인 — 의식 소실 등으로 탑승자가 버튼을 누르지 못할 수 있으므로,
   일정 시간 무응답 시 '최악(부상)'으로 간주해 자동으로 119를 출동시키는 페일세이프.
   (실제 자율주행 eCall 과 동일한 안전 원칙: 이상무 확인이 없으면 구조를 보낸다) */
const INJURY_TIMEOUT_SEC = 15;
let injuryTimer = null, injuryTick = null;
function clearInjuryCheck() {
  if (injuryTimer) { clearTimeout(injuryTimer); injuryTimer = null; }
  if (injuryTick) { clearInterval(injuryTick); injuryTick = null; }
}
function startInjuryCheck() {
  clearInjuryCheck();
  let left = INJURY_TIMEOUT_SEC;
  const warn = bubble(`⚠️ 응답이 없으면 ${left}초 후 안전을 위해 자동으로 119에 연결됩니다.`);
  const box = choices([
    ["부상 없음", () => { clearInjuryCheck(); if (warn) warn.remove(); afterInjury(false); }],
    ["부상 있음 / 도움 요청", () => { clearInjuryCheck(); if (warn) warn.remove(); afterInjury(true); }, true],
  ]);
  injuryTick = setInterval(() => {
    left -= 1;
    if (left > 0 && warn) warn.textContent = `⚠️ 응답이 없으면 ${left}초 후 안전을 위해 자동으로 119에 연결됩니다.`;
  }, 1000);
  injuryTimer = setTimeout(() => {
    clearInjuryCheck();
    if (warn) warn.remove();
    if (box && box.parentElement) box.remove();
    bubble("탑승자 응답이 확인되지 않아, 안전을 위해 자동으로 119와 관제센터에 연결합니다.");
    addNoti("무응답 감지 · 자동 119 연결", true);
    afterInjury(true);           // 무응답 → 부상으로 간주하고 구급차 출동
  }, INJURY_TIMEOUT_SEC * 1000);
}

async function afterInjury(injured) {
  await wait(500);
  if (injured) {
    // 부상 있음 — 실제로 119 구급차를 출동시킨다(지도에서 이동이 보인다).
    bubble("즉시 119와 관제센터에 연결했습니다.\n구조대가 도착할 때까지 안전한 자세를 유지해 주세요.");
    S.free = true;
    addNoti("부상 접수 · 119 구급차 출동", true);
    let amb = null;
    try { amb = await api.post("/api/rider/injury", { session: sid }); } catch (e) {}
    await wait(900);
    if (amb && amb.ok) {
      bubble(`${amb.station}에서 119 구급차가 출동했습니다.\n약 ${amb.eta_min}분 후 도착 예정이며, ${amb.hospital}으로 이송됩니다.`);
      bubbleSys("🚑 구급차가 지도에서 이동 중입니다");
    } else {
      bubble("현재 위치를 응급 구조대에 전송했습니다.");
    }
    await wait(1100);
    bubble("차량 문은 자동으로 잠금 해제되었습니다.\n구조대 도착 시까지 움직이지 마시고 대기해 주세요.");
    await wait(1100);
    bubble("사고 구간 운행 비용은 전액 무료로 처리되며,\n치료비와 보상 금액은 산정 후 담당자가 연락드립니다.\n불편을 드려 진심으로 죄송합니다.");
    await wait(1000);
    // 운행 종료(무료) 처리 — 재배차 없이 마무리
    finishTrip(true);
    return;
  }
  bubble("‘부상 없음’ 확인했습니다.\n불편을 드려 진심으로 죄송합니다.");
  await wait(800);
  // 부상 없음 → 사고 구간은 무료 처리(재배차하든 하차하든 동일)
  S.free = true;
  bubble("계속 이용하시겠습니까?\n새 차량을 다시 배정해드리거나,\n현재 위치에서 하차를 도와드릴 수 있습니다.");
  choices([
    ["차량재배치", doReassign],
    ["하차", doDropOff, true],
  ]);
}
async function doReassign() {
  await wait(500);
  bubble("차량 재배치를 진행합니다.\n안전한 곳에 하차하여 대기해주시길 바랍니다.");
  const r = await api.post("/api/rider/reassign", { session: sid });
  await wait(900);
  if (!r.ok) { bubble(r.msg || "지금은 배차 가능한 차량이 없어요."); return; }
  bubble("재배차가 완료되었습니다.");
  S.plate = r.plate;
  S.carType = r.car_type || S.carType;
  addNoti(`재배차 완료 · ${r.plate}`);
  incidentFired = false;
  // 새 차량은 아직 오는 중 — 승차 완료를 눌러야 출발한다.
  S.started = false;
  $("ride-map-wrap").hidden = true;
  $("a-plate").textContent = r.plate;
  $("a-badge").textContent = "배정완료"; $("a-badge").classList.add("on");
  $("a-title").textContent = `${r.plate} 차량이 오고 있어요`;
  $("a-sub").textContent = `약 ${r.eta_min}분 후 도착 예정`;
  $("a-board").classList.remove("pulse");
  startPolling();
  await wait(800);
  bubble(`${r.plate} 차량이 약 ${r.eta_min}분 후 도착합니다.\n도착하면 ‘차량 문 열기 · 탑승하기’를 눌러주세요.`);
  await wait(1200);
  show("assign");
}
function askCompensation() {
  choices([["운행 비용 및 보상은 어떻게 되나요?", async () => {
    await wait(700);
    bubble("사고 구간 운행 비용은 무료로 처리되며,\n보상 금액 산정 후 추후 연락드리겠습니다.\n불편을 드려 진심으로 죄송합니다.");
    S.free = true;
    await wait(900);
    suggestRouteChange();
  }]]);
}
async function suggestRouteChange() {
  bubble("변경할 경로를 선택해 주세요.");
  choices([
    ["추천 경로",()=>pickRoute("실시간 추천",S.etaMin)],
    ["무료 우선 경로",()=>pickRoute("무료 우선",S.etaMin),true],
    ["현재 경로 유지",()=>{bubble("현재 경로로 계속 운행합니다.");offerRouteChange();},true],
  ]);
}
async function doDropOff() {
  await wait(600);
  bubble("현재 위치에서 안전하게 하차를 도와드리겠습니다.");
  await wait(700);
  bubble("사고 구간 운행 비용은 무료로 처리되며,\n결제된 금액은 환불됩니다.");
  S.free = true;
  await wait(800);
  finishTrip(true);
}

/* 운행 관련 상태를 초기화 — 다음 호출에 사고 지점·경유지 등이 남지 않도록 */
function resetTripState() {
  clearInjuryCheck();
  S.started = false;
  S.rideActive = false;
  incidentFired = false;
  S.free = false;
  S.route = null;
  S.routeOpt = null;
  S.plate = "";
  S.carType = "SEDAN";
  S.via = "";
  S.request = "";
  // 출발지는 기본값(한국공학대)으로 복귀 — '사고 지점'이 남지 않게
  S.start = S.places.includes(HOME_PLACE) ? HOME_PLACE : (S.places[0] || "");
  S.end = "";
  [$("h-start"), $("c-start"), $("r-start")].forEach((e) => e && setField(e, S.start));
  [$("h-end"), $("c-end"), $("r-end")].forEach((e) => {
    if (e) { e.querySelector("span").textContent = "장소"; e.classList.remove("filled"); }
  });
  [$("c-via"), $("r-via")].forEach((e) => {
    if (e) { e.querySelector("span") ? (e.querySelector("span").textContent = "") : null;
             e.innerHTML = "＋ <span>경유지</span>"; e.classList.remove("filled"); }
  });
  $("p-via").hidden = true;
  $("p-req").value = "";
  $("a-board").classList.remove("pulse");
  const aImg = document.querySelector("#sc-assign .veh-img");
  if (aImg) { aImg.src = "/static/img/car.png"; aImg.classList.remove("suv"); }
}

/* 목적지 도착 → 결제 → 영수증 */
async function onArrived() {
  if ($("sc-chat").hidden) { show("chat"); }
  await wait(500);
  bubble("운행이 종료되었습니다.");
  await wait(600);
  finishTrip(S.free === true);
}
async function finishTrip(free) {
  const rc = await api.post("/api/rider/complete", { session: sid, free: !!free });
  if (!rc.ok) return;
  S.rideActive = false; updateRideBanner();   // 운행 종료 확정 → '이용 중 택시로 돌아가기' 배너 숨김
  await wait(400);
  if (free) bubble("사고 구간 운행 비용은 무료로 처리되었으며, 보상 금액 산정 후 별도 연락드릴 예정입니다.");
  else {
    bubble(`예상 결제금액 ${won(rc.meter_fare)} 이 결제됩니다.`);
    await wait(700);
    bubble("결제가 정상 처리되었습니다.");
  }
  await wait(500);
  bubbleHTML(receiptHTML(rc));
  const box = document.createElement("div");
  box.className = "choices";
  const b = document.createElement("button");
  b.className = "choice"; b.textContent = "이용을 종료합니다";
  b.addEventListener("click", () => {
    box.remove(); stopPolling(); clearRide();
    $("ride-map-wrap").hidden = true;
    resetTripState();
    addNoti(free ? "사고 구간 무료 처리 · 운행 종료" : `${won(rc.fare)} 결제 완료`);
    toast("이용해 주셔서 감사합니다!");
    show("home");
  });
  box.appendChild(b); $("chat-log").appendChild(box); scrollChat();
  S.trips.unshift(rc); renderTrips(); saveLocal();
}
function receiptHTML(r) {
  return `<div class="receipt">
    <h4>🚕 택시 이용 상세</h4><div class="rdate">${r.date}</div>
    <div class="sect">운행 정보</div>
    <div class="rrow"><span>출발</span><b>${r.origin}</b></div>
    <div class="rrow"><span>도착</span><b>${r.dest}</b></div>
    <div class="rrow"><span>운행 시간</span><b>${r.time_range}</b></div>
    <div class="rrow"><span>호출 옵션</span><b>${S.routeOpt || S.route || "실시간 추천"}</b></div>
    <div class="sect">택시 정보</div>
    <div class="rrow"><span>차량 번호</span><b>${r.plate}</b></div>
    <div class="rrow"><span>기사명</span><b>${r.driver}</b></div>
    <div class="rrow"><span>제휴 브랜드</span><b>${r.brand}</b></div>
    <div class="sect">요금 정보</div>
    <div class="rrow"><span>운행 요금(미터기요금)</span><b>${won(r.meter_fare)}</b></div>
    <hr/>
    <div class="rrow hl"><span>결제 금액</span><b>${r.free ? "0원 (무료)" : won(r.fare)}</b></div>
    <div class="rrow"><span>결제 수단</span><b>${r.payment_method || S.method}</b></div>
    <div class="rrow"><span>결제 일시</span><b>${r.paid_at}</b></div>
  </div>`;
}

/* ───────────────────────── 상담사 연결 (관리자와 실시간 채팅) */
let supPoll = null, supLastTs = 0;

async function startSupport() {
  S.supportMode = true;
  supLastTs = 0;
  await api.post("/api/support/request", {
    session: sid, plate: S.plate || null, dest: S.end || null,
  });
  bubble("상담사를 연결 중입니다. 잠시만 기다려 주세요.");
  $("chat-text").placeholder = "상담사에게 메시지를 보내세요.";
  // 상담 종료 버튼
  const box = document.createElement("div");
  box.className = "choices"; box.id = "sup-end-box";
  const b = document.createElement("button");
  b.className = "choice ghost"; b.textContent = "상담 종료";
  b.addEventListener("click", endSupport);
  box.appendChild(b);
  $("chat-log").appendChild(box); scrollChat();
  startSupportPoll();
}

function startSupportPoll() {
  if (supPoll) return;
  const tick = async () => {
    let d;
    try { d = await api.get("/api/support/poll?session=" + encodeURIComponent(sid) + "&after=" + supLastTs); }
    catch (e) { return; }
    const closed = !d.active;                        // 관리자(관제사)가 상담을 종료한 경우
    (d.messages || []).forEach((m) => {
      if (m.who === "admin") bubble(m.text);         // 상담사 이름 접두사 없이 메시지만 표시
      else if (m.who === "system" && !closed) bubbleSys(m.text);   // 종료 안내는 아래에서 통일해 표시
    });
    if (d.last_ts) supLastTs = d.last_ts;
    if (closed) {                                    // 관리자 대시보드에서 '상담 종료'를 누름 → 고객 앱에도 종료 안내
      stopSupportPoll();
      S.supportMode = false;
      $("chat-text").placeholder = "채팅을 입력해 주세요.";
      const eb = document.getElementById("sup-end-box");
      if (eb) eb.remove();                           // 고객측 '상담 종료' 버튼 제거(이미 종료됨)
      bubbleSys("상담이 종료되었습니다.");
    }
  };
  tick(); supPoll = setInterval(tick, 1500);
}
function stopSupportPoll() { if (supPoll) { clearInterval(supPoll); supPoll = null; } }

async function endSupport() {
  await api.post("/api/support/close", { session: sid });
  stopSupportPoll();
  S.supportMode = false;
  $("chat-text").placeholder = "채팅을 입력해 주세요.";
  const eb = document.getElementById("sup-end-box");
  if (eb) eb.remove();
  bubbleSys("상담이 종료되었습니다.");
}

function bubbleSys(text) {
  const d = document.createElement("div");
  d.className = "bubble sys"; d.textContent = text;
  $("chat-log").appendChild(d); scrollChat();
}

/* ───────────────────────── 고객지원 채팅 (서버 AI) */
/* 진입점 구분: 배차 화면의 '챗봇문의하기' vs 마이페이지의 '문의하기' */
function openSupport(mode) {
  S.chatMode = "support";
  $("chat-title").textContent = "로보택시 AI 채팅";
  $("chat-log").innerHTML = "";
  $("ride-map-wrap").hidden = !S.started;   // 운행 중이면 지도 유지
  show("chat");
  if (S.started) setTimeout(() => relayout("chat-map"), 120);
  bubble("홍길동님, 환영합니다.\n문의사항이 있으신가요?");

  if (mode === "mypage") {
    // 이용 후 문의 — 탑승인원 변경은 호출 시점의 문의라 여기서는 제외
    choices([
      ["분실물 문의", lostItemFlow],
      ["자주 묻는 질문", () => faq(), true],
      ["상담사연결", startSupport, true],
    ]);
  } else {
    // 호출·운행 중 문의
    choices([
      ["탑승인원변경", paxChangeFlow],
      ["자주 묻는 질문", () => faq(), true],
      ["상담사연결", startSupport, true],
    ]);
  }
}

/* ── 분실물 문의: AI 가 택시 상태를 보고 자동 처리 ── */
async function lostItemFlow() {
  await wait(500);
  typing(true);
  let lt;
  try { lt = await api.get("/api/rider/last-trip?session=" + encodeURIComponent(sid)); }
  catch (e) { lt = { ok: false }; }
  typing(false);

  if (!lt.ok) {
    bubble("최근 이용 내역을 찾을 수 없어요.\n상담사를 연결해 드릴까요?");
    choices([["상담사연결", startSupport], ["괜찮아요", async () => { await wait(300); bubble("필요하시면 언제든 불러주세요."); }, true]]);
    return;
  }

  bubble(`가장 최근 이용 내역이에요.\n· 차량: ${lt.plate}\n· 하차: ${lt.dest}\n· 일시: ${lt.date} ${lt.time_range}\n\n어떤 물건을 두고 내리셨나요?`);
  choices([
    ["지갑", () => submitLost("지갑")],
    ["휴대폰", () => submitLost("휴대폰")],
    ["가방", () => submitLost("가방"), true],
    ["기타", async () => {
      await wait(300);
      bubble("두고 내리신 물건을 입력해 주세요. (예: 우산)");
      S.awaitLostItem = true;      // 다음 입력을 분실물 이름으로 처리
    }, true],
  ]);
}

async function submitLost(item) {
  await wait(400);
  typing(true);
  let r;
  try { r = await api.post("/api/rider/lost-item", { session: sid, item }); }
  catch (e) { r = { ok: false, need_agent: true, msg: "처리 중 오류가 발생했어요." }; }
  typing(false);

  bubble(r.msg || "확인 중이에요.");
  if (r.ok && r.mode === "deliver") {
    // 택시가 한가함 → 고객 위치로 배송 (지도에서 차량이 실제로 이동)
    addNoti(`${item} 전달 · ${r.plate} 차량이 ${r.place}(으)로 이동 중`);
    await wait(700);
    bubble("차량 도착 시 알림으로 알려드릴게요.\n다른 도움이 필요하시면 말씀해 주세요 🙂");
  } else if (r.ok && r.mode === "shop") {
    // 택시가 운행 중 → 정비센터 보관
    addNoti(`${item} 보관 · ${r.shop}`);
    await wait(700);
    bubble(`${r.shop} 운영시간(09:00~18:00)에 방문해 주세요.\n신분증을 지참해 주시면 빠르게 확인됩니다.`);
  } else {
    // AI 가 해결할 수 없음 → 상담사 연결 추천
    await wait(500);
    bubble("제가 바로 처리하기 어려운 문의예요.\n상담사를 연결해 드릴까요?");
    choices([["상담사연결", startSupport], ["나중에", async () => { await wait(300); bubble("필요하시면 언제든 불러주세요."); }, true]]);
  }
}

$("a-chat").addEventListener("click", () => openSupport("ride"));
$("my-ask").addEventListener("click", () => openSupport("mypage"));
$("chat-back").addEventListener("click", () => { show(S.started ? "assign" : "home"); });

async function paxChangeFlow() {
  await wait(500);
  bubble(`현재 선택한 탑승인원은 ${S.pax}명 입니다.\n몇 명으로 변경해드릴까요?`);
  choices(PAX.map((p) => [p, async () => {
    const pax = parseInt(p);
    await wait(400);
    // 배차된 차량이 있으면 서버에 반영 — 5인 이상이면 SUV 로 실제 교체된다.
    if (S.plate) {
      typing(true);
      const r = await api.post("/api/rider/passengers", { session: sid, passengers: pax });
      typing(false);
      if (!r.ok) { bubble(r.msg || "지금은 변경할 수 없어요."); return; }
      S.pax = r.passengers;
      [$("c-pax"), $("r-pax")].forEach((e) => setField(e, S.pax + "명"));
      $("p-pax").querySelector("span").textContent = S.pax + "명";
      bubble(`${S.pax}명 으로 탑승인원이 변경되었습니다.`);
      if (r.changed) {
        await wait(700);
        bubble(r.msg);
        S.plate = r.plate; S.carType = r.car_type;
        const isSuv = r.car_type === "SUV";
        $("a-plate").textContent = r.plate;
        $("a-title").textContent = `${r.plate} 차량이 오고 있어요`;
        $("a-sub").textContent = `약 ${r.eta_min}분 후 도착 예정`;
        document.querySelector("#sc-assign .veh-desc").textContent =
          isSuv ? "ROBITA 대형택시(SUV · 6인승) · 완전 자율주행" : "ROBITA 차량 · 완전 자율주행";
        const aImg = document.querySelector("#sc-assign .veh-img");
        if (aImg) { aImg.src = isSuv ? "/static/img/suv.png" : "/static/img/car.png";
                    aImg.classList.toggle("suv", isSuv); }
        addNoti(`${r.plate}${isSuv ? " (SUV)" : ""} 차량으로 변경 · ${S.pax}명`);
        $("a-board").classList.remove("pulse");
        startPolling();
      }
      return;
    }
    // 아직 호출 전 — 화면 값만 갱신
    S.pax = pax;
    [$("c-pax"), $("r-pax")].forEach((e) => setField(e, p));
    $("p-pax").querySelector("span").textContent = p;
    bubble(`${p} 으로 탑승인원이 변경되었습니다.`);
    if (pax >= 5) {
      await wait(600);
      bubble("5인 이상은 대형(SUV) 차량으로 배차됩니다.\n쾌적한 탑승을 준비하겠습니다.");
    }
  }]));
}

const FAQ_ITEMS = [
  ["분실물을 두고 내렸어요", lostItemFlow],
  ["요금은 어떻게 계산되나요?", async () => {
    await wait(400);
    bubble("기본요금에 이동 거리와 예상 소요 시간을 반영해 계산돼요.\n결제 전 ‘결제 예상 금액’으로 미리 확인하실 수 있어요.");
    await wait(700); faqFollowUp();
  }],
  ["사고가 나면 어떻게 되나요?", async () => {
    await wait(400);
    bubble("사고가 감지되면 즉시 탑승자 상태를 확인하고,\n부상이 있으면 119와 관제센터를 연결해 드려요.\n사고 구간 운행 비용은 전액 무료로 처리됩니다.");
    await wait(700); faqFollowUp();
  }],
  ["영수증을 다시 받고 싶어요", async () => {
    await wait(400);
    bubble("마이페이지 > 이용 기록에서 지난 운행 내역을 확인하실 수 있어요.");
    await wait(700); faqFollowUp();
  }],
];

async function faq() {
  await wait(400);
  bubble("자주 묻는 질문이에요.\n아래에서 선택하시거나, 궁금한 점을 직접 입력해 주세요.");
  choices(FAQ_ITEMS.map(([label, fn], i) => [label, fn, i > 0]));
}

/* FAQ 로 해결되지 않으면 상담사에게 넘긴다 */
function faqFollowUp() {
  bubble("도움이 되셨나요?");
  choices([
    ["해결됐어요", async () => { await wait(300); bubble("도움이 되어 다행이에요! 필요하시면 언제든 불러주세요 🙂"); }],
    ["해결 안 됐어요", async () => {
      await wait(400);
      bubble("불편을 드려 죄송해요.\n상담사에게 문의를 넘겨드릴게요.");
      await wait(600);
      startSupport();
    }, true],
  ]);
}

/* 직접 입력 → 서버 AI(ChatGPT) */
async function sendChat() {
  const text = $("chat-text").value.trim();
  if (!text) return;
  $("chat-text").value = "";
  bubble(text, "me");
  typing(true);
  let res;
  if (S.supportMode) {
    // 상담사 연결 중 — 관리자 대시보드로 전달
    typing(false);
    await api.post("/api/support/send", { session: sid, text });
    return;
  }
  if (S.started) {
    // 탑승 중 — 차량 편의 요청(에어컨/음악/창문 등)을 GPT 가 처리
    res = await api.post("/api/rider/request", { session: sid, text });
    typing(false);
    bubble((res && res.reply) || "잠시 후 다시 시도해 주세요.");
    if (!res) return;
    // AI 가 실제로 차량을 조종한 경우 화면·지도에 반영
    if (res.action === "change_destination" && res.ok) {
      S.end = res.dest;
      S.etaMin = res.eta_min || S.etaMin;
      $("a-to").textContent = res.dest;
      addNoti(`목적지 변경 · ${res.old} → ${res.dest}`);
      await bubbleRouteMap("변경된 경로", S.etaMin, res.dest + " →");
    } else if (res.action === "add_stop" && res.ok) {
      addNoti(`경유지 추가 · ${res.stop}`);
      await bubbleRouteMap("경유", S.etaMin, res.stop + " →");
    } else if (res.action === "route") {
      await wait(500); suggestRouteChange();
    } else if (res.action === "support") {
      await wait(400); startSupport();
    }
    return;
  } else if (S.awaitLostItem) {
    // '기타' 선택 후 물건 이름 입력
    S.awaitLostItem = false;
    typing(false);
    submitLost(text);
    return;
  } else if (S.chatMode === "support") {
    // 이용 후 문의 — 분실물 자동 처리 / 해결 불가 시 상담사 연결 추천
    res = await api.post("/api/rider/inquiry", { session: sid, text });
    typing(false);
    bubble((res && res.msg) || "잠시 후 다시 시도해 주세요.");
    if (!res) return;
    if (res.action === "lost" && res.ok) {
      if (res.mode === "deliver") addNoti(`분실물 전달 · ${r_plate(res)} 차량 이동 중`);
      else if (res.mode === "shop") addNoti(`분실물 보관 · ${res.shop}`);
      return;
    }
    if (res.need_agent) {
      await wait(500);
      bubble("상담사를 연결해 드릴까요?");
      choices([["상담사연결", startSupport], ["나중에", async () => { await wait(300); bubble("필요하시면 언제든 불러주세요."); }, true]]);
    }
    return;
  } else {
    res = await api.post("/api/rider/message", { session: sid, text });
  }
  typing(false);
  bubble((res && res.reply) || "잠시 후 다시 시도해 주세요.");
}

function r_plate(res) { return res.plate || "차량"; }
$("chat-send").addEventListener("click", sendChat);
$("chat-text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

/* ───────────────────────── 예약 */
function renderReservation(r) {
  S.reserved=r && r.status==="pending";
  if(!r)return;
  S.reservation=r;
  $("rd-when").textContent=r.when_label+" 예약";
  $("rd-from").textContent=r.origin;$("rd-to").textContent=r.destination;
  $("rd-time").textContent=r.when_label;$("rd-method").textContent=r.payment_method;
  $("rd-plate").textContent=r.plate||"예약 접수";
  $("rd-badge").textContent=r.status==="pending"?"예약 대기":r.status==="dispatched"?"배차 완료":r.status==="failed"?"배차 실패":"취소 완료";
  $("rd-cancel").hidden=r.status!=="pending";
}
$("r-go").addEventListener("click",async()=>{
  if(!S.start||!S.end){toast("출발지와 도착지를 선택해 주세요.");return;}
  if(!S.reserveAt){toast("예약 시간을 선택해 주세요.");return;}
  const button=$("r-go");button.disabled=true;
  try{
    const r=await api.post("/api/rider/reserve",{session:sid,origin:S.start,destination:S.end,
      via:S.via,passengers:S.pax,payment_method:S.method,request:$("r-req").value.trim(),when:S.reserveISO});
    if(!r.ok){toast(r.msg);return;}
    renderReservation(r.reservation);addNoti(S.reserveAt+" 예약 접수 완료 · 예약 시간에 배차됩니다.");
    show("rdone");
  }finally{button.disabled=false;}
});
$("rd-cancel").addEventListener("click",()=>popup("예약을 취소할까요?",["예, 취소합니다","아니오"],async v=>{
  if(!v.startsWith("예"))return;
  const r=await api.post("/api/rider/reservation/cancel",{session:sid});
  if(!r.ok){toast(r.msg);return;}
  S.reserved=false;addNoti("예약이 취소되었습니다.");toast("예약이 취소되었습니다.");show("home");
}));
function saveLocal(){ localStorage.setItem("rider_data_"+sid,JSON.stringify({method:S.method,notis:S.notis})); }
function restoreLocal(){
  try{const d=JSON.parse(localStorage.getItem("rider_data_"+sid)||"{}");S.method=d.method||S.method;S.notis=d.notis||[];}catch(e){}
  [$("p-method"),$("r-method")].forEach(e=>setField(e,S.method));
}
async function restoreRide(tr) {
  S.rideActive=true;S.started=tr.boarded||tr.phase==="onboard";
  S.plate=tr.vehicle.plate;S.fare=tr.fare||0;S.etaMin=tr.eta_min||0;S.pax=tr.passengers||1;S.carType=tr.vehicle.car_type;S.free=!!tr.free;
  S.start=tr.pickup.name;S.end=tr.dest.name;S.method=tr.payment_method||S.method;
  S.via=tr.via?.name||"";S.request=tr.request||"";
  ["h-start","c-start","r-start"].forEach(id=>setField($(id),S.start));
  ["h-end","c-end","r-end"].forEach(id=>setField($(id),S.end));
  ["c-pax","r-pax"].forEach(id=>setField($(id),S.pax+"명"));
  ["p-method","r-method"].forEach(id=>setField($(id),S.method));
  $("a-plate").textContent=S.plate;$("a-method").textContent=S.method;
  $("a-badge").textContent="배정완료";$("a-badge").classList.add("on");
  $("a-from").textContent=S.start;$("a-to").textContent=S.end;$("a-fare").textContent=won(S.fare);
  const img=document.querySelector("#sc-assign .veh-img");img.src=S.carType==="SUV"?"/static/img/suv.png":"/static/img/car.png";
  $("ride-map-wrap").hidden=!S.started;
  show(S.started?"chat":"assign");drawRide(tr);
  if(S.started)bubble("이용 중인 운행을 다시 연결했어요. 목적지: "+S.end);
  startPolling();
}
async function syncState(){
  const d=await api.get("/api/rider/state?session="+encodeURIComponent(sid));
  S.trips=d.trips||[];renderTrips();
  const prev=S.reservation?.status;renderReservation(d.reservation);
  if(d.reservation?.status==="dispatched" && prev!=="dispatched" && !S.rideActive){
    addNoti("예약 차량 배차 완료 · "+d.reservation.plate);
    const tr=await api.get("/api/rider/track?session="+encodeURIComponent(sid));if(tr.active)restoreRide(tr);
  }
  if(d.reservation?.status==="failed" && prev!=="failed")addNoti(d.reservation.msg||"예약 배차 실패",true);
}

/* ───────────────────────── 시작 */
async function init() {
  restoreLocal();renderNotis();renderTrips();
  show("home");waitForKakao();refreshFleet();
  setInterval(refreshFleet,2500);
  try{
    await loadPlaces();await syncState();
    const tr=await api.get("/api/rider/track?session="+encodeURIComponent(sid));
    if(tr.active)await restoreRide(tr);
  }catch(e){toast("서버 연결을 확인해 주세요.");}
  setInterval(()=>syncState().catch(()=>{}),5000);
}
window.addEventListener("unhandledrejection",event=>{console.error(event.reason);toast("연결을 확인한 후 다시 시도해 주세요.");});
init();
