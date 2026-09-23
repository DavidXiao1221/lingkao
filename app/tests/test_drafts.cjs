'use strict';

// Dependency-free browser-state regressions. Each VM is a tab; reloads reuse
// only its sessionStorage and the browser's shared localStorage, not JS memory.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const source = fs.readFileSync(path.resolve(__dirname, '../web/app.js'), 'utf8');
const withoutBoot = source.slice(0, source.lastIndexOf('\n(async () =>'));

function storage() {
  const values = new Map();
  return {
    get length() { return values.size; },
    key(index) { return [...values.keys()][index] ?? null; },
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
}

function state(revision = 4, extra = {}) {
  return {
    revision, participant: 'same-person', version: 'test', csrf: 'test-csrf',
    course: {version: 'substitution-v1'}, phase: 'training', stage: 'guided',
    progress: {current: 1, total: 4, label: '训练'}, question: {kind: 'indefinite', formula_html: '<math></math>'},
    instance_id: 'question-1', feedback: null, hints: [], hint_count: 3,
    chat: [], export_allowed: true, ...extra,
  };
}

function tab(value, shared = storage(), session = storage()) {
  const elements = new Map();
  const listeners = new Map();
  const requests = [];
  const document = {activeElement: null};
  function element() {
    return {
      value: '', textContent: '', innerHTML: '', disabled: false, readOnly: false,
      dataset: {}, selectionStart: 0, selectionEnd: 0,
      classList: {add() {}, remove() {}},
      focus() { document.activeElement = this; },
      setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
      setAttribute() {}, removeAttribute() {}, scrollIntoView() {},
    };
  }
  const app = element();
  Object.defineProperty(app, 'innerHTML', {
    set(html) {
      elements.delete('#answer');
      if (html.includes('id="answer"')) {
        const input = element();
        input.dataset.instance = html.match(/data-instance="([^"]+)"/)[1];
        input.dataset.draftKey = html.match(/data-draft-key="([^"]+)"/)[1];
        input.readOnly = /maxlength="180" readonly/.test(html);
        elements.set('#answer', input);
      }
      for (const id of ['#draftStatus', '#draftRecoveries', '#previewMath', '#previewStatus', '#answerError', '#busyText']) elements.set(id, element());
    },
  });
  elements.set('#app', app);
  elements.set('#main', element());
  elements.set('#toast', element());
  document.querySelector = selector => elements.get(selector) || null;
  document.querySelectorAll = () => [...elements.values()];
  document.addEventListener = () => {};
  const context = vm.createContext({
    document, localStorage: shared, sessionStorage: session,
    window: {addEventListener: (name, fn) => listeners.set(name, fn)},
    crypto: {randomUUID}, navigator: {}, AbortController,
    setTimeout: () => 1, clearTimeout() {},
    fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
  });
  vm.runInContext(withoutBoot, context);
  const run = code => vm.runInContext(code, context);
  run(`state = ${JSON.stringify(value)}; render();`);
  return {
    run, shared, session, requests, elements,
    get input() { return elements.get('#answer'); },
    type(answer) { this.input.value = answer; run('answerEdited(false)'); },
    respond(data, status = 200) {
      assert.ok(requests.length, 'a request is pending');
      requests.shift().resolve({ok: status < 400, status, json: async () => data});
    },
    storageEvent() { listeners.get('storage')({key: run('`${participantPrefix()}revision`')}); },
  };
}

