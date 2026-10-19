// Dependency-free event regression tests. These mocks do not replace browser QA.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const os = require('node:os');
const root = path.resolve(__dirname, '..');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.handlers = {}; this.attributes = {}; this.value = ''; this.checked = false; this.disabled = false; this.className = ''; this.style = {}; this.textContent = ''; this.classList = { add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x), toggle: (x, on) => on ? this.classes.add(x) : this.classes.delete(x) }; this.classes = new Set(); }
  setAttribute(key, value) { this.attributes[key] = value; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  replaceWith(child) { child.parent = this.parent; this.parent.children[this.parent.children.indexOf(this)] = child; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  async fire(type, event) { for (const handler of this.handlers[type] || []) await handler(event); }
  focus() { document.activeElement = this; }
  matches(selector) {
    return selector.split(',').some(piece => {
      const s = piece.trim();
      if (s === '[data-qid]') return !!this.dataset.qid;
      if (s === '[data-unsaved="true"]') return this.dataset.unsaved === 'true';
      if (s === '[contenteditable="true"]') return this.attributes.contenteditable === 'true';
      if (s === '[data-act="re-answer"]') return this.dataset.act === 're-answer';
      if (s === 'button:not([disabled])') return this.tagName === 'button' && !this.disabled;
      if (s.startsWith('.')) return this.className.split(' ').includes(s.slice(1));
      const m = /^(\w+)(?:\[type=(\w+)\])?(:checked)?$/.exec(s);
      return m && this.tagName === m[1] && (!m[2] || this.type === m[2]) && (!m[3] || this.checked);
    });
  }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); }
}

