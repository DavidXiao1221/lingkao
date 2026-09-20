'use strict';

let state = null;
let busy = false;
let toastTimer = null;
let previewTimer = null;
let previewController = null;
let previewSequence = 0;
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const phaseNames = {setup: '开始与范围', diagnostic: '起点诊断', plan: '单元训练', training: '单元训练', post: '独立复测', report: '学习报告'};
const outcomes = {correct: '完整作答', incomplete: '答案不完整', incorrect: '结果未通过', skipped: '暂时跳过', ungraded: '待核对'};
const presets = ['第一步怎么选？', '这个方法什么时候不能用？', '怎样检查结果？'];
const templates = [
  {label: '( )', title: '插入括号', before: '(', after: ')'},
  {label: '分数', title: '插入分式', before: '(', after: ')/()'},
  {label: '×', title: '插入乘号', before: '*', after: ''},
  {label: 'xⁿ', title: '插入幂指数', before: '^(', after: ')'},
  {label: 'sin', title: '插入 sin 函数', before: 'sin(', after: ')'},
  {label: 'cos', title: '插入 cos 函数', before: 'cos(', after: ')'},
  {label: 'exp', title: '插入指数函数', before: 'exp(', after: ')'},
  {label: 'ln', title: '插入自然对数', before: 'ln(', after: ')'},
  {label: '√', title: '插入平方根', before: 'sqrt(', after: ')'},
  {label: '+ C', title: '插入积分常数', before: '+C', after: ''},
];

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
    <main class="main" id="main">${body}<div class="footer-note">仅本机试用 · 学习记录保存在本机数据库 · 不使用在线模型</div></main>
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
  return `<label class="answer-label" for="answer">${state.question.kind === 'indefinite' ? '填写原函数，完整作答请加 +C' : '填写最终常数结果'}</label>
    <div class="math-toolbar" role="group" aria-label="数学输入工具">${templates.map((item, index) => `<button type="button" class="math-key" data-template="${index}" title="${item.title}" aria-label="${item.title}">${item.label}</button>`).join('')}</div>
    <input id="answer" class="answer-input" data-instance="${esc(state.instance_id)}" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="180" aria-describedby="syntax previewStatus" placeholder="输入最终数学表达式">
    <div class="formula-preview" aria-label="公式输入预览"><div class="preview-label">公式预览 · 仅检查输入格式</div><div id="previewMath" class="preview-math"></div><div id="previewStatus" class="preview-status" aria-live="polite">输入后显示公式，不核对对错，也不记录作答。</div></div>
    <div class="syntax" id="syntax">乘法用 <code>*</code>，幂用 <code>^</code>；支持 <code>sin(x)</code>、<code>cos(x)</code>、<code>exp(x)</code>、<code>ln(x)</code>、<code>sqrt(x)</code>。只填表达式，不用写等号。</div>`;
}

