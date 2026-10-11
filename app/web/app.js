'use strict';

let state = null;
let busy = false;
let toastTimer = null;
let previewTimer = null;
let previewController = null;
let previewSequence = 0;
const draftPrefix = 'lingkao:draft:v1:';
const pageDrafts = new Map();
const pageAIDrafts = new Map();
const recoveryDrafts = new Map();
let storageAvailable = true;
let recoveryStorageAvailable = true;
let draftConflict = false;
let draftSequence = 0;
let aiPollTimer = null;
let aiPollFailures = 0;
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const phaseNames = {setup: '开始与范围', diagnostic: '起点诊断', plan: '单元训练', training: '单元训练', post: '独立复测', report: '学习报告'};
const outcomes = {correct: '完整作答', incomplete: '答案不完整', incorrect: '结果未通过', skipped: '暂时跳过', ungraded: '待核对'};
const presets = ['第一步怎么选？', '这个方法什么时候不能用？', '怎样检查结果？'];
const templates = [
  {label: '( )', title: '插入括号', before: '(', after: ')'},
  {label: '分数', title: '插入分式；选中文字作为分子', before: '(', after: ')/()', fraction: true},
  {label: '×', title: '插入乘号', before: '*', after: ''},
  {label: 'xⁿ', title: '插入幂指数', before: '^(', after: ')'},
  {label: 'sin', title: '插入 sin 函数', before: 'sin(', after: ')'},
  {label: 'cos', title: '插入 cos 函数', before: 'cos(', after: ')'},
  {label: 'exp', title: '插入指数函数', before: 'exp(', after: ')'},
  {label: 'ln', title: '插入自然对数', before: 'ln(', after: ')'},
  {label: '√', title: '插入平方根', before: 'sqrt(', after: ')'},
  {label: '+ C', title: '插入积分常数', before: '+C', after: ''},
];

function participantPrefix(participant = state.participant) {
  return `${draftPrefix}${encodeURIComponent(participant)}:`;
}

function draftKey(value = state) {
  if (!value?.question || !value.instance_id) return null;
  if (value.question.ref && value.question.run_id) {
    return `${participantPrefix(value.participant)}question:${encodeURIComponent(value.question.ref)}:${encodeURIComponent(value.question.version)}:${encodeURIComponent(value.question.run_id)}`;
  }
  const location = value.stage === 'foundation' ? 'foundation' : value.progress.current;
  return `${participantPrefix(value.participant)}${encodeURIComponent(value.course.version)}:${value.phase}:${value.stage || ''}:${location}`;
}

function readStored(key) {
  if (!storageAvailable) return null;
  try { return JSON.parse(localStorage.getItem(key) || 'null'); }
  catch { storageAvailable = false; return null; }
}

function writeStored(key, value) {
  if (!storageAvailable) return;
  try { localStorage.setItem(key, JSON.stringify(value)); }
  catch { storageAvailable = false; }
}

function latestRevision(participant = state.participant) {
  return Number(readStored(`${participantPrefix(participant)}revision`) || 0);
}

function markRevision(value = state) {
  const key = `${participantPrefix(value.participant)}revision`;
  writeStored(key, Math.max(Number(value.revision || 0), latestRevision(value.participant)));
}

