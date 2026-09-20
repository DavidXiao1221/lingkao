"""Versioned local learning state machine; no model makes learning decisions."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent
VERSION = '0.2-alpha.1'
STATE_VERSION = 2
COURSE = json.loads((ROOT / 'course.json').read_text(encoding='utf-8'))
QUESTIONS = {q['id']: q for q in COURSE['questions']}
WORKER_SLOTS = threading.BoundedSemaphore(2)
ASSESSMENTS = ('diagnostic', 'post')
PRESETS = ('第一步怎么选？', '这个方法什么时候不能用？', '怎样检查结果？')
QUESTION_ACTIONS = {
    'begin_practice', 'answer', 'skip', 'next', 'hint', 'example',
    'solution', 'foundation', 'return_training', 'tutor',
}


class ActionError(ValueError):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def new_state():
    return {
        'version': STATE_VERSION, 'course_version': COURSE['version'],
        'grader_version': VERSION, 'revision': 0,
        'participant': secrets.token_hex(6), 'csrf': secrets.token_urlsafe(32),
        'created_at': now(), 'phase': 'setup', 'minutes': 30,
        'qid': None, 'instance_id': None, 'stage': None,
        'test_index': 0, 'main_index': 0, 'started': False,
        'completed': False, 'lesson_seen': False, 'active_foundation': False,
        'post_done': False, 'attempts': [], 'events': [], 'evidence': {},
        'feedbacks': {}, 'question_started_at': None,
    }


def event(s, name, **details):
    s['events'].append({'at': now(), 'event': name, **details})
    # Durable evidence is separate from this bounded diagnostic event stream.
    s['events'] = s['events'][-2000:]


def evidence_key(qid):
    return COURSE['version'] + ':' + qid


def evidence(s, qid):
    return s['evidence'].setdefault(evidence_key(qid), {
        'first_seen_at': now(), 'hints': [], 'help_used': False,
        'example_viewed': False, 'solution_viewed': False,
        'answer_exposed': False, 'chat': [],
    })


def _read_evidence(s, qid):
    return s['evidence'].get(evidence_key(qid), {})


def current_q(s):
    return QUESTIONS.get(s.get('qid'))


def _activate(s, qid, stage=None):
    s['qid'] = qid
    s['instance_id'] = secrets.token_urlsafe(24)
    s['question_started_at'] = now()
    s['stage'] = stage
    evidence(s, qid)
    event(s, 'question_presented', qid=qid, phase=s['phase'], stage=stage)


def _deactivate(s):
    s['instance_id'] = None
    s['stage'] = None


def _check_instance(s, payload):
    token = payload.get('instance_id')
    if not isinstance(token, str) or not s.get('instance_id') or token != s['instance_id']:
        raise ActionError('题目已经变化，本次操作未记录。请使用最新页面。', 409)


def _check_revision(s, payload):
    if type(payload.get('revision')) is not int or payload['revision'] != s['revision']:
        raise ActionError('学习进度已经更新，本次操作未记录。请使用最新页面。', 409)


def _worker(payload, preview_only=False):
    if not WORKER_SLOTS.acquire(timeout=0.3):
        raise ActionError('表达式处理正忙，请稍后重试；本次没有记录答案。', 503)
    try:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'grader.py')],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            text=True, encoding='utf-8', errors='strict', env=env, cwd=ROOT, timeout=4,
        )
        if proc.returncode:
            raise ValueError('worker failed')
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError('bad worker response')
        return result
    except (subprocess.TimeoutExpired, ValueError, UnicodeError, OSError):
        if preview_only:
            return {'valid': False, 'message': '预览未完成。可以简化表达式后重试；尚未提交答案。'}
        return {'verdict': 'ungraded', 'answer_complete': False,
                'message': '系统暂时无法可靠核对，记录为待核对，不记作答错。'}
    finally:
        WORKER_SLOTS.release()


def isolated_grade(answer, question):
    return _worker({'operation': 'grade', 'answer': answer, 'question': question})


def preview(s, payload):
    _check_instance(s, payload)
    if s['phase'] not in (*ASSESSMENTS, 'training') or s['stage'] == 'lesson':
        raise ActionError('当前不是作答环节。')
    expression = payload.get('expression')
    if not isinstance(expression, str) or len(expression) > 180:
        return {'valid': False, 'message': '请使用180字以内的数学表达式。'}
    # No question, reference solution, or state is sent to the parser.
    return _worker({'operation': 'preview', 'expression': expression}, preview_only=True)


def outcome(result):
    if result['verdict'] == 'correct' and not result.get('answer_complete', not result.get('constant_missing', False)):
        return 'incomplete'
    return result['verdict']


def _records(s, qid):
    return [a for a in s['attempts'] if a['qid'] == qid and a['course_version'] == COURSE['version']]


def _training_only(s, allow_lesson=False):
    if s['phase'] != 'training' or (not allow_lesson and s['stage'] == 'lesson'):
        raise ActionError('此操作仅在训练作答环节可用。')


def _activate_main(s):
    s['phase'] = 'training'
    qid = COURSE['training_order'][s['main_index']]
    _activate(s, qid, 'lesson' if not s['lesson_seen'] else QUESTIONS[qid]['phase'])


def _return_training(s):
    s['active_foundation'] = False
    _activate_main(s)
    event(s, 'foundation_returned')


def _next_training(s):
    if s['stage'] == 'foundation':
        _return_training(s)
        return
    s['main_index'] += 1
    if s['main_index'] >= len(COURSE['training_order']):
        s['completed'] = True
        s['phase'] = 'plan'
        _deactivate(s)
        event(s, 'unit_completed')
    else:
        _activate_main(s)


def _expose_report_answers(s):
    for qid in {a['qid'] for a in s['attempts']}:
        evidence(s, qid)['answer_exposed'] = True
    event(s, 'report_viewed')


def _next_test(s):
    order = COURSE['diagnostic_order' if s['phase'] == 'diagnostic' else 'post_order']
    s['test_index'] += 1
    if s['test_index'] < len(order):
        _activate(s, order[s['test_index']])
        return
    is_post = s['phase'] == 'post'
    s['phase'] = 'report' if is_post else 'plan'
    _deactivate(s)
    event(s, 'post_completed' if is_post else 'diagnostic_completed')
    if is_post:
        s['post_done'] = True
        _expose_report_answers(s)


def _answer(s, payload, skip=False):
    if s['phase'] not in (*ASSESSMENTS, 'training') or s['stage'] == 'lesson':
        raise ActionError('当前没有可以提交的题目。')
    q = current_q(s)
    previous = _records(s, q['id'])
    feedback = s['feedbacks'].get(q['id'])
    if s['phase'] == 'training' and feedback and outcome(feedback) == 'correct':
        raise ActionError('本题已经规范完成，请进入下一步。')
    answer = payload.get('answer', '')
    if not isinstance(answer, str) or len(answer) > 180:
        raise ActionError('请填写180字以内的数学表达式。')
    if not skip and len(previous) >= 10:
        raise ActionError('本题已尝试10次，可以查看讲解或暂时跳过。')
    result = ({'verdict': 'skipped', 'answer_complete': False, 'message': '已记录为暂时不会。'}
              if skip else isolated_grade(answer, q))
    if result.get('verdict') == 'invalid':
        raise ActionError(result['message'])
    if result.get('verdict') not in ('correct', 'incorrect', 'ungraded', 'skipped'):
        raise ActionError('判题返回异常，请重试；本次没有记录答案。', 503)
    result.setdefault('answer_complete', result['verdict'] == 'correct' and not result.get('constant_missing', False))
    e = evidence(s, q['id'])
    assisted = bool(e['help_used'] or e['answer_exposed'])
    independent = q['phase'] in ('diagnostic', 'transfer', 'post') and not previous and not assisted
    record = {
        'at': now(), 'started_at': s['question_started_at'],
        'qid': q['id'], 'course_version': COURSE['version'], 'grader_version': VERSION,
        'phase': q['phase'], 'answer': '' if skip else answer,
        'verdict': result['verdict'], 'outcome': outcome(result),
        'constant_missing': bool(result.get('constant_missing')),
        'answer_complete': bool(result['answer_complete']),
        'first_attempt': not previous, 'assisted': assisted,
        'independent': independent,
        'independent_complete': independent and outcome(result) == 'correct',
        'hints_used': len(e['hints']),
    }
    s['attempts'].append(record)
    event(s, 'answer_submitted', qid=q['id'], outcome=record['outcome'], assisted=assisted)
    if s['phase'] in ASSESSMENTS:
        _next_test(s)
    else:
        s['feedbacks'][q['id']] = copy.deepcopy(result)
        if skip:
            _next_training(s)


def _mutate(s, payload):
    name = payload.get('action')
    if name in QUESTION_ACTIONS or (name == 'plan' and s['phase'] == 'training'):
        _check_instance(s, payload)
    if name == 'start':
        if s['phase'] != 'setup':
            raise ActionError('本次学习已经开始。')
        minutes = payload.get('minutes', 30)
        if type(minutes) is not int or not 15 <= minutes <= 240:
            raise ActionError('学习预算应为15到240之间的整数分钟。')
        s.update(minutes=minutes, phase='diagnostic', test_index=0)
        _activate(s, COURSE['diagnostic_order'][0])
        event(s, 'session_started', minutes=minutes)
    elif name in ('start_skill', 'restart_training'):
        if s['phase'] not in ('plan', 'report'):
            raise ActionError('请先返回训练安排。')
        if name == 'restart_training':
            s.update(main_index=0, completed=False, lesson_seen=False, active_foundation=False, feedbacks={})
            event(s, 'training_restarted')
        elif s['completed']:
            raise ActionError('本轮训练已完成，可以复测或选择再练一次。')
        s['started'] = True
        if s['active_foundation']:
            s['phase'] = 'training'
            _activate(s, COURSE['foundation']['question_id'], 'foundation')
        else:
            _activate_main(s)
    elif name == 'begin_practice':
        if s['phase'] != 'training' or s['stage'] != 'lesson':
            raise ActionError('当前不是示范环节。')
        s['lesson_seen'] = True
        _activate_main(s)
    elif name in ('answer', 'skip'):
        _answer(s, payload, skip=name == 'skip')
    elif name == 'next':
        _training_only(s)
        if current_q(s)['id'] not in s['feedbacks']:
            raise ActionError('请先提交答案，或选择暂时不会。')
        _next_training(s)
    elif name in ('hint', 'example', 'solution'):
        _training_only(s)
        q = current_q(s)
        e = evidence(s, q['id'])
        if name == 'hint':
            n = len(e['hints'])
            if n >= len(q['hint_steps']):
                raise ActionError('本题提示已全部展示，可以查看完整讲解。')
            e['hints'].append(q['hint_steps'][n])
            event(s, 'hint_viewed', qid=q['id'], level=n + 1)
        elif name == 'example':
            e['example_viewed'] = True
            event(s, 'example_reviewed', qid=q['id'])
        else:
            e.update(solution_viewed=True, answer_exposed=True)
            event(s, 'solution_viewed', qid=q['id'])
        e['help_used'] = True
    elif name == 'tutor':
        _training_only(s)
        message = payload.get('message')
        if message not in PRESETS:
            raise ActionError('本批仅提供三个预设讲解按钮，尚未接入自由AI答疑。')
        q = current_q(s)
        e = evidence(s, q['id'])
        reply = (q['hint_steps'][0] if message == PRESETS[0] else
                 COURSE['unit']['boundary'] if message == PRESETS[1] else
                 '把写出的原函数求导，检查能否还原被积函数；核对系数、符号和积分常数。')
        e['chat'].extend([
            {'role': 'user', 'content': message, 'at': now()},
            {'role': 'assistant', 'content': reply, 'at': now(), 'mode': 'preset'},
        ])
        e['chat'] = e['chat'][-40:]
        e['help_used'] = True
        event(s, 'preset_used', qid=q['id'], message=message)
    elif name == 'foundation':
        _training_only(s, allow_lesson=True)
        if s['active_foundation']:
            raise ActionError('已经在基础补练中。')
        if s['stage'] != 'lesson':
            evidence(s, s['qid'])['help_used'] = True
            event(s, 'foundation_help_used', qid=s['qid'])
        s['active_foundation'] = True
        _activate(s, COURSE['foundation']['question_id'], 'foundation')
    elif name == 'return_training':
        _training_only(s)
        if not s['active_foundation']:
            raise ActionError('当前不是基础补练。')
        _return_training(s)
    elif name == 'plan':
        if s['phase'] not in ('plan', 'training', 'report'):
            raise ActionError('请先完成当前诊断或复测。')
        s['phase'] = 'plan'
        _deactivate(s)
        event(s, 'returned_to_plan')
    elif name == 'post':
        if s['phase'] != 'plan' or not s['completed']:
            raise ActionError('请先走完本轮训练，再开始独立新题复测。')
        if s['post_done']:
            raise ActionError('这套复测题已经见过，不能再次作为新题复测。')
        s.update(phase='post', test_index=0)
        _activate(s, COURSE['post_order'][0])
        event(s, 'post_started')
    elif name == 'report':
        if not s['post_done'] or s['phase'] not in ('plan', 'report'):
            raise ActionError('请先完成独立复测。')
        s['phase'] = 'report'
        _deactivate(s)
        _expose_report_answers(s)
    else:
        raise ActionError('不支持的操作。')


def action(s, payload):
    if not isinstance(payload, dict):
        raise ActionError('请求应为JSON对象。', 400)
    _check_revision(s, payload)
    if s.get('course_version') != COURSE['version'] or s.get('version') != STATE_VERSION:
        raise ActionError('此学习记录版本与当前课程不一致，请先导出记录并新建学习。', 409)
    # Transactional even when used directly by tests or another adapter.
    draft = copy.deepcopy(s)
    _mutate(draft, payload)
    draft['revision'] += 1
    s.clear()
    s.update(draft)
    return s


def tutor(s, payload):
    return action(s, {**payload, 'action': 'tutor'})


def _lesson():
    u = COURSE['unit']
    return {k: copy.deepcopy(u[k]) for k in ('name', 'goal', 'rule', 'boundary', 'knowledge', 'example')}


def _summary(s, qids):
    first = [_records(s, qid)[0] for qid in qids if _records(s, qid)]
    return {
        'total': len(qids), 'complete': sum(a['outcome'] == 'correct' for a in first),
        'independent_complete': sum(a['independent_complete'] for a in first),
    }


def _report(s):
    transfers = [qid for qid in COURSE['training_order'] if QUESTIONS[qid]['phase'] == 'transfer']
    rows = []
    labels = {'diagnostic': '诊断', 'guided': '引导练习', 'transfer': '独立变式', 'post': '独立复测', 'foundation': '基础补练'}
    counters = {}
    for qid in dict.fromkeys(a['qid'] for a in s['attempts']):
        q = QUESTIONS[qid]
        records = _records(s, qid)
        first, latest = records[0], records[-1]
        phase = q['phase']
        counters[phase] = counters.get(phase, 0) + 1
        rows.append({
            'stage': phase, 'label': labels.get(phase, phase) + ' ' + str(counters[phase]),
            'formula_html': q['formula_html'], 'first_outcome': first['outcome'],
            'latest_outcome': latest['outcome'], 'answer': latest['answer'],
            'attempt_count': len(records), 'assisted': any(a['assisted'] for a in records),
            'independent_complete': first['independent_complete'],
            'answer_html': q['answer_html'],
        })
    post = _summary(s, COURSE['post_order'])
    if post['independent_complete'] == post['total']:
        next_step = '两道新题已首次独立完整作答。隔一段时间再用不同的新题检查；本批暂不提供延迟复测。'
    elif any(a['outcome'] == 'ungraded' and a['phase'] == 'post' for a in s['attempts']):
        next_step = '复测含待核对结果，请导出记录并结合纸笔过程人工核对，暂不据此判断是否会做。'
    else:
        next_step = '先回看存在缺口的题目，核对换元结构、系数和积分常数；重做已见题不能替代新的独立复测。'
    return {'diagnostic': _summary(s, COURSE['diagnostic_order']),
            'transfers': _summary(s, transfers), 'post': post,
            'rows': rows, 'next_step': next_step}


def public(s):
    assessing = s['phase'] in ASSESSMENTS
    value = {
        'version': VERSION, 'revision': s['revision'], 'participant': s['participant'],
        'csrf': s['csrf'], 'phase': s['phase'], 'minutes': s['minutes'],
        'course': {k: COURSE[k] for k in ('id', 'version', 'title', 'scope')},
        'ai_enabled': False, 'export_allowed': not assessing,
        'instance_id': s['instance_id'], 'question': None,
        'progress': {'current': 0, 'total': len(COURSE['training_order']), 'label': '单元训练'},
        'stage': s['stage'] if not assessing else None,
        'feedback': None, 'hints': [], 'hint_count': 0, 'help_used': False,
        'chat': [], 'lesson': None, 'solution': None, 'foundation_info': None,
        'plan': None, 'report': None,
    }
    if s['phase'] in (*ASSESSMENTS, 'training'):
        q = current_q(s)
        value['question'] = {k: q[k] for k in ('kind', 'formula_html', 'source')}
        value['question']['instance_id'] = s['instance_id']
        if assessing:
            order = COURSE['diagnostic_order' if s['phase'] == 'diagnostic' else 'post_order']
            value['progress'] = {'current': s['test_index'] + 1, 'total': len(order), 'label': '独立作答'}
        else:
            e = _read_evidence(s, q['id'])
            value['progress'] = {'current': s['main_index'] + 1, 'total': len(COURSE['training_order']),
                                 'label': '基础补练' if s['stage'] == 'foundation' else '单元训练'}
            value.update(hints=copy.deepcopy(e.get('hints', [])), hint_count=len(q['hint_steps']),
                         help_used=bool(e.get('help_used') or e.get('answer_exposed')),
                         feedback=copy.deepcopy(s['feedbacks'].get(q['id'])),
                         chat=copy.deepcopy(e.get('chat', [])))
            if s['stage'] == 'lesson' or e.get('example_viewed'):
                value['lesson'] = _lesson()
            if e.get('solution_viewed'):
                value['solution'] = {'steps': q['solution_steps'], 'answer_html': q['answer_html']}
            if s['stage'] == 'foundation':
                value['foundation_info'] = {k: copy.deepcopy(COURSE['foundation'][k]) for k in ('title', 'knowledge')}
    if s['phase'] == 'plan':
        diag = _summary(s, COURSE['diagnostic_order'])
        reason = ('诊断首次作答完整，通过不同结构的变式和新题再检查。' if diag['complete'] == diag['total']
                  else '诊断尚无完整独立作答证据，先跟随示范练习；基础不熟时可进入短补练。')
        value['plan'] = {'started': s['started'], 'completed': s['completed'],
                         'can_resume': s['started'] and not s['completed'],
                         'post_done': s['post_done'], 'reason': reason}
    if s['phase'] == 'report':
        value['report'] = _report(s)
    return value


def export_state(s):
    if s['phase'] in ASSESSMENTS:
        raise ActionError('请完成当前诊断或复测后再导出，测评期间不提供中途结果。', 409)
    # Build labels locally, but emit only references used by happened records.
    labels = {qid: f'诊断{i}' for i, qid in enumerate(COURSE['diagnostic_order'], 1)}
    counts = {}
    names = {'guided': '引导练习', 'transfer': '独立变式'}
    for qid in COURSE['training_order']:
        phase = QUESTIONS[qid]['phase']
        counts[phase] = counts.get(phase, 0) + 1
        labels[qid] = names.get(phase, '训练题') + str(counts[phase])
    labels[COURSE['foundation']['question_id']] = '基础补练1'
    labels.update({qid: f'独立复测{i}' for i, qid in enumerate(COURSE['post_order'], 1)})
    references = {}

    def reference(qid, course_version):
        key = (course_version, qid)
        if key not in references:
            number = len(references) + 1
            label = labels.get(qid) if course_version == COURSE['version'] else None
            references[key] = {
                # Export-local correlation only; this is neither a qid nor an instance token.
                'question_ref': f'record-question-{number}',
                'question_label': label or f'历史题目{number}',
                'course_version': course_version,
            }
        return references[key]

    def external_record(record):
        item = copy.deepcopy(record)
        qid = item.pop('qid', None)
        if qid is not None:
            item.update(reference(qid, item.get('course_version', s['course_version'])))
        return item

    attempts = [external_record(record) for record in s['attempts']]
    exported_evidence = {}
    for key, record in s['evidence'].items():
        # Existing versions keep their own association even if no current question matches.
        if ':' in key:
            course_version, qid = key.rsplit(':', 1)
        else:
            course_version, qid = s['course_version'], key
        identity = reference(qid, course_version)
        exported_evidence[identity['question_ref']] = {**copy.deepcopy(record), **identity}
    events = [external_record(record) for record in s['events']]
    # Allowlist: no cookie, CSRF, instance tokens, internal qids, answer bank or unvisited lessons.
    return copy.deepcopy({
        'export_format_version': 2,
        'app_version': VERSION, 'state_version': s['version'],
        'course_version': s['course_version'], 'grader_version': s['grader_version'],
        'participant': s['participant'], 'created_at': s['created_at'],
        'exported_at': now(), 'phase': s['phase'], 'minutes': s['minutes'],
        'main_index': s['main_index'], 'completed': s['completed'], 'post_done': s['post_done'],
        'attempts': attempts, 'evidence': exported_evidence, 'events': events,
        'limitations': ['仅为换元积分单元练习', '前后题目未做难度等值标定',
                        '只核对结果，不批改完整过程或预测考试成绩', '答疑是预设讲解，未调用在线模型'],
    })
