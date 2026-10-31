const CONFIG = {
  statusIntervalMs: 250,
  eventsIntervalMs: 2500,
  robotTimeoutMs: 5000,
  videoPort: 8080,
  videoTopic: "/image_raw",
  videoType: "ros_compressed",
  cameraFovDeg: 62,
  viewRangeM: 1.5,
  robotRadiusM: 0.12,
  mapRotation: 0,
  doneEventHours: 24,
  toastMaxItems: 1,
  logsIntervalMs: 700,
  controlIntervalMs: 1000,
  toastFreshSeconds: 60,
  logMaxLines: 300,
};

const STATUS_LABELS = {
  observing: "관찰 중",
  confirmed: "낙상 의심",
  uncertain: "확인 필요",
  false_alarm: "오탐",
  acknowledged: "확인됨",
  resolved: "조치 완료",
};

const ROBOT_STATE_LABELS = {
  idle: "대기",
  patrol: "순찰",
  observe: "관찰",
  alert: "알림",
};

const ROBOT_STEPS = ["idle", "patrol", "observe", "alert"];

// Older API clients and stored logs keep their original values.
const LEGACY_ROBOT_STATE_LABELS = { suspect: "의심 감지", approach: "접근 중" };

function robotDisplayState(value) {
  return value === "suspect" || value === "approach" ? "observe" : value;
}

function robotLogLabel(value) {
  return ROBOT_STATE_LABELS[value] ?? LEGACY_ROBOT_STATE_LABELS[value] ?? value;
}

const SNAPSHOT_LABELS = { detected: "① 발견", observing: "② 관찰 시작", decision: "③ 판단" };

// 낙상 의심·확인 필요는 중간 단계, 조치 완료·오탐이 최종 단계다.
// 알림을 보고 현장에 다녀온 사람이 바로 조치 완료를 누른다 (확인 단계 없음)
const ACTIONS = {
  false_alarm: { label: "오탐", status: "false_alarm", style: "" },
  resolve: { label: "조치 완료", status: "resolved", style: "btn-resolve" },
};

const ACTIONS_BY_STATUS = {
  observing: ["resolve", "false_alarm"],
  confirmed: ["resolve", "false_alarm"],
  uncertain: ["resolve", "false_alarm"],
  acknowledged: ["resolve", "false_alarm"],
  resolved: [],
  false_alarm: [],
};

const FILTERS = [
  { key: "all", label: "전체", statuses: null },
  { key: "alert", label: "처리 필요", statuses: ["confirmed", "uncertain"] },
  { key: "resolved", label: "조치 완료", statuses: ["resolved"] },
  { key: "false_alarm", label: "오탐", statuses: ["false_alarm"] },
];

const ALERT_STATUSES = new Set(["confirmed", "uncertain"]);
const ACTIVE_STATUSES = new Set(["observing", "confirmed", "uncertain", "acknowledged"]);
const EVENT_TONES = { confirmed: "confirmed", uncertain: "uncertain", resolved: "resolved" };

const state = {
  events: [],
  eventsKey: "",
  filter: "all",
  selectedId: null,
  detailPhoto: null,
  robot: null,
  robotChangedAt: 0,
  serverOnline: null,
  map: null,
  mapImage: null,
  view: null,
  lastFrame: 0,
  control: null,
  dismissedToasts: new Set(),
  shownToasts: new Set(),
};

const $ = (selector) => document.querySelector(selector);
const canvas = $("#map");
const ctx = canvas.getContext("2d");
const detailDialog = $("#detail");

const kstFormatter = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function formatTime(iso) {
  return iso ? kstFormatter.format(new Date(iso)) : "-";
}

