// Behavioral tests for the send-destination modal polling loop.
//
// These execute the real static/dashboard.js in a vm context with
// stubbed DOM globals, fake timers, and a scripted fetch mock, and
// assert on the resulting button state and status text. They are
// deliberately not string-presence checks: the old code passes the
// string checks but fails these tests, because its 170s timeout
// guards cleared the timer without re-enabling the Send button.
//
// Run: node --test tests/test_nav_modal.js
// Run against another copy: DASHBOARD_JS=/path/to/dashboard.js
//   node --test tests/test_nav_modal.js
const { describe, test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const DASHBOARD_JS = process.env.DASHBOARD_JS ||
  path.join(__dirname, '..', 'static', 'dashboard.js');

function makeEl() {
  const classes = new Set();
  return {
    textContent: '',
    innerHTML: '',
    value: '',
    disabled: false,
    dataset: {},
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      toggle(c) {
        if (classes.has(c)) classes.delete(c); else classes.add(c);
      },
      contains(c) { return classes.has(c); },
    },
    addEventListener() {},
    focus() {},
    click() {},
    submit() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    insertAdjacentHTML() {},
    style: {},
    getAttribute() { return null; },
    getBoundingClientRect() { return { top: 0, bottom: 0 }; },
  };
}

function flush() {
  // Drain the promise chains the modal code builds (fetch .then
  // handlers run on microtasks, so a few macrotask hops suffice).
  let p = Promise.resolve();
  for (let i = 0; i < 5; i++) {
    p = p.then(() => new Promise((r) => setImmediate(r)));
  }
  return p;
}

function loadModal(fetchMock) {
  const elements = {};
  const timers = new Map();
  let nextTimer = 1;
  const reloadCalls = { count: 0 };
  const alerts = [];
  const confirmResult = { value: false };
  const sandbox = {
    window: {
      scrollY: 0,
      innerHeight: 768,
      addEventListener() {},
      scrollTo() {},
      matchMedia() { return { matches: false }; },
    },
    document: {
      getElementById: (id) => {
        const el = (elements[id] ||= makeEl());
        // The activity list IIFE parses this element at load time.
        if (id === 'actdata') el.textContent = '{"days":[]}';
        return el;
      },
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => makeEl(),
      addEventListener() {},
      documentElement: { scrollHeight: 0 },
      activeElement: null,
    },
    requestAnimationFrame: () => 0,
    performance: { now: () => 0 },
    location: { reload: () => { reloadCalls.count += 1; } },
    alert: (msg) => { alerts.push(msg); },
    confirm: () => confirmResult.value,
    console,
    fetch: fetchMock,
    setInterval: (fn) => {
      const id = nextTimer++;
      timers.set(id, fn);
      return id;
    },
    clearInterval: (id) => { timers.delete(id); },
    setTimeout,
    clearTimeout,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(DASHBOARD_JS, 'utf8'), sandbox,
    { filename: DASHBOARD_JS });
  return { sandbox, elements, timers, reloadCalls, alerts, confirmResult };
}

// Drives one polling tick: invoke the nav interval callback, then
// let its fetch promise chain settle.
async function tick(timers, navTimerId) {
  const fn = timers.get(navTimerId);
  assert.ok(fn, 'expected the nav polling timer to still be running');
  fn();
  await flush();
}

function startSend(ctx, fetchCalls) {
  const { sandbox, timers } = ctx;
  sandbox._navPlace = {
    name: 'Test Street',
    address: 'Test Street, Testville',
    lat: 34.0,
    lon: -118.0,
    place_id: null,
    url: null,
  };
  sandbox._navMode = 'direct';
  sandbox._navRound = false;
  const before = new Set(timers.keys());
  sandbox.navSend();
  return { before, fetchCalls };
}

async function driveToTimeout(ctx, navTimerId) {
  // waited advances 3 per tick; 57 ticks reach 171 > 170.
  for (let i = 0; i < 57; i++) {
    await tick(ctx.timers, navTimerId);
  }
}