function assessment() {
  const isPost = state.phase === 'post';
  return `<div class="assessment-wrap"><div class="section-head"><div><div class="eyebrow">${isPost ? '未见过的新题 · 独立完成' : '先了解起点 · 不要求全会'}</div><h1>${isPost ? '关掉提示，自己做一次。' : '从这道题开始。'}</h1><p class="lead">${isPost ? '自己判断怎么做。全部完成后统一显示结果；同一套题只有一次新题复测机会。' : '可以在纸上写步骤，再输入结果。不会就跳过，后面会从必要知识和示范开始。'}</p></div></div>${progressMarkup()}<section class="card question-card"><div class="q-meta"><span>计算下面的积分</span><span class="tag">提示与答疑已关闭</span></div><div class="formula">${state.question.formula_html}</div>${answerBox()}<div class="question-actions">${button('提交并继续 →', 'answer')}${button('暂时不会，跳过', 'skip', 'ghost')}</div><div id="busyText" class="busy-indicator" role="status"></div><p class="tiny-note">只核对最终表达式，暂不批改完整推导过程。</p></section><div class="note neutral assess-note">测评期间暂不可导出记录，完成后恢复。${isPost ? '本组只检查换元积分训练，不代表整门高数的考试水平。' : '一道题的结果只作为本单元的学习起点。'}</div></div>`;
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
  return `<div class="section-head"><div><div class="eyebrow">换元积分 · ${foundation ? '基础补练' : independent ? '独立变式' : '引导练习'}</div><h1>${foundation ? '把必要的基础补上。' : independent ? '换一道题，先自己做。' : '把方法用在一道题上。'}</h1><p class="lead">${foundation ? '补练完成或暂时离开，都可以回到原来的训练位置。' : independent ? '先独立尝试。首次有效提交、未使用辅助、结果正确且完整，才留下首次独立完成的证据。' : '按需要查看分层提示，或看一遍完整讲解，再亲自写出结果。'}</p></div>${button('暂停，返回安排', 'plan', 'ghost')}</div>${foundation ? `<section class="card foundation-info mb"><h3>${esc(state.foundation_info.title)}</h3>${knowledgeMarkup(state.foundation_info.knowledge)}</section>` : progressMarkup()}<div class="training-grid"><section class="card question-card"><div class="q-meta"><span>计算下面的积分</span><span class="tag ${state.help_used ? 'amber' : 'green'}">${state.help_used ? '已使用辅助' : '本题尚未使用辅助'}</span></div><div class="formula">${state.question.formula_html}</div>${answerBox()}${feedback ? `<div class="feedback ${esc(feedback.verdict)}" role="status">${esc(feedback.message)}${feedback.verdict === 'correct' && !done ? '<p>可以补全后再次核对；首次答案不完整的记录会保留。</p>' : ''}</div>` : ''}<div class="question-actions">${!done ? button(feedback ? '修改后再核对' : '核对我的结果 →', 'answer') : ''}${feedback ? button(foundation ? '回到原训练 →' : done ? '进入下一步 →' : '记录当前结果，下一步', foundation ? 'return_training' : 'next', done ? '' : 'secondary') : button(foundation ? '暂时跳过，回原训练' : '暂时不会，跳过', 'skip', 'ghost')}</div><div id="busyText" class="busy-indicator" role="status"></div><div class="divider"></div><div class="help-heading"><h3>需要一点帮助？</h3><span>辅助操作会保留记录</span></div><div class="question-actions">${button(`分层提示 ${state.hints.length} / ${state.hint_count}`, 'hint', 'secondary small', !hintRemaining)}${button('回看标准示范', 'example', 'secondary small', Boolean(state.lesson))}${button('查看完整讲解', 'solution', 'secondary small', Boolean(state.solution))}</div>${state.hints.map((hint, index) => `<div class="hint-box">提示 ${index + 1}：${esc(hint)}</div>`).join('')}${state.lesson ? `<section class="example-review">${exampleMarkup(state.lesson)}</section>` : ''}${state.solution ? `<section class="example-review"><h3>本题完整讲解</h3>${stepsMarkup(state.solution.steps)}<div class="review-answer">参考结果<div class="formula compact">${state.solution.answer_html}</div></div></section>` : ''}<div class="question-actions">${foundation ? button('先回到原训练', 'return_training', 'ghost small') : button('基础不熟？进入短补练', 'foundation', 'ghost small')}</div><p class="tiny-note">${independent ? '做错后重试、漏写常数后补写，以及查看帮助后完成，都单独记录，不覆盖首次作答。' : foundation ? '基础补练不推进主线，也不计入独立变式或复测结果。' : '引导练习用于熟悉方法；独立完成的证据将在变式与复测中记录。'}</p></section>${coach()}</div>`;
}

function evidenceTag(row) {
  if (row.independent_complete) return '<span class="tag green">首次独立完整完成</span>';
  if (row.latest_outcome === 'skipped') return '<span class="tag">跳过</span>';
  if (row.latest_outcome === 'ungraded') return '<span class="tag">待核对</span>';
  if (row.latest_outcome === 'correct' && row.assisted) return '<span class="tag amber">辅助完成</span>';
  if (row.latest_outcome === 'correct' && row.attempt_count > 1) return `<span class="tag">${row.first_outcome === 'incorrect' ? '错误后重试完成' : '重试后完成'}</span>`;
  if (row.latest_outcome === 'correct') return `<span class="tag">${row.stage === 'foundation' ? '基础补练完成' : '引导练习完成'}</span>`;
  return tag(row.latest_outcome);
}