function formatPosition(x, y) {
  return x === null || y === null ? "-" : `(${x.toFixed(2)}, ${y.toFixed(2)})`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const COLOR_NAMES = ["robot", "confirmed", "uncertain", "active", "done", "resolved", "map-free", "map-wall"];
const COLORS = {};

function readColors() {
  for (const name of COLOR_NAMES) COLORS[name] = cssVar(`--${name}`);
}
readColors();

function hexToRgb(hex) {
  const value = parseInt(hex.replace("#", ""), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function withAlpha(hex, alpha) {
  return `rgb(${hexToRgb(hex).join(" ")} / ${alpha})`;
}

function badge(status) {
  return `<span class="badge badge-${status}">${STATUS_LABELS[status]}</span>`;
}

function actionButtons(event) {
  return ACTIONS_BY_STATUS[event.status]
    .map((key) => {
      const action = ACTIONS[key];
      return `<button class="btn ${action.style}" data-action="${key}" data-id="${event.id}">${action.label}</button>`;
    })
    .join("");
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(`${response.status} ${path}`);
  return response.json();
}

async function updateEvent(id, body) {
  const updated = await api(`/events/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  state.events = state.events.map((event) => (event.id === id ? updated : event));
  state.eventsKey = "";
  renderEvents();
  return updated;
}

function poll(task, intervalMs) {
  const run = async () => {
    await task();
    setTimeout(run, intervalMs);
  };
  run();
}

async function pollStatus() {
  try {
    const robot = await api("/status");
    if (!state.robot || robot.updated_at !== state.robot.updated_at) {
      state.robotChangedAt = performance.now();
    }
    state.robot = robot;
    state.serverOnline = true;
  } catch {
    state.serverOnline = false;
  }
  renderRobot();
}

async function pollEvents() {
  try {
    state.events = await api("/events?limit=200");
  } catch {
    return;
  }
  renderEvents();
}

async function pollControl() {
  try {
    state.control = await api("/control");
  } catch {
    return;
  }
  renderDrive();
}

function renderDrive() {
  const drive = state.control?.drive;
  const group = $("#drive");
  group.dataset.current = drive ?? "";
  group.querySelectorAll("[data-drive]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.drive === drive));
  });
}

async function sendDrive(drive) {
  const group = $("#drive");
  group.classList.add("busy");
  try {
    state.control = await api("/control", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drive }),
    });
    renderDrive();
  } catch (error) {
    alert(`주행 명령 실패: ${error.message}`);
  } finally {
    group.classList.remove("busy");
  }
}

function robotIsStale() {
  return !state.robot || performance.now() - state.robotChangedAt > CONFIG.robotTimeoutMs;
}

function renderRobot() {
  const [tone, label] =
    state.serverOnline === false
      ? ["error", "서버 연결 끊김"]
      : robotIsStale()
        ? ["warn", "로봇 응답 없음"]
        : ["ok", "로봇 연결됨"];
  const connection = $("#connection");
  connection.className = `status-pill status-${tone}`;
  connection.lastElementChild.textContent = label;

  const robot = state.robot;
  if (!robot) return;
  const offline = state.serverOnline === false || robotIsStale();
  const displayState = robotDisplayState(robot.state);
  $("#robot-hero").className = `robot-hero state-${offline ? "offline" : displayState}`;
  $("#robot-state").textContent = offline ? "연결 끊김" : ROBOT_STATE_LABELS[displayState] ?? robot.state;
  $("#robot-sub").textContent = offline ? "마지막으로 받은 위치를 표시 중" : robotSubtitle(robot);

  const current = offline ? -1 : ROBOT_STEPS.indexOf(displayState);
  document.querySelectorAll("#robot-steps li").forEach((item, index) => {
    item.classList.toggle("done", index < current);
    item.classList.toggle("current", index === current);
    item.classList.toggle("alert", index === current && displayState === "alert");
  });

  $("#robot-x").textContent = robot.x === null ? "-" : robot.x.toFixed(2);
  $("#robot-y").textContent = robot.y === null ? "-" : robot.y.toFixed(2);
  $("#robot-heading").textContent =
    robot.yaw === null ? "-" : `${Math.round(((robot.yaw * 180) / Math.PI + 360) % 360)}°`;
}

function robotSubtitle(robot) {
  if (robot.state === "suspect" || robot.state === "approach") {
    return `이전 상태 수신: ${LEGACY_ROBOT_STATE_LABELS[robot.state]}`;
  }
  switch (robotDisplayState(robot.state)) {
    case "patrol":
      return robot.current_waypoint === null ? "순찰 경로 이동 중" : `${robot.current_waypoint}번 지점으로 이동 중`;
    case "observe":
      return "움직임 관찰 중";
    case "alert":
      return "확인이 필요한 알림 발생";
    default:
      return "대기 중";
  }
}

function renderRobotAge() {
  if (!state.robot) return;
  const seconds = (performance.now() - state.robotChangedAt) / 1000;
  $("#robot-age").textContent = seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m`;
}

function renderEvents() {
  const key = JSON.stringify(state.events);
  if (key === state.eventsKey) return;
  state.eventsKey = key;
  renderToasts();
  renderFilters();
  renderRows();
  renderDetail();
}

// 처리 필요 알림은 먼저 생긴 것부터 (나중 알림 때문에 먼저 쓰러진 사람이 밀리지 않게)
function oldestFirst(a, b) {
  return Date.parse(a.created_at) - Date.parse(b.created_at);
}

function renderToasts() {
  const alerts = state.events.filter((event) => ALERT_STATUSES.has(event.status)).sort(oldestFirst);
  const visible = alerts.filter((event) => !state.dismissedToasts.has(event.id));
  const hiddenCount = visible.length - CONFIG.toastMaxItems;
  const more =
    hiddenCount > 0
      ? `<button class="toast-more" data-filter="alert">처리 필요 알림 ${hiddenCount}건 더 보기</button>`
      : "";
  $("#toasts").innerHTML =
    more +
    visible
      .slice(0, CONFIG.toastMaxItems)
      .map((event) => {
        const fresh = state.shownToasts.has(event.id) ? "" : " toast-new";

        const thumb = event.snapshot_url
          ? `<img class="toast-thumb" src="${event.snapshot_url}" alt="">`
          : `<span class="toast-thumb"></span>`;
        return `
        <section class="toast toast-${event.status}${fresh}" role="alert">
          <header class="toast-head">
            <span class="toast-pulse"></span>
            <span class="toast-heading"></span>
            <span class="toast-age" data-since="${event.created_at}"></span>
            <button class="toast-close" data-dismiss="${event.id}" aria-label="알림 닫기">✕</button>
          </header>
          <div class="toast-main" data-open="${event.id}">
            ${thumb}
            <div class="toast-body">
              <strong>${STATUS_LABELS[event.status]} <span>#${event.id}</span></strong>
              <span class="toast-meta">${formatTime(event.created_at)} · 위치 ${formatPosition(event.x, event.y)}</span>
              <span class="toast-meta">신뢰도 ${event.confidence === null ? "-" : `${Math.round(event.confidence * 100)}%`} · 눌러서 사진 보기</span>
            </div>
          </div>
          <div class="actions">${actionButtons(event)}</div>
        </section>`;
      })
      .join("");
  visible.slice(0, CONFIG.toastMaxItems).forEach((event) => state.shownToasts.add(event.id));
  renderToastAge();
  $(".events-card").classList.toggle("has-alerts", alerts.length > 0);
  document.title = alerts.length ? `(${alerts.length}) 낙상 의심 감지` : "낙상 의심 감지";
}

// 팝업의 경과 시간은 알림 내용이 그대로여도 매초 갱신한다
function renderToastAge() {
  for (const age of document.querySelectorAll(".toast-age")) {
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(age.dataset.since)) / 1000));
    const minutes = Math.floor(seconds / 60);
    age.textContent =
      seconds < 60 ? `${seconds}초 경과` : minutes < 10 ? `${minutes}분 ${seconds % 60}초 경과` : `${minutes}분 경과`;
    age.closest(".toast").querySelector(".toast-heading").textContent =
      seconds < CONFIG.toastFreshSeconds ? "새 알림" : "처리 대기 중인 알림";
  }
}

function renderFilters() {
  $("#filters").innerHTML = FILTERS.map((filter) => {
    const count = filter.statuses
      ? state.events.filter((event) => filter.statuses.includes(event.status)).length
      : state.events.length;
    const active = filter.key === state.filter ? " active" : "";
    const urgent = filter.key === "alert" && count > 0 ? " tab-alert" : "";
    return `<button class="tab${active}${urgent}" data-filter="${filter.key}">${filter.label} ${count}</button>`;
  }).join("");
}

function renderRows() {
  const filter = FILTERS.find((item) => item.key === state.filter);
  // 처리 필요 이벤트는 맨 위에 먼저 생긴 순으로, 나머지 기록은 그 아래 최신순으로 둔다
  const filtered = filter.statuses
    ? state.events.filter((event) => filter.statuses.includes(event.status))
    : state.events;
  const events = [
    ...filtered.filter((event) => ALERT_STATUSES.has(event.status)).sort(oldestFirst),
    ...filtered.filter((event) => !ALERT_STATUSES.has(event.status)),
  ];
  $("#events-empty").hidden = events.length > 0;
  $("#event-rows").innerHTML = events
    .map(
      (event) => `
        <tr data-open="${event.id}"${ALERT_STATUSES.has(event.status) ? ` class="row-alert row-${event.status}"` : ""}>
          <td>${event.snapshot_url ? `<img class="thumb" src="${event.snapshot_url}" alt="" loading="lazy">` : ""}</td>
          <td>${formatTime(event.created_at)}</td>
          <td>${badge(event.status)}</td>
          <td>${formatPosition(event.x, event.y)}</td>
          <td>${event.confidence === null ? "-" : `${Math.round(event.confidence * 100)}%`}</td>
        </tr>`,
    )
    .join("");
}

function selectedEvent() {
  return state.events.find((event) => event.id === state.selectedId);
}

function renderShots(event) {
  const byKind = Object.fromEntries(event.snapshots.map((shot) => [shot.kind, shot]));
  return Object.entries(SNAPSHOT_LABELS)
    .map(([kind, label]) => {
      const shot = byKind[kind];
      if (!shot) return `<div class="shot shot-empty"><span>${label}</span><em>대기</em></div>`;
      const active = shot.url === state.detailPhoto ? " active" : "";
      return `<button class="shot${active}" data-shot="${shot.url}">
          <img src="${shot.url}" alt=""><span>${label}</span><em>${kstClock.format(new Date(shot.created_at))}</em>
        </button>`;
    })
    .join("");
}

function renderDetail() {
  const event = selectedEvent();
  if (!event || !detailDialog.open) return;
  if (!event.snapshots.some((shot) => shot.url === state.detailPhoto)) state.detailPhoto = event.snapshot_url;
  $("#detail-body").innerHTML = `
    <h2>이벤트 #${event.id} ${badge(event.status)}</h2>
    ${state.detailPhoto ? `<img id="detail-photo" class="detail-photo" src="${state.detailPhoto}" alt="스냅샷">` : ""}
    <div class="shots">${renderShots(event)}</div>
    <dl class="detail-fields">
      <dt>발생</dt><dd>${formatTime(event.created_at)}</dd>
      <dt>위치</dt><dd>${formatPosition(event.x, event.y)}</dd>
      <dt>신뢰도</dt><dd>${event.confidence === null ? "-" : `${Math.round(event.confidence * 100)}%`}</dd>
      <dt>확인</dt><dd>${formatTime(event.acknowledged_at)}</dd>
      <dt>조치 완료</dt><dd>${formatTime(event.resolved_at)}</dd>
    </dl>
    <div class="history">
      <span class="eyebrow">History</span>
      <ol id="detail-history" class="history-list"><li class="history-empty">불러오는 중…</li></ol>
    </div>`;
  $("#detail-actions").innerHTML = actionButtons(event);
  loadHistory(event.id);
}

async function loadHistory(eventId) {
  let page;
  try {
    page = await api(`/logs?event_id=${eventId}&limit=100`);
  } catch {
    return;
  }
  const list = $("#detail-history");
  if (!list || state.selectedId !== eventId) return;
  list.innerHTML = page.entries.length
    ? page.entries
        .map((entry) => {
          const { tone, message } = formatLog(entry);
          return `<li><time>${formatTime(entry.created_at)}</time><i class="tone-${tone}"></i><span>${escapeHtml(message)}</span></li>`;
        })
        .join("")
    : `<li class="history-empty">기록 없음</li>`;
}

function openDetail(id) {
  if (state.selectedId !== id) state.detailPhoto = null;
  state.selectedId = id;
  const event = selectedEvent();
  if (!event) return;
  $("#detail-note").value = event.note ?? "";
  if (!detailDialog.open) detailDialog.showModal();
  renderDetail();
}

async function loadMap() {
  try {
    state.map = await api("/map");
  } catch {
    $("#map-empty").textContent = "지도 없음 (maps/map.png, map.yaml 필요)";
    setTimeout(loadMap, 10000);
    return;
  }
  const image = new Image();
  image.onload = () => {
    const buffer = document.createElement("canvas");
    buffer.width = image.width;
    buffer.height = image.height;
    const bufferCtx = buffer.getContext("2d");
    bufferCtx.drawImage(image, 0, 0);
    state.mapPixels = bufferCtx.getImageData(0, 0, image.width, image.height).data;
    state.mapLayer = themedMapLayer(image.width, image.height, state.mapPixels);
    state.mapImage = image;
    $("#map-empty").hidden = true;
    resizeCanvas();
  };
  image.src = state.map.image_url;
}

function themedMapLayer(width, height, pixels) {
  const layer = document.createElement("canvas");
  layer.width = width;
  layer.height = height;
  const layerCtx = layer.getContext("2d");
  const output = layerCtx.createImageData(width, height);
  const free = hexToRgb(COLORS["map-free"]);
  const wall = hexToRgb(COLORS["map-wall"]);
  for (let i = 0; i < pixels.length; i += 4) {
    const color = pixels[i] > 250 ? free : pixels[i] < 100 ? wall : null;
    if (color) output.data.set([...color, 255], i);
  }
  layerCtx.putImageData(output, 0, 0);
  return layer;
}

function mapViewSize() {
  const { width, height } = state.mapImage;
  const angle = (CONFIG.mapRotation * Math.PI) / 180;
  const cos = Math.abs(Math.cos(angle));
  const sin = Math.abs(Math.sin(angle));
  return [width * cos + height * sin, width * sin + height * cos];
}

function mapMatrix(scale) {
  const { width, height } = state.mapImage;
  const [viewWidth, viewHeight] = mapViewSize();
  return new DOMMatrix()
    .translate((viewWidth * scale) / 2, (viewHeight * scale) / 2)
    .rotate(CONFIG.mapRotation)
    .scale(scale)
    .translate(-width / 2, -height / 2);
}

function resizeCanvas() {
  if (!state.mapImage) return;
  const ratio = window.devicePixelRatio || 1;
  const wrap = $("#map-wrap");
  const [viewWidth, viewHeight] = mapViewSize();
  const fit = Math.min(wrap.clientWidth / viewWidth, wrap.clientHeight / viewHeight);
  const width = viewWidth * fit;
  const height = viewHeight * fit;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  state.viewLayer = buildViewLayer();
}

// 회전된 지도를 한 번만 그려 두고 매 프레임 그대로 복사한다.
// 축소를 먼저 끝낸 뒤 회전해야 벽 가장자리가 계단처럼 끊기지 않는다.
function buildViewLayer() {
  const { width, height } = state.mapImage;
  const scale = canvas.width / mapViewSize()[0];
  let source = state.mapLayer;
  while (scale > 0 && source.width / 2 > width * scale) {
    const half = document.createElement("canvas");
    half.width = Math.ceil(source.width / 2);
    half.height = Math.ceil(source.height / 2);
    const halfCtx = half.getContext("2d");
    halfCtx.imageSmoothingQuality = "high";
    halfCtx.drawImage(source, 0, 0, half.width, half.height);
    source = half;
  }

  const layer = document.createElement("canvas");
  layer.width = canvas.width;
  layer.height = canvas.height;
  const layerCtx = layer.getContext("2d");
  layerCtx.imageSmoothingQuality = "high";
  layerCtx.setTransform(mapMatrix(scale));
  layerCtx.drawImage(source, 0, 0, width, height);
  layerCtx.setTransform(1, 0, 0, 1, 0, 0);
  drawGrid(layerCtx, scale / state.map.resolution);
  return layer;
}

// 격자는 화면 기준으로 반듯하게 1m 간격으로 그린다.
function drawGrid(target, step) {
  const { width, height } = target.canvas;
  target.save();
  target.globalCompositeOperation = "source-atop";
  target.strokeStyle = "rgb(255 255 255 / 0.05)";
  target.lineWidth = window.devicePixelRatio || 1;
  target.beginPath();
  for (let x = (width / 2) % step; x < width; x += step) {
    target.moveTo(x, 0);
    target.lineTo(x, height);
  }
  for (let y = (height / 2) % step; y < height; y += step) {
    target.moveTo(0, y);
    target.lineTo(width, y);
  }
  target.stroke();
  target.restore();
}

// map.yaml origin의 yaw만큼 돌아간 지도 그림 기준으로 월드 좌표를 픽셀로 바꾼다.
function toPixel(x, y) {
  const { resolution, origin_x, origin_y, origin_yaw = 0 } = state.map;
  const dx = x - origin_x;
  const dy = y - origin_y;
  const cos = Math.cos(origin_yaw);
  const sin = Math.sin(origin_yaw);
  return [(dx * cos + dy * sin) / resolution, state.mapImage.height - (dy * cos - dx * sin) / resolution];
}

function updateView(dt) {
  const robot = state.robot;
  if (!robot || robot.x === null || robot.y === null) return;
  const target = { x: robot.x, y: robot.y, yaw: robot.yaw ?? 0 };
  if (!state.view || Math.hypot(target.x - state.view.x, target.y - state.view.y) > 2) {
    state.view = { ...target };
    return;
  }
  const k = 1 - Math.exp(-dt * 12);
  const yawDiff = Math.atan2(Math.sin(target.yaw - state.view.yaw), Math.cos(target.yaw - state.view.yaw));
  state.view.x += (target.x - state.view.x) * k;
  state.view.y += (target.y - state.view.y) * k;
  state.view.yaw += yawDiff * k;
}

function isFreePixel(x, y) {
  const { width, height } = state.mapImage;
  const col = Math.floor(x);
  const row = Math.floor(y);
  if (col < 0 || row < 0 || col >= width || row >= height) return false;
  return state.mapPixels[(row * width + col) * 4] > 250;
}

function castRay(x, y, angle, range) {
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  let distance = 0;
  while (distance < range && isFreePixel(x + cos * distance, y + sin * distance)) {
    distance += 0.5;
  }
  return [x + cos * distance, y + sin * distance];
}

function eventColor(event) {
  if (EVENT_TONES[event.status]) return COLORS[EVENT_TONES[event.status]];
  return ACTIVE_STATUSES.has(event.status) ? COLORS.active : COLORS.done;
}

function visibleMapEvents() {
  const cutoff = Date.now() - CONFIG.doneEventHours * 3600 * 1000;
  return state.events.filter(
    (event) =>
      event.x !== null &&
      event.y !== null &&
      event.status !== "false_alarm" &&
      (ACTIVE_STATUSES.has(event.status) || Date.parse(event.created_at) >= cutoff),
  );
}

function drawMap(now) {
  requestAnimationFrame(drawMap);
  const dt = state.lastFrame ? (now - state.lastFrame) / 1000 : 0;
  state.lastFrame = now;
  if (!state.mapImage) return;

  updateView(dt);
  const ratio = window.devicePixelRatio || 1;
  const scale = canvas.width / mapViewSize()[0];
  const px = ratio / scale;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (state.viewLayer) ctx.drawImage(state.viewLayer, 0, 0);
  ctx.setTransform(mapMatrix(scale));

  const pulse = 0.5 + 0.5 * Math.sin(now / 260);
  const events = visibleMapEvents().sort((a, b) => ACTIVE_STATUSES.has(a.status) - ACTIVE_STATUSES.has(b.status));
  for (const event of events) {
    const [x, y] = toPixel(event.x, event.y);
    const color = eventColor(event);
    const active = ACTIVE_STATUSES.has(event.status);
    if (event.status === "confirmed") {
      ctx.beginPath();
      ctx.arc(x, y, (6 + 10 * pulse) * px, 0, Math.PI * 2);
      ctx.fillStyle = withAlpha(color, 0.25 * (1 - pulse));
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(x, y, (active ? 4.5 : 3) * px, 0, Math.PI * 2);
    ctx.fillStyle = active ? color : withAlpha(color, 0.85);
    ctx.shadowColor = color;
    ctx.shadowBlur = active ? 12 * ratio : 0;
    ctx.fill();
    ctx.shadowBlur = 0;
    if (event.id === state.selectedId && detailDialog.open) {
      ctx.beginPath();
      ctx.arc(x, y, 9 * px, 0, Math.PI * 2);
      ctx.lineWidth = 1.5 * px;
      ctx.strokeStyle = color;
      ctx.stroke();
    }
  }

  if (!state.view) return;
  const [rx, ry] = toPixel(state.view.x, state.view.y);
  const robotColor = robotIsStale() ? COLORS.done : COLORS.robot;
  const heading = (state.map.origin_yaw ?? 0) - state.view.yaw;
  const halfFov = ((CONFIG.cameraFovDeg / 2) * Math.PI) / 180;
  const rangePx = CONFIG.viewRangeM / state.map.resolution;
  const rays = 48;

  ctx.beginPath();
  ctx.moveTo(rx, ry);
  for (let i = 0; i <= rays; i++) {
    const [ex, ey] = castRay(rx, ry, heading - halfFov + (2 * halfFov * i) / rays, rangePx);
    ctx.lineTo(ex, ey);
  }
  ctx.closePath();
  const beam = ctx.createRadialGradient(rx, ry, 0, rx, ry, rangePx);
  beam.addColorStop(0, withAlpha(robotColor, 0.38));
  beam.addColorStop(1, withAlpha(robotColor, 0.02));
  ctx.fillStyle = beam;
  ctx.fill();

  ctx.beginPath();
  ctx.arc(rx, ry, Math.max(CONFIG.robotRadiusM / state.map.resolution, 7 * px), 0, Math.PI * 2);
  ctx.fillStyle = withAlpha(robotColor, 0.18);
  ctx.fill();
  ctx.lineWidth = 1.5 * px;
  ctx.strokeStyle = withAlpha(robotColor, 0.8);
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(rx, ry, 3.5 * px, 0, Math.PI * 2);
  ctx.fillStyle = robotColor;
  ctx.shadowColor = robotColor;
  ctx.shadowBlur = 16 * ratio;
  ctx.fill();
  ctx.shadowBlur = 0;
}

function eventAtCanvasPoint(clientX, clientY) {
  if (!state.mapImage) return null;
  const rect = canvas.getBoundingClientRect();
  const scale = rect.width / mapViewSize()[0];
  const point = mapMatrix(scale).inverse().transformPoint(new DOMPoint(clientX - rect.left, clientY - rect.top));
  const mx = point.x;
  const my = point.y;
  let best = null;
  let bestDistance = 12 / scale;
  for (const event of visibleMapEvents()) {
    const [x, y] = toPixel(event.x, event.y);
    const distance = Math.hypot(x - mx, y - my);
    if (distance < bestDistance) {
      best = event;
      bestDistance = distance;
    }
  }
  return best;
}

function setLive(live) {
  const badge = $("#live-badge");
  badge.className = `live ${live ? "live-on" : "live-off"}`;
  badge.lastElementChild.textContent = live ? "LIVE" : "OFFLINE";
  $("#video-empty").hidden = live;
  $("#video").style.visibility = live ? "visible" : "hidden";
}

function startVideo() {
  const video = $("#video");
  const host = `${location.hostname}:${CONFIG.videoPort}`;
  const url = `${location.protocol}//${host}/stream?topic=${CONFIG.videoTopic}&type=${CONFIG.videoType}`;
  $("#camera-topic").textContent = CONFIG.videoTopic;
  $("#video-hint").textContent = host;
  const connect = () => {
    video.src = `${url}&t=${Date.now()}`;
  };
  video.addEventListener("error", () => {
    setLive(false);
    $("#video-message").textContent = "카메라 신호 없음";
    $("#video-hint").textContent = `web_video_server ${host} 확인`;
    setTimeout(connect, 5000);
  });
  setInterval(() => setLive(video.naturalWidth > 0 && video.complete !== false), 1000);
  connect();
}

const kstClock = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
const kstDate = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" });

function renderClock() {
  const now = new Date();
  const time = kstClock.format(now);
  $("#clock").textContent = `KST ${time}`;
  $("#camera-time").textContent = `${kstDate.format(now)} ${time}`;
}

const logState = { loaded: false, lastId: 0 };

function formatLog(entry) {
  const d = entry.detail;
  const id = entry.event_id;
  const at = d.x === null || d.x === undefined ? "" : ` @ ${formatPosition(d.x, d.y)}`;
  switch (entry.kind) {
    case "robot_state":
      return {
        tag: "ROBOT",
        tone: d.after === "alert" ? "alert" : "robot",
        message: `로봇 ${robotLogLabel(d.before)} → ${robotLogLabel(d.after)}${at}`,
      };
    case "ignore_check":
      return d.ignored
        ? { tag: "CHECK", tone: "check", message: `무시 구역 확인${at} → 무시 (이벤트 #${id})` }
        : { tag: "CHECK", tone: "ok", message: `무시 구역 확인${at} → 접근 허용` };
    case "event_created": {
      const confidence = d.confidence === null ? "" : ` · 신뢰도 ${Math.round(d.confidence * 100)}%`;
      const photos = d.snapshots?.length ? ` · 사진 ${d.snapshots.length}장` : "";
      return { tag: "EVENT", tone: "event", message: `이벤트 #${id} 생성 · ${STATUS_LABELS[d.status]}${at}${confidence}${photos}` };
    }
    case "event_snapshot": {
      return { tag: "PHOTO", tone: "check", message: `이벤트 #${id} 사진 추가 · ${SNAPSHOT_LABELS[d.kind] ?? d.kind}` };
    }
    case "event_status": {
      const tone = { confirmed: "alert", uncertain: "warn", resolved: "ok" }[d.after] ?? "event";
      const tag = { alert: "ALERT", warn: "WARN" }[tone] ?? "EVENT";
      return { tag, tone, message: `이벤트 #${id} ${STATUS_LABELS[d.before]} → ${STATUS_LABELS[d.after]}` };
    }
    case "drive_command":
      return d.after === "run"
        ? { tag: "DRIVE", tone: "ok", message: "웹에서 주행 시작 명령" }
        : { tag: "DRIVE", tone: "alert", message: "웹에서 주행 정지 명령" };
    case "event_note":
      return { tag: "NOTE", tone: "check", message: d.note ? `이벤트 #${id} 메모 · "${d.note}"` : `이벤트 #${id} 메모 삭제` };
    default:
      return { tag: entry.kind.toUpperCase(), tone: "check", message: JSON.stringify(d) };
  }
}

function appendLogs(entries, fresh) {
  const list = $("#log");
  const follow = $("#log-follow").checked;
  if (entries.length) list.querySelector(".empty-log")?.remove();
  for (const entry of entries) {
    const { tag, tone, message } = formatLog(entry);
    const item = document.createElement("li");
    if (fresh) item.className = "fresh";
    item.innerHTML = `<time>${kstClock.format(new Date(entry.created_at))}</time><span class="tag tag-${tone}">${tag}</span><span class="msg">${escapeHtml(message)}</span>`;
    list.append(item);
  }
  while (list.children.length > CONFIG.logMaxLines) list.firstElementChild.remove();
  if (follow && entries.length) list.scrollTop = list.scrollHeight;
}

function showEmptyLog() {
  $("#log").innerHTML = `<li class="empty-log">로그 대기 중…</li>`;
}

async function pollLogs() {
  let page;
  try {
    page = await api(`/logs?after=${logState.lastId}&limit=200`);
  } catch {
    return;
  }
  if (page.last_id < logState.lastId) {
    logState.lastId = 0;
    showEmptyLog();
    return;
  }
  const firstLoad = !logState.loaded;
  logState.loaded = true;
  if (page.entries.length) logState.lastId = page.entries[page.entries.length - 1].id;
  appendLogs(page.entries, !firstLoad);
}

document.addEventListener("click", async (e) => {
  const actionButton = e.target.closest("[data-action]");
  if (actionButton) {
    e.stopPropagation();
    const id = Number(actionButton.dataset.id);
    actionButton.disabled = true;
    try {
      await updateEvent(id, { status: ACTIONS[actionButton.dataset.action].status });
    } catch (error) {
      alert(`변경 실패: ${error.message}`);
      actionButton.disabled = false;
    }
    return;
  }
  const shotButton = e.target.closest("[data-shot]");
  if (shotButton) {
    state.detailPhoto = shotButton.dataset.shot;
    $("#detail-photo").src = state.detailPhoto;
    document.querySelectorAll(".shot").forEach((shot) => shot.classList.toggle("active", shot === shotButton));
    return;
  }
  const driveButton = e.target.closest("[data-drive]");
  if (driveButton) {
    sendDrive(driveButton.dataset.drive);
    return;
  }
  const dismissButton = e.target.closest("[data-dismiss]");
  if (dismissButton) {
    state.dismissedToasts.add(Number(dismissButton.dataset.dismiss));
    renderToasts();
    return;
  }
  const filterButton = e.target.closest("[data-filter]");
  if (filterButton) {
    state.filter = filterButton.dataset.filter;
    renderFilters();
    renderRows();
    $(".events-card .scroll").scrollTop = 0;
    return;
  }
  const openTarget = e.target.closest("[data-open]");
  if (openTarget) openDetail(Number(openTarget.dataset.open));
});

$("#note-save").addEventListener("click", async () => {
  const button = $("#note-save");
  button.disabled = true;
  try {
    await updateEvent(state.selectedId, { note: $("#detail-note").value.trim() || null });
    button.textContent = "저장됨";
    setTimeout(() => (button.textContent = "메모 저장"), 1200);
  } catch (error) {
    alert(`저장 실패: ${error.message}`);
  } finally {
    button.disabled = false;
  }
});

detailDialog.addEventListener("click", (e) => {
  if (e.target === detailDialog) detailDialog.close();
});

canvas.addEventListener("click", (e) => {
  const event = eventAtCanvasPoint(e.clientX, e.clientY);
  if (event) openDetail(event.id);
});

$("#log-clear").addEventListener("click", showEmptyLog);

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light") root.dataset.theme = "light";
  else delete root.dataset.theme;
  const label = theme === "light" ? "다크 모드로 전환" : "라이트 모드로 전환";
  $("#theme-toggle").setAttribute("aria-label", label);
  $("#theme-toggle").title = label;
  readColors();
  if (state.mapPixels) {
    state.mapLayer = themedMapLayer(state.mapImage.width, state.mapImage.height, state.mapPixels);
    resizeCanvas();
  }
}

$("#theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  try {
    localStorage.setItem("theme", next);
  } catch {}
  applyTheme(next);
});
applyTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");

new ResizeObserver(resizeCanvas).observe($("#map-wrap"));

showEmptyLog();
renderClock();
setInterval(renderClock, 1000);
setInterval(renderToastAge, 1000);
setInterval(renderRobotAge, 200);
poll(pollStatus, CONFIG.statusIntervalMs);
poll(pollEvents, CONFIG.eventsIntervalMs);
poll(pollLogs, CONFIG.logsIntervalMs);
poll(pollControl, CONFIG.controlIntervalMs);
loadMap();
startVideo();
requestAnimationFrame(drawMap);