function makeFetchMock(statusOutcomes) {
  // statusOutcomes: per status poll, either { state, cloud_accepted }
  // (resolved) or the string 'reject' (fetch failure). The initial
  // send POST always succeeds with a job id.
  const calls = { send: 0, status: 0 };
  let i = 0;
  const mock = (url, opts) => {
    if (opts && opts.method === 'POST') {
      calls.send++;
      return Promise.resolve({
        json: () => Promise.resolve({ ok: true, job_id: 'job1' }),
      });
    }
    calls.status++;
    const outcome = statusOutcomes[i++];
    if (outcome === 'reject' || outcome === undefined) {
      return Promise.reject(new Error('offline'));
    }
    return Promise.resolve({
      json: () => Promise.resolve({ ok: true, job: outcome }),
    });
  };
  mock.calls = calls;
  return mock;
}

describe('nav modal 170s timeout', () => {
  test('accepted send + repeated polling failures: sent text stays, Send re-enables', async () => {
    const fetchMock = makeFetchMock([
      { state: 'sending', cloud_accepted: true },
      ...Array(60).fill('reject'),
    ]);
    const ctx = loadModal(fetchMock);
    const { before } = startSend(ctx, fetchMock.calls);
    await flush();
    const navTimerId = [...ctx.timers.keys()]
      .find((id) => !before.has(id));
    assert.ok(navTimerId, 'expected navSend to start a polling timer');
    // First tick: the accepted status sets the sent text.
    await tick(ctx.timers, navTimerId);
    assert.equal(
      ctx.elements.navstatus.textContent, 'Sent. Verify in the car app.');
    assert.equal(ctx.elements.navsendbtn.disabled, true);
    // Remaining ticks: every poll fails until past 170s.
    for (let i = 1; i < 57; i++) {
      await tick(ctx.timers, navTimerId);
    }
    assert.equal(ctx.timers.size, 0, 'polling timer must be cleared');
    assert.equal(
      ctx.elements.navstatus.textContent, 'Sent. Verify in the car app.',
      'the sent text must not be overwritten by the timeout');
    assert.equal(ctx.elements.navsendbtn.disabled, false,
      'Send must be usable again after polling stops');
  });

  test('unaccepted send + repeated polling failures: neutral text, Send re-enables', async () => {
    const fetchMock = makeFetchMock(Array(60).fill('reject'));
    const ctx = loadModal(fetchMock);
    const { before } = startSend(ctx, fetchMock.calls);
    await flush();
    const navTimerId = [...ctx.timers.keys()]
      .find((id) => !before.has(id));
    assert.ok(navTimerId, 'expected navSend to start a polling timer');
    await driveToTimeout(ctx, navTimerId);
    assert.equal(ctx.timers.size, 0, 'polling timer must be cleared');
    assert.equal(
      ctx.elements.navstatus.textContent,
      'Still no confirmation from the car. Check the car screen before sending again.');
    assert.equal(ctx.elements.navsendbtn.disabled, false,
      'Send must be usable again after polling stops');
  });
});

function makePostMock(postOutcomes) {
  // postOutcomes: per send POST, either { payload } (resolved JSON)
  // or { reject: true } (fetch failure). Records request bodies.
  const bodies = [];
  let i = 0;
  const mock = (url, opts) => {
    if (opts && opts.method === 'POST') {
      bodies.push(JSON.parse(opts.body));
      const outcome = postOutcomes[i++];
      if (!outcome || outcome.reject) {
        return Promise.reject(new Error('offline'));
      }
      return Promise.resolve({
        json: () => Promise.resolve(outcome.payload),
      });
    }
    return Promise.reject(new Error('unexpected GET'));
  };
  mock.bodies = bodies;
  return mock;
}

