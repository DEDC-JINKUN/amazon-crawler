const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const APP = fs.readFileSync(path.join(__dirname, '..', 'console', 'app.js'), 'utf8');

class FakeElement {
  constructor() {
    this.textContent = '';
    this.hidden = false;
    this.value = '';
    this.firstChild = null;
    this.listeners = {};
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      contains: (name) => this.classList.values.has(name),
    };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); this.firstChild = this.children[0] || null; }
  removeChild(child) { this.children = this.children.filter((value) => value !== child); this.firstChild = this.children[0] || null; }
  get options() { return this.children; }
  setAttribute() {}
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
}

async function loadConsole(fetchImpl, promptImpl) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement());
    return elements.get(id);
  };
  let interval = null;
  const storage = new Map();
  const storageWrites = [];
  const window = {
    location: { href: 'http://127.0.0.1:8771/', origin: 'http://127.0.0.1:8771' },
    history: { replaceState() {} },
    prompt: promptImpl,
    setInterval(callback) { interval = callback; return 1; },
  };
  const context = vm.createContext({
    window,
    document: { getElementById: element, createElement: () => new FakeElement() },
    sessionStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => { storageWrites.push([key, value]); storage.set(key, value); },
      removeItem: (key) => { storageWrites.push([key, null]); storage.delete(key); },
    },
    fetch: fetchImpl,
    URL,
    URLSearchParams,
    Intl,
    Option: class Option { constructor(label, value) { this.label = label; this.value = value; } },
    console,
  });
  vm.runInContext(APP, context, { filename: 'console/app.js' });
  await settle();
  return { element, storageWrites, run: (code) => vm.runInContext(code,context), tick: async () => { interval(); await settle(); } };
}

function response(status, payload = {}) {
  return { status, ok: status >= 200 && status < 300, async json() { return payload; } };
}

test('recovery panel distinguishes unknown budgets and persistent pause', async () => {
  const app = await loadConsole(async () => response(401), () => null);
  app.run("renderRecovery({availability:'unknown'})");
  assert.match(app.element('recoverySummary').textContent, /unknown/);
  app.run("renderRecovery({availability:'available',counts:{transport:2},egress:[{paused_until:'2099-01-01T00:00:00Z'}]})");
  assert.match(app.element('recoverySummary').textContent, /暂停/);
  assert.doesNotMatch(app.element('recoverySummary').textContent, /transport|job|跨run/);
});

test('cancelled API key prompt stays locked across automatic refresh until button click', async () => {
  let prompts = 0;
  const app = await loadConsole(
    async () => response(401),
    () => { prompts += 1; return null; },
  );

  assert.equal(prompts, 1);
  await app.tick();
  await app.tick();
  assert.equal(prompts, 1);
  assert.match(app.element('errorBanner').textContent, /需要本地API Key|locked/i);
  assert.doesNotMatch(app.element('errorBanner').textContent, /网络离线/);
  assert.equal(app.element('liveBadge').classList.contains('offline'), false);

  await app.element('apiKeyButton').listeners.click();
  await settle();
  assert.equal(prompts, 2);
});

test('network failure is offline and never prompts for an API key', async () => {
  let prompts = 0;
  const app = await loadConsole(
    async () => { throw new Error('network unavailable'); },
    () => { prompts += 1; return null; },
  );

  assert.equal(prompts, 0);
  assert.match(app.element('errorBanner').textContent, /网络离线/);
  assert.equal(app.element('liveBadge').classList.contains('offline'), true);
});

test('accepted API key remains memory-only and is retried as a request header', async () => {
  const headers = [];
  let calls = 0;
  const app = await loadConsole(
    async (_target, options) => {
      headers.push(options.headers);
      calls += 1;
      return calls === 1 ? response(401) : response(200, { items: [] });
    },
    () => 'fixture-console-key',
  );

  assert.equal(headers[1]['X-Collection-API-Key'], 'fixture-console-key');
  assert.deepEqual(app.storageWrites, []);
});

test('run view uses readable names and one progress snapshot without exposing debug prose', async () => {
  const app = await loadConsole(async () => response(401), () => null);
  app.run(`document.getElementById('runSelector').append(new Option('old 14/42','run-control-fixture'));
    renderRun({run_id:'run-control-fixture',started_at:'2026-09-04T07:57:00Z',requested_actions:42,
      recorded_actions:15,terminal_status:'running',worker_duration_seconds:780,
      outcome_counts:{completed:10,variant_redirect:4,blocked:1,failed:0},
      capacity_authorization:{reservation_id:'capacity-secret-debug'},items:[]})`);
  const summary = app.element('runSummary').textContent;
  assert.match(summary, /15\s*\/\s*42/);
  assert.doesNotMatch(summary, /run-control|canary|reservation|capacity-secret|unknown/);
  assert.match(app.element('runSelector').options[0].text, /15\s*\/\s*42/);
  assert.doesNotMatch(app.element('runSelector').options[0].text, /run-control/);
  assert.match(app.element('runTechnical').children[0].textContent, /capacity-secret-debug/);
});

test('result names distinguish resolved CAPTCHA from current errors and sibling identity', async () => {
  const app = await loadConsole(async () => response(401), () => null);
  assert.equal(app.run(`resultLabel({outcome:'completed',context_quality:'full',attempts:[{block_reason:'captcha'}]})`), '已采集');
  assert.equal(app.run(`resultLabel({outcome:'blocked',block_reason:'captcha'})`), '验证码');
  assert.equal(app.run(`resultLabel({outcome:'variant_redirect',error_code:'asin_mismatch'})`), '同族变体');
  assert.equal(app.run(`resultLabel({outcome:'failed',error_code:'asin_mismatch'})`), '商品身份不符');
  assert.equal(app.run(`issueLabel('recovery_job_budget_exhausted')`), '该商品达到请求上限');
});

test('operations and technical diagnostics are collapsed by default', () => {
  const html = fs.readFileSync(path.join(__dirname,'..','console','index.html'),'utf8');
  assert.match(html, /<details[^>]*id="operationPanel"[^>]*>/);
  assert.doesNotMatch(html.match(/<details[^>]*id="operationPanel"[^>]*>/)[0], /\bopen\b/);
  assert.match(html, /<details[^>]*class="run-diagnostics"[^>]*>/);
});

test('clearing selection prevents an older in-flight run from repainting the page', async () => {
  let finish;
  const app = await loadConsole(async (target) => {
    if (target.pathname === '/api/runs/run-a') return new Promise((resolve) => { finish = resolve; });
    return response(401);
  }, () => null);
  const pending = app.run("selectRun('run-a')");
  await app.run("selectRun('')");
  finish(response(200,{run_id:'run-a',requested_actions:99,recorded_actions:10,items:[],outcome_counts:{},terminal_status:'running'}));
  await pending;
  assert.equal(app.run('state.selectedRun'), '');
  assert.equal(app.element('runSelector').value, '');
  assert.doesNotMatch(app.element('runSummary').textContent, /10\/99/);
});