function newDraftId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}-${++draftSequence}`;
}

function draftId(draft) {
  // Old browser drafts remain readable; the next edit receives its own identifier.
  return draft?.edit_id || (draft && `legacy:${draft.revision}:${draft.answer}`);
}

function clearedKey(key, id) {
  return `${key}:submitted:${encodeURIComponent(id)}`;
}

function resetRevision(participant = state.participant) {
  return Number(readStored(`${participantPrefix(participant)}reset_revision`) || 0);
}

function usableDraft(key, draft) {
  return draft && !draft.cleared && typeof draft.answer === 'string'
    && Number(draft.revision || 0) >= resetRevision()
    && !readStored(clearedKey(key, draftId(draft)));
}

function getDraft(key) {
  const stored = readStored(key);
  const memory = pageDrafts.get(key);
  const result = storageAvailable ? stored : memory;
  return usableDraft(key, result) ? result : null;
}

function recoveryLabel() {
  if (state.question?.title) return state.question.title;
  return `${phaseNames[state.phase]} · ${state.stage === 'foundation' ? '基础补练' : `第 ${state.progress.current} 题`}`;
}

function preserveDraft(key, value) {
  if (!key || !value?.answer || value.revision < resetRevision()) return;
  const draft = {...value, label: value.label || recoveryLabel()};
  recoveryDrafts.set(key, draft);
  if (recoveryStorageAvailable) {
    try { sessionStorage.setItem(key, JSON.stringify(draft)); }
    catch { recoveryStorageAvailable = false; }
  }
}

function removeRecovery(key, id = null) {
  const memory = recoveryDrafts.get(key);
  if (!id || draftId(memory) === id) recoveryDrafts.delete(key);
  if (recoveryStorageAvailable) {
    try {
      const stored = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (!id || draftId(stored) === id) sessionStorage.removeItem(key);
    } catch { recoveryStorageAvailable = false; }
  }
}

function recoveries() {
  const prefix = participantPrefix();
  const keys = new Set([...recoveryDrafts.keys()].filter(key => key.startsWith(prefix)));
  if (recoveryStorageAvailable) {
    try {
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const key = sessionStorage.key(index);
        if (key?.startsWith(prefix)) {
          keys.add(key);
          recoveryDrafts.set(key, JSON.parse(sessionStorage.getItem(key) || 'null'));
        }
      }
    } catch { recoveryStorageAvailable = false; }
  }
  return [...keys].flatMap(key => {
    const value = recoveryDrafts.get(key);
    if (!usableDraft(key, value)) {
      removeRecovery(key);
      return [];
    }
    return [{key, ...value}];
  });
}

function updateRecoveries() {
  const panel = $('#draftRecoveries');
  if (!panel) return;
  panel.innerHTML = recoveries().map(draft => {
    const canRestore = draft.key === draftKey() && !completeFeedback() && latestRevision() <= state.revision;
    return `<section class="note sand draft-recovery"><strong>此页面保留的输入副本 · ${esc(draft.label)}</strong><p>其他页面更新了进度，此副本尚未提交。${recoveryStorageAvailable ? '刷新后仍可查看；关闭此标签页会清除副本。' : '浏览器无法保存此副本，请先复制；刷新或关闭页面会丢失。'}${canRestore ? '可主动恢复到当前题目。' : '当前不可恢复到输入框，可先复制保留。'}</p><textarea class="recovery-answer" readonly aria-label="未提交的输入副本">${esc(draft.answer)}</textarea><div class="question-actions">${canRestore ? `<button type="button" class="btn secondary small" data-action="restore_draft" data-recovery-key="${esc(draft.key)}">用此副本替换当前输入</button>` : ''}<button type="button" class="btn secondary small" data-action="copy_draft" data-recovery-key="${esc(draft.key)}">复制副本</button><button type="button" class="btn ghost small" data-action="discard_draft" data-recovery-key="${esc(draft.key)}">删除副本</button></div></section>`;
  }).join('');
}

function updateDraftStatus(restored = false) {
  const status = $('#draftStatus');
  const input = $('#answer');
  if (!status || !input) return;
  if (draftConflict) {
    status.textContent = state.revision < resetRevision()
      ? '其他页面已清除或重新开始学习。此页旧输入不会恢复；需要保留请先复制，再刷新。'
      : recoveryStorageAvailable
      ? '其他页面已更新进度。此页输入已单独保留，刷新后可在输入副本中恢复或复制。'
      : '其他页面已更新进度，浏览器无法保存此页输入；请先复制，再刷新。';
    status.classList.add('format-error');
    return;
  }
  status.classList.remove('format-error');
  if (state.phase === 'training' && completeFeedback()) {
    status.textContent = '本题已完整提交，答案可复制；选择下一步继续训练。';
    return;
  }
  const draft = getDraft(input.dataset.draftKey);
  if (draft) {
    status.textContent = storageAvailable
      ? `${restored ? '已恢复草稿' : '草稿已保存到此浏览器'} · 尚未提交，不计入作答记录。`
      : '浏览器无法保存草稿，输入仅在本页保留；刷新或关闭页面会丢失。尚未提交。';
  } else {
    status.textContent = state.phase === 'training' && typeof state.last_answer === 'string'
      ? '已恢复最近一次提交；修改后需要再次核对，才会新增记录。'
      : storageAvailable ? '输入会在此浏览器自动保存；点击提交后才计入记录。' : '浏览器无法保存草稿，输入仅在本页保留；刷新或关闭页面会丢失。';
  }
}

function saveDraft(newEdit = true) {
  const input = $('#answer');
  const key = draftKey();
  if (!input || !key || busy || input.readOnly || input.dataset.instance !== state.instance_id) return;
  const value = {answer: input.value, revision: state.revision, edit_id: !newEdit && input.dataset.draftId ? input.dataset.draftId : newDraftId()};
  input.dataset.draftId = value.edit_id;
  const stored = readStored(key);
  if (latestRevision() > state.revision || Number(stored?.revision || 0) > state.revision) {
    draftConflict = true;
    if (value.answer) preserveDraft(key, value);
    else removeRecovery(key);
    updateDraftStatus();
    updateRecoveries();
    return;
  }
  draftConflict = false;
  pageDrafts.set(key, value);
  writeStored(key, value);
  updateDraftStatus();
}

function clearDraft(key, id) {
  if (!key || !id) return;
  // Do not replace the shared draft: another tab may have edited it even at the
  // same server revision. A marker can only invalidate the submitted edit.
  writeStored(clearedKey(key, id), true);
  if (draftId(pageDrafts.get(key)) === id) pageDrafts.delete(key);
  removeRecovery(key, id);
}

function clearParticipantDrafts(participant, revision) {
  const prefix = participantPrefix(participant);
  // The revision barrier also invalidates copies held by other tabs without
  // deleting drafts created after the restart response was committed.
  writeStored(`${prefix}reset_revision`, Math.max(revision, resetRevision(participant)));
  for (const key of pageDrafts.keys()) if (key.startsWith(prefix)) pageDrafts.delete(key);
  for (const key of pageAIDrafts.keys()) if (key.startsWith(prefix)) pageAIDrafts.delete(key);
  for (const key of recoveryDrafts.keys()) if (key.startsWith(prefix)) removeRecovery(key);
  if (recoveryStorageAvailable) {
    try {
      const keys = [];
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const key = sessionStorage.key(index);
        if (key?.startsWith(prefix)) keys.push(key);
      }
      for (const key of keys) sessionStorage.removeItem(key);
    } catch { recoveryStorageAvailable = false; }
  }
  if (revision === Number.MAX_SAFE_INTEGER && storageAvailable) {
    try {
      const keys = [];
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (key?.startsWith(prefix) && !key.endsWith(':revision') && !key.endsWith(':reset_revision')) keys.push(key);
      }
      for (const key of keys) localStorage.removeItem(key);
    } catch { storageAvailable = false; }
  }
  writeStored(`${prefix}revision`, Math.max(revision, latestRevision(participant)));
}

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 6500);
}

function button(text, action, className = '', disabled = false) {
  return `<button type="button" class="btn ${className}" data-action="${esc(action)}" ${disabled ? 'disabled' : ''}>${text}</button>`;
}

function tag(outcome) {
  return `<span class="tag ${outcome === 'correct' ? 'green' : ['incorrect', 'incomplete'].includes(outcome) ? 'amber' : ''}">${esc(outcomes[outcome] || outcome)}</span>`;
}

function shell(body) {
  const current = ['setup', 'diagnostic', 'plan', 'post', 'report'].indexOf(state.phase === 'training' ? 'plan' : state.phase);
  return `<div class="layout"><aside class="sidebar">
    <div class="brand"><span class="mark" aria-hidden="true">∫</span>临考</div>
    <div class="brand-caption">从不会，到独立做对</div><div class="nav-label">本次学习</div>
    <nav class="nav" aria-label="学习步骤">${['开始与范围', '起点诊断', '单元训练', '独立复测', '学习报告'].map((label, index) => `<div class="nav-item ${current === index ? 'active' : current > index ? 'done' : ''}" ${current === index ? 'aria-current="step"' : ''}><span class="nav-no">${current > index ? '✓' : String(index + 1).padStart(2, '0')}</span>${label}</div>`).join('')}</nav>
    <div class="sidebar-foot"><div>本地试验版 · v${esc(state.version)}</div><p>换元积分单元<br>诊断、训练、独立复测</p></div>
  </aside><div class="workspace"><header class="topbar"><div class="breadcrumb">高等数学 <span>／</span><strong>${phaseNames[state.phase]}</strong></div><div class="status"><span class="dot"></span>预设讲解 · 未接AI</div></header>
    <div class="utility-bar"><span>进度自动保存到本机</span><div>${button('导出记录', 'export', 'ghost small', !state.export_allowed)}${button('清除／更换测试者', 'reset', 'ghost small')}</div></div>
    <main class="main" id="main"><div id="draftRecoveries"></div>${body}<div class="footer-note">仅本机试用 · 学习记录保存在本机数据库 · 不使用在线模型</div></main>
  </div></div>`;
}

function setup() {
  return `<div class="eyebrow">高数积分 · 一个单元，一次完整练习</div><div class="hero-grid">
    <section class="hero-copy"><h1>时间不多，<br>先把下一道题<br><span class="green-text">独立做出来。</span></h1><p class="lead">从一道诊断题开始。看一次完整示范，练习不同结构，再用未见过的新题检查自己。</p><div class="hero-steps"><div class="step-pill"><span>01</span>找到起点</div><div class="step-pill"><span>02</span>示范与练习</div><div class="step-pill"><span>03</span>独立复测</div></div></section>
    <section class="card setup-card"><div class="card-label">开始换元积分训练</div><h2>留一点时间，完整练一次。</h2><p class="muted spaced">本次练习包含必要知识、示范、引导练习、独立变式与复测。</p><div class="field"><label for="minutes">本次可投入时间／分钟</label><input id="minutes" type="number" value="${esc(state.minutes || 30)}" min="15" max="240" step="1"><div class="field-note">只是你的时间预算，不是倒计时，也不会影响作答结果。</div></div><div class="note spaced">可以在纸上推导，再输入最终表达式。不会的题可以跳过。</div>${button('开始起点诊断 →', 'start', 'full')}<div class="field-note">诊断与复测不开放提示，完成后再显示结果。</div></section>
  </div><div class="scope-strip"><strong>本次范围</strong><div class="scope-tags"><span class="tag green">换元积分</span><span class="tag">可选基础补练</span><span class="tag">最终表达式核对</span></div></div><div class="principles"><div class="principle"><div class="number">01 / 有下一步</div><h3>沿着一个单元学下去</h3><p>每一步都能暂停，回来继续保存的位置。</p></div><div class="principle"><div class="number">02 / 需要时有帮助</div><h3>分层提示与完整讲解</h3><p>查看帮助会留下记录，预设答疑无需联网。</p></div><div class="principle"><div class="number">03 / 看见真实证据</div><h3>看懂后，再独立做一次</h3><p>首次作答、重试与辅助完成分别记录。</p></div></div><p class="tiny-note">${esc(state.course.scope)}</p>`;
}

function progressMarkup() {
  const {current, total, label} = state.progress;
  return `<div class="progress-caption"><span>${esc(label)}</span><span>第 ${esc(current)} / ${esc(total)} 题</span></div><progress class="learning-progress" value="${Math.max(0, current - 1)}" max="${Math.max(1, total)}" aria-label="当前题目进度">${esc(current)} / ${esc(total)}</progress>`;
}

function answerBox() {
  const completed = state.phase === 'training' && completeFeedback();
  return `<label class="answer-label" for="answer">${state.question.kind === 'indefinite' ? '填写原函数，完整作答请加 +C' : '填写最终常数结果'}</label>
    <div class="math-toolbar" role="group" aria-label="数学输入工具">${templates.map((item, index) => `<button type="button" class="math-key" data-template="${index}" title="${item.title}" aria-label="${item.title}" ${completed ? 'disabled' : ''}>${item.label}</button>`).join('')}</div>
    <input id="answer" class="answer-input" data-instance="${esc(state.instance_id)}" data-draft-key="${esc(draftKey())}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="180" ${completed ? 'readonly' : ''} aria-describedby="draftStatus answerError syntax previewStatus" placeholder="输入最终数学表达式">
    <div id="answerError" class="answer-error" role="alert"></div><div id="draftStatus" class="draft-status" role="status"></div>
    <div class="formula-preview" aria-label="公式输入预览"><div class="preview-label">公式预览 · 仅检查输入格式</div><div id="previewMath" class="preview-math"></div><div id="previewStatus" class="preview-status" aria-live="polite">输入后显示公式，不核对对错，也不记录作答。</div></div>
    <details class="syntax" id="syntax"><summary>输入方法与示例</summary><p>乘法用 <code>*</code>，幂用 <code>^</code>。例如 <code>x^3/3+C</code>、<code>(x+2)/(x+3)</code>；这些只是输入示例，并非当前题目的答案。</p><p>支持 <code>sin(x)</code>、<code>cos(x)</code>、<code>exp(x)</code>、<code>ln(x)</code>、<code>sqrt(x)</code>。只填表达式，不用写等号。选中文字后点「分数」，可将其放入分子，再输入分母。</p></details>`;
}

function assessment() {
  const isPost = state.phase === 'post';
  return `<div class="assessment-wrap"><div class="section-head"><div><div class="eyebrow">${isPost ? '未见过的新题 · 独立完成' : '先了解起点 · 不要求全会'}</div><h1>${isPost ? '关掉提示，自己做一次。' : '从这道题开始。'}</h1><p class="lead">${isPost ? '自己判断怎么做。全部完成后统一显示结果；同一套题只有一次新题复测机会。' : '可以在纸上写步骤，再输入结果。不会就跳过，后面会从必要知识和示范开始。'}</p></div></div>${progressMarkup()}<section class="card question-card"><div class="q-meta"><span>计算下面的积分</span><span class="tag">提示与答疑已关闭</span></div><div class="formula">${state.question.formula_html}</div>${answerBox()}<div class="question-actions">${button('提交并继续 →', 'answer')}${button('暂时不会，跳过', 'skip', 'ghost')}</div><div id="busyText" class="busy-indicator" role="status"></div><p class="tiny-note">只核对最终表达式，暂不批改完整推导过程。</p></section><div class="note neutral assess-note">测评期间暂不可导出记录，完成后恢复。${isPost ? '本组只检查换元积分训练，不代表整门高数的考试水平。' : '一道题的结果只作为本单元的学习起点。'}</div>${state.legacy_mode ? `<div class="assessment-foot"><span>正在完成旧版冻结测评；已展示的题会保留已见记录。</span>${button('提前结束旧版测评', 'end_assessment', 'ghost')}</div>` : ''}</div>`;
}

function plan() {
  const planState = state.plan;
  return `<div class="section-head"><div><div class="eyebrow">换元积分 · 训练安排</div><h1>${planState.completed ? '训练已走完，再独立检查。' : planState.can_resume ? '从上次的位置继续。' : '先看方法，再动手练。'}</h1><p class="lead">${esc(planState.reason)}</p></div></div><section class="card unit single-unit"><div class="unit-top"><span class="unit-number">本次训练单元</span><span class="tag ${planState.completed ? 'green' : ''}">${planState.completed ? '已走完训练' : planState.can_resume ? '学习中 · 进度已保存' : '准备开始'}</span></div><h2>换元积分</h2><p class="lead">找到内层函数，检查导数与系数能否匹配，最后把结果写完整。</p><div class="unit-route" aria-label="单元学习顺序"><span>必要知识与示范</span><span>引导练习</span><span>独立变式</span><span>新题复测</span></div><div class="note neutral spaced">本次时间预算 ${esc(state.minutes)} 分钟。训练共 ${esc(state.progress.total)} 道作答题；基础不熟时，可以进入短补练再回到原位置。</div><div class="question-actions">${planState.completed ? button('再练一次', 'restart_training', 'secondary') : button(planState.can_resume ? '继续保存的进度 →' : '开始单元 →', 'start_skill')}</div>${planState.completed ? '<p class="tiny-note">重练会保留已有作答与辅助记录。重复做对已见题，不会变成新的首次独立完成。</p>' : ''}</section><div class="bottom-callout spaced"><div><h3>${planState.post_done ? '新题复测已完成' : '练完后，看看能否自己完成新题。'}</h3><p>${planState.post_done ? '同一套复测不再重复开放。可以查看学习报告，或继续补练。' : planState.completed ? '提示、讲解和答疑将关闭；结果统一在复测结束后展示。' : '走完本轮训练后，开放独立复测。'}</p></div>${planState.post_done ? button('查看学习报告 →', 'report') : button('开始独立复测 →', 'post', '', !planState.completed)}</div>`;
}

function knowledgeMarkup(items) {
  return `<div class="knowledge-list">${(items || []).map(item => `<div><h3>${esc(item.title)}</h3><p>${esc(item.text)}</p></div>`).join('')}</div>`;
}

function stepsMarkup(steps) {
  return `<ol class="example-steps">${(steps || []).map((text, index) => `<li><span class="step-circle" aria-hidden="true">${index + 1}</span><span>${esc(text)}</span></li>`).join('')}</ol>`;
}

function exampleMarkup(lesson) {
  return `<h3 class="example-title">标准示范</h3><div class="formula">${lesson.example.question_html}</div>${stepsMarkup(lesson.example.steps)}<div class="review-answer">参考结果<div class="formula compact">${lesson.example.answer_html}</div></div>`;
}

function lesson() {
  const data = state.lesson;
  return `<div class="lesson"><div class="section-head"><div><div class="eyebrow">必要知识 · 先看一次完整示范</div><h1>${esc(data.name)}</h1><p class="lead">${esc(data.goal)}</p></div>${button('暂停，返回安排', 'plan', 'ghost')}</div><section class="card"><div class="card-label">把方法用对的几个关键</div><p class="rule">${esc(data.rule)}</p><div class="note sand">${esc(data.boundary)}</div>${knowledgeMarkup(data.knowledge)}<div class="divider"></div>${exampleMarkup(data)}<div class="question-actions">${button('看完示范，开始引导练习 →', 'begin_practice')}${button('先补一下基础', 'foundation', 'secondary')}</div><p class="tiny-note">阅读示范不会增加独立完成记录。之后练习的每次提示和作答会分别保留。</p></section></div>`;
}

function coach() {
  return `<aside class="card coach"><div class="coach-title"><span class="coach-icon" aria-hidden="true">∫</span><h3>当前题目的预设讲解</h3></div><p class="coach-subtitle">需要帮助时点选。使用讲解后，本题会记录为已用辅助。</p><div class="quick-actions">${presets.map(message => `<button type="button" class="quick" data-action="quick" data-message="${esc(message)}">${esc(message)} <span aria-hidden="true">↗</span></button>`).join('')}</div><div class="chat-history" id="chatHistory">${state.chat.map(message => `<div class="chat ${message.role === 'user' ? 'user' : ''}"><span class="chat-name">${message.role === 'user' ? '你的问题' : '预设讲解'}</span>${esc(message.content)}</div>`).join('')}</div><label class="answer-label spaced" for="chatInput">自由AI提问暂未开放</label><textarea id="chatInput" class="chat-input" disabled placeholder="本版仅提供上方三个预设讲解，不调用在线AI。"></textarea></aside>`;
}

function completeFeedback() {
  const feedback = state.feedback;
  return feedback?.verdict === 'correct' && feedback.answer_complete !== false && !feedback.constant_missing;
}

function training() {
  if (state.stage === 'lesson') return lesson();
  const foundation = state.stage === 'foundation';
  const independent = state.stage === 'transfer';
  const feedback = state.feedback;
  const done = completeFeedback();
  const hintRemaining = Math.max(0, state.hint_count - state.hints.length);
  return `<div class="section-head"><div><div class="eyebrow">换元积分 · ${foundation ? '基础补练' : independent ? '独立变式' : '引导练习'}</div><h1>${foundation ? '把必要的基础补上。' : independent ? '换一道题，先自己做。' : '把方法用在一道题上。'}</h1><p class="lead">${foundation ? '补练完成或暂时离开，都可以回到原来的训练位置。' : independent ? '先独立尝试。首次有效提交、未使用辅助、结果正确且完整，才留下首次独立完成的证据。' : '按需要查看分层提示，或看一遍完整讲解，再亲自写出结果。'}</p></div>${button('暂停，返回安排', 'plan', 'ghost')}</div>${foundation ? `<section class="card foundation-info mb"><h3>${esc(state.foundation_info.title)}</h3>${knowledgeMarkup(state.foundation_info.knowledge)}</section>` : progressMarkup()}<div class="training-grid"><section class="card question-card"><div class="q-meta"><span>计算下面的积分</span><span class="tag ${state.help_used ? 'amber' : 'green'}">${state.help_used ? '已使用辅助' : '本题尚未使用辅助'}</span></div><div class="formula">${state.question.formula_html}</div>${answerBox()}${feedback ? `<div class="feedback ${esc(feedback.verdict)}" role="status"><strong class="feedback-label">上次提交的结果</strong>${esc(feedback.message)}${feedback.verdict === 'correct' && !done ? '<p>可以补全后再次核对；首次答案不完整的记录会保留。</p>' : ''}</div>` : ''}<div class="question-actions">${!done ? button(feedback ? '修改后再核对' : '核对我的结果 →', 'answer') : ''}${feedback ? button(foundation ? '回到原训练 →' : done ? '进入下一步 →' : '记录当前结果，下一步', foundation ? 'return_training' : 'next', done ? '' : 'secondary') : button(foundation ? '暂时跳过，回原训练' : '暂时不会，跳过', 'skip', 'ghost')}</div><div id="busyText" class="busy-indicator" role="status"></div><div class="divider"></div><div class="help-heading"><h3>需要一点帮助？</h3><span>辅助操作会保留记录</span></div><div class="question-actions">${button(`分层提示 ${state.hints.length} / ${state.hint_count}`, 'hint', 'secondary small', !hintRemaining)}${button('回看标准示范', 'example', 'secondary small', Boolean(state.lesson))}${button('查看完整讲解', 'solution', 'secondary small', Boolean(state.solution))}</div>${state.hints.map((hint, index) => `<div class="hint-box">提示 ${index + 1}：${esc(hint)}</div>`).join('')}${state.lesson ? `<section class="example-review">${exampleMarkup(state.lesson)}</section>` : ''}${state.solution ? `<section class="example-review"><h3>本题完整讲解</h3>${stepsMarkup(state.solution.steps)}<div class="review-answer">参考结果<div class="formula compact">${state.solution.answer_html}</div></div></section>` : ''}<div class="question-actions">${foundation ? button('先回到原训练', 'return_training', 'ghost small') : button('基础不熟？进入短补练', 'foundation', 'ghost small')}</div><p class="tiny-note">${independent ? '做错后重试、漏写常数后补写，以及查看帮助后完成，都单独记录，不覆盖首次作答。' : foundation ? '基础补练不推进主线，也不计入独立变式或复测结果。' : '引导练习用于熟悉方法；独立完成的证据将在变式与复测中记录。'}</p></section>${coach()}</div>`;
}

function evidenceTag(row) {
  if (row.independent_complete) return `<span class="tag green">首次独立完整完成</span>${row.latest_outcome !== 'correct' ? `<span class="tag amber">最新：${esc(outcomes[row.latest_outcome] || row.latest_outcome)}</span>` : ''}`;
  if (row.latest_outcome === 'skipped') return '<span class="tag">跳过</span>';
  if (row.latest_outcome === 'ungraded') return '<span class="tag">待核对</span>';
  if (row.latest_outcome === 'correct' && row.assisted) return '<span class="tag amber">辅助完成</span>';
  if (row.latest_outcome === 'correct' && row.attempt_count > 1) return `<span class="tag">${row.first_outcome === 'incorrect' ? '错误后重试完成' : '重试后完成'}</span>`;
  if (row.latest_outcome === 'correct') return `<span class="tag">${row.stage === 'foundation' ? '基础补练完成' : '引导练习完成'}</span>`;
  return tag(row.latest_outcome);
}

function attemptsMarkup(row) {
  const attempts = Array.isArray(row.attempts) ? row.attempts : [];
  return `<details class="attempt-history"><summary>展开逐次作答 · ${esc(attempts.length)} 次记录</summary><ol>${attempts.map(attempt => `<li><div class="attempt-heading"><strong>第 ${esc(attempt.number)} 次</strong>${tag(attempt.outcome)}</div><div class="submitted-answer">${attempt.outcome === 'skipped' ? '未提交表达式（跳过）' : esc(attempt.answer)}</div><p class="attempt-note">${attempt.independent_complete ? '首次独立完整完成' : attempt.assisted ? '本次作答前已使用辅助' : '本次作答前未使用辅助'}${attempt.hints_used ? ` · 已查看 ${esc(attempt.hints_used)} 层提示` : ''}</p></li>`).join('')}</ol></details>`;
}

function helpSummaryMarkup(row) {
  const help = Array.isArray(row.help_summary) ? row.help_summary : [];
  if (!row.help_used && !help.length) return '';
  return `<p class="tiny-note evidence-note">本题辅助记录：${esc(help.join('、') || '使用过辅助')}。可能包含最后一次作答后的查看，不改变当时的作答记录。</p>`;
}

function report() {
  const data = state.report;
  const summaries = [
    {label: '诊断首次完整作答', value: data.diagnostic.complete, total: data.diagnostic.total, note: '本单元的起点记录'},
    {label: '变式首次独立完成', value: data.transfers.independent_complete, total: data.transfers.total, note: '首次、无辅助、答案完整'},
    {label: '复测首次独立完成', value: data.post.independent_complete, total: data.post.total, note: '训练中未展示的新题'},
  ];
  return `<div class="section-head"><div><div class="eyebrow">本次学习 · 具体作答证据</div><h1>看懂之后，自己做出来了吗？</h1><p class="lead">分别查看第一次作答与后续重试。结果只覆盖本次换元积分单元。</p></div>${button('导出学习记录 ↗', 'export', 'secondary')}</div><div class="stats-row">${summaries.map(item => `<div class="stat-card"><div class="stat-label">${item.label}</div><div class="stat-value">${item.value}<small> / ${item.total}题</small></div><div class="stat-sub">${item.note}</div></div>`).join('')}</div><div class="note"><strong>下一步：</strong>${esc(data.next_step)}</div><section class="card spaced"><h2>每道题的记录</h2><p class="tiny-note">首次记录不会被后续重试覆盖。下面的参考结果已展示，重做这些题不再属于未见题复测。</p><div class="record-table-wrap"><table class="record-table"><thead><tr><th scope="col">环节</th><th scope="col">首次结果</th><th scope="col">最新结果</th><th scope="col">作答证据</th></tr></thead><tbody>${data.rows.map(row => `<tr><th scope="row">${esc(row.label)}</th><td>${tag(row.first_outcome)}</td><td>${tag(row.latest_outcome)}</td><td>${evidenceTag(row)}<div class="record-detail">${row.attempt_count}次记录${row.assisted ? ' · 使用过辅助' : ''}</div></td></tr>`).join('')}</tbody></table></div></section><div class="report-reviews">${data.rows.map(row => `<section class="card"><div class="q-meta"><strong>${esc(row.label)}</strong>${evidenceTag(row)}</div><div class="formula">${row.formula_html}</div><div class="submitted-answer">最近一次提交：${esc(row.answer || '暂时跳过')}</div><div class="review-answer">参考结果<div class="formula compact">${row.answer_html}</div></div><p class="tiny-note">首次：${esc(outcomes[row.first_outcome])} · 最新：${esc(outcomes[row.latest_outcome])} · ${esc(row.attempt_count)}次记录</p>${helpSummaryMarkup(row)}${attemptsMarkup(row)}</section>`).join('')}</div><div class="end-buttons">${button('返回训练安排', 'plan')}${button('导出学习记录', 'export', 'secondary')}${button('清除／下一位测试者', 'reset', 'ghost')}</div><div class="note neutral spaced">本次只核对最终表达式，不评价纸笔推导过程。诊断与复测题尚未做难度等值标定，不能据此换算考试提分或通过概率。</div>`;
}

function cancelPreview() {
  clearTimeout(previewTimer);
  previewController?.abort();
  previewController = null;
  previewSequence += 1;
}

function render() {
  const oldInput = $('#answer');
  const oldDraft = oldInput ? {instance: oldInput.dataset.instance, answer: oldInput.value, focused: document.activeElement === oldInput, start: oldInput.selectionStart, end: oldInput.selectionEnd} : null;
  cancelPreview();
  markRevision();
  draftConflict = latestRevision() > state.revision;
  migrateBrowserDrafts();
  const modern = typeof isV3 === 'function' && isV3();
  const views = modern
    ? {setup: v3Setup, catalog: v3Catalog, review: v3Review, training: v3Training, assessment: v3Assessment, report: v3Report}
    : {setup, diagnostic: assessment, plan, training, post: assessment, report};
  $('#app').innerHTML = modern ? v3Shell((views[state.phase] || v3Catalog)()) : shell(views[state.phase]());
  const input = $('#answer');
  if (input) {
    const draft = completeFeedback() ? null : getDraft(draftKey());
    input.value = draft?.answer ?? (state.phase === 'training' ? state.last_answer || '' : '');
    input.dataset.draftId = draft ? draftId(draft) : '';
    updateDraftStatus(Boolean(draft));
    if (oldDraft?.instance === state.instance_id && oldDraft.focused) {
      input.focus({preventScroll: true});
      input.setSelectionRange(oldDraft.start, oldDraft.end);
    }
  }
  updateRecoveries();
  if (input?.value) schedulePreview();
  const history = $('#chatHistory');
  if (history) history.scrollTop = history.scrollHeight;
  restoreAIInput();
  scheduleAIPoll();
}

function migrateBrowserDrafts() {
  if (!state || !storageAvailable) return;
  for (const mapping of state.legacy_drafts || []) {
    const suffix = mapping.old_key_suffix || mapping.old_suffix;
    if (!suffix || !mapping.question_ref || !mapping.run_id) continue;
    const oldKey = participantPrefix() + suffix;
    const old = getDraft(oldKey);
    const newKey = `${participantPrefix()}question:${encodeURIComponent(mapping.question_ref)}:${encodeURIComponent(mapping.question_version)}:${encodeURIComponent(mapping.run_id)}`;
    if (old && !getDraft(newKey) && !readStored(`${oldKey}:migrated`)) {
      const value = {...old, edit_id: old.edit_id || newDraftId()};
      if (writeStored(newKey, value) !== false && readStored(newKey)?.answer === value.answer) {
        clearDraft(oldKey, draftId(old));
        writeStored(`${oldKey}:migrated`, true);
      }
    }
  }
  // Unmapped v2 inputs stay copyable, never silently enter a different question.
  if (typeof isV3 === 'function' && isV3()) {
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (!key?.startsWith(`${participantPrefix()}0.2-alpha.1:`)) continue;
        const value = getDraft(key);
        if (value?.answer) preserveDraft(key, {...value, label: '旧版草稿 · 请复制后核对对应题目'});
      }
    } catch { storageAvailable = false; }
  }
}

function aiDraftKey() {
  const q = state?.question;
  return q?.ref ? `${participantPrefix()}ai:${q.ref}:${q.version}:${q.run_id}` : null;
}
function aiRequestId(value = state) { return value?.ai_request?.request_id || value?.ai_request?.id; }
function readAIDraft(key) { return readStored(key) || pageAIDrafts.get(key); }
function writeAIDraft(key, value) {
  pageAIDrafts.set(key, value);
  writeStored(key, value);
}
function saveAIInput() {
  const key = aiDraftKey();
  const input = $('#aiMessage');
  if (key && input && !input.disabled) writeAIDraft(key, {message: input.value});
}
function restoreAIInput() {
  const key = aiDraftKey();
  const input = $('#aiMessage');
  if (!key || !input) return;
  const saved = readAIDraft(key);
  const status = state.ai_request?.status;
  if (saved?.message && ['completed', 'succeeded', 'success'].includes(status)) {
    const messages = (state.chat || []).filter(item => item.role === 'user');
    if (messages.at(-1)?.content === saved.message && saved.request_id === aiRequestId()) {
      writeAIDraft(key, {message: ''});
      input.value = '';
      return;
    }
  }
  input.value = saved?.message || '';
}
function scheduleAIPoll() {
  clearTimeout(aiPollTimer);
  if (state?.ai_request?.status !== 'pending') return;
  aiPollTimer = setTimeout(async () => {
    if (busy) return scheduleAIPoll();
    const participant = state.participant;
    try {
      const updated = await request('/api/state');
      aiPollFailures = 0;
      if (busy || state.participant !== participant || updated.participant !== participant || updated.revision < state.revision) return scheduleAIPoll();
      if (updated.revision !== state.revision || updated.ai_request?.status !== state.ai_request?.status) {
        saveDraft(false);
        saveAIInput();
        state = updated;
        render();
      } else scheduleAIPoll();
    } catch {
      aiPollFailures += 1;
      if (aiPollFailures === 3) toast('暂时连接不到本地服务，输入已保留。保持服务运行后会继续查询，不会重复发送问题。');
      scheduleAIPoll();
    }
  }, aiPollFailures ? 3000 : 1000);
}

async function request(path, payload, signal) {
  const options = {cache: 'no-store', signal};
  if (payload !== undefined) {
    Object.assign(options, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf}, body: JSON.stringify(payload)});
  }
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error('本地服务返回异常，请检查服务是否仍在运行。'); }
  if (!response.ok) {
    const error = new Error(data.error || '请求未完成，请重试。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function setBusy(message) {
  busy = true;
  cancelPreview();
  document.querySelectorAll('button, input, textarea').forEach(control => { control.disabled = true; });
  if ($('#busyText')) $('#busyText').textContent = message;
  $('#app').setAttribute('aria-busy', 'true');
}

async function mutate(path, payload, scroll = false) {
  if (busy) return;
  saveDraft(false);
  saveAIInput();
  const oldAIKey = aiDraftKey();
  const oldInstance = state.instance_id;
  const oldKey = draftKey();
  const oldParticipant = state.participant;
  const oldInput = $('#answer');
  const submittedDraft = oldInput && !oldInput.readOnly
    ? {answer: oldInput.value, edit_id: oldInput.dataset.draftId || newDraftId(), revision: state.revision, label: recoveryLabel()}
    : null;
  setBusy(payload.action === 'answer' ? '正在核对并保存……' : '正在保存……');
  let errorMessage = '';
  let inputError = false;
  let keepFailedInput = false;
  try {
    state = await request(path, {...payload, revision: state.revision});
    if (path === '/api/tutor' && oldAIKey && aiRequestId()) {
      writeAIDraft(oldAIKey, {message: payload.message, request_id: aiRequestId()});
    }
    if (payload.action === 'answer' || payload.action === 'skip') clearDraft(oldKey, submittedDraft?.edit_id);
    if (path === '/api/reset') clearParticipantDrafts(oldParticipant, Number.MAX_SAFE_INTEGER);
    if (payload.action === 'restart_training') clearParticipantDrafts(oldParticipant, state.revision);
  } catch (error) {
    errorMessage = error.message;
    inputError = payload.action === 'answer' && error.status === 422;
    keepFailedInput = error.status !== 409 && error.status !== 403 && submittedDraft
      && draftId(getDraft(oldKey)) !== submittedDraft.edit_id;
    if (keepFailedInput) preserveDraft(oldKey, submittedDraft);
    if (error.status === 409 || error.status === 403 || path === '/api/tutor') {
      preserveDraft(oldKey, submittedDraft);
      try {
        state = await request('/api/state');
        errorMessage = path === '/api/tutor' && state.ai_request?.status === 'pending'
          ? '问题已发送，正在查询答疑结果。'
          : path === '/api/tutor' ? error.message : '其他页面已更新进度，或当前题目已变化。本次操作未记录，已载入最新进度。';
      } catch {
        errorMessage = '进度发生变化，但暂时无法连接服务。请刷新页面后再试，本次操作未记录。';
      }
    }
  } finally {
    busy = false;
    $('#app').removeAttribute('aria-busy');
    render();
    if (keepFailedInput && oldKey === draftKey() && $('#answer') && !$('#answer').readOnly) {
      $('#answer').value = submittedDraft.answer;
      $('#answer').dataset.draftId = submittedDraft.edit_id;
      draftConflict = true;
      updateDraftStatus();
      schedulePreview();
    }
    if (scroll || oldInstance !== state.instance_id) $('#main').scrollIntoView({block: 'start', behavior: 'instant'});
    if (inputError && $('#previewStatus')) {
      cancelPreview();
      $('#previewStatus').textContent = errorMessage;
      $('#previewStatus').classList.add('format-error');
      $('#answer').setAttribute('aria-invalid', 'true');
      $('#answer').focus({preventScroll: true});
    }
    if (errorMessage) toast(errorMessage);
  }
}

function runAction(action, extra = {}) {
  const payload = {action, ...extra};
  if (state.instance_id) payload.instance_id = state.instance_id;
  return mutate('/api/action', payload, ['start', 'start_skill', 'begin_practice', 'restart_training', 'post', 'plan', 'report', 'foundation', 'return_training', 'next', 'select', 'catalog', 'review', 'diagnostic', 'assess', 'end_assessment'].includes(action));
}

function schedulePreview() {
  cancelPreview();
  const input = $('#answer');
  if (!input || busy) return;
  const expression = input.value;
  const instance = input.dataset.instance;
  const sequence = previewSequence;
  $('#previewMath').innerHTML = '';
  $('#previewStatus').classList.remove('format-error');
  $('#previewStatus').textContent = expression.trim() ? '正在等待输入……' : '输入后显示公式，不核对对错，也不记录作答。';
  if (!expression.trim()) return;
  previewTimer = setTimeout(async () => {
    const controller = new AbortController();
    previewController = controller;
    const stillCurrent = () => sequence === previewSequence && state.instance_id === instance && $('#answer')?.value === expression;
    if (!stillCurrent()) return;
    $('#previewStatus').textContent = '正在解析输入格式……';
    try {
      const result = await request('/api/preview', {instance_id: instance, expression}, controller.signal);
      if (!stillCurrent()) return;
      if (result.valid) {
        $('#previewMath').innerHTML = result.mathml;
        $('#previewStatus').textContent = '以上仅为输入预览，尚未核对答案。';
      } else {
        $('#previewStatus').textContent = result.message;
        $('#previewStatus').classList.add('format-error');
      }
    } catch (error) {
      if (error.name === 'AbortError' || !stillCurrent()) return;
      $('#previewStatus').textContent = error.status === 409 ? '题目进度已变化，请刷新后继续输入。' : '暂时无法预览，可检查本地服务后重试；尚未提交答案。';
      $('#previewStatus').classList.add('format-error');
    }
  }, 350);
}

function insertTemplate(index) {
  const input = $('#answer');
  const template = templates[index];
  if (!input || !template || busy || input.readOnly) return;
  const start = input.selectionStart;
  const end = input.selectionEnd;
  const selected = input.value.slice(start, end);
  const insertion = template.after ? template.before + selected + template.after : template.before;
  if (input.value.length - (end - start) + insertion.length > 180) {
    toast('表达式最多180字，请先简化输入。');
    return;
  }
  input.setRangeText(insertion, start, end, 'end');
  input.focus({preventScroll: true});
  if (!template.after) {
    input.setSelectionRange(start + insertion.length, start + insertion.length);
  } else if (template.fraction && selected) {
    const denominator = start + insertion.length - 1;
    input.setSelectionRange(denominator, denominator);
  } else {
    const caret = start + template.before.length;
    input.setSelectionRange(caret, caret + selected.length);
  }
  answerEdited();
}

function answerEdited(preview = true) {
  if ($('#answerError')) $('#answerError').textContent = '';
  $('#answer')?.removeAttribute('aria-invalid');
  saveDraft();
  if (preview) schedulePreview();
}

async function handleRecovery(action, key, control) {
  const draft = recoveries().find(item => item.key === key);
  if (!draft) {
    updateRecoveries();
    return;
  }
  if (action === 'discard_draft') {
    removeRecovery(key);
    updateRecoveries();
    return;
  }
  if (action === 'copy_draft') {
    try {
      await navigator.clipboard.writeText(draft.answer);
      toast('已复制输入副本。');
    } catch {
      const input = control.closest('.draft-recovery').querySelector('textarea');
      input.focus();
      input.select();
      toast('已选中输入副本，请复制所选文字。');
    }
    return;
  }
  const input = $('#answer');
  if (!input || input.readOnly || key !== draftKey() || latestRevision() > state.revision) {
    toast('题目进度已变化，请先刷新；输入副本仍保留。');
    updateRecoveries();
    return;
  }
  input.value = draft.answer;
  answerEdited();
  if (!draftConflict) removeRecovery(key);
  updateRecoveries();
  input.focus({preventScroll: true});
}

function submitAnswer() {
  const input = $('#answer');
  if (!input || busy || completeFeedback()) return;
  if (state?.ai_request?.status === 'pending') {
    saveDraft(false);
    toast('答疑正在进行，可以继续编辑草稿；请等待回复或取消等待后再提交。');
    return;
  }
  if (!input.value.trim()) {
    cancelPreview();
    $('#answerError').textContent = '请先填写最终表达式；暂时不会时，可以选择跳过。';
    input.setAttribute('aria-invalid', 'true');
    input.focus({preventScroll: true});
    return;
  }
  saveDraft(false);
  return runAction('answer', {answer: input.value});
}

async function exportData() {
  if (!state.export_allowed || busy) return;
  try {
    const data = await request('/api/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `临考-学习记录-${state.participant}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast('已导出本次学习记录。');
  } catch (error) { toast(error.message); }
}

