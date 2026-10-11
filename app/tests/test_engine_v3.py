"""V3 navigation, protected assessments, migrations and tutor transaction checks."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as eng
import legacy_engine as legacy
from grader import grade


class EngineV3Tests(unittest.TestCase):
    def setUp(self):
        self.s = eng.new_state()
        self.patcher = patch.object(eng, 'isolated_grade', side_effect=grade)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def act(self, action, **kw):
        return eng.action(self.s, {'action': action, 'revision': self.s['revision'],
                                  'instance_id': self.s.get('instance_id'), **kw})

    def start(self, **kw):
        self.act('start', minutes=kw.pop('minutes', 60), **kw)

    def select(self, qid=None, skill=None, index=0):
        qid = qid or eng._practice(skill)[index]['id']
        self.act('select', question_ref=eng.QUESTIONS[qid]['ref'])
        return qid

    def answer(self, answer=None):
        self.act('answer', answer=eng.current_q(self.s)['answer'] if answer is None else answer)

    def practice_two(self, skill):
        for q in eng._practice(skill)[:2]:
            self.select(q['id'])
            self.answer()
        self.act('catalog')

    def finish_assessment(self):
        while self.s['phase'] == 'assessment':
            self.answer()

    def test_catalog_only_contains_practice(self):
        self.start()
        before = copy.deepcopy(self.s)
        view = eng.public(self.s)
        self.assertEqual(self.s, before)
        self.assertEqual(len(view['catalog']['questions']), 16)
        self.assertEqual(len(view['catalog']['skills']), 4)
        encoded = json.dumps(view)
        for q in eng.QUESTIONS.values():
            if q['pool'] != 'practice':
                self.assertNotIn(q['ref'], encoded)
                self.assertNotIn(q['id'], encoded)
        for q in eng.QUESTIONS.values():
            if q['pool'] != 'practice':
                with self.assertRaises(eng.ActionError):
                    self.act('select', question_ref=q['ref'])

    def test_free_selection_preserves_round_and_last_answer(self):
        self.start()
        qid = self.select()
        first_run, instance = self.s['run_id'], self.s['instance_id']
        self.answer('0')
        self.select(index=1)
        self.select(qid)
        self.assertEqual(first_run, self.s['run_id'])
        self.assertNotEqual(instance, self.s['instance_id'])
        self.assertEqual(eng.public(self.s)['last_answer'], '0')
        self.assertEqual(eng.public(self.s)['question']['ref'], eng.QUESTIONS[qid]['ref'])

    def test_stale_revision_or_instance_does_not_mutate(self):
        self.start()
        self.select()
        stale = {'action': 'answer', 'answer': '0', 'revision': self.s['revision'], 'instance_id': self.s['instance_id']}
        self.select(index=1)
        before = copy.deepcopy(self.s)
        with self.assertRaises(eng.ActionError):
            eng.action(self.s, stale)
        stale['revision'] = self.s['revision']
        with self.assertRaises(eng.ActionError):
            eng.action(self.s, stale)
        self.assertEqual(before, self.s)

    def test_first_attempt_survives_restart_and_bank_version(self):
        self.start()
        qid = self.select()
        self.answer('0')
        first = copy.deepcopy(self.s['attempts'][0])
        self.act('restart_question')
        self.answer()
        self.assertEqual(self.s['attempts'][0], first)
        self.assertFalse(self.s['attempts'][-1]['first_attempt'])
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])
        with patch.dict(eng.COURSE, version='0.3-content-added'):
            self.assertEqual(len(eng._records(self.s, qid)), 2)
            self.assertFalse(eng._is_unseen(self.s, qid))

    def test_help_persists_but_does_not_rewrite_past_attempt(self):
        self.start()
        self.select()
        self.answer()
        original = copy.deepcopy(self.s['attempts'][0])
        self.act('hint')
        self.assertEqual(self.s['attempts'][0], original)
        self.act('restart_question')
        self.answer()
        self.assertTrue(self.s['attempts'][-1]['assisted'])
        self.assertFalse(self.s['attempts'][-1]['run_assisted'])
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_skip_counts_but_invalid_expression_does_not(self):
        self.start()
        qid = self.select()
        before = copy.deepcopy(self.s)
        with self.assertRaises(eng.ActionError):
            self.answer('this is not math')
        self.assertEqual(self.s, before)
        self.act('skip')
        self.assertEqual(self.s['attempts'][-1]['outcome'], 'skipped')
        recommendation = eng.public(self.s)['recommendation']
        self.assertNotEqual(recommendation['ref'], eng.QUESTIONS[qid]['ref'])
        self.select(qid)
        self.answer()
        self.assertFalse(self.s['attempts'][-1]['first_attempt'])

    def test_attempt_cap_resets_per_question_round_not_first_evidence(self):
        self.start()
        self.select()
        for _ in range(10):
            self.answer('0')
        before = copy.deepcopy(self.s)
        with self.assertRaises(eng.ActionError):
            self.answer('0')
        self.assertEqual(self.s, before)
        self.act('restart_question')
        self.answer()
        self.assertEqual(len(self.s['attempts']), 11)
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_ungraded_remains_distinct_and_does_not_block_new_work(self):
        self.start()
        qid = self.select()
        with patch.object(eng, 'isolated_grade', return_value={'verdict': 'ungraded', 'answer_complete': False, 'message': '待核对'}):
            self.answer('0')
        self.assertEqual(eng._status(self.s, qid), 'ungraded')
        self.assertNotEqual(eng.public(self.s)['recommendation']['ref'], eng.QUESTIONS[qid]['ref'])
        self.assertNotEqual(self.s['attempts'][-1]['outcome'], 'incorrect')

    def test_budget_charged_once_per_round_and_per_assessment(self):
        self.start(minutes=60)
        qid = self.select()
        cost = eng.QUESTIONS[qid]['minutes']
        self.s['active_since'] = '2000-01-01T00:00:00+00:00'
        self.act('hint')
        self.assertEqual(eng.public(self.s)['budget']['spent'], 0)
        self.answer('0')
        self.assertEqual(eng.public(self.s)['budget']['spent'], cost)
        self.answer()
        self.assertEqual(eng.public(self.s)['budget']['spent'], cost)
        self.act('restart_question')
        self.act('skip')
        self.assertEqual(eng.public(self.s)['budget']['spent'], 2 * cost)

    def test_budget_reserves_each_selected_unit_and_stops_when_empty(self):
        self.start(minutes=240)
        budget = eng.public(self.s)['budget']
        skills = {x['skill'] for x in budget['tasks']}
        expected = sum(eng.QUESTIONS[x]['minutes'] for skill in skills for x in eng.UNITS[skill]['post_order'])
        self.assertEqual(budget['assessment_minutes'], expected)
        self.assertLessEqual(sum(x['minutes'] for x in budget['tasks']) + expected + budget['summary_minutes'], budget['remaining'])
        self.s['spent_seconds'] = self.s['minutes'] * 60
        self.assertIsNone(eng.public(self.s)['recommendation'])

    def test_explicit_new_budget_session_keeps_learning_identity_and_restarts_estimates(self):
        self.start(minutes=30)
        qid = self.select()
        self.act('hint')
        self.answer('0')
        cost = eng.QUESTIONS[qid]['minutes']
        self.assertEqual(self.s['spent_seconds'], cost * 60)
        # Editing the budget, a truthy number or a string are not authorization
        # to start a new accounting session.
        for extra in ({}, {'new_session': 1}, {'new_session': 'true'}):
            self.act('budget', minutes=60, **extra)
            self.assertEqual(self.s['spent_seconds'], cost * 60)
        before = copy.deepcopy(self.s)
        self.act('budget', minutes=45, new_session=True)
        self.assertEqual(self.s['spent_seconds'], 0)
        self.assertEqual(self.s['minutes'], 45)
        for key in ('qid', 'instance_id', 'run_id', 'participant', 'csrf', 'attempts', 'evidence',
                    'assessment_runs', 'legacy_drafts', 'ai_consent'):
            self.assertEqual(self.s[key], before[key])
        expected_runs = copy.deepcopy(before['practice_runs'])
        for run in expected_runs.values():
            run['charged'] = False
        self.assertEqual(self.s['practice_runs'], expected_runs)
        self.assertTrue(any(e['event'] == 'budget_session_started' and e['minutes'] == 45
                            for e in self.s['events']))
        self.answer()
        self.assertEqual(self.s['spent_seconds'], cost * 60)
        self.assertTrue(self.s['attempts'][-1]['assisted'])
        self.assertFalse(self.s['attempts'][-1]['first_attempt'])
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_assisted_completion_recommends_new_work(self):
        self.start()
        qid = self.select(skill='basic')
        self.act('hint')
        self.answer()
        self.act('catalog')
        rec = eng.public(self.s)['recommendation']
        self.assertNotEqual(rec['ref'], eng.QUESTIONS[qid]['ref'])
        self.assertEqual(rec['skill'], 'basic')

    def test_all_assisted_prerequisite_questions_never_loop_completed_runs(self):
        self.start(minutes=240)
        for q in eng._practice('basic'):
            self.select(q['id'])
            self.act('hint')
            self.answer()
        self.act('catalog')
        for _ in range(3):
            rec = eng.public(self.s)['recommendation']
            self.assertIsNotNone(rec)
            self.assertNotEqual(rec['skill'], 'basic')
            self.act('select', question_ref=rec['ref'])
            self.assertIsNone(eng.public(self.s)['feedback'])
            self.answer()
            self.act('next')
            self.assertNotEqual(eng.current_q(self.s)['skill'], 'basic')
            self.assertIsNone(eng.public(self.s)['feedback'])
            self.act('catalog')

    def test_diagnostic_current_result_and_banks_remain_hidden(self):
        self.start(diagnostic=True)
        self.answer()
        view = eng.public(self.s)
        self.assertEqual(self.s['phase'], 'assessment')
        for key in ('catalog', 'recommendation', 'budget', 'report', 'lesson', 'solution', 'feedback', 'last_answer'):
            self.assertIsNone(view[key])
        for key in ('skill', 'skill_name', 'source_refs', 'answer', 'hint_steps'):
            self.assertNotIn(key, view['question'])
        self.assertEqual(view['chat'], [])
        self.assertEqual(view['hints'], [])
        self.assertFalse(view['export_allowed'])
        with self.assertRaises(eng.ActionError):
            eng.catalog(self.s)
        with self.assertRaises(eng.ActionError):
            eng.export_state(self.s)
        for action in ('catalog', 'report', 'review', 'hint', 'solution', 'preset'):
            with self.assertRaises(eng.ActionError):
                self.act(action, message=eng.PRESETS[0])
        with self.assertRaises(eng.ActionError):
            eng.prepare_ai(self.s, {'revision': self.s['revision'], 'instance_id': self.s['instance_id'], 'message': '讲解'})

    def test_skill_diagnostic_labels_distinguish_unseen_skipped_and_partial_run(self):
        def skills():
            return {row['id']: row for row in eng.public(self.s)['catalog']['skills']}

        self.start()
        self.assertTrue(all(row['diagnostic_state'] == 'unseen' and row['diagnostic_label'] == '未测试'
                            for row in skills().values()))
        # Ordinary practice success does not imply the diagnostic was taken.
        self.select(skill='basic')
        self.answer()
        self.act('catalog')
        self.assertEqual(skills()['basic']['diagnostic_label'], '未测试')
        self.act('diagnostic')
        skipped_skill = eng.current_q(self.s)['skill']
        self.act('skip')
        shown_skill = eng.current_q(self.s)['skill']
        self.act('end_assessment')
        labels = skills()
        self.assertEqual(labels[skipped_skill]['diagnostic_state'], 'incomplete')
        self.assertIn('跳过', labels[skipped_skill]['diagnostic_label'])
        self.assertEqual(labels[shown_skill]['diagnostic_state'], 'seen_unanswered')
        for skill in set(eng.UNITS) - {skipped_skill, shown_skill}:
            self.assertEqual(labels[skill]['diagnostic_label'], '未测试')

        # In a separate learner's run, completing one item never completes the
        # already-displayed second item or either unvisited item.
        self.s = eng.new_state()
        self.start(diagnostic=True)
        answered_skill = eng.current_q(self.s)['skill']
        self.answer()
        shown_skill = eng.current_q(self.s)['skill']
        self.act('end_assessment')
        labels = skills()
        self.assertEqual(labels[answered_skill]['diagnostic_state'], 'completed')
        self.assertEqual(labels[answered_skill]['diagnostic_label'], '已诊断：首次独立完成')
        self.assertEqual(labels[shown_skill]['diagnostic_state'], 'seen_unanswered')
        for skill in set(eng.UNITS) - {answered_skill, shown_skill}:
            self.assertEqual(labels[skill]['diagnostic_state'], 'unseen')
        encoded = json.dumps(labels)
        for q in eng.QUESTIONS.values():
            if q['pool'] != 'practice':
                self.assertNotIn(q['ref'], encoded)
                self.assertNotIn(q['id'], encoded)
                self.assertNotIn(q['formula_html'], encoded)

    def test_assessment_ref_run_stable_on_read_but_changes_between_items(self):
        self.start(diagnostic=True)
        q = eng.public(self.s)['question']
        self.assertEqual(eng.public(self.s)['question'], q)
        self.answer()
        other = eng.public(self.s)['question']
        self.assertNotEqual(q['ref'], other['ref'])
        self.assertEqual(q['run_id'], other['run_id'])

    def test_post_gate_and_delayed_exact_24h(self):
        self.start()
        skill = 'substitution'
        with self.assertRaises(eng.ActionError):
            self.act('assess', skill=skill)
        self.practice_two(skill)
        self.assertTrue(eng._available(self.s, skill, 'post'))
        self.act('assess', skill=skill)
        self.finish_assessment()
        post = eng._post_completed(self.s, skill)
        self.assertFalse(eng._available(self.s, skill, 'post'))
        self.assertFalse(eng._available(self.s, skill, 'delayed'))
        later = (dt.datetime.fromisoformat(post['ended_at']) + dt.timedelta(hours=24)).isoformat()
        with patch.object(eng, 'now', return_value=later):
            self.assertTrue(eng._available(self.s, skill, 'delayed'))
            self.act('assess', skill=skill, mode='delayed')
            self.finish_assessment()
            self.assertFalse(eng._available(self.s, skill, 'delayed'))

    def test_end_assessment_consumes_presented_question_without_fabricating_attempt(self):
        self.start(diagnostic=True)
        qid = self.s['qid']
        self.act('end_assessment')
        self.assertEqual(self.s['phase'], 'report')
        self.assertEqual(len(self.s['attempts']), 0)
        self.assertFalse(eng._is_unseen(self.s, qid))
        self.assertEqual(self.s['assessment_runs'][-1]['status'], 'aborted')
        self.assertEqual(eng.public(self.s)['report']['rows'], [])

    def test_each_units_post_can_complete_and_report_only_attempted(self):
        self.start(minutes=240)
        for skill in eng.UNITS:
            self.practice_two(skill)
            self.act('assess', skill=skill)
            self.finish_assessment()
        report = eng.public(self.s)['report']
        self.assertEqual(len(report['rows']), 16)
        self.assertEqual(report['post']['independent_complete'], 8)
        self.assertEqual(report['delayed']['total'], 0)
        encoded = json.dumps(report)
        for q in eng.QUESTIONS.values():
            if q['pool'] == 'delayed':
                self.assertNotIn(q['ref'], encoded)
                self.assertNotIn(q['formula_html'], encoded)

    def test_report_does_not_create_help_or_overwrite_first_evidence(self):
        self.start()
        qid = self.select()
        self.answer()
        first = copy.deepcopy(self.s['attempts'][0])
        self.act('report')
        self.assertEqual(first, self.s['attempts'][0])
        self.assertTrue(eng._read_evidence(self.s, qid)['answer_exposed'])
        self.assertFalse(eng._read_evidence(self.s, qid)['help_used'])

    def test_foundation_returns_same_main_run(self):
        self.start()
        qid = self.select(skill='substitution')
        rid = self.s['run_id']
        self.act('foundation')
        self.assertEqual(self.s['qid'], 'basic_foundation')
        self.act('catalog')
        self.select('basic_foundation')
        self.assertIsNotNone(eng.public(self.s)['foundation_info'])
        self.act('return_training')
        self.assertEqual(self.s['qid'], qid)
        self.assertEqual(self.s['run_id'], rid)
        self.assertTrue(eng.public(self.s)['help_used'])

    def test_export_has_no_runtime_ids_or_unvisited_content(self):
        self.start()
        qid = self.select()
        self.answer()
        data = eng.export_state(self.s)
        encoded = json.dumps(data)
        for value in (self.s['csrf'], self.s['instance_id'], self.s['run_id'], qid):
            self.assertNotIn(value, encoded)
        for q in eng.QUESTIONS.values():
            if q['pool'] != 'practice':
                self.assertNotIn(q['ref'], encoded)
        self.assertEqual(data['attempts'][0]['question_ref'], next(iter(data['evidence'])))

    def test_prepare_ai_is_readonly_and_only_current_content(self):
        self.start()
        self.select(skill='definite')
        before = copy.deepcopy(self.s)
        messages = eng.prepare_ai(self.s, {'revision': self.s['revision'], 'instance_id': self.s['instance_id'], 'message': '怎么做？'})
        self.assertEqual(self.s, before)
        context = json.loads(messages[1]['content'].split('：', 1)[1])
        self.assertEqual(context['lower'], eng.current_q(self.s)['lower'])
        self.assertEqual(context['upper'], eng.current_q(self.s)['upper'])
        encoded = json.dumps(messages)
        for q in eng.QUESTIONS.values():
            if q['pool'] != 'practice':
                self.assertNotIn(q['ref'], encoded)
                self.assertNotIn(q['id'], encoded)

    def ai_begin(self, rid='one'):
        payload = {'revision': self.s['revision'], 'instance_id': self.s['instance_id'], 'message': '为什么？'}
        eng.begin_ai(self.s, payload, rid, time.time() + 60)

    def test_ai_pending_locks_learning_and_cancellation_discards_reply(self):
        self.start()
        self.select()
        self.ai_begin()
        before = copy.deepcopy(self.s)
        for name in ('answer', 'select', 'report', 'restart_question', 'hint'):
            with self.assertRaises(eng.ActionError):
                self.act(name)
        self.assertEqual(before, self.s)
        self.act('cancel_ai', request_id='one')
        self.assertFalse(eng.finish_ai(self.s, 'one', reply='answer'))
        self.assertFalse(eng.public(self.s)['help_used'])
        self.assertEqual(eng.public(self.s)['chat'], [])

    def test_ai_success_is_durable_and_idempotent(self):
        self.start()
        self.select()
        self.ai_begin()
        revision = self.s['revision']
        self.assertTrue(eng.finish_ai(self.s, 'one', reply='先看结构。', usage={'total_tokens': 12, 'secret': 'x'}))
        self.assertEqual(self.s['revision'], revision + 1)
        self.assertTrue(eng.public(self.s)['help_used'])
        self.assertEqual(len(eng.public(self.s)['chat']), 2)
        before = copy.deepcopy(self.s)
        self.assertFalse(eng.finish_ai(self.s, 'one', reply='second'))
        self.assertEqual(before, self.s)
        self.answer()
        self.assertFalse(self.s['attempts'][-1]['independent_complete'])

    def test_ai_failure_timeout_and_binding_change_do_not_grant_help(self):
        self.start()
        self.select()
        for failure in ('error', 'timeout', 'binding'):
            self.ai_begin(failure)
            if failure == 'timeout':
                self.s['ai_request']['deadline'] = time.time() - 1
            elif failure == 'binding':
                self.s['instance_id'] = 'changed'
            self.assertTrue(eng.finish_ai(self.s, failure, reply='reply', error='服务暂不可用' if failure == 'error' else None))
            self.assertFalse(eng.public(self.s)['help_used'])
            self.assertEqual(eng.public(self.s)['chat'], [])
            self.assertFalse(eng.expire_ai(self.s))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.s = legacy.new_state()
        self.p = patch.object(legacy, 'isolated_grade', side_effect=grade)
        self.p.start()
        self.addCleanup(self.p.stop)

    def old(self, action, **kw):
        legacy.action(self.s, {'action': action, 'revision': self.s['revision'],
                              'instance_id': self.s['instance_id'], **kw})

    def old_training(self):
        self.old('start', minutes=30)
        self.old('answer', answer=legacy.current_q(self.s)['answer'])
        self.old('start_skill')
        self.old('begin_practice')

    def test_active_legacy_diagnostic_frozen_until_completion(self):
        self.old('start', minutes=30)
        before = copy.deepcopy(self.s)
        self.assertFalse(eng.migrate_state(self.s))
        self.assertEqual(before, self.s)
        self.assertTrue(eng.public(self.s)['legacy_mode'])
        eng.action(self.s, {'action': 'answer', 'revision': self.s['revision'], 'instance_id': self.s['instance_id'],
                            'answer': legacy.current_q(self.s)['answer']})
        self.assertEqual(self.s['version'], 3)
        self.assertEqual(self.s['participant'], before['participant'])
        self.assertEqual(self.s['phase'], 'catalog')
        self.assertEqual(self.s['revision'], before['revision'] + 1)
        self.assertTrue(self.s['attempts'][0]['independent_complete'])
        self.assertFalse(eng._is_unseen(self.s, 'substitution_diagnostic'))

    def test_regular_migration_preserves_first_evidence_help_and_draft(self):
        self.old_training()
        self.old('hint')
        self.old('answer', answer='0')
        original = copy.deepcopy(self.s)
        self.assertTrue(eng.migrate_state(self.s))
        self.assertEqual(self.s['revision'], original['revision'] + 1)
        self.assertEqual(self.s['instance_id'], original['instance_id'])
        self.assertEqual(self.s['qid'], original['qid'])
        self.assertTrue(eng.public(self.s)['help_used'])
        self.assertEqual(eng.public(self.s)['last_answer'], '0')
        for before, after in zip(original['attempts'], self.s['attempts']):
            for key, value in before.items():
                self.assertEqual(after[key], value)
        mappings = eng.public(self.s)['legacy_drafts']
        current = next(x for x in mappings if x['question_ref'] == eng.current_q(self.s)['ref'])
        self.assertEqual(current['run_id'], self.s['run_id'])
        self.assertEqual(current['old_key_suffix'], legacy.COURSE['version'] + ':training:guided:1')
        snapshot = copy.deepcopy(self.s)
        self.assertFalse(eng.migrate_state(self.s))
        self.assertEqual(self.s, snapshot)

    def test_unknown_history_kept_without_current_answer_attachment(self):
        self.old_training()
        self.old('answer', answer='0')
        self.s['course_version'] = 'unknown-old-version'
        for item in self.s['attempts']:
            item['course_version'] = 'unknown-old-version'
        self.s['evidence'] = {'unknown-old-version:' + self.s['qid']: {'first_seen_at': 'old', 'help_used': True}}
        eng.migrate_state(self.s)
        self.assertEqual(self.s['phase'], 'catalog')
        eng.action(self.s, {'action': 'report', 'revision': self.s['revision']})
        report = eng.public(self.s)['report']
        self.assertTrue(all(not row['answer_html'] for row in report['rows']))
        data = eng.export_state(self.s)
        self.assertTrue(any(item['help_used'] for item in data['evidence'].values()))
        self.assertEqual(len(data['attempts']), 2)

    def test_unknown_top_level_version_keeps_known_per_question_evidence(self):
        self.old_training()
        self.old('hint')
        self.old('tutor', message=legacy.PRESETS[0])
        qid = self.s['qid']
        self.s['course_version'] = 'unknown-session-course'
        eng.migrate_state(self.s)
        e = eng._read_evidence(self.s, qid)
        self.assertTrue(e['help_used'])
        self.assertEqual(len(e['chat']), 2)
        self.assertFalse(eng._is_unseen(self.s, qid))
        exported = eng.export_state(self.s)
        self.assertEqual(sum(len(x.get('chat', [])) for x in exported['evidence'].values()), 2)
        self.assertTrue(any(x.get('question_label') == '历史题目' for x in exported['events']))

    def test_old_post_resumes_then_migrates_completed_pool(self):
        self.old_training()
        while self.s['phase'] == 'training':
            self.old('answer', answer=legacy.current_q(self.s)['answer'])
            self.old('next')
        self.old('post')
        self.old('answer', answer=legacy.current_q(self.s)['answer'])
        self.assertFalse(eng.migrate_state(self.s))
        eng.action(self.s, {'action': 'answer', 'revision': self.s['revision'], 'instance_id': self.s['instance_id'],
                            'answer': legacy.current_q(self.s)['answer']})
        self.assertEqual(self.s['phase'], 'report')
        self.assertEqual(self.s['version'], 3)
        self.assertFalse(eng._available(self.s, 'substitution', 'post'))
        self.assertEqual(eng.public(self.s)['report']['post']['independent_complete'], 2)

    def test_paused_legacy_foundation_keeps_return_route_and_draft(self):
        self.old_training()
        main_qid = self.s['qid']
        self.old('foundation')
        self.old('plan')
        eng.migrate_state(self.s)
        self.assertEqual(self.s['phase'], 'catalog')
        self.assertEqual(self.s['resume_qid'], 'basic_foundation')
        eng.action(self.s, {'action': 'select', 'revision': self.s['revision'],
                            'question_ref': eng.QUESTIONS['basic_foundation']['ref']})
        self.assertEqual(self.s['stage'], 'foundation')
        eng.action(self.s, {'action': 'return_training', 'revision': self.s['revision'],
                            'instance_id': self.s['instance_id']})
        self.assertEqual(self.s['qid'], main_qid)


if __name__ == '__main__':
    unittest.main()
