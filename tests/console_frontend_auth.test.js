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
    this.classList = {
      values: new Set(),
      add: (...names) => names.forEach((name) => this.classList.values.add(name)),
      remove: (...names) => names.forEach((name) => this.classList.values.delete(name)),
      contains: (name) => this.classList.values.has(name),
    };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append() {}
  removeChild() { this.firstChild = null; }
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
  return { element, storageWrites, tick: async () => { interval(); await settle(); } };
}

function response(status, payload = {}) {
  return { status, ok: status >= 200 && status < 300, async json() { return payload; } };
}

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
