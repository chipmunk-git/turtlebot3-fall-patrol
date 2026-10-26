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
  bannerMaxItems: 3,
  logsIntervalMs: 700,
  logMaxLines: 300,
};

const STATUS_LABELS = {
  observing: "관찰 중",
  confirmed: "낙상 의심",
  uncertain: "판단 불가",
  false_alarm: "오탐",
  acknowledged: "확인됨",
  resolved: "처리 완료",
};

const ROBOT_STATE_LABELS = {
  idle: "대기",
  patrol: "순찰 중",
  suspect: "의심 감지",
  approach: "접근 중",
  observe: "관찰 중",
  alert: "알림",
};

const ROBOT_STEPS = ["patrol", "suspect", "approach", "observe", "alert"];

const SNAPSHOT_LABELS = { detected: "① 발견", observing: "② 관찰 시작", decision: "③ 판단" };

const ACTIONS = {
  acknowledge: { label: "확인", status: "acknowledged", style: "btn-primary" },
  confirm: { label: "낙상 의심으로 변경", status: "confirmed", style: "btn-danger" },
  false_alarm: { label: "오탐", status: "false_alarm", style: "" },
  resolve: { label: "처리 완료", status: "resolved", style: "btn-primary" },
};

const ACTIONS_BY_STATUS = {
  observing: ["false_alarm"],
  confirmed: ["acknowledge", "false_alarm"],
  uncertain: ["confirm", "false_alarm"],
  acknowledged: ["resolve"],
  resolved: [],
  false_alarm: [],
};

const FILTERS = [
  { key: "all", label: "전체", statuses: null },
  { key: "alert", label: "처리 필요", statuses: ["confirmed", "uncertain"] },
  { key: "observing", label: "관찰 중", statuses: ["observing"] },
  { key: "acknowledged", label: "확인됨", statuses: ["acknowledged"] },
  { key: "resolved", label: "처리 완료", statuses: ["resolved"] },
  { key: "false_alarm", label: "오탐", statuses: ["false_alarm"] },
];

const ALERT_STATUSES = new Set(["confirmed", "uncertain"]);
const ACTIVE_STATUSES = new Set(["observing", "confirmed", "uncertain", "acknowledged"]);
const EVENT_TONES = { confirmed: "confirmed", uncertain: "uncertain" };

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

const COLORS = Object.fromEntries(
  ["robot", "confirmed", "uncertain", "active", "done", "map-free", "map-wall"].map((name) => [name, cssVar(`--${name}`)]),
);

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
  $("#robot-hero").className = `robot-hero state-${offline ? "offline" : robot.state}`;
  $("#robot-state").textContent = offline ? "연결 끊김" : ROBOT_STATE_LABELS[robot.state] ?? robot.state;
  $("#robot-sub").textContent = offline ? "마지막으로 받은 위치를 표시 중" : robotSubtitle(robot);

  const current = offline ? -1 : ROBOT_STEPS.indexOf(robot.state);
  document.querySelectorAll("#robot-steps li").forEach((item, index) => {
    item.classList.toggle("done", index < current);
    item.classList.toggle("current", index === current);
    item.classList.toggle("alert", index === current && robot.state === "alert");
  });

  $("#robot-x").textContent = robot.x === null ? "-" : robot.x.toFixed(2);
  $("#robot-y").textContent = robot.y === null ? "-" : robot.y.toFixed(2);
  $("#robot-heading").textContent =
    robot.yaw === null ? "-" : `${Math.round(((robot.yaw * 180) / Math.PI + 360) % 360)}°`;
}

