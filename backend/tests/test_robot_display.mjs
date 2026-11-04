// Run with: node backend/tests/test_robot_display.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

export async function runChecks() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  new vm.Script(source); // Parse the complete production script, including startup.
  const start = source.indexOf('\ndocument.addEventListener("click"');
  assert.ok(start > 0, 'dashboard startup boundary must exist');

  function declarations(selector) {
    const values = {};
    for (const match of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      if (!match[1].trim().split(',').map(s => s.trim()).includes(selector)) continue;
      for (const declaration of match[2].split(';')) {
        const colon = declaration.indexOf(':');
        if (colon > 0) values[declaration.slice(0, colon).trim()] = declaration.slice(colon + 1).trim();
      }
    }
    return values;
  }
  const variables = declarations(':root');
  function resolve(value) {
    const match = /^var\((--[^)]+)\)$/.exec(value);
    return match ? resolve(variables[match[1]]) : value;
  }
  function element() {
    const classes = new Set();
    return { className: '', textContent: '', innerHTML: '', dataset: {}, style: {}, attributes: {},
      pauseCount: 0, loadCount: 0,
      pause() { this.pauseCount++; }, load() { this.loadCount++; },
      removeAttribute(name) { delete this[name]; delete this.attributes[name]; },
      lastElementChild: { textContent: '' },
      classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
                   add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
                   contains(name) { return classes.has(name); } },
      addEventListener() {}, setAttribute(name, value) { this.attributes[name] = value; },
      querySelector(selector) { return driveButtons.find(button => selector === `[data-drive="${button.dataset.drive}"]`) ?? null; },
      querySelectorAll(selector) { return selector === '[data-drive]' ? driveButtons : []; },
      getContext() { return {}; } };
  }
  const nodes = new Map();
  const steps = Array.from({ length: 4 }, element);
  const driveButtons = ['run', 'stop'].map(drive => ({...element(), dataset: {drive}}));
  const query = selector => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  const handlers = {};
  const requests = [];
  const alerts = [];
  const frames = [];
  let popupRect = {top: 10, left: 10, bottom: 200, right: 500, width: 490, height: 190};
  let respond;
  const documentElement = {dataset: {}};
  const context = vm.createContext({
    document: { documentElement, visibilityState: 'visible', querySelector: query,
      addEventListener: (name, fn) => { handlers[name] = fn; },
      querySelectorAll: selector => selector === '#robot-steps li' ? steps
        : selector === '[data-alarm-event]'
          ? [...query('#toasts').innerHTML.matchAll(/data-alarm-event="(\d+)"/g)].map(match =>
              ({dataset: {alarmEvent: match[1]}, getBoundingClientRect: () => popupRect}))
          : [] },
    window: {innerWidth: 1280, innerHeight: 800},
    getComputedStyle: () => ({ getPropertyValue: name => variables[name] || '' }),
    performance: { now: () => 1000 },
    fetch: (url, options) => {
      requests.push(url);
      return respond ? respond(url, options) : new Promise(() => {});
    },
    alert: message => alerts.push(message),
    setInterval() {}, setTimeout() {}, requestAnimationFrame: fn => frames.push(fn),
    ResizeObserver: class { observe() {} },
    location: {hostname: 'localhost', protocol: 'http:'},
    localStorage: {setItem() {}},
  });
  // Use the actual functions with a minimal DOM; do not start network polling.
  vm.runInContext(source + `
    globalThis.displayTest = {state, renderRobot, robotSubtitle, formatLog, badge,
      actionButtons, ROBOT_STEPS, ROBOT_STATE_LABELS, STATUS_LABELS, ACTIONS_BY_STATUS,
      renderToasts, renderRows, toPixel, mapViewSize, visibleMapEvents, applyTheme,
      pollControl, sendDrive, scheduleVisibleAlarms, pollAlarms, muteEventAlarm, alarmButton,
      renderVoice, pollDetailVoice, renderDrive, pollStatus, robotCanDrive,
      visibleNote, restoreNoteMarkers, renderDetail, openDetail, appendLogs};
  `, context);
  const api = context.displayTest;
  frames.length = 0; // Do not run the unrelated map animation startup.
  const results = [];
  function check(name, fn) { fn(); results.push(name); }
  async function checkAsync(name, fn) { await fn(); results.push(name); }
  function render(value) {
    api.state.serverOnline = true;
    api.state.robotChangedAt = 1000;
    api.state.robot = { state: value, x: 1, y: 2, yaw: 0, current_waypoint: 3 };
    api.renderRobot();
  }

  check('four states render matching labels and current steps', () => {
    const states = ['idle', 'patrol', 'observe', 'alert'];
    const labels = ['대기', '순찰', '관찰', '알림'];
    states.forEach((value, index) => {
      render(value);
      assert.equal(query('#robot-state').textContent, labels[index]);
      assert.equal(query('#robot-hero').className, 'robot-hero state-' + value);
      assert.deepEqual(steps.map(item => item.classList.contains('current')), states.map((_, i) => i === index));
      assert.equal(api.state.robot.state, value);
    });
    assert.equal(query('#robot-sub').textContent, '확인이 필요한 알림 발생');
  });
  check('legacy API states display observation without rewriting received data', () => {
    for (const value of ['suspect', 'approach']) {
      render(value);
      assert.equal(query('#robot-state').textContent, '관찰');
      assert.equal(query('#robot-hero').className, 'robot-hero state-observe');
      assert.ok(steps[2].classList.contains('current'));
      assert.equal(api.state.robot.state, value);
      assert.match(query('#robot-sub').textContent, /^이전 상태 수신:/);
    }
  });
  check('historical robot logs retain legacy labels', () => {
    const record = {kind: 'robot_state', detail: {before: 'suspect', after: 'approach'}};
    const log = api.formatLog(record);
    assert.equal(log.message, '로봇 의심 감지 → 접근 중');
    assert.equal(record.detail.after, 'approach');
  });
  check('robot logs display categories and severity without changing robot or event state', () => {
    const before = JSON.stringify({robot: api.state.robot, events: api.state.events, control: api.state.control});
    const categories = {system: 'SYSTEM', drive: 'DRIVE', route: 'ROUTE', vision: 'VISION',
      event: 'EVENT', voice: 'VOICE', output: 'OUTPUT'};
    for (const [category, tag] of Object.entries(categories)) {
      for (const [level, tone] of Object.entries({info: 'check', ok: 'ok', warn: 'warn', error: 'alert'})) {
        const record = {kind: 'robot_log', detail: {category, level, source: 'patrol', message: '정지 사유'}};
        const formatted = api.formatLog(record);
        assert.equal(formatted.tag, tag);
        assert.equal(formatted.tone, tone);
        assert.equal(formatted.message, '정지 사유');
      }
    }
    assert.equal(JSON.stringify({robot: api.state.robot, events: api.state.events, control: api.state.control}), before);
  });
  check('robot log text is escaped before entering the log view', () => {
    const list = query('#log');
    list.children = [];
    list.append = item => list.children.push(item);
    context.document.createElement = element;
    api.appendLogs([{kind: 'robot_log', created_at: '2026-11-05T00:00:00Z',
      detail: {category: 'voice', level: 'info', message: '<img src=x onerror=alert(1)>'}}], true);
    assert.equal(list.children.length, 1);
    assert.doesNotMatch(list.children[0].innerHTML, /<img/);
    assert.match(list.children[0].innerHTML, /&#60;img/);
  });
  check('confidence stays in event data while its display is hidden', () => {
    assert.doesNotMatch(html, /신뢰도/);
    assert.doesNotMatch(source, /event\.confidence|d\.confidence|신뢰도/);
    for (const confidence of [null, 0, 0.75, 1]) {
      const entry = {kind: 'event_created', event_id: 7,
        detail: {status: 'confirmed', confidence}};
      const before = JSON.stringify(entry);
      const formatted = api.formatLog(entry);
      assert.match(formatted.message, /이벤트 #7 생성/);
      assert.doesNotMatch(formatted.message, /신뢰도|%/);
      assert.equal(JSON.stringify(entry), before);
    }
  });
  check('server disconnect and stale robot remain separate from blue idle', () => {
    for (const failure of ['server', 'robot']) {
      render('idle');
      if (failure === 'server') api.state.serverOnline = false;
      else api.state.robotChangedAt = -10000;
      api.renderRobot();
      assert.equal(query('#robot-hero').className, 'robot-hero state-offline');
      assert.equal(query('#robot-state').textContent, '연결 끊김');
      assert.ok(steps.every(item => !item.classList.contains('current')));
      assert.equal(api.state.robot.state, 'idle');
    }
  });
  check('unrecognized state is visible instead of becoming idle', () => {
    render('future_state');
    assert.equal(query('#robot-state').textContent, 'future_state');
    assert.ok(steps.every(item => !item.classList.contains('current')));
  });
  check('event statuses, badges and actions remain independent', () => {
    assert.deepEqual(JSON.parse(JSON.stringify(api.STATUS_LABELS)), {
      observing: '관찰 중', confirmed: '낙상 의심', uncertain: '확인 필요',
      false_alarm: '오탐', acknowledged: '확인됨', resolved: '조치 완료',
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.ACTIONS_BY_STATUS)), {
      observing: ['resolve', 'false_alarm'], confirmed: ['resolve', 'false_alarm'],
      uncertain: ['resolve', 'false_alarm'], acknowledged: ['resolve', 'false_alarm'], resolved: [], false_alarm: [],
    });
    assert.match(api.badge('observing'), /badge-observing.*관찰 중/);
    assert.match(api.actionButtons({id: 7, status: 'uncertain'}), /data-action="resolve"/);
    assert.match(api.formatLog({kind: 'event_status', event_id: 7,
      detail: {before: 'confirmed', after: 'false_alarm'}}).message, /낙상 의심 → 오탐/);
  });
  check('HTML and JavaScript expose the same four ordered states', () => {
    const list = /<ol id="robot-steps"[^>]*>([\s\S]*?)<\/ol>/.exec(html)[1];
    const actual = [...list.matchAll(/data-state="([^"]+)"/g)].map(m => m[1]);
    assert.deepEqual(actual, Array.from(api.ROBOT_STEPS));
    assert.deepEqual(actual, ['idle', 'patrol', 'observe', 'alert']);
    assert.equal(declarations('.steps')['grid-template-columns'], 'repeat(4, 1fr)');
  });
  check('hero and current step use each robot color independently of event colors', () => {
    const palette = {idle: '#5fb8ff', patrol: '#4fd1a1', observe: '#f5b94a', alert: '#ff6b81'};
    for (const [value, expected] of Object.entries(palette)) {
      assert.equal(resolve(declarations('.state-' + value)['--state']), expected);
      assert.equal(resolve(declarations('.steps li[data-state="' + value + '"]')['--glow']), expected);
    }
    assert.equal(resolve(declarations('.state-offline')['--state']), '#626b7f');
    assert.equal(variables['--active'], '#a78bfa');
    assert.equal(variables['--confirmed'], '#ff6b81');
    assert.equal(variables['--uncertain'], '#f5b94a');
    assert.equal(declarations('.badge-observing')['--tone'], 'var(--active)');
  });
  check('complete dashboard startup includes control polling and matching drive buttons', () => {
    assert.deepEqual(requests, ['/status', '/events?limit=200', '/logs?after=0&limit=200', '/control', '/events/alarms', '/map']);
    assert.deepEqual([...html.matchAll(/data-drive="([^"]+)"/g)].map(match => match[1]), ['run', 'stop']);
    assert.match(html, /aria-label="주행 제어"/);
    assert.match(source, /sendDrive\(driveButton.dataset.drive\)/);
    assert.equal(declarations('.drive')['grid-template-columns'], '1fr 1fr');
  });
  await checkAsync('drive buttons PUT run and stop without rewriting reported robot state', async () => {
    api.state.robotReporting = true;
    api.state.serverOnline = true;
    api.state.robotChangedAt = 1000;
    const before = JSON.stringify(api.state.robot);
    for (const drive of ['run', 'stop']) {
      respond = async (url, options) => {
        assert.equal(url, '/control');
        assert.equal(options.method, 'PUT');
        assert.equal(options.headers['Content-Type'], 'application/json');
        assert.deepEqual(JSON.parse(options.body), {drive});
        return {ok: true, json: async () => ({drive})};
      };
      await api.sendDrive(drive);
      assert.equal(api.state.control.drive, drive);
      assert.equal(query('#drive').dataset.current, drive);
      assert.deepEqual(driveButtons.map(button => button.attributes['aria-pressed']),
        ['run', 'stop'].map(value => String(value === drive)));
      assert.equal(query('#drive').classList.contains('busy'), false);
    }
    assert.equal(JSON.stringify(api.state.robot), before);
  });
  await checkAsync('control polling displays the command saved on the server', async () => {
    respond = async (url, options) => {
      assert.equal(url, '/control');
      assert.equal(options.method, undefined);
      return {ok: true, json: async () => ({drive: 'run'})};
    };
    await api.pollControl();
    assert.equal(query('#drive').dataset.current, 'run');
  });
  await checkAsync('failed drive update preserves the last command and releases busy state', async () => {
    respond = async () => ({ok: false, status: 503});
    await api.sendDrive('stop');
    assert.equal(api.state.control.drive, 'run');
    assert.equal(query('#drive').dataset.current, 'run');
    assert.equal(query('#drive').classList.contains('busy'), false);
    assert.match(alerts.at(-1), /주행 명령 실패: 503/);
    respond = undefined;
  });
  await checkAsync('saved robot status cannot enable run until another report arrives', async () => {
    api.state.robot = null;
    api.state.robotReporting = false;
    let updated_at = '2026-11-04T00:00:00Z';
    respond = async () => ({ok: true, json: async () => ({state: 'idle', x: null, y: null, yaw: null, updated_at})});
    await api.pollStatus();
    assert.equal(api.robotCanDrive(), false);
    assert.equal(driveButtons[0].disabled, true);
    await api.pollStatus();
    assert.equal(api.robotCanDrive(), false);
    updated_at = '2026-11-04T00:00:01Z';
    await api.pollStatus();
    assert.equal(api.robotCanDrive(), true);
    assert.equal(driveButtons[0].disabled, false);
    assert.notEqual(driveButtons[1].disabled, true);
  });
  await checkAsync('stale or disconnected robot blocks run while stop remains available', async () => {
    api.state.robotChangedAt = -1000000;
    api.renderDrive();
    assert.equal(driveButtons[0].disabled, true);
    const count = requests.length;
    await api.sendDrive('run');
    assert.equal(requests.length, count);
    respond = async () => ({ok: true, json: async () => ({drive: 'stop'})});
    await api.sendDrive('stop');
    assert.equal(requests.length, count + 1);
    api.state.robotChangedAt = 1000;
    respond = async () => ({ok: false, status: 503});
    await api.pollStatus();
    assert.equal(api.robotCanDrive(), false);
    assert.equal(driveButtons[0].disabled, true);
    assert.notEqual(driveButtons[1].disabled, true);
    api.state.serverOnline = true;
    respond = undefined;
  });
  check('internal note markers are hidden while user and voice text remain visible', () => {
    const note = '관리자 메모\n[robot:obs-1] 대상 관찰\n[voice:obs-1] 도와주세요\n[voice:old]';
    assert.equal(api.visibleNote(note), '관리자 메모\n대상 관찰\n도와주세요');
    assert.equal(api.visibleNote(null), '');
    assert.equal(api.visibleNote('일반 [voice:obs-1] 메모'), '일반 [voice:obs-1] 메모');
    assert.doesNotMatch(api.formatLog({kind: 'event_note', event_id: 7, detail: {note}}).message, /\[(?:robot|voice):/);
  });
  check('editing or deleting generated note text preserves deduplication markers', () => {
    const original = '관리자 메모\n[robot:obs-1] 대상 관찰\n[voice:obs-1] 도와주세요';
    const unchanged = api.restoreNoteMarkers(api.visibleNote(original), original);
    assert.equal(unchanged, original);
    const edited = api.restoreNoteMarkers('수정 메모\n도와주세요', original);
    assert.match(edited, /\[voice:obs-1\] 도와주세요/);
    assert.match(edited, /\[robot:obs-1\]$/);
    assert.equal(api.visibleNote(edited), '수정 메모\n도와주세요');
    assert.equal(api.restoreNoteMarkers('', original), '[robot:obs-1]\n[voice:obs-1]');
    assert.equal(api.restoreNoteMarkers('', null), null);
  });
  const event = (id, status, created_at) => ({id, status, created_at, snapshot_url: null,
    x: 0, y: 0, confidence: null});
  check('detail uses the representative photo and shows notes without internal markers', () => {
    const record = {...event(17, 'confirmed', '2026-11-04T00:00:00Z'),
      snapshot_url: '/snapshots/main.jpg',
      snapshots: ['/snapshots/first.jpg', '/snapshots/second.jpg'],
      note: '관리자 메모\n[voice:obs-17] 도와주세요'};
    api.state.events = [record];
    query('#detail').open = true;
    api.openDetail(17);
    assert.match(query('#detail-body').innerHTML, /src="\/snapshots\/main.jpg"/);
    assert.doesNotMatch(query('#detail-body').innerHTML, /first\.jpg|second\.jpg|class="shots"/);
    assert.equal(query('#detail-note').value, '관리자 메모\n도와주세요');
    assert.equal(record.note, '관리자 메모\n[voice:obs-17] 도와주세요');
    query('#detail').open = false;
    api.state.selectedId = null;
  });
  check('popup, event rows and detail hide confidence without changing received events', () => {
    const saved = {events: api.state.events, selectedId: api.state.selectedId,
      filter: api.state.filter, shownToasts: api.state.shownToasts, voicePending: api.state.voicePending};
    const savedFrames = frames.length;
    api.state.shownToasts = new Set(saved.shownToasts);
    api.state.filter = 'all';
    query('#detail').open = true;
    try {
      for (const confidence of [null, 0, 0.75, 1]) {
        const record = {...event(77, 'confirmed', '2026-11-05T00:00:00Z'), confidence};
        const before = JSON.stringify(record);
        api.state.events = [record];
        api.renderToasts();
        api.renderRows();
        api.openDetail(77);
        assert.doesNotMatch(query('#toasts').innerHTML, /신뢰도|75%|100%/);
        assert.doesNotMatch(query('#event-rows').innerHTML, /신뢰도|75%|100%/);
        assert.equal((query('#event-rows').innerHTML.match(/<td>/g) ?? []).length, 4);
        assert.doesNotMatch(query('#detail-body').innerHTML, /신뢰도|75%|100%/);
        assert.match(query('#detail-body').innerHTML, /이벤트 #77/);
        assert.equal(JSON.stringify(record), before);
      }
    } finally {
      Object.assign(api.state, saved);
      query('#detail').open = false;
      frames.length = savedFrames;
    }
  });
  check('toasts show the oldest pending event and keep the next alert available', () => {
    api.state.events = [event(2, 'uncertain', '2026-10-31T01:01:00Z'),
      event(3, 'resolved', '2026-10-31T01:02:00Z'),
      event(1, 'confirmed', '2026-10-31T01:00:00Z')];
    const before = JSON.stringify(api.state.events);
    api.renderToasts();
    assert.match(query('#toasts').innerHTML, /data-open="1"/);
    assert.doesNotMatch(query('#toasts').innerHTML, /data-open="2"/);
    assert.match(query('#toasts').innerHTML, /처리 필요 알림 1건 더 보기/);
    assert.deepEqual(Array.from(api.state.shownToasts), [1]);
    handlers.click({target: {closest: selector => selector === '[data-dismiss]'
      ? {dataset: {dismiss: '1'}} : null}});
    assert.match(query('#toasts').innerHTML, /data-open="2"/);
    assert.match(query('#toasts').innerHTML, /toast-new/);
    assert.equal(JSON.stringify(api.state.events), before);
  });
  check('closing a toast does not close or remove its event from the list', () => {
    api.state.filter = 'all';
    api.renderRows();
    const rows = query('#event-rows').innerHTML;
    assert.ok(rows.indexOf('data-open="1"') < rows.indexOf('data-open="2"'));
    assert.ok(rows.indexOf('data-open="2"') < rows.indexOf('data-open="3"'));
    assert.equal(api.state.events.find(item => item.id === 1).status, 'confirmed');
  });
  check('world positions follow map origin rotation while zero-yaw maps remain compatible', () => {
    api.state.mapImage = {width: 200, height: 100};
    api.state.map = {origin_x: 10, origin_y: 20, resolution: 0.5};
    assert.deepEqual(Array.from(api.toPixel(11, 21)), [2, 98]);
    api.state.map.origin_yaw = Math.PI / 2;
    const pixel = Array.from(api.toPixel(9, 21));
    assert.ok(Math.abs(pixel[0] - 2) < 1e-10);
    assert.ok(Math.abs(pixel[1] - 98) < 1e-10);
  });
  check('false alarms disappear from map markers but remain in event history', () => {
    api.state.events = [event(1, 'confirmed', '2026-10-31T01:00:00Z'),
      event(2, 'false_alarm', new Date().toISOString())];
    assert.deepEqual(Array.from(api.visibleMapEvents(), item => item.id), [1]);
    assert.equal(api.state.events.length, 2);
  });
  check('theme switching preserves received robot and event data', () => {
    const before = JSON.stringify({robot: api.state.robot, events: api.state.events});
    api.applyTheme('light');
    assert.equal(documentElement.dataset.theme, 'light');
    api.applyTheme('dark');
    assert.equal(documentElement.dataset.theme, undefined);
    assert.equal(JSON.stringify({robot: api.state.robot, events: api.state.events}), before);
    assert.match(css, /:root\[data-theme="light"\]/);
    assert.match(css, /\.toasts\s*\{/);
  });
  function paintFrame() {
    frames.splice(0).forEach(fn => fn(1000));
  }
  async function settle() {
    for (let i = 0; i < 8; i++) await Promise.resolve();
  }
  function resetAlarms() {
    frames.length = 0;
    requests.length = 0;
    api.state.presentedAlarms.clear();
    api.state.presentingAlarms.clear();
    api.state.mutedAlarms.clear();
    api.state.mutingAlarms.clear();
    api.state.dismissedToasts.clear();
    api.state.alarmReady = true;
    context.document.visibilityState = 'visible';
    popupRect = {top: 10, left: 10, bottom: 200, right: 500, width: 490, height: 190};
    api.state.events = [event(7, 'confirmed', '2026-10-31T01:00:00Z'),
      event(8, 'uncertain', '2026-10-31T01:01:00Z')];
  }
  await checkAsync('only the painted popup is reported, not an event waiting behind it', async () => {
    resetAlarms();
    respond = async () => ({ok: true, json: async () => ({})});
    api.renderToasts();
    assert.deepEqual(requests, []);
    paintFrame();
    assert.deepEqual(requests, []);
    paintFrame();
    await settle();
    assert.deepEqual(requests, ['/events/7/alarm/presented']);
    api.scheduleVisibleAlarms();
    paintFrame(); paintFrame(); await settle();
    assert.equal(requests.length, 1);
  });
  await checkAsync('hidden tabs wait until the popup is visible to start an alarm', async () => {
    resetAlarms();
    respond = async () => ({ok: true, json: async () => ({})});
    context.document.visibilityState = 'hidden';
    api.renderToasts();
    paintFrame(); paintFrame(); await settle();
    assert.deepEqual(requests, []);
    context.document.visibilityState = 'visible';
    handlers.visibilitychange();
    paintFrame(); paintFrame(); await settle();
    assert.deepEqual(requests, ['/events/7/alarm/presented']);
  });
  await checkAsync('offscreen, removed and already resolved popups are not reported', async () => {
    for (const condition of ['offscreen', 'removed', 'resolved']) {
      resetAlarms();
      api.renderToasts();
      if (condition === 'offscreen') popupRect = {...popupRect, top: 900, bottom: 1000};
      if (condition === 'removed') query('#toasts').innerHTML = '';
      if (condition === 'resolved') api.state.events[0].status = 'resolved';
      paintFrame(); paintFrame(); await settle();
      assert.deepEqual(requests, [], condition);
    }
  });
  await checkAsync('failed presentation reports retry while visible without permanently marking success', async () => {
    resetAlarms();
    respond = async () => ({ok: false, status: 503});
    api.renderToasts();
    paintFrame(); paintFrame(); await settle();
    assert.equal(api.state.presentedAlarms.has(7), false);
    assert.equal(api.state.presentingAlarms.has(7), false);
    respond = async () => ({ok: true, json: async () => ({})});
    api.scheduleVisibleAlarms();
    paintFrame(); paintFrame(); await settle();
    assert.equal(api.state.presentedAlarms.has(7), true);
    assert.equal(requests.length, 2);
  });
  await checkAsync('detail mute targets only one event and preserves status, notes and drive state', async () => {
    resetAlarms();
    const before = JSON.stringify({events: api.state.events, robot: api.state.robot, control: api.state.control});
    respond = async (url, options) => {
      assert.equal(url, '/events/7/alarm/mute');
      assert.equal(options.method, 'POST');
      return {ok: true, json: async () => ({event_id: 7, muted: true, active: false})};
    };
    await handlers.click({target: {closest: selector => selector === '[data-mute-alarm]'
      ? {dataset: {muteAlarm: '7'}} : null}});
    assert.deepEqual(Array.from(api.state.mutedAlarms), [7]);
    assert.match(api.alarmButton(api.state.events[0]), /disabled.*이 사건 경보음 꺼짐/);
    assert.doesNotMatch(api.alarmButton(api.state.events[1]), /disabled/);
    assert.equal(api.alarmButton({id: 9, status: 'resolved'}), '');
    assert.equal(JSON.stringify({events: api.state.events, robot: api.state.robot, control: api.state.control}), before);
    assert.match(source, /actionButtons\(event\) \+ alarmButton\(event\)/);
  });
  await checkAsync('failed mute is visible and does not claim the event was silenced', async () => {
    resetAlarms();
    respond = async () => ({ok: false, status: 503});
    await api.muteEventAlarm(7);
    assert.equal(api.state.mutedAlarms.has(7), false);
    assert.equal(api.state.mutingAlarms.has(7), false);
    assert.match(alerts.at(-1), /경보음 끄기 실패/);
    assert.doesNotMatch(api.alarmButton(api.state.events[0]), /disabled/);
  });
  await checkAsync('a stale alarm poll cannot undo a newly confirmed mute', async () => {
    resetAlarms();
    let finish;
    respond = () => new Promise(resolve => { finish = resolve; });
    const polling = api.pollAlarms();
    respond = async () => ({ok: true, json: async () => ({muted: true})});
    await api.muteEventAlarm(7);
    finish({ok: true, json: async () => ({presented_event_ids: [7], muted_event_ids: [], active_event_ids: [7]})});
    await polling;
    assert.equal(api.state.mutedAlarms.has(7), true);
  });
  await checkAsync('shared server mute state survives another browser and allows new events', async () => {
    resetAlarms();
    respond = async () => ({ok: true, json: async () =>
      ({presented_event_ids: [7], muted_event_ids: [7], active_event_ids: []})});
    await api.pollAlarms();
    assert.match(api.alarmButton(api.state.events[0]), /이 사건 경보음 꺼짐/);
    assert.doesNotMatch(api.alarmButton(api.state.events[1]), /disabled/);
    assert.match(api.formatLog({kind: 'event_alarm_muted', event_id: 7, detail: {}}).message, /경보음 끄기/);
  });
  check('voice panel shows progress and final text separately from administrator notes', () => {
    query('#detail-note').value = '관리자가 작성 중인 메모';
    for (const [phase, label] of Object.entries({pending: '요청 대기', playback: '안내 재생 중',
      recording: '녹음 중', upload: '전송 중', recognition: '인식 중', completed: '인식 완료',
      failed: '처리 실패', cancelled: '취소됨'})) {
      api.renderVoice({phase, text: null, classification: null, audio_url: null});
      assert.equal(query('#voice-progress').textContent, label);
    }
    api.renderVoice({phase: 'completed', text: '<script>test</script>', classification: '도움 필요'});
    assert.equal(query('#voice-text').textContent, '<script>test</script>');
    assert.equal(query('#voice-text').innerHTML, '');
    assert.equal(query('#voice-classification').textContent, '도움 필요');
    assert.equal(query('#detail-note').value, '관리자가 작성 중인 메모');
    assert.match(html, /<audio[^>]*controls[^>]*preload="none"/);
    assert.doesNotMatch(html, /<audio[^>]*autoplay/);
  });
  check('voice polling preserves active playback until another recording is selected', () => {
    const audio = query('#voice-audio');
    api.renderVoice({phase: 'recognition', audio_url: '/voice/one/audio'});
    const loads = audio.loadCount, pauses = audio.pauseCount;
    api.renderVoice({phase: 'completed', audio_url: '/voice/one/audio', text: '괜찮아요', classification: '도움 불필요'});
    assert.equal(audio.loadCount, loads);
    assert.equal(audio.pauseCount, pauses);
    api.renderVoice({phase: 'completed', audio_url: '/voice/two/audio'});
    assert.equal(audio.loadCount, loads+1);
    assert.equal(audio.pauseCount, pauses+1);
    api.renderVoice(null);
    assert.equal(audio.hidden, true);
    assert.equal(audio.src, undefined);
  });
  await checkAsync('voice status arrives asynchronously without altering event color or notes', async () => {
    const before = JSON.stringify(api.state.events);
    api.state.selectedId = 7;
    query('#detail').open = true;
    respond = async url => {
      assert.equal(url, '/voice/events/7');
      return {ok: true, json: async () => ({phase: 'completed', text: '도와주세요', classification: '도움 필요'})};
    };
    await api.pollDetailVoice();
    assert.equal(query('#voice-text').textContent, '도와주세요');
    assert.equal(JSON.stringify(api.state.events), before);
    assert.equal(query('#detail-note').value, '관리자가 작성 중인 메모');
  });
  await checkAsync('late responses from a previous detail cannot replace the current voice', async () => {
    let finish;
    api.state.selectedId = 7;
    respond = () => new Promise(resolve => { finish = resolve; });
    const old = api.pollDetailVoice();
    api.state.selectedId = 8;
    respond = async () => ({ok: true, json: async () => ({phase: 'recording', text: null})});
    await api.pollDetailVoice();
    finish({ok: true, json: async () => ({phase: 'completed', text: '옛 사건 문장'})});
    await old;
    assert.equal(query('#voice-progress').textContent, '녹음 중');
    assert.notEqual(query('#voice-text').textContent, '옛 사건 문장');
  });
  await checkAsync('closed detail does not fetch or display a late voice response', async () => {
    query('#detail').open = false;
    const count = requests.length;
    await api.pollDetailVoice();
    assert.equal(requests.length, count);
    query('#detail').open = true;
    let finish;
    respond = () => new Promise(resolve => { finish = resolve; });
    const request = api.pollDetailVoice();
    query('#detail').open = false;
    finish({ok: true, json: async () => ({phase: 'completed', text: '닫힌 창 문장'})});
    await request;
    assert.notEqual(query('#voice-text').textContent, '닫힌 창 문장');
  });
  await checkAsync('voice transport failure is shown without clearing existing notes', async () => {
    query('#detail').open = true;
    respond = async () => ({ok: false, status: 503});
    await api.pollDetailVoice();
    assert.equal(query('#voice-progress').textContent, '음성 정보 연결 대기');
    assert.equal(query('#detail-note').value, '관리자가 작성 중인 메모');
    query('#detail').open = false;
  });
  return results;
}

export const results = await runChecks();
for (const name of results) console.log('ok - ' + name);