let passed = 0;
async function check(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok ${passed} - ${name}`);
}

(async () => {
  await check('delayed answer response preserves a newer edit at the response revision', async () => {
    const a = tab(state());
    a.type('x');
    const pending = a.run('submitAnswer()');
    const b = tab(state(5), a.shared);
    b.type('exp(x^2)/2+C');
    const newId = b.input.dataset.draftId;
    a.respond(state(5, {last_answer: 'x', feedback: {verdict: 'incorrect'}}));
    await pending;
    assert.equal(b.run('getDraft(draftKey()).edit_id'), newId);
    assert.equal(a.input.value, 'exp(x^2)/2+C');
    assert.equal(tab(state(5), a.shared).input.value, 'exp(x^2)/2+C');
  });

  await check('same-revision edit and an edit interleaved with clearing also survive', async () => {
    const a = tab(state());
    a.type('x');
    const pending = a.run('submitAnswer()');
    const b = tab(state(), a.shared);
    b.type('x^2+C');
    const setItem = a.shared.setItem;
    a.shared.setItem = (key, value) => {
      if (key.includes(':submitted:')) b.type('x^3+C');
      setItem(key, value);
    };
    a.respond(state(5, {last_answer: 'x', feedback: {verdict: 'incorrect'}}));
    await pending;
    assert.equal(b.run('getDraft(draftKey()).answer'), 'x^3+C');
  });

  await check('successful submit clears only its captured edit and its private copy', async () => {
    const a = tab(state());
    a.type('x');
    a.run('preserveDraft(draftKey(), getDraft(draftKey()))');
    const pending = a.run('submitAnswer()');
    a.respond(state(5, {last_answer: 'x', feedback: {verdict: 'incorrect'}}));
    await pending;
    assert.equal(a.run('getDraft(draftKey())'), null);
    assert.equal(a.run('recoveries().length'), 0);
    assert.equal(a.input.value, 'x');
  });

  await check('conflict input survives reload without overwriting the shared newer draft', async () => {
    const a = tab(state());
    a.type('exp(x)');
    const b = tab(state(5), a.shared);
    b.type('x^2+C');
    a.type('exp(x^2)/2+C');
    assert.equal(b.run('getDraft(draftKey()).answer'), 'x^2+C');
    assert.match(a.elements.get('#draftStatus').textContent, /单独保留/);
    const reload = tab(state(5), a.shared, a.session);
    assert.equal(reload.input.value, 'x^2+C');
    assert.equal(reload.run('recoveries()[0].answer'), 'exp(x^2)/2+C');
    assert.match(reload.elements.get('#draftRecoveries').innerHTML, /用此副本替换当前输入/);
    await reload.run("handleRecovery('restore_draft', draftKey())");
    assert.equal(reload.input.value, 'exp(x^2)/2+C');
    assert.equal(reload.run('getDraft(draftKey()).answer'), 'exp(x^2)/2+C');
    assert.equal(reload.run('recoveries().length'), 0);
  });

  await check('storage notification preserves visible input before the user refreshes', () => {
    const a = tab(state());
    a.type('x+C');
    const b = tab(state(5), a.shared);
    b.type('x^2+C');
    a.storageEvent();
    assert.equal(tab(state(5), a.shared, a.session).run('recoveries()[0].answer'), 'x+C');
  });

  await check('later notifications cannot replace an unresolved copy after refresh', () => {
    const a = tab(state());
    a.type('local');
    const b = tab(state(5), a.shared);
    b.type('shared');
    a.storageEvent();
    const reload = tab(state(5), a.shared, a.session);
    assert.equal(reload.input.value, 'shared');
    b.run('state.revision = 6; markRevision()');
    reload.storageEvent();
    assert.equal(tab(state(6), a.shared, a.session).run('recoveries()[0].answer'), 'local');
  });

  await check('a failed format submission preserves its own input during concurrent edits', async () => {
    const a = tab(state());
    a.type('x+(');
    const pending = a.run('submitAnswer()');
    const b = tab(state(), a.shared);
    b.type('x^2+C');
    a.respond({error: '括号未闭合'}, 422);
    await pending;
    assert.equal(a.input.value, 'x+(');
    assert.equal(a.run('recoveries()[0].answer'), 'x+(');
    assert.equal(b.run('getDraft(draftKey()).answer'), 'x^2+C');
    assert.equal(a.elements.get('#previewStatus').textContent, '括号未闭合');
  });

  await check('skip clears the displayed draft without removing a concurrent new one', async () => {
    const a = tab(state());
    a.type('x');
    const pending = a.run("runAction('skip')");
    const b = tab(state(5), a.shared);
    b.type('retry');
    a.respond(state(5, {instance_id: 'question-2', progress: {current: 2, total: 4}}));
    await pending;
    assert.equal(b.run('getDraft(draftKey()).answer'), 'retry');
    assert.equal(a.input.value, '');
  });

  await check('409 fetches latest progress while retaining the rejected local expression', async () => {
    const a = tab(state());
    a.type('exp(x^2)/2+C');
    const b = tab(state(5), a.shared);
    b.type('x^2+C');
    const pending = a.run('submitAnswer()');
    a.respond({error: 'stale'}, 409);
    // Drain the fetch/JSON promise chain across both VM realms.
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(a.requests[0]?.url, '/api/state');
    a.respond(state(5));
    await pending;
    assert.equal(a.input.value, 'x^2+C');
    assert.equal(a.run('recoveries()[0].answer'), 'exp(x^2)/2+C');
    assert.equal(tab(state(5), a.shared, a.session).run('recoveries()[0].answer'), 'exp(x^2)/2+C');
  });

  await check('different question and completed answers offer copying but never restoration', async () => {
    const a = tab(state());
    a.type('</textarea><script>alert(1)</script>');
    a.run('preserveDraft(draftKey(), getDraft(draftKey()))');
    const next = tab(state(5, {instance_id: 'question-2', progress: {current: 2, total: 4}}), a.shared, a.session);
    const markup = next.elements.get('#draftRecoveries').innerHTML;
    assert.match(markup, /copy_draft/);
    assert.doesNotMatch(markup, /restore_draft|<script>/);
    await next.run("handleRecovery('restore_draft', recoveries()[0].key)");
    assert.equal(next.input.value, '');
    const completed = tab(state(5, {last_answer: 'x^2+C', feedback: {verdict: 'correct', answer_complete: true}}), a.shared, a.session);
    assert.equal(completed.input.readOnly, true);
    assert.equal(completed.input.value, 'x^2+C');
    await completed.run("handleRecovery('restore_draft', draftKey())");
    assert.equal(completed.input.value, 'x^2+C');
    assert.doesNotMatch(completed.elements.get('#draftRecoveries').innerHTML, /restore_draft/);
  });

  await check('reset and restart discard old shared/private copies, across tabs and reloads', () => {
    for (const revision of [5, Number.MAX_SAFE_INTEGER]) {
      const a = tab(state());
      a.type('x+C');
      a.run('preserveDraft(draftKey(), getDraft(draftKey()))');
      const b = tab(state(), a.shared);
      b.type('x^2+C');
      b.run('preserveDraft(draftKey(), getDraft(draftKey()))');
      a.run(`clearParticipantDrafts(state.participant, ${revision})`);
      assert.equal(a.run('getDraft(draftKey())'), null);
      assert.equal(a.run('recoveries().length'), 0);
      assert.equal(b.run('getDraft(draftKey())'), null);
      assert.equal(tab(state(5), a.shared, b.session).run('recoveries().length'), 0);
    }
  });

  await check('restart response does not clear drafts made at the committed new revision', () => {
    const a = tab(state());
    a.type('old');
    const b = tab(state(5), a.shared);
    b.type('fresh');
    a.run('clearParticipantDrafts(state.participant, 5)');
    assert.equal(b.run('getDraft(draftKey()).answer'), 'fresh');
  });

  await check('another tester never sees or loses the previous tester drafts', () => {
    const a = tab(state());
    a.type('private');
    a.run('preserveDraft(draftKey(), getDraft(draftKey()))');
    const other = tab(state(1, {participant: 'new-person'}), a.shared, a.session);
    assert.equal(other.input.value, '');
    assert.equal(other.run('recoveries().length'), 0);
    other.type('other');
    a.run('clearParticipantDrafts(state.participant, Number.MAX_SAFE_INTEGER)');
    assert.equal(other.run('getDraft(draftKey()).answer'), 'other');
  });

  await check('legacy drafts are restored and receive independent IDs on edit', () => {
    const a = tab(state());
    a.shared.setItem(a.run('draftKey()'), JSON.stringify({answer: 'legacy', revision: 4}));
    const b = tab(state(), a.shared);
    assert.equal(b.input.value, 'legacy');
    assert.match(b.input.dataset.draftId, /^legacy:/);
    b.type('edited');
    assert.ok(b.run('getDraft(draftKey()).edit_id'));
    b.run('clearDraft(draftKey(), getDraft(draftKey()).edit_id)');
    assert.equal(b.run('getDraft(draftKey())'), null);
  });

  await check('unavailable storage preserves page memory and clearly warns against refreshing', () => {
    const unavailable = {getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); }, get length() { throw Error('blocked'); }};
    const a = tab(state(), unavailable, unavailable);
    a.type('x+C');
    assert.equal(a.run('getDraft(draftKey()).answer'), 'x+C');
    assert.match(a.elements.get('#draftStatus').textContent, /刷新或关闭页面会丢失/);
    a.run('preserveDraft(draftKey(), getDraft(draftKey())); updateRecoveries()');
    assert.equal(a.run('recoveries().length'), 1);
    assert.match(a.elements.get('#draftRecoveries').innerHTML, /请先复制/);
    a.run('clearParticipantDrafts(state.participant, 5)');
    assert.equal(a.run('recoveries().length'), 0);
  });

  await check('assessment recovery contains only entered text and cannot restore a different assessment item', () => {
    const a = tab(state(4, {phase: 'post', stage: null}));
    a.type('my expression');
    a.run('preserveDraft(draftKey(), getDraft(draftKey()))');
    const next = tab(state(5, {phase: 'post', stage: null, instance_id: 'post-2', progress: {current: 2, total: 2}}), a.shared, a.session);
    assert.equal(next.input.value, '');
    const markup = next.elements.get('#draftRecoveries').innerHTML;
    assert.match(markup, /my expression/);
    assert.doesNotMatch(markup, /restore_draft|参考结果|正确答案|讲解/);
  });
  console.log(`Draft regressions: ${passed} passed.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