document.addEventListener('pointerdown', event => {
  if (event.target.closest('[data-template]')) event.preventDefault();
});

document.addEventListener('click', event => {
  const templateButton = event.target.closest('[data-template]');
  if (templateButton && !templateButton.disabled) return insertTemplate(Number(templateButton.dataset.template));
  const control = event.target.closest('[data-action]');
  if (!control || control.disabled || busy) return;
  const action = control.dataset.action;
  if (action === 'end_assessment' && state.legacy_mode) {
    if (!confirm('确定结束旧版测评吗？已展示的题会保留已见记录，之后进入新版题单。')) return;
    return runAction('end_assessment');
  }
  if (typeof isV3 === 'function' && isV3()) {
    if (action === 'time_pick') { $('#minutes').value = control.dataset.minutes; return; }
    if (action === 'filter_skill' || action === 'clear_filters') {
      listFilters = action === 'clear_filters' ? {skill: '', difficulty: '', status: '', search: ''} : {...listFilters, skill: control.dataset.skill};
      render(); return;
    }
    if (action === 'work_tab') {
      saveDraft(false); saveAIInput();
      workTab = control.dataset.tab;
      if (workTab === 'knowledge' && !state.lesson) return runAction('knowledge');
      render(); return;
    }
    if (action === 'toggle_tutor') { saveDraft(false); saveAIInput(); tutorOpen = !tutorOpen; render(); return; }
    if (action === 'select') return runAction('select', {question_ref: control.dataset.ref});
    if (action === 'budget' || action === 'new_session') {
      const minutes = Number($('#budgetMinutes').value);
      if (!Number.isInteger(minutes) || minutes < 15 || minutes > 240) return toast('请填写15到240之间的整数分钟。');
      return runAction('budget', {minutes, new_session: action === 'new_session'});
    }
    if (action === 'assess') return runAction('assess', {skill: control.dataset.skill, mode: control.dataset.mode || 'post'});
    if (action === 'end_assessment') {
      if (!confirm('提前结束后，已展示的题目将保留已见记录，未提交题标记为本轮未完成。确定结束吗？')) return;
      return runAction('end_assessment');
    }
    if (action === 'example') { workTab = 'knowledge'; return runAction(action); }
    if (action === 'solution') { workTab = 'solution'; return runAction(action); }
    if (action === 'send_ai') {
      const message = $('#aiMessage')?.value.trim();
      if (!message) return toast('先写下你对当前题目的问题。');
      if (message.length > 600) return toast('问题请控制在600字以内。');
      tutorOpen = true;
      return mutate('/api/tutor', {instance_id: state.instance_id, message});
    }
    if (action === 'cancel_ai') return runAction('cancel_ai', {request_id: control.dataset.requestId});
    if (action === 'quick') { tutorOpen = true; return runAction('preset', {message: control.dataset.message}); }
  }
  if (['restore_draft', 'copy_draft', 'discard_draft'].includes(action)) return handleRecovery(action, control.dataset.recoveryKey, control);
  if (action === 'export') return exportData();
  if (action === 'reset') {
    if (!confirm('确定清除当前测试者的全部学习记录吗？此操作无法恢复。需要保留的数据请先导出。')) return;
    return mutate('/api/reset', {}, true);
  }
  if (action === 'start') {
    const minutes = Number($('#minutes').value);
    if (!Number.isInteger(minutes) || minutes < 15 || minutes > 240) {
      toast('请填写15到240之间的整数分钟。');
      $('#minutes').focus();
      return;
    }
    return runAction(action, {minutes, diagnostic: Boolean($('#diagnosticChoice')?.checked)});
  }
  if (action === 'answer') return submitAnswer();
  if (action === 'skip' && ['diagnostic', 'post', 'assessment'].includes(state.phase)) {
    if (!confirm('跳过后会记录为“跳过”，本轮测评不能返回这道题。确定跳过吗？')) return;
  }
  if (action === 'quick') return mutate('/api/tutor', {instance_id: state.instance_id, message: control.dataset.message});
  return runAction(action);
});