describe('nav modal active-trip guard', () => {
  test('active trip 409 shows Send anyway and arms force', async () => {
    const fetchMock = makePostMock([{
      payload: {
        ok: false,
        code: 'active_trip',
        error: 'Car is already navigating to Other Place. Send anyway?',
      },
    }]);
    const ctx = loadModal(fetchMock);
    startSend(ctx);
    await flush();
    assert.equal(
      ctx.elements.navstatus.textContent,
      'Car is already navigating to Other Place. Send anyway?');
    assert.equal(ctx.elements.navsendbtn.textContent, 'Send anyway');
    assert.equal(ctx.elements.navsendbtn.disabled, false);
    assert.equal(ctx.sandbox._navForce, true);
  });

  test('forced re-send carries force and consumes the flag', async () => {
    const fetchMock = makePostMock([
      {
        payload: {
          ok: false,
          code: 'active_trip',
          error: 'Car is already navigating to Other Place. Send anyway?',
        },
      },
      { payload: { ok: false, error: 'Send failed.' } },
    ]);
    const ctx = loadModal(fetchMock);
    startSend(ctx);
    await flush();
    assert.equal(ctx.sandbox._navForce, true);
    startSend(ctx);
    await flush();
    assert.equal(fetchMock.bodies.length, 2);
    assert.equal(fetchMock.bodies[0].force, false);
    assert.equal(fetchMock.bodies[1].force, true);
    assert.equal(ctx.sandbox._navForce, false);
    assert.equal(ctx.elements.navstatus.textContent, 'Send failed.');
    assert.equal(ctx.elements.navsendbtn.textContent, 'Send');
    assert.equal(ctx.elements.navsendbtn.disabled, false);
  });
});

function makeCostMock(outcomes) {
  // outcomes: per POST, { status, body } or { reject: true } (fetch
  // failure) or { status, badJson: true } (a non-JSON error page).
  // Records the request URL and parsed body of every call.
  const calls = [];
  let i = 0;
  const mock = (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    const outcome = outcomes[i++];
    if (!outcome || outcome.reject) {
      return Promise.reject(new Error('offline'));
    }
    if (outcome.badJson) {
      return Promise.resolve({
        status: outcome.status,
        json: () => Promise.reject(
          new SyntaxError('Unexpected token < in JSON')),
      });
    }
    return Promise.resolve({
      status: outcome.status,
      json: () => Promise.resolve(outcome.body),
    });
  };
  mock.calls = calls;
  return mock;
}

