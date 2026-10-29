// Run with: node backend/tests/test_robot_display.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

export function runChecks() {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
  new vm.Script(source); // Parse the complete production script, including startup.
  const start = source.indexOf('\ndocument.addEventListener("click"');
  assert.ok(start > 0, 'dashboard startup boundary must exist');

  function declarations(selector) {
    const values = {};
    for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
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
    return { className: '', textContent: '', lastElementChild: { textContent: '' },
      classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); },
                   contains(name) { return classes.has(name); } },
      getContext() { return {}; } };
  }
  const nodes = new Map();
  const steps = Array.from({ length: 4 }, element);
  const query = selector => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  const context = vm.createContext({
    document: { documentElement: {}, querySelector: query,
      querySelectorAll: selector => selector === '#robot-steps li' ? steps : [] },
    getComputedStyle: () => ({ getPropertyValue: name => variables[name] || '' }),
    performance: { now: () => 1000 },
  });
  // Use the actual functions with a minimal DOM; do not start network polling.
  vm.runInContext(source.slice(0, start) + `
    globalThis.displayTest = {state, renderRobot, robotSubtitle, formatLog, badge,
      actionButtons, ROBOT_STEPS, ROBOT_STATE_LABELS, STATUS_LABELS, ACTIONS_BY_STATUS};
  `, context);
  const api = context.displayTest;
  const results = [];
  function check(name, fn) { fn(); results.push(name); }
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
      observing: '관찰 중', confirmed: '낙상 의심', uncertain: '판단 불가',
      false_alarm: '오탐', acknowledged: '확인됨', resolved: '처리 완료',
    });
    assert.deepEqual(JSON.parse(JSON.stringify(api.ACTIONS_BY_STATUS)), {
      observing: ['false_alarm'], confirmed: ['acknowledge', 'false_alarm'],
      uncertain: ['confirm', 'false_alarm'], acknowledged: ['resolve'], resolved: [], false_alarm: [],
    });
    assert.match(api.badge('observing'), /badge-observing.*관찰 중/);
    assert.match(api.actionButtons({id: 7, status: 'uncertain'}), /data-action="confirm"/);
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
  return results;
}

export const results = runChecks();
for (const name of results) console.log('ok - ' + name);
