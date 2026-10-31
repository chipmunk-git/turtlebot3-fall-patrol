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
      lastElementChild: { textContent: '' },
      classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
                   add(name) { classes.add(name); }, remove(name) { classes.delete(name); },
                   contains(name) { return classes.has(name); } },
      addEventListener() {}, setAttribute(name, value) { this.attributes[name] = value; },
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
  let respond;
  const documentElement = {dataset: {}};
  const context = vm.createContext({
    document: { documentElement, querySelector: query,
      addEventListener: (name, fn) => { handlers[name] = fn; },
      querySelectorAll: selector => selector === '#robot-steps li' ? steps : [] },
    getComputedStyle: () => ({ getPropertyValue: name => variables[name] || '' }),
    performance: { now: () => 1000 },
    fetch: (url, options) => {
      requests.push(url);
      return respond ? respond(url, options) : new Promise(() => {});
    },
    alert: message => alerts.push(message),
    setInterval() {}, setTimeout() {}, requestAnimationFrame() {},
    ResizeObserver: class { observe() {} },
    location: {hostname: 'localhost', protocol: 'http:'},
    localStorage: {setItem() {}},
  });
  // Use the actual functions with a minimal DOM; do not start network polling.
  vm.runInContext(source + `
    globalThis.displayTest = {state, renderRobot, robotSubtitle, formatLog, badge,
      actionButtons, ROBOT_STEPS, ROBOT_STATE_LABELS, STATUS_LABELS, ACTIONS_BY_STATUS,
      renderToasts, renderRows, toPixel, mapViewSize, visibleMapEvents, applyTheme,
      pollControl, sendDrive};
  `, context);
  const api = context.displayTest;
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
    assert.deepEqual(requests, ['/status', '/events?limit=200', '/logs?after=0&limit=200', '/control', '/map']);
    assert.deepEqual([...html.matchAll(/data-drive="([^"]+)"/g)].map(match => match[1]), ['run', 'stop']);
    assert.match(html, /aria-label="주행 제어"/);
    assert.match(source, /sendDrive\(driveButton.dataset.drive\)/);
    assert.equal(declarations('.drive')['grid-template-columns'], '1fr 1fr');
  });
  await checkAsync('drive buttons PUT run and stop without rewriting reported robot state', async () => {
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
  const event = (id, status, created_at) => ({id, status, created_at, snapshot_url: null,
    x: 0, y: 0, confidence: null});
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
  return results;
}

export const results = await runChecks();
for (const name of results) console.log('ok - ' + name);