describe('cost modal AJAX save', () => {
  function openCost(ctx, chargeId, currentVal, startTs) {
    ctx.sandbox.openCostModal(chargeId, currentVal, startTs);
  }

  test('saveCost posts the cost and reloads on success', async () => {
    const fetchMock = makeCostMock([
      { status: 200, body: { ok: true, cost_per_kwh: 0.5 } },
    ]);
    const ctx = loadModal(fetchMock);
    openCost(ctx, 1, null, 100);
    ctx.elements.costinput.value = '0.5';
    ctx.sandbox.saveCost();
    await flush();
    assert.equal(fetchMock.calls.length, 1);
    assert.equal(fetchMock.calls[0].url, '/api/charge-cost');
    assert.deepEqual(fetchMock.calls[0].body,
      { charge_id: 1, start_ts: 100, cost_per_kwh: 0.5 });
    assert.equal(ctx.reloadCalls.count, 1);
    assert.equal(ctx.elements.costmodal.classList.contains('hidden'), true);
    assert.equal(ctx.elements.costsave.disabled, false);
  });

  test('saveCost rejects bad input without fetching', async () => {
    const fetchMock = makeCostMock([]);
    const ctx = loadModal(fetchMock);
    openCost(ctx, 1, null, 100);
    for (const value of ['abc', '-1', '11', '']) {
      ctx.elements.costinput.value = value;
      ctx.sandbox.saveCost();
    }
    await flush();
    assert.equal(fetchMock.calls.length, 0);
    assert.equal(ctx.alerts.length, 4);
    assert.ok(ctx.alerts.every((m) => m === 'Enter a valid price'));
    assert.equal(ctx.reloadCalls.count, 0);
  });

  test('saveCost server error alerts and does not reload', async () => {
    const fetchMock = makeCostMock([
      { status: 400,
        body: { ok: false,
                error: 'Enter a price between 0 and 10 $/kWh.' } },
    ]);
    const ctx = loadModal(fetchMock);
    openCost(ctx, 1, null, 100);
    ctx.elements.costinput.value = '0.5';
    ctx.sandbox.saveCost();
    await flush();
    assert.equal(fetchMock.calls.length, 1);
    assert.equal(ctx.alerts.length, 1);
    assert.equal(ctx.alerts[0],
      'Could not save: Enter a price between 0 and 10 $/kWh.');
    assert.equal(ctx.reloadCalls.count, 0);
    assert.equal(ctx.elements.costsave.disabled, false);
  });

  test('_postCost survives a non-JSON 500 page', async () => {
    const fetchMock = makeCostMock([{ status: 500, badJson: true }]);
    const ctx = loadModal(fetchMock);
    openCost(ctx, 1, null, 100);
    ctx.elements.costinput.value = '0.5';
    ctx.sandbox.saveCost();
    await flush();
    assert.equal(ctx.alerts.length, 1);
    assert.equal(ctx.alerts[0], 'Could not save: HTTP 500');
    assert.equal(ctx.reloadCalls.count, 0);
  });

  test('_postCost fetch failure alerts and re-enables', async () => {
    const fetchMock = makeCostMock([{ reject: true }]);
    const ctx = loadModal(fetchMock);
    openCost(ctx, 1, null, 100);
    ctx.elements.costinput.value = '0.5';
    ctx.sandbox.saveCost();
    await flush();
    assert.equal(ctx.alerts.length, 1);
    assert.ok(ctx.alerts[0].startsWith('Could not save: '));
    assert.ok(ctx.alerts[0].includes('offline'));
    assert.equal(ctx.reloadCalls.count, 0);
    assert.equal(ctx.elements.costsave.disabled, false);
  });

  test('clearCost posts an empty cost after confirm', async () => {
    const fetchMock = makeCostMock([
      { status: 200, body: { ok: true, cost_per_kwh: null } },
    ]);
    const ctx = loadModal(fetchMock);
    ctx.confirmResult.value = true;
    openCost(ctx, 1, 0.5, 100);
    ctx.sandbox.clearCost();
    await flush();
    assert.equal(fetchMock.calls.length, 1);
    assert.equal(fetchMock.calls[0].body.cost_per_kwh, '');
    assert.equal(ctx.reloadCalls.count, 1);
  });

  test('clearCost does nothing when confirm is cancelled', async () => {
    const fetchMock = makeCostMock([]);
    const ctx = loadModal(fetchMock);
    ctx.confirmResult.value = false;
    openCost(ctx, 1, 0.5, 100);
    ctx.sandbox.clearCost();
    await flush();
    assert.equal(fetchMock.calls.length, 0);
    assert.equal(ctx.reloadCalls.count, 0);
  });

  test('double saveCost while a save is in flight posts only once',
    async () => {
      let resolveFetch;
      const calls = [];
      const fetchMock = (url, opts) => {
        calls.push({ url, body: JSON.parse(opts.body) });
        return new Promise((resolve) => { resolveFetch = resolve; });
      };
      const ctx = loadModal(fetchMock);
      openCost(ctx, 1, null, 100);
      ctx.elements.costinput.value = '0.5';
      ctx.sandbox.saveCost();
      assert.equal(ctx.elements.costsave.disabled, true);
      ctx.sandbox.saveCost();
      await flush();
      assert.equal(calls.length, 1);
      resolveFetch({
        status: 200,
        json: () => Promise.resolve({ ok: true, cost_per_kwh: 0.5 }),
      });
      await flush();
      assert.equal(ctx.reloadCalls.count, 1);
      assert.equal(ctx.elements.costsave.disabled, false);
    });
});
