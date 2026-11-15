#!/usr/bin/env python3
"""检查交付规格、对应关系和合成数学期望；不测试未来系统，不访问网络。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML_STEPS = {
    '1', '1.1', '1.2', '2', '3', '3.1', '4', '4.1', '4.2',
    '5', '5.1', '5.2', '5.3', '5.4', '6', '6.1', '6.2', '6.3',
    '7', '8', '8.1', '8.2', '8.3', '8.4', '9', '9.1', '10', '11', '12', '13',
}
LAYERS = {'offline', 'lab', 'package', 'parity', 'model', 'external', 'usability'}


def load(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def check_data(req_doc, case_doc, fixture):
    issues = []
    reqs = req_doc.get('requirements', [])
    cases = case_doc.get('cases', [])
    rids = [r['id'] for r in reqs]
    cids = [c['id'] for c in cases]
    expected = ({f'FLOW-{i:02d}' for i in range(1, 31)}
                | {f'AI-{i:02d}' for i in range(1, 7)}
                | {f'MET-{i:02d}' for i in range(1, 5)}
                | {f'SYS-{i:02d}' for i in range(1, 13)}
                | {f'UX-{i:02d}' for i in range(1, 9)})
    if set(rids) != expected or len(rids) != len(expected):
        issues.append('需求必须完整且不重复地覆盖 FLOW 30、AI 6、MET 4、SYS 12、UX 8。')
    if len(cids) != len(set(cids)):
        issues.append('验收场景 ID 重复。')
    if {r.get('html_step') for r in reqs if r['id'].startswith('FLOW-')} != HTML_STEPS:
        issues.append('HTML 原始步骤对应不完整。')
    by_req = {rid: set() for rid in rids}
    for c in cases:
        if c.get('layer') not in LAYERS:
            issues.append(f"{c['id']} 验收层无效。")
        for field in ('title', 'preconditions', 'steps', 'expected', 'required_evidence'):
            if not c.get(field):
                issues.append(f"{c['id']} 缺少 {field}。")
        for rid in c.get('requirements', []):
            if rid not in by_req:
                issues.append(f"{c['id']} 引用未知需求 {rid}。")
            else:
                by_req[rid].add(c['id'])
    for r in reqs:
        if not all(r.get(k) for k in ('module', 'contract', 'milestone', 'invariant')):
            issues.append(f"{r['id']} 缺少模块、契约、批次或规则。")
        if set(r.get('test_ids', [])) != by_req[r['id']]:
            issues.append(f"{r['id']} 双向测试对应不一致。")
        if len(by_req[r['id']]) < 2:
            issues.append(f"{r['id']} 少于两项验收。仍需人工检查其正反路径语义。")
    adoption = fixture['adoption_metrics']
    rows = adoption['dispositions']
    origins = set(adoption['origin_ids'])
    accepted = {r['origin'] for r in rows if r['decision'] == 'accepted'}
    changed = {r['origin'] for r in rows if r['human_semantic_change']}
    actual = {'adoption_numerator': len(accepted), 'adoption_denominator': len(origins),
              'adoption_value': len(accepted) / len(origins),
              'modification_numerator': len(changed), 'modification_denominator': len(origins),
              'modification_value': len(changed) / len(origins)}
    if actual != adoption['expected']:
        issues.append('采纳/人工修改夹具的期望与原始集合不一致。')
    defects = fixture['defect_metrics']
    valid = [d for d in defects['defects'] if d.get('valid') and not d.get('duplicate_of')]
    online = [d for d in valid if d.get('found_in') == 'production']
    confirmed = [d for d in valid if d.get('confirmed_escape')]
    de = defects['expected']
    if (len(online), len(valid), len(confirmed), f'{len(online) / len(valid) * 100:.2f}') != (
            de['online_numerator'], de['valid_unique_denominator'],
            de['confirmed_escape_count'], de['online_display_percent']):
        issues.append('缺陷夹具去重、分母或显示期望错误。')
    times = fixture['time_metrics']
    start, end = (datetime.fromisoformat(times[k]) for k in ('entry_accepted_at', 'exit_decided_at'))
    intervals = sorted((max(start, datetime.fromisoformat(p['start'])),
                        min(end, datetime.fromisoformat(p['end']))) for p in times['pauses'])
    merged = []
    for a, b in intervals:
        if a >= b:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    elapsed = (end - start).total_seconds() / 3600
    paused = sum((b - a).total_seconds() / 3600 for a, b in merged)
    effort = sum(x['hours'] for x in {x['id']: x for x in times['effort_entries']}.values())
    actual = {'elapsed_hours': elapsed, 'pause_union_hours': paused, 'active_hours': elapsed - paused,
              'person_hours': effort, 'person_days': effort / times['workday_hours']}
    if actual != times['expected']:
        issues.append('时间夹具的区间并集或投入期望错误。')
    usability = fixture.get('usability', {})
    tasks = usability.get('task_definitions', [])
    if {t.get('id') for t in tasks} != {f'N{i}' for i in range(1, 6)} or len(tasks) != 5:
        issues.append('新人任务必须完整且不重复地包含 N1 至 N5。')
    if not all(t.get('start_state') and t.get('goal') for t in tasks):
        issues.append('新人任务缺少初始条件或完成目标。')
    gate = usability.get('proposed_acceptance', {})
    if (gate.get('required_participants'), gate.get('required_tasks_per_participant'),
            gate.get('required_independent_completions')) != (5, 5, 25):
        issues.append('拟定新人门槛与 5 人各 5 项任务不一致。')
    for key in ('max_implementation_help_events', 'max_manual_json_or_source_edits',
                'max_unsafe_or_duplicate_side_effects', 'max_repeated_question_same_valid_scope'):
        if gate.get(key) != 0:
            issues.append(f'新人规格缺少明确的零次约束：{key}。')
    trial = usability.get('sample_incomplete_trial', {})
    should_pass = (trial.get('independent_completions') == gate.get('required_independent_completions')
                   and trial.get('implementation_help_events') == 0)
    if trial.get('expected_pass') is not should_pass:
        issues.append('不完整新人实验不能标为通过。')
    if len([c for c in cases if c.get('layer') == 'usability']) < 1:
        issues.append('缺少真人可用性场景。')
    return issues


def check_files(req_doc, case_doc, verify_manifest=True):
    issues = []
    design = (ROOT / '01-rebuild-design.md').read_text(encoding='utf-8')
    test = (ROOT / '02-validation-plan.md').read_text(encoding='utf-8')
    for r in req_doc['requirements']:
        if r['id'] not in design:
            issues.append(f"设计缺少 {r['id']}。")
    for c in case_doc['cases']:
        if c['id'] not in design or c['id'] not in test:
            issues.append(f"两份文档未完整引用 {c['id']}。")
    for p in ROOT.glob('*.md'):
        content = p.read_text(encoding='utf-8')
        if content.count('```') % 2:
            issues.append(f'{p.name} 代码块未闭合。')
        if re.search(r'__[A-Z][A-Z_]+__', content):
            issues.append(f'{p.name} 仍有模板占位符。')
        for link in re.findall(r'\]\(([^)]+)\)', content):
            if '://' not in link and not link.startswith('#'):
                if not (p.parent / link.split('#', 1)[0]).is_file():
                    issues.append(f'{p.name} 链接不存在：{link}')
    if verify_manifest:
        manifest = load('handoff-manifest.json')
        listed = {item['path'] for item in manifest['files']}
        actual = {p.name for p in ROOT.iterdir() if p.is_file() and p.name != 'handoff-manifest.json'}
        if listed != actual:
            issues.append('交付目录与摘要清单不一致，可能缺文件或残留旧版。')
        for item in manifest['files']:
            if Path(item['path']).name != item['path']:
                issues.append('交付摘要只能引用本目录文件。')
                continue
            p = ROOT / item['path']
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != item['sha256']:
                issues.append(f"交付文件摘要不匹配：{item['path']}")
    return issues


def negative_selfcheck(req_doc, case_doc, fixture):
    probes = []
    r = copy.deepcopy(req_doc)
    r['requirements'].pop()
    probes.append(('删除需求', r, case_doc, fixture))
    c = copy.deepcopy(case_doc)
    c['cases'][0]['requirements'].append('UNKNOWN-999')
    probes.append(('未知需求引用', req_doc, c, fixture))
    r = copy.deepcopy(req_doc)
    r['requirements'][0]['test_ids'] = []
    probes.append(('破坏双向对应', r, case_doc, fixture))
    c = copy.deepcopy(case_doc)
    c['cases'][0]['layer'] = 'always_pass'
    probes.append(('非法测试层', req_doc, c, fixture))
    f = copy.deepcopy(fixture)
    f['adoption_metrics']['expected']['adoption_numerator'] = 999
    probes.append(('错误采纳期望', req_doc, case_doc, f))
    f = copy.deepcopy(fixture)
    f['time_metrics']['expected']['active_hours'] = 21
    probes.append(('重复扣除暂停', req_doc, case_doc, f))
    f = copy.deepcopy(fixture)
    f['usability']['task_definitions'].pop()
    probes.append(('删除新人任务', req_doc, case_doc, f))
    f = copy.deepcopy(fixture)
    f['usability']['sample_incomplete_trial']['expected_pass'] = True
    probes.append(('把新人未完成样本标通过', req_doc, case_doc, f))
    issues = []
    for label, r, c, f in probes:
        if not check_data(r, c, f):
            issues.append(f'负向自检未发现：{label}')
    return issues, len(probes)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--negative-selfcheck', action='store_true')
    ap.add_argument('--source-root', type=Path, help='可选：核对原仓库关键源文件摘要，不修改来源')
    args = ap.parse_args()
    req_doc, case_doc, fixture = (load(n) for n in ('requirements.json', 'acceptance-cases.json', 'lab-fixtures.json'))
    issues = check_data(req_doc, case_doc, fixture) + check_files(req_doc, case_doc)
    if args.source_root:
        for item in load('source-baseline.json')['repository_files']:
            p = args.source_root / item['path']
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != item['sha256']:
                issues.append(f"源文件不同或不存在：{item['path']}")
    if args.negative_selfcheck:
        problems, count = negative_selfcheck(req_doc, case_doc, fixture)
        issues += problems
        if not problems:
            print(f'文档检查器的 {count} 项负向自检通过。')
    if issues:
        for issue in issues:
            print(f'失败：{issue}')
        return 1
    print(f"交付规格检查通过：{len(req_doc['requirements'])} 项需求，{len(case_doc['cases'])} 个验收场景。")
    print('此结果仅证明交付规格与合成期望自洽；未来系统尚未实现，产品验收未执行。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