function robotSubtitle(robot) {
  switch (robot.state) {
    case "patrol":
      return robot.current_waypoint === null ? "순찰 경로 이동 중" : `${robot.current_waypoint}번 지점으로 이동 중`;
    case "suspect":
      return "사람 감지, 무시 구역 확인 중";
    case "approach":
      return "대상에게 접근 중";
    case "observe":
      return "움직임 관찰 중";
    case "alert":
      return "낙상 의심 알림 발생";
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
  renderBanner();
  renderFilters();
  renderRows();
  renderDetail();
}

function renderBanner() {
  const alerts = state.events.filter((event) => ALERT_STATUSES.has(event.status));
  const banner = $("#banner");
  banner.hidden = alerts.length === 0;
  const hiddenCount = alerts.length - CONFIG.bannerMaxItems;
  const more =
    hiddenCount > 0
      ? `<button class="alert-more" data-filter="alert" data-scroll="events">외 ${hiddenCount}건 더 보기</button>`
      : "";
  banner.innerHTML =
    alerts
      .slice(0, CONFIG.bannerMaxItems)
      .map(
      (event) => `
        <div class="alert alert-${event.status}" data-open="${event.id}">
          <span class="alert-title">${STATUS_LABELS[event.status]}</span>
          <span class="alert-meta">#${event.id} · ${formatTime(event.created_at)} · 위치 ${formatPosition(event.x, event.y)}</span>
          <div class="actions">${actionButtons(event)}</div>
        </div>`,
      )
      .join("") + more;
  document.title = alerts.length ? `(${alerts.length}) 낙상 의심 감지` : "낙상 의심 감지";
}

function renderFilters() {
  $("#filters").innerHTML = FILTERS.map((filter) => {
    const count = filter.statuses
      ? state.events.filter((event) => filter.statuses.includes(event.status)).length
      : state.events.length;
    const active = filter.key === state.filter ? " active" : "";
    return `<button class="tab${active}" data-filter="${filter.key}">${filter.label} ${count}</button>`;
  }).join("");
}

function renderRows() {
  const filter = FILTERS.find((item) => item.key === state.filter);
  const events = filter.statuses
    ? state.events.filter((event) => filter.statuses.includes(event.status))
    : state.events;
  $("#events-empty").hidden = events.length > 0;
  $("#event-rows").innerHTML = events
    .map(
      (event) => `
        <tr data-open="${event.id}">
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
      <dt>처리 완료</dt><dd>${formatTime(event.resolved_at)}</dd>
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
  return CONFIG.mapRotation === 90 ? [height, width] : [width, height];
}

function mapMatrix(scale, canvasWidth) {
  return CONFIG.mapRotation === 90
    ? new DOMMatrix([0, scale, -scale, 0, canvasWidth, 0])
    : new DOMMatrix([scale, 0, 0, scale, 0, 0]);
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
}

function drawGrid(px) {
  const { resolution, origin_x, origin_y } = state.map;
  const { width, height } = state.mapImage;
  const step = 1 / resolution;
  ctx.save();
  ctx.globalCompositeOperation = "source-atop";
  ctx.strokeStyle = "rgb(255 255 255 / 0.05)";
  ctx.lineWidth = px;
  ctx.beginPath();
  for (let x = (Math.ceil(origin_x) - origin_x) / resolution; x < width; x += step) {
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
  }
  for (let y = height - (Math.ceil(origin_y) - origin_y) / resolution; y > 0; y -= step) {
    ctx.moveTo(0, y);
    ctx.lineTo(width, y);
  }
  ctx.stroke();
  ctx.restore();
}

function toPixel(x, y) {
  const { resolution, origin_x, origin_y } = state.map;
  return [(x - origin_x) / resolution, state.mapImage.height - (y - origin_y) / resolution];
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
  ctx.setTransform(mapMatrix(scale, canvas.width));
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(state.mapLayer, 0, 0);
  drawGrid(px);

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
    ctx.fillStyle = active ? color : withAlpha(color, 0.7);
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
  const heading = -state.view.yaw;
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
  const point = mapMatrix(scale, rect.width).inverse().transformPoint(new DOMPoint(clientX - rect.left, clientY - rect.top));
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
        message: `로봇 ${ROBOT_STATE_LABELS[d.before]} → ${ROBOT_STATE_LABELS[d.after]}${at}`,
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
  const filterButton = e.target.closest("[data-filter]");
  if (filterButton) {
    state.filter = filterButton.dataset.filter;
    renderFilters();
    renderRows();
    if (filterButton.dataset.scroll) $(".events-card").scrollIntoView({ behavior: "smooth" });
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

new ResizeObserver(resizeCanvas).observe($("#map-wrap"));

showEmptyLog();
renderClock();
setInterval(renderClock, 1000);
setInterval(renderRobotAge, 200);
poll(pollStatus, CONFIG.statusIntervalMs);
poll(pollEvents, CONFIG.eventsIntervalMs);
poll(pollLogs, CONFIG.logsIntervalMs);
loadMap();
startVideo();
requestAnimationFrame(drawMap);
