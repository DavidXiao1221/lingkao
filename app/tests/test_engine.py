"""Learning evidence and state-transition regression tests."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine as eng
from grader import grade


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.s = eng.new_state()
        self.patcher = patch.object(eng, 'isolated_grade', side_effect=grade)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def act(self, name, **kwargs):
        return eng.action(self.s, {'action': name, 'revision': self.s['revision'],
                                  'instance_id': self.s['instance_id'], **kwargs})

    def answer(self, value=None):
        self.act('answer', answer=eng.current_q(self.s)['answer'] if value is None else value)

    def begin(self):
        self.act('start', minutes=30)

    def finish_diagnostic(self):
        self.begin()
        while self.s['phase'] == 'diagnostic':
            self.answer()

    def training(self):
        self.finish_diagnostic()
        self.act('start_skill')
        self.act('begin_practice')

    def transfer(self):
        self.training()
        while self.s['stage'] == 'guided':
            self.answer()
            self.act('next')

    def finish_training(self):
        self.training()
        while self.s['phase'] == 'training':
            self.answer()
            self.act('next')

    def finish_all(self):
        self.finish_training()
        self.act('post')
        while self.s['phase'] == 'post':
            self.answer()

    def test_complete_flow_and_counts(self):
        self.finish_all()
        self.assertEqual(self.s['phase'], 'report')
        r = eng.public(self.s)['report']
        self.assertEqual(r['diagnostic']['total'], 1)
        self.assertEqual(r['transfers'], {'total': 2, 'complete': 2, 'independent_complete': 2})
        self.assertEqual(r['post'], {'total': 2, 'complete': 2, 'independent_complete': 2})
        self.assertEqual(len(self.s['attempts']), 7)
        self.assertEqual(len(r['rows']), 7)

    def test_course_sequences_not_inferred_from_ids(self):
        self.training()
        visited = []
        while self.s['phase'] == 'training':
            visited.append(self.s['qid'])
            self.act('skip')
        self.assertEqual(visited, eng.COURSE['training_order'])
        self.assertTrue(self.s['completed'])
        self.assertFalse(any(a['independent_complete'] for a in self.s['attempts'][1:]))

    def test_assessment_public_is_minimal(self):
        self.begin()
        v = eng.public(self.s)
        self.assertFalse(v['export_allowed'])
        self.assertIsNone(v['stage'])
        for key in ('lesson', 'solution', 'foundation_info', 'feedback', 'plan', 'report'):
            self.assertIsNone(v[key])
        self.assertEqual(v['hints'], [])
        self.assertEqual(v['chat'], [])
        encoded = json.dumps(v)
        for qid in eng.QUESTIONS:
            self.assertNotIn(qid, encoded)
        for key in ('answer', 'answer_html', 'hint_steps', 'rule', 'skills'):
            self.assertNotIn('"' + key + '"', encoded)
        self.assertEqual(set(v['question']), {'instance_id', 'kind', 'formula_html', 'source'})

    def test_no_post_intermediate_result(self):
        self.finish_training()
        self.act('post')
        self.answer()
        v = eng.public(self.s)
        self.assertEqual(v['phase'], 'post')
        self.assertIsNone(v['report'])
        self.assertIsNone(v['feedback'])
        self.assertEqual(v['progress']['current'], 2)
        with self.assertRaises(eng.ActionError) as ctx:
            eng.export_state(self.s)
        self.assertEqual(ctx.exception.status, 409)

    def test_assessment_cannot_use_teaching_or_leave(self):
        self.begin()
        for name in ('hint', 'example', 'solution', 'tutor', 'foundation', 'plan', 'next', 'start_skill', 'report'):
            with self.subTest(action=name):
                before = copy.deepcopy(self.s)
                with self.assertRaises(eng.ActionError):
                    self.act(name, message=eng.PRESETS[0])
                self.assertEqual(before, self.s)

    def test_invalid_does_not_consume_first_attempt(self):
        self.begin()
        before = copy.deepcopy(self.s)
        with self.assertRaises(eng.ActionError):
            self.answer('2x')
        self.assertEqual(before, self.s)
        self.answer()
        self.assertTrue(self.s['attempts'][0]['independent_complete'])

    def test_wrong_retry_never_rewrites_first(self):
        self.transfer()
        self.answer('0')
        self.answer()
        qid = self.s['qid']
        a = eng._records(self.s, qid)
        self.assertEqual([r['outcome'] for r in a], ['incorrect', 'correct'])
        self.assertFalse(a[-1]['independent_complete'])

    def test_constant_missing_can_retry_but_not_independent(self):
        self.transfer()
        correct = eng.current_q(self.s)['answer']
        self.answer(correct.replace('+C', '').replace('+ C', ''))
        self.assertEqual(self.s['attempts'][-1]['outcome'], 'incomplete')
        self.assertFalse(self.s['attempts'][-1]['answer_complete'])
        self.answer(correct)
        self.assertEqual(self.s['attempts'][-1]['outcome'], 'correct')
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_hint_survives_exit_refresh_and_disk_roundtrip(self):
        self.transfer()
        qid = self.s['qid']
        old_instance = self.s['instance_id']
        self.act('hint')
        self.assertEqual(eng.public(self.s)['hints'], [eng.QUESTIONS[qid]['hint_steps'][0]])
        self.act('plan')
        self.s = json.loads(json.dumps(self.s, ensure_ascii=False))
        self.act('start_skill')
        self.assertEqual(self.s['qid'], qid)
        self.assertNotEqual(self.s['instance_id'], old_instance)
        self.assertTrue(eng.public(self.s)['help_used'])
        self.assertEqual(len(eng.public(self.s)['hints']), 1)
        self.answer()
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_example_solution_and_tutor_are_persistent_assistance(self):
        for name in ('example', 'solution', 'tutor'):
            with self.subTest(name=name):
                self.s = eng.new_state()
                self.transfer()
                self.act(name, message=eng.PRESETS[0])
                self.act('plan')
                self.act('start_skill')
                self.answer()
                self.assertTrue(self.s['attempts'][-1]['assisted'])
                self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_initial_lesson_does_not_contaminate_transfer(self):
        self.transfer()
        self.assertFalse(eng.public(self.s)['help_used'])
        self.answer()
        self.assertTrue(self.s['attempts'][-1]['independent_complete'])

    def test_foundation_returns_to_original_cursor(self):
        self.transfer()
        qid, cursor = self.s['qid'], self.s['main_index']
        self.act('foundation')
        self.assertEqual(self.s['stage'], 'foundation')
        self.answer()
        self.act('next')
        self.assertEqual(self.s['qid'], qid)
        self.assertEqual(self.s['main_index'], cursor)
        self.assertTrue(eng.public(self.s)['help_used'])
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_foundation_can_return_without_answer_and_resume_from_plan(self):
        self.training()
        qid = self.s['qid']
        self.act('foundation')
        self.act('plan')
        self.act('start_skill')
        self.assertEqual(self.s['stage'], 'foundation')
        self.act('return_training')
        self.assertEqual(self.s['qid'], qid)
        self.assertEqual(len(self.s['attempts']), 1)

    def test_foundation_in_lesson_returns_to_lesson(self):
        self.finish_diagnostic()
        self.act('start_skill')
        self.act('foundation')
        self.act('skip')
        self.assertEqual(self.s['stage'], 'lesson')
        self.act('begin_practice')
        self.assertEqual(self.s['main_index'], 0)

    def test_stale_instance_all_question_mutations_have_no_effect(self):
        self.training()
        old = self.s['instance_id']
        self.act('skip')
        for name in eng.QUESTION_ACTIONS | {'plan'}:
            with self.subTest(name=name):
                before = copy.deepcopy(self.s)
                with self.assertRaises(eng.ActionError) as ctx:
                    self.act(name, instance_id=old, answer='0', message=eng.PRESETS[0])
                self.assertEqual(ctx.exception.status, 409)
                self.assertEqual(before, self.s)

    def test_duplicate_request_rejected(self):
        self.training()
        payload = {'action': 'answer', 'revision': self.s['revision'],
                   'instance_id': self.s['instance_id'], 'answer': '0'}
        eng.action(self.s, payload)
        before = copy.deepcopy(self.s)
        with self.assertRaises(eng.ActionError) as ctx:
            eng.action(self.s, payload)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.s, before)

    def test_read_state_does_not_rotate_or_mutate(self):
        self.training()
        before = copy.deepcopy(self.s)
        self.assertEqual(eng.public(self.s), eng.public(self.s))
        self.assertEqual(before, self.s)

    def test_no_second_new_post(self):
        self.finish_all()
        self.act('plan')
        with self.assertRaises(eng.ActionError):
            self.act('post')
        self.act('restart_training')
        self.act('begin_practice')
        while self.s['phase'] == 'training':
            self.act('skip')
        with self.assertRaises(eng.ActionError):
            self.act('post')

    def test_restart_keeps_evidence_and_attempts(self):
        self.finish_all()
        original = copy.deepcopy(self.s['attempts'])
        self.act('restart_training')
        self.act('begin_practice')
        self.answer()
        self.assertEqual(self.s['attempts'][:len(original)], original)
        self.assertTrue(self.s['attempts'][-1]['assisted'])

    def test_export_contains_no_credentials_or_unseen_answers(self):
        self.finish_diagnostic()
        exported = eng.export_state(self.s)
        raw = json.dumps(exported, ensure_ascii=False)
        for key in ('csrf', 'instance_id', 'answer_html', 'questions', 'solution_steps'):
            self.assertNotIn('"' + key + '"', raw)
        self.assertNotIn(self.s['csrf'], raw)
        for qid in eng.QUESTIONS:
            self.assertNotIn(qid, raw)

    def test_export_correlates_attempts_help_and_events_without_internal_ids(self):
        self.training()
        self.act('hint')
        self.act('tutor', message=eng.PRESETS[0])
        self.answer('0')
        self.answer()
        original = copy.deepcopy(self.s)
        exported = eng.export_state(self.s)
        self.assertEqual(exported['export_format_version'], 2)
        attempts = [a for a in exported['attempts'] if a['question_label'] == '引导练习1']
        self.assertEqual(len(attempts), 2)
        ref = attempts[0]['question_ref']
        self.assertTrue(all(a['question_ref'] == ref for a in attempts))
        evidence = exported['evidence'][ref]
        self.assertEqual(evidence['question_label'], '引导练习1')
        self.assertEqual(len(evidence['hints']), 1)
        self.assertEqual(len(evidence['chat']), 2)
        events = [e for e in exported['events'] if e.get('question_ref') == ref]
        for name in ('hint_viewed', 'preset_used', 'answer_submitted'):
            self.assertIn(name, [e['event'] for e in events])
        raw = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn('"qid"', raw)
        for qid in eng.QUESTIONS:
            self.assertNotIn(qid, raw)
        self.assertNotIn('独立复测1', raw)
        self.assertNotIn('独立变式1', raw)
        self.assertEqual(self.s, original)

    def test_export_labels_follow_configured_sequences(self):
        self.training()
        self.act('foundation')
        self.act('hint')
        self.act('skip')
        while self.s['phase'] == 'training':
            self.act('skip')
        self.act('post')
        while self.s['phase'] == 'post':
            self.act('skip')
        exported = eng.export_state(self.s)
        self.assertEqual([a['question_label'] for a in exported['attempts']], [
            '诊断1', '基础补练1', '引导练习1', '引导练习2',
            '独立变式1', '独立变式2', '独立复测1', '独立复测2',
        ])
        refs = {a['question_ref'] for a in exported['attempts']}
        self.assertEqual(len(refs), 8)
        self.assertEqual(refs, set(exported['evidence']))

    def test_export_preserves_unknown_historical_versions_without_current_lookup(self):
        self.finish_diagnostic()
        historical = copy.deepcopy(self.s)
        old_qid = 'removed-private-question-id'
        old_version = 'legacy-course-version'
        historical['course_version'] = old_version
        historical['version'] = 1
        historical['attempts'][0].update(qid=old_qid, course_version=old_version)
        old_evidence = next(iter(historical['evidence'].values()))
        historical['evidence'] = {old_version + ':' + old_qid: old_evidence}
        for event in historical['events']:
            if 'qid' in event:
                event['qid'] = old_qid
        before = copy.deepcopy(historical)
        exported = eng.export_state(historical)
        attempt = exported['attempts'][0]
        self.assertEqual(attempt['question_label'], '历史题目1')
        self.assertEqual(attempt['course_version'], old_version)
        self.assertIn(attempt['question_ref'], exported['evidence'])
        for event in exported['events']:
            if 'question_ref' in event:
                self.assertEqual(event['question_ref'], attempt['question_ref'])
        raw = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn(old_qid, raw)
        self.assertNotIn('answer_html', raw)
        self.assertEqual(historical, before)

    def test_preview_only_parses_and_does_not_mutate(self):
        self.begin()
        before = copy.deepcopy(self.s)
        with patch.object(eng, '_worker', return_value={'valid': True, 'mathml': '<math/>'}) as worker:
            result = eng.preview(self.s, {'instance_id': self.s['instance_id'], 'expression': '0'})
        self.assertTrue(result['valid'])
        self.assertEqual(worker.call_args.args[0], {'operation': 'preview', 'expression': '0'})
        self.assertEqual(before, self.s)

    def test_preview_rejects_stale_instance(self):
        self.training()
        old = self.s['instance_id']
        self.act('skip')
        with self.assertRaises(eng.ActionError) as ctx:
            eng.preview(self.s, {'instance_id': old, 'expression': 'x'})
        self.assertEqual(ctx.exception.status, 409)

    def test_ungraded_is_separate_from_wrong(self):
        self.begin()
        with patch.object(eng, 'isolated_grade', return_value={'verdict': 'ungraded', 'message': '待核对', 'answer_complete': False}):
            self.answer('sin(x)')
        self.assertEqual(self.s['attempts'][0]['outcome'], 'ungraded')
        self.assertFalse(self.s['attempts'][0]['independent_complete'])

    def test_setup_validation(self):
        for value in (0, 241, True, '30', 30.5):
            with self.subTest(value=value):
                with self.assertRaises(eng.ActionError):
                    self.act('start', minutes=value)
                self.assertEqual(self.s['phase'], 'setup')

    def test_tutor_free_text_and_api_key_never_enable_online(self):
        self.training()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-do-not-connect'}):
            with self.assertRaises(eng.ActionError):
                self.act('tutor', message='请联网帮我做题')
            self.act('tutor', message=eng.PRESETS[0])
            self.assertFalse(eng.public(self.s)['ai_enabled'])
            self.assertEqual(eng.public(self.s)['chat'][-1]['mode'], 'preset')


class WorkerTests(unittest.TestCase):
    def test_worker_unicode_and_preview_real_process(self):
        q = eng.QUESTIONS[eng.COURSE['training_order'][0]]
        with patch.dict(os.environ, {'PYTHONIOENCODING': 'ascii'}):
            result = eng.isolated_grade(q['answer'], q)
            self.assertEqual(result['verdict'], 'correct')
            result = eng._worker({'operation': 'preview', 'expression': 'x²'}, preview_only=True)
            self.assertIn('valid', result)
            self.assertIsInstance(result.get('message', result.get('mathml')), str)


if __name__ == '__main__':
    unittest.main()