document.addEventListener('input', event => {
  if (event.target.id === 'answer') answerEdited(!event.isComposing);
  if (event.target.id === 'aiMessage') saveAIInput();
  if (event.target.id === 'questionSearch') { listFilters.search = event.target.value; $('#questionRows').innerHTML = questionRows(); }
});
document.addEventListener('change', event => {
  if (event.target.id === 'difficultyFilter') listFilters.difficulty = event.target.value;
  else if (event.target.id === 'statusFilter') listFilters.status = event.target.value;
  else return;
  $('#questionRows').innerHTML = questionRows();
});
document.addEventListener('compositionend', event => {
  if (event.target.id === 'answer') answerEdited();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Enter' && event.target.id === 'answer' && !event.isComposing) {
    event.preventDefault();
    submitAnswer();
  }
});

window.addEventListener('storage', event => {
  if (state && event.key?.startsWith(participantPrefix()) && latestRevision() > state.revision) {
    draftConflict = true;
    const input = $('#answer');
    if (input && !input.readOnly) {
      const key = draftKey();
      const recovery = recoveryDrafts.get(key);
      // A refreshed tab may display the shared draft alongside an older local
      // copy. Further notifications must not overwrite that unresolved copy.
      if (!recovery || draftId(recovery) === input.dataset.draftId || draftId(pageDrafts.get(key)) === input.dataset.draftId) {
        preserveDraft(key, {answer: input.value, edit_id: input.dataset.draftId || newDraftId(), revision: state.revision});
      }
    }
    updateDraftStatus();
    updateRecoveries();
  }
});

(async () => {
  try {
    state = await request('/api/state');
    render();
  } catch (error) {
    $('#app').innerHTML = `<div class="initial"><h2>本地服务暂未连接</h2><p>请保持“一键启动”窗口运行，然后刷新此页面。</p><p>${esc(error.message)}</p><button class="btn secondary" type="button" id="reload">重新连接</button></div>`;
    $('#reload').addEventListener('click', () => window.location.reload());
  }
})();
