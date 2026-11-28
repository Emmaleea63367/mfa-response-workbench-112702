"""多因素认证异常检测与缓解台的规则测试。"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from duty_desk import Desk
from source_records import SOURCES

ROOT = SOURCES.parent


def desk_through(seq: int) -> Desk:
    desk = Desk()
    desk.ingest_through(seq)
    return desk


def full_desk() -> Desk:
    desk = Desk()
    desk.ingest_all()
    return desk


def find_attempt(review: dict, source: str, attempt: str, account: str | None = None) -> dict:
    for entry in review['accounts']:
        if account is not None and entry['account'] != account:
            continue
        for item in entry['attempts']:
            if item['source'] == source and item['attempt'] == attempt:
                return item
    raise AssertionError(f'未找到尝试 {source}/{attempt}')


def find_account(review: dict, account: str) -> dict:
    for entry in review['accounts']:
        if entry['account'] == account:
            return entry
    raise AssertionError(f'未找到账号 {account}')


def find_mitigation(mitigations: list[dict], source: str, attempt: str) -> dict:
    for item in mitigations:
        if item['target'] == [source, attempt]:
            return item
    raise AssertionError(f'未找到处置目标 {source}/{attempt}')


class IngestionTest(unittest.TestCase):
    def test_duplicate_delivery_is_one_attempt_without_extra_risk_evidence(self):
        desk = full_desk()
        attempts = desk.attempts('dept-n:23')
        keys = [(a.source, a.attempt) for a in attempts]
        # seq 1 与 seq 3 是同一尝试的重复投递，只能有一个 query/a-9
        self.assertEqual(keys.count(('query', 'a-9')), 1)
        view = next(a for a in attempts if (a.source, a.attempt) == ('query', 'a-9'))
        self.assertEqual(view.arrival_count, 3)  # seq 1、3、5
        self.assertEqual(view.state, 'completed')  # 最新观测覆盖 waiting
        self.assertEqual(view.observed_at, 1)  # 事实发生时刻锚定首次观测
        self.assertEqual(view.arrivals[-1], (5, 4, 'completed'))  # 后续到达完整留痕

    def test_local_attempt_numbers_collide_across_sources_and_domains(self):
        desk = full_desk()
        review = desk.review()
        n_item = find_attempt(review, 'query', 'a-9', 'dept-n:23')
        s_item = find_attempt(review, 'reply', 'a-9', 'dept-s:23')
        self.assertNotEqual(n_item['factor'], s_item['factor'])
        self.assertEqual(len(desk.attempts('dept-s:23')), 1)

    def test_replay_prefix_refects_partial_observation(self):
        early = desk_through(4)
        view = next(a for a in early.attempts('dept-n:23') if (a.source, a.attempt) == ('query', 'a-9'))
        self.assertEqual(view.state, 'waiting')
        self.assertEqual(early.attempts('dept-n:23')[0].arrival_count, 2)
        later = desk_through(5)
        view = next(a for a in later.attempts('dept-n:23') if (a.source, a.attempt) == ('query', 'a-9'))
        self.assertEqual(view.state, 'completed')


class ReportLifecycleTest(unittest.TestCase):
    def test_correction_withdraws_specific_report(self):
        before = desk_through(17)
        self.assertTrue(before.active_reports('dept-n:23', 'query', 'a-9'))
        after = full_desk()
        self.assertFalse(after.active_reports('dept-n:23', 'query', 'a-9'))
        ticket7 = after.report('ticket-7')
        self.assertEqual(ticket7.superseded_by, 'ticket-9')

    def test_account_level_report_stays_active_and_carries_checkable_range(self):
        desk = full_desk()
        review = desk.review('dept-n:23')
        entry = find_account(review, 'dept-n:23')
        concerns = entry['account_concerns']
        self.assertEqual([c['report'] for c in concerns if c['active']], ['ticket-8'])
        concern = next(c for c in concerns if c['report'] == 'ticket-8')
        self.assertEqual(concern['checkable_window'], [2, 14])  # 14 - horizon 12
        self.assertIsNone(concern['source'])
        self.assertIsNone(concern['attempt'])


class EnrollmentTest(unittest.TestCase):
    def test_scheduled_is_not_enrolled_and_enrolled_device_is_registered(self):
        desk = full_desk()
        # en-2 预约 effective=9，但 t=10 时尚未登记
        self.assertFalse(desk.is_registered('dept-n:23', 'push-new', 'phone-m', 10))
        # t=12 完成登记后才合法
        self.assertTrue(desk.is_registered('dept-n:23', 'push-new', 'phone-m', 12))
        # 旧设备 en-1 在 t=1 处于登记期，t=13 起退役
        self.assertTrue(desk.is_registered('dept-n:23', 'push-old', 'phone-m', 1))
        self.assertFalse(desk.is_registered('dept-n:23', 'push-old', 'phone-m', 13))

    def test_shared_display_name_alone_cannot_identify_device(self):
        desk = full_desk()
        # 同一 display 对应两台设备，仅凭显示名解析不出 device_identity
        self.assertIsNone(desk.device_identity_by_display('dept-n:23', 'phone-m'))
        # credential 与时刻可以唯一解析
        self.assertEqual(
            desk.device_identity_for('dept-n:23', 'push-old', 'phone-m', 1), 'device-24'
        )
        self.assertEqual(
            desk.device_identity_for('dept-n:23', 'push-new', 'phone-m', 13), 'device-89'
        )


class PolicyTest(unittest.TestCase):
    def test_latest_revision_in_effect_controls_basis_and_horizon(self):
        rev1_desk = desk_through(12)
        policy = rev1_desk.policy_for('dept-n:23')
        self.assertEqual(policy.revision, 1)
        self.assertEqual(policy.horizon, 20)
        self.assertNotIn('enrollment', policy.basis)
        rev2 = full_desk().policy_for('dept-n:23')
        self.assertEqual(rev2.revision, 2)
        self.assertEqual(rev2.horizon, 12)
        self.assertIn('enrollment', rev2.basis)
        self.assertEqual(rev2.retained, ('hardware-k',))

    def test_horizon_defines_checkable_window(self):
        old = desk_through(12).review('dept-n:23')
        old_a9 = find_attempt(old, 'query', 'a-9')
        self.assertTrue(old_a9['in_window'])  # horizon 20 覆盖 t=1
        current = full_desk().review('dept-n:23')
        entry = find_account(current, 'dept-n:23')
        self.assertEqual(entry['window'], {'at': 14, 'start': 2, 'end': 14})
        new_a9 = find_attempt(current, 'query', 'a-9')
        self.assertFalse(new_a9['in_window'])  # t=1 落在 horizon 12 之外
        self.assertTrue(find_attempt(current, 'query', 'a-10')['in_window'])


class DetectionTest(unittest.TestCase):
    def test_specific_denial_matching_definite_record_is_quarantine(self):
        desk = desk_through(10)
        review = desk.review('dept-n:23')
        item = find_attempt(review, 'query', 'a-9')
        self.assertEqual(item['risk'], 'quarantine')
        codes = {reason['code'] for reason in item['reasons']}
        self.assertIn('specific-denial', codes)
        evidence_seqs = {e['seq'] for e in item['evidence']}
        self.assertIn(4, evidence_seqs)   # ticket-7
        self.assertIn(5, evidence_seqs)   # 完成态认证记录
        self.assertEqual(item['policy_revision'], 1)

    def test_account_level_report_keeps_identity_pending_without_isolation(self):
        desk = desk_through(12)
        review = desk.review('dept-n:23')
        item = find_attempt(review, 'query', 'a-10')
        self.assertEqual(item['risk'], 'pending')
        self.assertIn(
            'account-level-report', {r['code'] for r in item['reasons']}
        )
        # 账号级报告不指向具体尝试，除具体否认项外不产生处置建议
        targets = [tuple(rec['target']) for rec in desk.recommend('dept-n:23')]
        self.assertNotIn(('query', 'a-10'), targets)
        self.assertEqual(
            targets, [('query', 'a-9')]
        )  # 仅 ticket-7 指向的具体否认项需要处置

    def test_correction_plus_enrolled_origin_clears_risk_item(self):
        desk = full_desk()
        item = find_attempt(desk.review('dept-n:23'), 'query', 'a-9')
        self.assertEqual(item['risk'], 'cleared')
        codes = {r['code'] for r in item['reasons']}
        self.assertIn('correction-withdrawn', codes)
        self.assertIn('enrolled-device-proven', codes)

    def test_enrollment_basis_gating_under_revision_one(self):
        # rev1 的 basis 不含 enrollment：即便更正也不能凭换机记录开脱
        desk = Desk()
        for row in [
            {'seq': 1, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            {'seq': 2, 'order': 'en-1', 'account': 'dept-n:23', 'credential': 'push-old',
             'display': 'phone-m', 'device_identity': 'device-24', 'status': 'enrolled', 'effective': 0},
            {'seq': 3, 'revision': 1, 'applies': 'dept-n', 'horizon': 20,
             'basis': ['auth-event', 'verified-report'], 'retained': ['hardware-k']},
            {'seq': 4, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
            {'seq': 5, 'report': 't-2', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'correction', 'supersedes': 't-1'},
        ]:
            desk.ingest(row)
        item = find_attempt(desk.review('dept-n:23'), 'query', 'a-1')
        self.assertEqual(item['risk'], 'pending')
        self.assertNotIn(
            'enrolled-device-proven', {r['code'] for r in item['reasons']}
        )

    def test_legitimate_swap_session_is_cleared(self):
        desk = full_desk()
        item = find_attempt(desk.review('dept-n:23'), 'reply', 'a-42')
        self.assertEqual(item['risk'], 'cleared')
        self.assertIn(
            'enrolled-device-proven', {r['code'] for r in item['reasons']}
        )

    def test_unobserved_device_and_incomplete_attempt_stay_pending(self):
        desk = full_desk()
        item = find_attempt(desk.review('dept-n:23'), 'reply', 'a-43')
        self.assertEqual(item['risk'], 'pending')
        codes = {r['code'] for r in item['reasons']}
        self.assertIn('device-unobserved', codes)
        self.assertIn('incomplete-auth', codes)

    def test_same_display_or_later_swap_or_one_approval_cannot_exonerate(self):
        desk = Desk()
        rows = [
            {'seq': 1, 'revision': 2, 'applies': 'dept-n', 'horizon': 30,
             'basis': ['auth-event', 'verified-report', 'enrollment'], 'retained': ['hardware-k']},
            # 在先的被否认会话：旧设备
            {'seq': 2, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            # 新设备一次成功批准，显示名完全相同
            {'seq': 3, 'source': 'query', 'attempt': 'a-2', 'account': 'dept-n:23',
             'factor': 'push-new', 'device': 'phone-m', 'state': 'completed', 'observed_at': 12},
            {'seq': 4, 'order': 'en-1', 'account': 'dept-n:23', 'credential': 'push-old',
             'display': 'phone-m', 'device_identity': 'device-24', 'status': 'enrolled', 'effective': 0},
            {'seq': 5, 'order': 'en-2', 'account': 'dept-n:23', 'credential': 'push-new',
             'display': 'phone-m', 'device_identity': 'device-89', 'status': 'scheduled', 'effective': 9},
            {'seq': 6, 'order': 'en-2', 'account': 'dept-n:23', 'credential': 'push-new',
             'display': 'phone-m', 'device_identity': 'device-89', 'status': 'enrolled', 'effective': 12},
            {'seq': 7, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
        ]
        for row in rows:
            desk.ingest(row)
        review = desk.review('dept-n:23')
        risky = find_attempt(review, 'query', 'a-1')
        self.assertEqual(risky['risk'], 'quarantine')
        approved = find_attempt(review, 'query', 'a-2')
        self.assertEqual(approved['risk'], 'cleared')  # 新会话自身合法
        # 被否认的旧会话不会因后来的成功批准/完成换机而脱责
        self.assertNotIn(
            'enrolled-device-proven', {r['code'] for r in risky['reasons']}
        )

    def test_clean_account_without_adverse_evidence_is_normal(self):
        desk = full_desk()
        item = find_attempt(desk.review('dept-s:23'), 'reply', 'a-9')
        self.assertEqual(item['risk'], 'normal')
        self.assertEqual(desk.recommend('dept-s:23'), [])

    def test_conclusions_cite_policy_and_observations(self):
        desk = desk_through(10)
        item = find_attempt(desk.review('dept-n:23'), 'query', 'a-9')
        self.assertEqual(item['policy_revision'], 1)
        self.assertTrue(item['evidence'])
        self.assertTrue(all('seq' in e and 'source' in e for e in item['evidence']))
        self.assertTrue(item['reasons'])


class MitigationTest(unittest.TestCase):
    def test_queue_progression_is_visible(self):
        queued = find_mitigation(desk_through(10).mitigations(), 'query', 'a-9')
        self.assertEqual(queued['queue_state'], 'queued')
        self.assertFalse(queued['restricted'])
        applied = find_mitigation(desk_through(19).mitigations(), 'query', 'a-9')
        self.assertEqual(applied['queue_state'], 'applied')
        self.assertTrue(applied['restricted'])

    def test_two_operations_on_same_target_remain_separately_visible(self):
        desk = desk_through(20)
        target = find_mitigation(desk.mitigations(), 'query', 'a-9')
        operations = {(op['operation'], op['cause']) for op in target['operations']}
        self.assertIn(('resp-n', 'ticket-7'), operations)
        self.assertIn(('resp-m', 'independent-admin'), operations)

    def test_release_cannot_restore_after_independent_revocation(self):
        desk = full_desk()
        target = find_mitigation(desk.mitigations(), 'query', 'a-9')
        self.assertEqual(target['queue_state'], 'target_unverified')
        self.assertTrue(target['restricted'])  # 会话仍被隔离
        release = target['release_decisions'][0]
        self.assertFalse(release['effective'])
        self.assertIn('independent-revocation', release['reasons'])
        self.assertIn('partially-observed', release['reasons'])
        self.assertIn('independent-revocation', target['blockers'])

    def test_release_restores_only_same_lineage_when_identity_holds(self):
        desk = Desk()
        rows = [
            {'seq': 1, 'revision': 2, 'applies': 'dept-n', 'horizon': 30,
             'basis': ['auth-event', 'verified-report', 'enrollment'], 'retained': ['hardware-k']},
            {'seq': 2, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            {'seq': 3, 'order': 'en-1', 'account': 'dept-n:23', 'credential': 'push-old',
             'display': 'phone-m', 'device_identity': 'device-24', 'status': 'enrolled', 'effective': 0},
            {'seq': 4, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
            {'seq': 5, 'operation': 'resp-x', 'account': 'dept-n:23', 'target': ['query', 'a-1'],
             'action': 'isolate', 'status': 'applied', 'cause': 't-1'},
            {'seq': 6, 'report': 't-2', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'correction', 'supersedes': 't-1'},
            {'seq': 7, 'operation': 'resp-x', 'account': 'dept-n:23', 'target': ['query', 'a-1'],
             'action': 'release', 'status': 'applied', 'cause': 't-2'},
        ]
        for row in rows:
            desk.ingest(row)
        target = find_mitigation(desk.mitigations(), 'query', 'a-1')
        self.assertFalse(target['restricted'])
        self.assertTrue(target['release_decisions'][0]['effective'])

    def test_recommendation_preserves_retained_entry_and_other_factors(self):
        desk = desk_through(10)
        recommendations = desk.recommend('dept-n:23')
        self.assertEqual(len(recommendations), 1)
        rec = recommendations[0]
        self.assertEqual(rec['target'], ['query', 'a-9'])
        self.assertEqual(rec['action'], 'isolate')  # 已完成会话 -> 隔离
        self.assertEqual(rec['factor'], 'push-old')
        self.assertIn('hardware-k', rec['preserved_factors'])
        # 保留入口上的会话不得进入处置范围
        self.assertEqual(rec['target'], ['query', 'a-9'])
        self.assertIn(['query', 'a-10'], rec['preserved_targets'])

    def test_incomplete_denied_attempt_asks_reauthentication(self):
        desk = Desk()
        rows = [
            {'seq': 1, 'revision': 1, 'applies': 'dept-n', 'horizon': 20,
             'basis': ['auth-event', 'verified-report'], 'retained': ['hardware-k']},
            {'seq': 2, 'source': 'query', 'attempt': 'a-7', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'incomplete', 'observed_at': 3},
            {'seq': 3, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-7', 'report_state': 'unrecognized', 'supersedes': None},
        ]
        for row in rows:
            desk.ingest(row)
        rec = desk.recommend('dept-n:23')[0]
        self.assertEqual(rec['action'], 'reauthenticate')

    def test_repeated_report_delivery_does_not_add_risk(self):
        desk = Desk()
        rows = [
            {'seq': 1, 'revision': 1, 'applies': 'dept-n', 'horizon': 20,
             'basis': ['auth-event', 'verified-report'], 'retained': ['hardware-k']},
            {'seq': 2, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            {'seq': 3, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
            {'seq': 4, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
        ]
        for row in rows:
            desk.ingest(row)
        item = find_attempt(desk.review('dept-n:23'), 'query', 'a-1')
        self.assertEqual(item['risk'], 'quarantine')
        self.assertEqual(
            sum(1 for e in item['evidence'] if e.get('ref') == 'ticket-t-1' or e.get('report') == 't-1'), 1
        )


class NoticeTest(unittest.TestCase):
    def test_affected_user_is_pointed_to_retained_entry(self):
        desk = full_desk()
        notices = desk.notices('dept-n:23')
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice['retained'], ['hardware-k'])
        self.assertIn(['query', 'a-9'], notice['targets'])
        self.assertTrue(notice['reauthenticate'])

    def test_no_notice_without_effective_restriction(self):
        self.assertEqual(desk_through(9).notices(), [])


class EdgeCaseTest(unittest.TestCase):
    def _desk(self, rows):
        desk = Desk()
        for row in rows:
            desk.ingest(row)
        return desk

    def test_retirement_effective_boundary_is_exact(self):
        desk = full_desk()
        # en-1 effective=13 退役：t=12 仍登记，t=13 起不再登记
        self.assertTrue(desk.is_registered('dept-n:23', 'push-old', 'phone-m', 12))
        self.assertFalse(desk.is_registered('dept-n:23', 'push-old', 'phone-m', 13))

    def test_release_before_independent_revocation_then_revocation_lands(self):
        desk = self._desk([
            {'seq': 1, 'revision': 2, 'applies': 'dept-n', 'horizon': 30,
             'basis': ['auth-event', 'verified-report', 'enrollment'], 'retained': ['hardware-k']},
            {'seq': 2, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            {'seq': 3, 'order': 'en-1', 'account': 'dept-n:23', 'credential': 'push-old',
             'display': 'phone-m', 'device_identity': 'device-24', 'status': 'enrolled', 'effective': 0},
            {'seq': 4, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
            {'seq': 5, 'operation': 'resp-x', 'account': 'dept-n:23', 'target': ['query', 'a-1'],
             'action': 'isolate', 'status': 'applied', 'cause': 't-1'},
            {'seq': 6, 'report': 't-2', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'correction', 'supersedes': 't-1'},
            {'seq': 7, 'operation': 'resp-x', 'account': 'dept-n:23', 'target': ['query', 'a-1'],
             'action': 'release', 'status': 'applied', 'cause': 't-2'},
            # 解除生效后又出现独立撤销：会话重新受限，旧解除不恢复它
            {'seq': 8, 'operation': 'resp-m', 'account': 'dept-n:23', 'target': ['query', 'a-1'],
             'action': 'isolate', 'status': 'applied', 'cause': 'independent-admin'},
        ])
        target = find_mitigation(desk.mitigations(), 'query', 'a-1')
        self.assertTrue(target['restricted'])
        self.assertEqual(target['release_decisions'][0]['reasons'], [])

    def test_repeated_report_delivery_keeps_single_evidence(self):
        desk = self._desk([
            {'seq': 1, 'revision': 1, 'applies': 'dept-n', 'horizon': 20,
             'basis': ['auth-event', 'verified-report'], 'retained': ['hardware-k']},
            {'seq': 2, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-n:23',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
            {'seq': 3, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
            {'seq': 4, 'report': 't-1', 'account': 'dept-n:23', 'source': 'query',
             'attempt': 'a-1', 'report_state': 'unrecognized', 'supersedes': None},
        ])
        item = find_attempt(desk.review('dept-n:23'), 'query', 'a-1')
        self.assertEqual(item['risk'], 'quarantine')
        report_evidence = [e for e in item['evidence'] if e['source'] == 'reports']
        self.assertEqual(len(report_evidence), 1)

    def test_unknown_record_shape_is_rejected(self):
        desk = Desk()
        with self.assertRaisesRegex(ValueError, '无法识别'):
            desk.ingest({'seq': 1, 'unexpected': True})

    def test_no_policy_means_observations_remain_unassessed_not_guessed(self):
        desk = self._desk([
            {'seq': 1, 'source': 'query', 'attempt': 'a-1', 'account': 'dept-x:1',
             'factor': 'push-old', 'device': 'phone-m', 'state': 'completed', 'observed_at': 1},
        ])
        item = find_attempt(desk.review('dept-x:1'), 'query', 'a-1')
        self.assertIsNone(item['policy_revision'])
        self.assertIsNone(item['risk'])


class CliTest(unittest.TestCase):
    def test_cli_emits_json_review(self):
        script = ROOT / 'duty_desk.py'
        output = subprocess.check_output(
            [sys.executable, script, '--through', '10'], text=True
        )
        payload = json.loads(output)
        self.assertEqual(set(payload), {'accounts', 'mitigations', 'notices'})
        item = find_attempt(payload, 'query', 'a-9', 'dept-n:23')
        self.assertEqual(item['risk'], 'quarantine')

    def test_cli_filters_account(self):
        script = ROOT / 'duty_desk.py'
        output = subprocess.check_output(
            [sys.executable, script, 'dept-s:23'], text=True
        )
        payload = json.loads(output)
        self.assertEqual([a['account'] for a in payload['accounts']], ['dept-s:23'])


if __name__ == '__main__':
    unittest.main()