let document;
function fixture(questions) {
  const form = new Element('form');
  for (const question of questions) {
    const item = new Element('div'); item.dataset = { qid: question.id, qtype: question.type };
    const answered = question.answer !== null && question.answer !== '' && (!Array.isArray(question.answer) || question.answer.length > 0);
    const body = new Element('div'); body.className = answered ? 'q-answered' : 'q-input';
    if (answered) {
      const button = new Element('button'); button.dataset = { act: 're-answer', qid: question.id }; body.append(button);
    } else if (question.type === 'radio' || question.type === 'checkbox') {
      for (const option of question.options) { const input = new Element('input'); input.type = question.type; input.value = option; body.append(input); }
    } else {
      const input = new Element(question.type === 'multiline' ? 'textarea' : question.type === 'select' ? 'select' : 'input');
      input.type = 'text'; body.append(input);
    }
    item.append(body); form.append(item);
  }
  const submit = new Element('button'); submit.type = 'submit'; form.append(submit);
  const elements = {};
  for (const id of ['commit-btn', 'copy-prompt-btn', 'dismiss-btn', 'commit-overlay', 'commit-count', 'commit-list', 'commit-result-detail', 'draft-count', 'inbox-count', 'draft-status', 'inbox-status', 'server-status']) elements[id] = new Element(id.endsWith('-btn') ? 'button' : 'div');
  const card = new Element('div'); card.className = 'commit-card'; elements['commit-overlay'].append(card);
  const close = new Element('button'); close.className = 'close';
  card.append(close, elements['copy-prompt-btn'], elements['dismiss-btn']);
  const tabLabels = ['plan', 'confirm', 'log'].map(id => {
    elements['tab-' + id] = new Element('input');
    const label = new Element('label'); label.htmlFor = 'tab-' + id; return label;
  });
  const page = new Element('div'); page.append(form, ...Object.entries(elements).filter(([id]) => !['copy-prompt-btn', 'dismiss-btn'].includes(id)).map(([, value]) => value));
  const handlers = {};
  document = { activeElement: submit, hidden: false, handlers,
    addEventListener(type, handler) { (handlers[type] ||= []).push(handler); },
    async fire(type, event) { for (const handler of handlers[type] || []) await handler(event); },
    getElementById: id => id === 'confirm-form-confirm' ? form : elements[id], createElement: tag => new Element(tag), createTextNode: text => { const e = new Element('text'); e.textContent = text; return e; },
    querySelector: selector => selector === '#commit-overlay .close' ? close : page.querySelector(selector),
    querySelectorAll: selector => selector === '.tabs label' ? tabLabels : page.querySelectorAll(selector) };
  const requests = [], messages = [], reloads = [];
  const context = { document, console, setInterval() {}, setTimeout() {}, window: { __STATE__: { version: 1, open_questions: questions, inbox: [], draft: [] }, location: { protocol: 'http:', origin: 'http://localhost:55501', reload: () => reloads.push(true) }, toast: (...args) => messages.push(args) }, fetch: async (url, options) => { requests.push(JSON.parse(options.body)); return { ok: true }; } };
  vm.createContext(context);
  const raw = fs.readFileSync(path.join(root, 'tabs/confirm.html'), 'utf8').replaceAll('{{TAB_ID}}', 'confirm');
  vm.runInContext(raw.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  context.window.__TAB_INITS__[0]();
  return { form, context, requests, messages, elements, reloads, tabLabels, close };
}

async function run() {
  let count = 0;
  const test = async (name, action) => { await action(); count++; console.log('PASS', name); };
  await test('rendered tab hooks survive shell registration', () => {
    const task = fs.mkdtempSync(path.join(os.tmpdir(), 'html-plan-ui-'));
    try {
      const state = JSON.parse(fs.readFileSync(path.join(root, 'examples/state-minimal.json')));
      state.tabs.push({ id: 'my-todos', type: 'todos' });
      fs.writeFileSync(path.join(task, 'state.json'), JSON.stringify(state));
      execFileSync('python3', [path.join(root, 'render.py'), '--task-dir', task]);
      const context = { window: { location: { protocol: 'http:', origin: 'http://localhost:55501' } }, document: { addEventListener() {} } };
      vm.createContext(context);
      for (const script of fs.readFileSync(path.join(task, 'plan.html'), 'utf8').matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(script[1], context);
      assert.equal(context.window.__TAB_INITS__.length, 2);
    } finally { fs.rmSync(task, { recursive: true, force: true }); }
  });
  await test('saving another question does not clear answered checkbox', async () => {
    const f = fixture([{ id: 'q1', type: 'checkbox', options: ['A', 'B'], answer: ['A'] }, { id: 'q2', type: 'text', answer: null }]);
    f.form.querySelector('input[type=text]').value = 'new answer';
    await f.form.fire('submit', { preventDefault() {} });
    assert.equal(f.requests.length, 1); assert.equal(f.requests[0].payload.question_id, 'q2');
  });
  for (const kind of ['radio', 'select', 'checkbox', 'text', 'multiline']) {
    await test(kind + ' re-answer preserves type and saves changed value', async () => {
      const f = fixture([{ id: 'q1', type: kind, options: ['A', 'B'], answer: kind === 'checkbox' ? ['A'] : 'A' }]);
      const button = f.form.querySelector('[data-act="re-answer"]');
      await f.form.fire('click', { target: button, preventDefault() {} });
      const inputs = f.form.querySelectorAll('input, select, textarea');
      if (kind === 'radio' || kind === 'checkbox') { inputs[0].checked = false; inputs[1].checked = true; } else inputs[0].value = 'B';
      await f.form.fire('submit', { preventDefault() {} });
      assert.equal(f.requests.length, 1);
      assert.deepEqual(JSON.parse(JSON.stringify(f.requests[0].payload.answer)), kind === 'checkbox' ? ['B'] : 'B');
    });
  }
  await test('unchanged re-answer and repeated save create no duplicate draft', async () => {
    const f = fixture([{ id: 'q1', type: 'radio', options: ['A', 'B'], answer: 'A' }]);
    await f.form.fire('click', { target: f.form.querySelector('[data-act="re-answer"]'), preventDefault() {} });
    await f.form.fire('submit', { preventDefault() {} }); assert.equal(f.requests.length, 0);
    const inputs = f.form.querySelectorAll('input'); inputs[0].checked = false; inputs[1].checked = true;
    await f.form.fire('submit', { preventDefault() {} }); await f.form.fire('submit', { preventDefault() {} });
    assert.equal(f.requests.length, 1);
  });
  await test('required empty answer is rejected and typed value is retained on failure', async () => {
    const f = fixture([{ id: 'q1', type: 'checkbox', options: ['A'], required: true, answer: ['A'] }]);
    await f.form.fire('click', { target: f.form.querySelector('[data-act="re-answer"]'), preventDefault() {} });
    f.form.querySelector('input').checked = false;
    await f.form.fire('submit', { preventDefault() {} }); assert.equal(f.requests.length, 0);
    const g = fixture([{ id: 'q1', type: 'text', answer: null }]);
    g.context.fetch = async () => { throw new Error('offline'); };
    g.form.querySelector('input').value = 'retain me';
    await g.form.fire('submit', { preventDefault() {} }); assert.equal(g.form.querySelector('input').value, 'retain me');
    assert.equal(g.form.querySelector('button[type=submit]').disabled, false);
  });
  await test('partial result shows actual count and user text stays text', () => {
    const f = fixture([]);
    const shell = fs.readFileSync(path.join(root, 'tabs/_shell.html'), 'utf8');
    vm.runInContext(shell.match(/<script>\s*window\.__STATE__[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '').replace('{{STATE_JSON}}', '{"version":1,"open_questions":[],"inbox":[],"draft":[]}').replace('{{SERVER_URL}}', 'http://localhost:55501'), f.context);
    f.context.showCommitOverlay({ ok: false, applied: ['d1'], inbox_ids: ['in1'], remaining_drafts: 1, error: 'missing target', state: { inbox: [{ id: 'in1', body: '<img src=x onerror=bad>' }] } });
    assert.equal(f.elements['commit-count'].textContent, 1);
    assert.match(f.elements['commit-result-detail'].textContent, /部分/);
    const preview = f.elements['commit-list'].children[0].children[1];
    assert.equal(preview.textContent, '<img src=x onerror=bad>'); assert.equal(preview.children.length, 0);
  });
  await test('hidden page does not poll and unchanged state does not reload', async () => {
    const f = fixture([]);
    const shell = fs.readFileSync(path.join(root, 'tabs/_shell.html'), 'utf8');
    vm.runInContext(shell.match(/<script>\s*window\.__STATE__[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '').replace('{{STATE_JSON}}', '{"version":1,"open_questions":[],"inbox":[],"draft":[]}').replace('{{SERVER_URL}}', 'http://localhost:55501'), f.context);
    let calls = 0; f.context.fetch = async () => { calls++; return { ok: true, status: 304 }; };
    f.context.document.hidden = true; await f.context.refreshStatusCounts(); assert.equal(calls, 0);
    await f.context.refreshStatusCounts(true); assert.equal(calls, 1); assert.equal(f.reloads.length, 0);
  });
  await test('new AI state does not reload while a form has unsaved input', async () => {
    const f = fixture([{ id: 'q1', type: 'text', answer: null }]);
    const shell = fs.readFileSync(path.join(root, 'tabs/_shell.html'), 'utf8');
    vm.runInContext(shell.match(/<script>\s*window\.__STATE__[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '').replace('{{STATE_JSON}}', '{"version":1,"open_questions":[],"inbox":[],"draft":[]}').replace('{{SERVER_URL}}', 'http://localhost:55501'), f.context);
    f.context.fetch = async () => ({ ok: true, status: 200, headers: { get: () => '"2"' }, json: async () => ({ version: 2, draft: [], inbox: [] }) });
    const input = f.form.querySelector('input'); input.value = 'unsaved';
    await f.form.fire('input', { target: input });
    await f.context.refreshStatusCounts(true);
    assert.equal(f.reloads.length, 0); assert.equal(input.value, 'unsaved');
    input.value = ''; await f.form.fire('input', { target: input });
    await f.context.refreshStatusCounts(true);
    assert.equal(f.reloads.length, 1);
  });
  await test('dialog traps Tab and restores focus on Escape', async () => {
    const f = fixture([]);
    const shell = fs.readFileSync(path.join(root, 'tabs/_shell.html'), 'utf8');
    vm.runInContext(shell.match(/<script>\s*window\.__STATE__[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '').replace('{{STATE_JSON}}', '{"version":1,"open_questions":[],"inbox":[],"draft":[]}').replace('{{SERVER_URL}}', 'http://localhost:55501'), f.context);
    f.context.fetch = async () => ({ ok: true, status: 304 });
    f.context.initActionbar();
    const original = f.context.document.activeElement;
    f.context.showCommitOverlay({ ok: true, applied: [], inbox_ids: [], state: { inbox: [] } });
    assert.equal(f.context.document.activeElement.className, 'commit-card');
    f.elements['dismiss-btn'].focus();
    await f.context.document.fire('keydown', { key: 'Tab', shiftKey: false, preventDefault() {} });
    assert.equal(f.context.document.activeElement, f.close);
    await f.context.document.fire('keydown', { key: 'Tab', shiftKey: true, preventDefault() {} });
    assert.equal(f.context.document.activeElement, f.elements['dismiss-btn']);
    await f.context.document.fire('keydown', { key: 'Escape', preventDefault() {} });
    assert.equal(f.elements['commit-overlay'].classList.contains('show'), false);
    assert.equal(f.context.document.activeElement, original);
  });
  await test('tab navigation handles arrows, Home and End', async () => {
    const f = fixture([]);
    const shell = fs.readFileSync(path.join(root, 'tabs/_shell.html'), 'utf8');
    vm.runInContext(shell.match(/<script>\s*window\.__STATE__[\s\S]*?<\/script>/)[0].replace(/^<script>|<\/script>$/g, '').replace('{{STATE_JSON}}', '{"version":1,"open_questions":[],"inbox":[],"draft":[]}').replace('{{SERVER_URL}}', 'http://localhost:55501'), f.context);
    f.context.fetch = async () => ({ ok: true, status: 304 }); f.context.initActionbar();
    await f.tabLabels[0].fire('keydown', { key: 'ArrowRight', preventDefault() {} });
    assert.equal(f.context.document.activeElement, f.tabLabels[1]);
    assert.equal(f.elements['tab-confirm'].checked, true);
    await f.tabLabels[1].fire('keydown', { key: 'End', preventDefault() {} });
    assert.equal(f.context.document.activeElement, f.tabLabels[2]);
    await f.tabLabels[2].fire('keydown', { key: 'Home', preventDefault() {} });
    assert.equal(f.context.document.activeElement, f.tabLabels[0]);
  });
  console.log(`${count} UI regression tests passed (mock DOM; browser QA separate).`);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
