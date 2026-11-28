"""多因素认证异常的检测与缓解台。

值班台只消费已核验的本地观测与治理回执：
认证尝试、可靠渠道报告、换机登记、值班策略、管理端处置回执。
它不生成确认请求、不接触因子秘密，所有结论都能回溯到策略与观测。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from source_records import SOURCES, read_source, source_names

# 风险结论：
#   quarantine 待隔离（可靠否认与确切认证记录对应）
#   cleared    风险项已消除（证据证明来自已登记且未被否认的合法换机）
#   pending    身份待核（观测不全或报告未指向具体尝试）
#   normal     无不利证据
RISK_ORDER = {'quarantine': 0, 'pending': 1, 'cleared': 2, 'normal': 3}


@dataclass(frozen=True)
class AttemptView:
    """去重后的一次身份尝试（重复投递合并为一条）。"""

    account: str
    source: str
    attempt: str
    factor: str
    device: str | None
    state: str
    observed_at: int
    seqs: tuple[int, ...]
    arrivals: tuple[tuple[int, int, str], ...] = ()

    @property
    def arrival_count(self) -> int:
        return len(self.seqs)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.account, self.source, self.attempt


@dataclass(frozen=True)
class ReportView:
    report: str
    account: str
    source: str | None
    attempt: str | None
    report_state: str
    supersedes: str | None
    seq: int
    superseded_by: str | None = None

    @property
    def key(self) -> tuple[str | None, str | None]:
        return self.source, self.attempt

    @property
    def specific(self) -> bool:
        return self.source is not None and self.attempt is not None


@dataclass(frozen=True)
class EnrollmentView:
    account: str
    credential: str
    display: str
    device_identity: str
    status: str
    effective: int
    seq: int


@dataclass(frozen=True)
class PolicyView:
    applies: str
    revision: int
    horizon: int
    basis: tuple[str, ...]
    retained: tuple[str, ...]
    seq: int

    @property
    def account_domain(self) -> str:
        return self.applies


@dataclass(frozen=True)
class ResponseView:
    operation: str
    account: str
    source: str
    attempt: str
    action: str
    status: str
    cause: str
    seq: int

    @property
    def target(self) -> tuple[str, str]:
        return self.source, self.attempt


@dataclass
class _DeskState:
    attempts: dict[tuple[str, str, str], AttemptView] = field(default_factory=dict)
    attempt_order: list[tuple[str, str, str]] = field(default_factory=list)
    reports: dict[str, ReportView] = field(default_factory=dict)
    report_order: list[str] = field(default_factory=list)
    enrollments: list[EnrollmentView] = field(default_factory=list)
    policies: dict[str, PolicyView] = field(default_factory=dict)
    responses: list[ResponseView] = field(default_factory=list)
    max_seq: int = 0


class Desk:
    """持续接收演练事件并给出检测结论与缓解视图。"""

    def __init__(self, directory: Path = SOURCES) -> None:
        self._directory = directory
        self._state = _DeskState()

    # ------------------------------------------------------------------ 接收

    def ingest_through(self, seq: int) -> None:
        """按全局到达顺序接收截至 seq（含）的全部来源记录。"""
        rows: list[dict] = []
        for name in source_names(self._directory):
            for row in read_source(name, self._directory):
                if row['seq'] <= seq:
                    rows.append(row)
        for row in sorted(rows, key=lambda item: item['seq']):
            self.ingest(row)

    def ingest_all(self) -> None:
        rows = [row for name in source_names(self._directory) for row in read_source(name, self._directory)]
        for row in sorted(rows, key=lambda item: item['seq']):
            self.ingest(row)

    def ingest(self, row: dict) -> None:
        """接收一条原始来源记录；重复投递不新增风险依据。"""
        seq = row['seq']
        self._state.max_seq = max(self._state.max_seq, seq)
        if 'state' in row and 'observed_at' in row:
            self._ingest_attempt(row)
        elif 'report_state' in row:
            self._ingest_report(row)
        elif 'order' in row:
            self._ingest_enrollment(row)
        elif 'horizon' in row and 'basis' in row:
            self._ingest_policy(row)
        elif 'operation' in row and 'target' in row:
            self._ingest_response(row)
        else:
            raise ValueError(f'seq {seq} 无法识别的来源记录')

    def _ingest_attempt(self, row: dict) -> None:
        key = (row['account'], row['source'], row['attempt'])
        current = self._state.attempts.get(key)
        arrival = (row['seq'], row['observed_at'], row['state'])
        if current is None:
            self._state.attempt_order.append(key)
            view = AttemptView(
                account=row['account'],
                source=row['source'],
                attempt=row['attempt'],
                factor=row['factor'],
                device=row['device'],
                state=row['state'],
                observed_at=row['observed_at'],
                seqs=(row['seq'],),
                arrivals=(arrival,),
            )
        else:
            # observed_at 是事实发生时刻，重复投递与后续状态观测不覆盖它；
            # 状态取最新到达，但风险判断仍锚定首次事实时刻。
            view = AttemptView(
                account=current.account,
                source=current.source,
                attempt=current.attempt,
                factor=row['factor'],
                device=row['device'],
                state=row['state'],
                observed_at=current.observed_at,
                seqs=current.seqs + (row['seq'],),
                arrivals=current.arrivals + (arrival,),
            )
        self._state.attempts[key] = view

    def _ingest_report(self, row: dict) -> None:
        report_id = row['report']
        view = ReportView(
            report=report_id,
            account=row['account'],
            source=row['source'],
            attempt=row['attempt'],
            report_state=row['report_state'],
            supersedes=row['supersedes'],
            seq=row['seq'],
        )
        if report_id not in self._state.reports:
            self._state.report_order.append(report_id)
        self._state.reports[report_id] = view
        # 登记更正链：被明确 supersedes 的报告不再有效
        if view.supersedes and view.supersedes in self._state.reports:
            old = self._state.reports[view.supersedes]
            self._state.reports[view.supersedes] = ReportView(
                report=old.report,
                account=old.account,
                source=old.source,
                attempt=old.attempt,
                report_state=old.report_state,
                supersedes=old.supersedes,
                seq=old.seq,
                superseded_by=report_id,
            )

    def _ingest_enrollment(self, row: dict) -> None:
        self._state.enrollments.append(
            EnrollmentView(
                account=row['account'],
                credential=row['credential'],
                display=row['display'],
                device_identity=row['device_identity'],
                status=row['status'],
                effective=row['effective'],
                seq=row['seq'],
            )
        )

    def _ingest_policy(self, row: dict) -> None:
        view = PolicyView(
            applies=row['applies'],
            revision=row['revision'],
            horizon=row['horizon'],
            basis=tuple(row['basis']),
            retained=tuple(row['retained']),
            seq=row['seq'],
        )
        current = self._state.policies.get(view.applies)
        if current is None or view.revision > current.revision:
            self._state.policies[view.applies] = view

    def _ingest_response(self, row: dict) -> None:
        source, attempt = row['target']
        self._state.responses.append(
            ResponseView(
                operation=row['operation'],
                account=row['account'],
                source=source,
                attempt=attempt,
                action=row['action'],
                status=row['status'],
                cause=row['cause'],
                seq=row['seq'],
            )
        )

    # ------------------------------------------------------------- 来源视图

    def attempts(self, account: str | None = None) -> list[AttemptView]:
        return [
            self._state.attempts[key]
            for key in self._state.attempt_order
            if account is None or key[0] == account
        ]

    def report(self, report_id: str) -> ReportView:
        return self._state.reports[report_id]

    def active_reports(self, account: str, source: str, attempt: str) -> list[ReportView]:
        """指向具体尝试且仍未被更正撤回的否认报告。"""
        return [
            report
            for report in self._state.reports.values()
            if report.account == account
            and report.source == source
            and report.attempt == attempt
            and report.report_state == 'unrecognized'
            and report.superseded_by is None
        ]

    def specific_reports(self, account: str, source: str, attempt: str) -> list[ReportView]:
        """指向该具体尝试的全部已接收报告（含更正）。"""
        return [
            self._state.reports[report_id]
            for report_id in self._state.report_order
            if (self._state.reports[report_id].account == account
                and self._state.reports[report_id].source == source
                and self._state.reports[report_id].attempt == attempt)
        ]

    def active_account_reports(self, account: str) -> list[ReportView]:
        return [
            report
            for report_id in self._state.report_order
            for report in [self._state.reports[report_id]]
            if report.account == account and not report.specific and report.superseded_by is None
        ]

    def policy_for(self, account: str) -> PolicyView | None:
        domain = account.split(':', 1)[0]
        return self._state.policies.get(domain)

    # ------------------------------------------------------------- 换机事实

    def _enrollments_for(self, account: str, credential: str, display: str) -> list[EnrollmentView]:
        return [
            item
            for item in self._state.enrollments
            if item.account == account
            and item.credential == credential
            and item.display == display
        ]

    def device_identity_by_display(self, account: str, display: str) -> str | None:
        """仅凭显示名无法区分共用 display 的设备。"""
        identities = {
            item.device_identity
            for item in self._state.enrollments
            if item.account == account and item.display == display
        }
        return next(iter(identities)) if len(identities) == 1 else None

    def device_identity_for(
        self, account: str, credential: str, display: str, at: int
    ) -> str | None:
        """结合凭据、显示名与时刻解析唯一设备身份。"""
        candidates = [
            item
            for item in self._enrollments_for(account, credential, display)
            if item.effective <= at
        ]
        identities = {item.device_identity for item in candidates}
        return next(iter(identities)) if len(identities) == 1 else None

    def is_registered(self, account: str, credential: str, display: str, at: int) -> bool:
        """该时刻凭据是否对应已登记且未退役的设备（预约不算登记）。"""
        identity = self.device_identity_for(account, credential, display, at)
        if identity is None:
            return False
        latest = max(
            (
                item
                for item in self._enrollments_for(account, credential, display)
                if item.device_identity == identity and item.effective <= at
            ),
            key=lambda item: item.seq,
        )
        return latest.status == 'enrolled'

    # ------------------------------------------------------------- 检测结论

    def review(self, account: str | None = None) -> dict:
        """按账号给出每个尝试的已核事实、风险结论、依据与可核对范围。"""
        accounts: dict[str, list[AttemptView]] = {}
        for key in self._state.attempt_order:
            accounts.setdefault(key[0], []).append(self._state.attempts[key])
        result = []
        for acct in sorted(accounts):
            if account is not None and acct != account:
                continue
            result.append(self._review_account(acct, accounts[acct]))
        return {'accounts': result}

    def _review_account(self, account: str, items: list[AttemptView]) -> dict:
        policy = self.policy_for(account)
        horizon = policy.horizon if policy else None
        # 比对时段按账号域统计：参考时刻取该域全部已观测时刻的最大值
        observed_moments = [
            observed_at
            for key, view in self._state.attempts.items()
            if policy is None or key[0].split(':', 1)[0] == policy.applies
            for _, observed_at, _ in view.arrivals
        ]
        latest_observed = max(observed_moments)
        start = latest_observed - horizon if horizon is not None else None
        window = (
            {'at': latest_observed, 'start': start, 'end': latest_observed}
            if horizon is not None
            else None
        )
        account_reports = self.active_account_reports(account)
        concerns = [
            {
                'report': report.report,
                'source': report.source,
                'attempt': report.attempt,
                'active': True,
                'checkable_window': [start, latest_observed] if start is not None else None,
                'seq': report.seq,
            }
            for report in account_reports
        ]
        attempts = [self._review_attempt(item, policy, bool(account_reports), start, latest_observed)
                    for item in items]
        attempts.sort(key=lambda item: (item['observed_at'], item['source'], item['attempt']))
        return {
            'account': account,
            'policy_revision': policy.revision if policy else None,
            'window': window,
            'account_concerns': concerns,
            'attempts': attempts,
        }

    def _review_attempt(
        self,
        item: AttemptView,
        policy: PolicyView | None,
        has_account_report: bool,
        window_start: int | None,
        window_end: int,
    ) -> dict:
        specific = self.specific_reports(item.account, item.source, item.attempt)
        denials = [report for report in specific
                   if report.report_state == 'unrecognized' and report.superseded_by is None]
        corrections = [report for report in specific if report.report_state == 'correction']

        reasons: list[dict] = []
        evidence: list[dict] = [
            {'source': 'attempts', 'seq': seq, 'observed_at': item.observed_at, 'state': item.state}
            for seq in item.seqs
        ]
        risk: str | None = 'normal'
        if policy is None:
            # 没有值班策略时不下结论，只保留已核观测，避免臆断
            return {
                'source': item.source,
                'attempt': item.attempt,
                'factor': item.factor,
                'device': item.device,
                'state': item.state,
                'observed_at': item.observed_at,
                'arrivals': item.arrival_count,
                'in_window': True,
                'risk': None,
                'policy_revision': None,
                'reasons': [{'code': 'no-policy', 'detail': '该账号域缺少值班策略，结论待策略到达'}],
                'evidence': evidence,
            }

        if denials:
            # 可靠渠道的明确否认与确切认证记录对应 -> 待隔离
            risk = 'quarantine'
            reasons.append({
                'code': 'specific-denial',
                'detail': '可靠渠道明确否认且与认证记录对应',
            })
            evidence.extend(self._report_evidence(report) for report in denials)
        elif corrections:
            # 用户更正撤回在先否认；能否开脱取决于能否证明合法换机
            evidence.extend(self._report_evidence(report) for report in corrections)
            reasons.append({
                'code': 'correction-withdrawn',
                'detail': '用户已更正在先否认',
            })
            proven, enroll_evidence = self._legal_swap_evidence(item, policy)
            if proven:
                risk = 'cleared'
                reasons.append({
                    'code': 'enrolled-device-proven',
                    'detail': '证据证明操作来自已登记且未被否认的合法换机',
                })
                evidence.extend(enroll_evidence)
            else:
                risk = 'pending'
                reasons.extend(self._pending_reasons(item, policy))
        else:
            proven, enroll_evidence = self._legal_swap_evidence(item, policy)
            if proven:
                risk = 'cleared'
                reasons.append({
                    'code': 'enrolled-device-proven',
                    'detail': '操作来自已登记设备',
                })
                evidence.extend(enroll_evidence)
            else:
                pending = self._pending_reasons(item, policy)
                if has_account_report:
                    pending = [
                        {'code': 'account-level-report',
                         'detail': '报告未指向具体尝试，身份待核，保留可核对范围'}
                    ] + pending
                if pending:
                    risk = 'pending'
                    reasons = pending
                else:
                    risk = 'normal'
                    reasons.append({'code': 'no-adverse-evidence', 'detail': '无不利证据'})

        # horizon 为比对时段跨度：可核对区间为 (at-horizon, at]，下界排他
        in_window = window_start is None or item.observed_at > window_start
        return {
            'source': item.source,
            'attempt': item.attempt,
            'factor': item.factor,
            'device': item.device,
            'state': item.state,
            'observed_at': item.observed_at,
            'arrivals': item.arrival_count,
            'in_window': in_window,
            'risk': risk,
            'policy_revision': policy.revision if policy else None,
            'reasons': reasons,
            'evidence': evidence,
        }

    def _pending_reasons(
        self, item: AttemptView, policy: PolicyView | None
    ) -> list[dict]:
        reasons: list[dict] = []
        if item.state != 'completed':
            reasons.append({
                'code': 'incomplete-auth',
                'detail': '认证尝试未完成，缺少确切认证记录',
            })
        if item.device is None:
            reasons.append({
                'code': 'device-unobserved',
                'detail': '设备归属缺失，无法核对登记',
            })
        elif policy is not None and 'enrollment' in policy.basis:
            if not self.is_registered(item.account, item.factor, item.device, item.observed_at):
                identity = self.device_identity_for(
                    item.account, item.factor, item.device, item.observed_at
                )
                code = 'device-unobserved' if identity is None else 'device-not-enrolled'
                detail = (
                    '观测范围内没有可对应的登记事实，身份待核'
                    if identity is None
                    else '设备在尝试时刻未处于已登记状态'
                )
                reasons.append({'code': code, 'detail': detail})
        return reasons

    def _legal_swap_evidence(
        self, item: AttemptView, policy: PolicyView | None
    ) -> tuple[bool, list[dict]]:
        """只有证据证明操作来自已登记且未被否认的合法换机时才成立。"""
        if policy is None or 'enrollment' not in policy.basis:
            return False, []
        if item.device is None:
            return False, []
        if not self.is_registered(item.account, item.factor, item.device, item.observed_at):
            return False, []
        identity = self.device_identity_for(
            item.account, item.factor, item.device, item.observed_at
        )
        enroll_evidence = [
            {
                'source': 'enrollment',
                'seq': entry.seq,
                'device_identity': entry.device_identity,
                'status': entry.status,
                'effective': entry.effective,
            }
            for entry in self._enrollments_for(item.account, item.factor, item.device)
            if entry.device_identity == identity and entry.effective <= item.observed_at
        ]
        return True, enroll_evidence

    @staticmethod
    def _report_evidence(report: ReportView) -> dict:
        return {
            'source': 'reports',
            'seq': report.seq,
            'report': report.report,
            'report_state': report.report_state,
            'superseded_by': report.superseded_by,
        }

    # ------------------------------------------------------------- 缓解处置

    def recommend(self, account: str) -> list[dict]:
        """按值班确认范围给出处置建议：会话隔离或要求重新验证。"""
        review = self._review_account(
            account, self.attempts(account)
        )
        policy = self.policy_for(account)
        retained = list(policy.retained) if policy else []
        recommendations = []
        for item in review['attempts']:
            if item['risk'] != 'quarantine' or not item['in_window']:
                continue
            preserved_targets = [
                [other['source'], other['attempt']]
                for other in review['attempts']
                if other['factor'] in retained
            ]
            recommendations.append({
                'account': account,
                'target': [item['source'], item['attempt']],
                'action': 'isolate' if item['state'] == 'completed' else 'reauthenticate',
                'factor': item['factor'],
                'preserved_factors': retained,
                'preserved_targets': preserved_targets,
            })
        return recommendations

    def mitigations(self) -> list[dict]:
        """管理端处置回执视角：队列状态、实际作用对象与解除决定。"""
        grouped: dict[tuple[str, str, str], list[ResponseView]] = {}
        order: list[tuple[str, str, str]] = []
        for response in self._state.responses:
            key = (response.account, response.source, response.attempt)
            if key not in grouped:
                order.append(key)
                grouped[key] = []
            grouped[key].append(response)

        result = []
        for key in order:
            account, source, attempt = key
            responses = sorted(grouped[key], key=lambda item: item.seq)
            operations = self._operation_views(responses)
            release_decisions, blockers = self._release_decisions(account, source, attempt, responses)
            latest_by_operation = {
                op['operation']: op['latest_status'] for op in operations
            }
            if any(status == 'partially_observed'
                   for r in responses if r.action == 'release'
                   for status in [r.status]):
                queue_state = 'target_unverified'
            elif 'applied' in latest_by_operation.values():
                queue_state = 'applied'
            elif 'queued' in latest_by_operation.values():
                queue_state = 'queued'
            else:
                queue_state = 'observed'
            independent_revocation = any(
                r.action == 'isolate' and r.status == 'applied' and r.cause == 'independent-admin'
                for r in responses
            )
            # 生效中的隔离且未被有效解除时，访问仍受限制
            restricted = any(
                r.action == 'isolate' and r.status == 'applied' for r in responses
            ) and (
                independent_revocation
                or not any(decision['effective'] for decision in release_decisions)
            )
            result.append({
                'account': account,
                'target': [source, attempt],
                'queue_state': queue_state,
                'restricted': restricted,
                'blockers': blockers,
                'operations': operations,
                'release_decisions': release_decisions,
            })
        return result

    @staticmethod
    def _operation_views(responses: list[ResponseView]) -> list[dict]:
        by_operation: dict[str, list[ResponseView]] = {}
        order: list[str] = []
        for response in responses:
            if response.operation not in by_operation:
                by_operation[response.operation] = []
                order.append(response.operation)
            by_operation[response.operation].append(response)
        return [
            {
                'operation': name,
                'cause': next(
                    (r.cause for r in by_operation[name] if r.action == 'isolate'),
                    by_operation[name][-1].cause,
                ),
                'latest_status': by_operation[name][-1].status,
                'history': [
                    {'seq': r.seq, 'action': r.action, 'status': r.status, 'cause': r.cause}
                    for r in by_operation[name]
                ],
            }
            for name in order
        ]

    def _release_decisions(
        self, account: str, source: str, attempt: str, responses: list[ResponseView]
    ) -> tuple[list[dict], list[str]]:
        decisions = []
        blockers: list[str] = []
        # 处置生效前管理端已收到的独立撤销，旧的解除决定不能恢复这些会话
        independent_revocations = [
            r for r in responses
            if r.action == 'isolate' and r.status == 'applied' and r.cause == 'independent-admin'
        ]
        for response in responses:
            if response.action != 'release':
                continue
            reasons: list[str] = []
            same_lineage = any(
                r.operation == response.operation
                and r.action == 'isolate'
                and r.status == 'applied'
                for r in responses
                if r.seq < response.seq
            )
            prior_revocation = [
                r for r in independent_revocations if r.seq < response.seq
            ]
            if response.status != 'applied':
                reasons.append('partially-observed')
            if prior_revocation:
                reasons.append('independent-revocation')
            if not same_lineage:
                reasons.append('outside-mitigation-scope')
            identity_holds = self._identity_holds(account, source, attempt)
            if not identity_holds:
                reasons.append('identity-not-satisfied')
            effective = not reasons
            decision = {
                'operation': response.operation,
                'seq': response.seq,
                'cause': response.cause,
                'status': response.status,
                'effective': effective,
                'reasons': reasons,
            }
            decisions.append(decision)
            if prior_revocation and 'independent-revocation' not in blockers:
                blockers.append('independent-revocation')
            if response.status == 'partially_observed' and 'partially-observed' not in blockers:
                blockers.append('partially-observed')
        return decisions, blockers

    def _identity_holds(self, account: str, source: str, attempt: str) -> bool:
        """解除只恢复仍满足身份条件的会话。"""
        item = self._state.attempts.get((account, source, attempt))
        if item is None:
            return False
        review = self._review_attempt(item, self.policy_for(account),
                                      bool(self.active_account_reports(account)), None, item.observed_at)
        return review['risk'] == 'cleared'

    # ------------------------------------------------------------- 用户告知

    def notices(self, account: str | None = None) -> list[dict]:
        """真正受影响的用户通过可用入口获知何时需要重新验证。"""
        restricted = [
            item for item in self.mitigations()
            if item['restricted'] and (account is None or item['account'] == account)
        ]
        grouped: dict[str, list[dict]] = {}
        for item in restricted:
            grouped.setdefault(item['account'], []).append(item['target'])
        notices = []
        for acct in sorted(grouped):
            policy = self.policy_for(acct)
            notices.append({
                'account': acct,
                'targets': grouped[acct],
                'retained': list(policy.retained) if policy else [],
                'reauthenticate': True,
            })
        return notices


def build_payload(desk: Desk, account: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {'accounts': desk.review(account)['accounts']}
    if account is None:
        payload['mitigations'] = desk.mitigations()
        payload['notices'] = desk.notices()
    else:
        payload['mitigations'] = [
            item for item in desk.mitigations() if item['account'] == account
        ]
        payload['notices'] = desk.notices(account)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description='多因素认证异常检测与缓解台')
    parser.add_argument('account', nargs='?', help='仅查看该账号')
    parser.add_argument('--through', type=int, help='仅按截至此到达序号的观测判断')
    args = parser.parse_args()
    desk = Desk()
    if args.through is not None:
        desk.ingest_through(args.through)
    else:
        desk.ingest_all()
    print(json.dumps(build_payload(desk, args.account), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
