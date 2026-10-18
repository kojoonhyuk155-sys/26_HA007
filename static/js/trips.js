/* =========================================================================
   금일 이용내역 페이지
   - /api/trips 를 주기적으로 조회해 요약 통계 + 운행 기록 표를 갱신
   - 운행 완료(자동) 건과 운영자 수동 호출(배차) 건이 함께 표시된다
   ========================================================================= */

const REFRESH_MS = 4000;
const won = (n) => Number(n || 0).toLocaleString("ko-KR");

function renderSummary(s) {
  const cards = [
    { label: "📋 총 이용 건수", value: `${won(s.count)}건`, sub: `완료 ${won(s.completed)} · 배차 ${won(s.dispatched)}` },
    { label: "💰 운행 수익", value: `${won(s.revenue)}원`, sub: "완료 운행 합계" },
    { label: "🧾 평균 요금", value: `${won(s.avg_fare)}원`, sub: "완료 건 기준" },
    { label: "⭐ 평균 평점", value: `${(s.avg_rating || 0).toFixed(2)}`, sub: "완료 건 기준" },
    { label: "🙋 수동 호출", value: `${won(s.manual)}건`, sub: "운영자 직접 호출" },
  ];
  document.getElementById("trip-summary").innerHTML = cards.map((c) => `
    <div class="card stat">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
    </div>`).join("");
}

function statusBadge(status) {
  const label = ["완료", "취소", "배차", "운행중"].includes(status) ? status : "배차";
  return `<span class="badge ${status === "완료" ? "done" : "wait"}">${label}</span>`;
}
function sourceTag(source) {
  if ((source || "").startsWith("고객앱")) return `<span class="src manual">고객 앱</span>`;
  return source === "수동"
    ? `<span class="src manual">수동</span>`
    : `<span class="src auto">자동</span>`;
}
function engineTag(engine) {
  if (engine === "openai") return `<span class="eng gemini">ChatGPT</span>`;
  if (engine === "gemini") return `<span class="eng gemini">Gemini</span>`;
  if (engine === "score") return `<span class="eng rule">📊 점수식</span>`;
  return `<span class="eng rule">규칙</span>`;
}

function renderRows(rows) {
  const body = document.getElementById("trip-body");
  if (!rows || !rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty-hint">아직 운행 기록이 없습니다. ‘택시 호출’에서 배차해 보세요.</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((t) => `
    <tr>
      <td class="mono">${t.time}</td>
      <td><b>${t.plate}</b><span class="sub2">${t.vehicle_id}</span></td>
      <td>${t.origin} <span class="arr">→</span> ${t.destination}</td>
      <td class="r">${t.distance_km != null ? t.distance_km + " km" : "—"}</td>
      <td class="r">${t.fare != null ? won(t.fare) + "원" : "—"}</td>
      <td class="c">${statusBadge(t.status)}</td>
      <td class="c">${sourceTag(t.source)}</td>
      <td class="c">${engineTag(t.engine)}</td>
    </tr>`).join("");
}

async function refresh() {
  try {
    const res = await fetch("/api/trips");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    renderSummary(data.summary);
    renderRows(data.rows);
    const now = new Date();
    document.getElementById("trips-ts").textContent =
      now.toLocaleTimeString("ko-KR", { hour12: false });
  } catch (e) {
    console.error("이용내역 조회 실패", e);
    document.getElementById("trips-ts").textContent = "갱신 실패 — 재시도 중";
  }
}

window.addEventListener("DOMContentLoaded", () => {
  refresh();
  setInterval(refresh, REFRESH_MS);
});
