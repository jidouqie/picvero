#!/usr/bin/env python3
"""Replay bookkeeping for frozen synthetic fixtures; never calls an image model.

Recorded reviews are only valid for the exact hashes in provenance.json.
"""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime', ROOT / 'shared/scripts/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def replay(out):
    out = Path(out).expanduser().resolve()
    if out.exists():
        raise ValueError('回放目录已存在，请使用新目录。')
    examples = ROOT / 'examples'
    provenance = runtime.read_json(examples / 'provenance.json')
    for name, expected in provenance['files'].items():
        if runtime.file_hash(examples / name) != expected:
            raise ValueError('样本已变化，不能套用已记录的视觉判断：' + name)
    prompts = runtime.read_json(examples / 'native-prompts.json')
    checks = runtime.read_json(examples / 'recorded-visual-checks.json')
    out.mkdir(parents=True)
    run = runtime.init_run(examples / 'request.json', out / 'runs')['run']
    events = []
    for shot, key, name in [('main', 'main', 'native-packshot.png'),
                             ('scene', 'scene_v1', 'native-scene-v1.png'),
                             ('scene', 'scene_v2', 'native-scene-v2.png')]:
        prompt_file = out / (key + '-prompt.txt')
        prompt_file.write_text(prompts[key], encoding='utf-8')
        result = runtime.record(run, 'HY17', shot, examples / name, prompt_file)
        report = runtime.review_template(run, 'HY17', shot)
        report['reviewer'] = '固定虚构样本的已记录 Codex 视觉检查；回放不重新评图'
        report['checks'] = checks[key]
        report_file = out / (key + '-review.json')
        runtime.save_json(report_file, report)
        reviewed = runtime.review(run, 'HY17', shot, report_file)
        expected = 'needs_revision' if key == 'scene_v1' else 'reviewed_candidate'
        assert reviewed['review_status'] == expected
        assert reviewed['selection'] == 'not_selected'
        events.append({'shot': shot, 'source': name, 'version': result['revision']['version'], 'review_status': reviewed['review_status']})
    delivery = runtime.export_run(run, out / 'delivery')
    result = {'kind': 'frozen-fixture bookkeeping replay', 'real_merchant_validation': False,
              'new_generation_calls': 0, 'recorded_checks_reused_only_after_hash_match': True,
              'events': events, 'status': runtime.status_run(run), 'delivery': delivery}
    runtime.save_json(out / 'replay-report.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', required=True)
    args = p.parse_args()
    print(json.dumps(replay(args.out), ensure_ascii=False, indent=2))