function report() {
  const data = state.report;
  const summaries = [
    {label: '诊断首次完整作答', value: data.diagnostic.complete, total: data.diagnostic.total, note: '本单元的起点记录'},
    {label: '变式首次独立完成', value: data.transfers.independent_complete, total: data.transfers.total, note: '首次、无辅助、答案完整'},
    {label: '复测首次独立完成', value: data.post.independent_complete, total: data.post.total, note: '训练中未展示的新题'},
  ];
  return `<div class="section-head"><div><div class="eyebrow">本次学习 · 具体作答证据</div><h1>看懂之后，自己做出来了吗？</h1><p class="lead">分别查看第一次作答与后续重试。结果只覆盖本次换元积分单元。</p></div>${button('导出学习记录 ↗', 'export', 'secondary')}</div><div class="stats-row">${summaries.map(item => `<div class="stat-card"><div class="stat-label">${item.label}</div><div class="stat-value">${item.value}<small> / ${item.total}题</small></div><div class="stat-sub">${item.note}</div></div>`).join('')}</div><div class="note"><strong>下一步：</strong>${esc(data.next_step)}</div><section class="card spaced"><h2>每道题的记录</h2><p class="tiny-note">首次记录不会被后续重试覆盖。下面的参考结果已展示，重做这些题不再属于未见题复测。</p><div class="record-table-wrap"><table class="record-table"><thead><tr><th scope="col">环节</th><th scope="col">首次结果</th><th scope="col">最新结果</th><th scope="col">作答证据</th></tr></thead><tbody>${data.rows.map(row => `<tr><th scope="row">${esc(row.label)}</th><td>${tag(row.first_outcome)}</td><td>${tag(row.latest_outcome)}</td><td>${evidenceTag(row)}<div class="record-detail">${row.attempt_count}次记录${row.assisted ? ' · 使用过辅助' : ''}</div></td></tr>`).join('')}</tbody></table></div></section><div class="report-reviews">${data.rows.map(row => `<section class="card"><div class="q-meta"><strong>${esc(row.label)}</strong>${evidenceTag(row)}</div><div class="formula">${row.formula_html}</div><div class="submitted-answer">最近一次提交：${esc(row.answer || '暂时跳过')}</div><div class="review-answer">参考结果<div class="formula compact">${row.answer_html}</div></div><p class="tiny-note">首次：${esc(outcomes[row.first_outcome])} · 最新：${esc(outcomes[row.latest_outcome])} · ${row.attempt_count}次记录</p></section>`).join('')}</div><div class="end-buttons">${button('返回训练安排', 'plan')}${button('导出学习记录', 'export', 'secondary')}${button('清除／下一位测试者', 'reset', 'ghost')}</div><div class="note neutral spaced">本次只核对最终表达式，不评价纸笔推导过程。诊断与复测题尚未做难度等值标定，不能据此换算考试提分或通过概率。</div>`;
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
  const views = {setup, diagnostic: assessment, plan, training, post: assessment, report};
  $('#app').innerHTML = shell(views[state.phase]());
  const input = $('#answer');
  if (input && oldDraft?.instance === state.instance_id) {
    input.value = oldDraft.answer;
    if (oldDraft.focused) {
      input.focus({preventScroll: true});
      input.setSelectionRange(oldDraft.start, oldDraft.end);
    }
  }
  if (input?.value) schedulePreview();
  const history = $('#chatHistory');
  if (history) history.scrollTop = history.scrollHeight;
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
  const oldInstance = state.instance_id;
  setBusy(payload.action === 'answer' ? '正在核对并保存……' : '正在保存……');
  let errorMessage = '';
  let inputError = false;
  try {
    state = await request(path, {...payload, revision: state.revision});
  } catch (error) {
    errorMessage = error.message;
    inputError = payload.action === 'answer' && error.status === 422;
    if (error.status === 409 || error.status === 403) {
      try {
        state = await request('/api/state');
        errorMessage = '其他页面已更新进度，或当前题目已变化。本次操作未记录，已载入最新进度。';
      } catch {
        errorMessage = '进度发生变化，但暂时无法连接服务。请刷新页面后再试，本次操作未记录。';
      }
    }
  } finally {
    busy = false;
    $('#app').removeAttribute('aria-busy');
    render();
    if (scroll || oldInstance !== state.instance_id) window.scrollTo({top: 0, behavior: 'instant'});
    if (inputError && $('#previewStatus')) {
      cancelPreview();
      $('#previewStatus').textContent = errorMessage;
      $('#previewStatus').classList.add('format-error');
      $('#answer').focus({preventScroll: true});
    }
    if (errorMessage) toast(errorMessage);
  }
}

function runAction(action, extra = {}) {
  const payload = {action, ...extra};
  if (state.instance_id) payload.instance_id = state.instance_id;
  return mutate('/api/action', payload, ['start', 'start_skill', 'begin_practice', 'restart_training', 'post', 'plan', 'report', 'foundation', 'return_training', 'next'].includes(action));
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
  if (!input || !template || busy) return;
  const start = input.selectionStart;
  const end = input.selectionEnd;
  const selected = input.value.slice(start, end);
  const insertion = template.before + selected + template.after;
  if (input.value.length - (end - start) + insertion.length > 180) {
    toast('表达式最多180字，请先简化输入。');
    return;
  }
  input.setRangeText(insertion, start, end, 'end');
  input.focus({preventScroll: true});
  const caret = start + template.before.length;
  input.setSelectionRange(caret, caret + selected.length);
  schedulePreview();
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
    return runAction(action, {minutes});
  }
  if (action === 'answer') return runAction(action, {answer: $('#answer').value});
  if (action === 'quick') return mutate('/api/tutor', {instance_id: state.instance_id, message: control.dataset.message});
  return runAction(action);
});

document.addEventListener('input', event => {
  if (event.target.id === 'answer' && !event.isComposing) schedulePreview();
});
document.addEventListener('compositionend', event => {
  if (event.target.id === 'answer') schedulePreview();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Enter' && event.target.id === 'answer' && !event.isComposing) {
    event.preventDefault();
    if (!busy && !completeFeedback()) runAction('answer', {answer: event.target.value});
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
