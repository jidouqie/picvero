import copy
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime', ROOT / 'shared/scripts/runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def png(path, color=(20, 100, 120), width=12, height=10):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\0' + bytes(color) * width for _ in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / 'source.png'
        self.image = self.root / 'candidate.png'
        png(self.original)
        png(self.image, color=(25, 105, 125))
        self.prompt = self.root / 'prompt.txt'
        self.prompt.write_text('保留商品，只改背景。')
        self.request = {
            'schema_version': 1, 'label': '样本 <script>alert(1)</script>', 'brand': {},
            'products': [{'sku': 'A001', 'name': '收纳 <img onerror=x>',
                          'references': [{'id': 'front', 'role': 'product', 'path': 'source.png'}],
                          'facts': [{'id': 'shape', 'value': '矩形', 'status': 'observed', 'source': {'kind': 'image', 'ref': 'front'}}],
                          'invariants': ['形状不变'],
                          'shots': [{'id': 'main', 'kind': 'packshot', 'goal': '基础图', 'text': [], 'fact_ids': ['shape'], 'aspect': '1:1', 'max_renders': 3}]}]}

    def tearDown(self):
        self.temp.cleanup()

    def make_run(self, data=None):
        p = self.root / 'request.json'
        p.write_text(json.dumps(data or self.request, ensure_ascii=False))
        return runtime.init_run(p, self.root / 'runs')['run']

    def record(self, run):
        return runtime.record(run, 'A001', 'main', self.image, self.prompt)

    def report(self, run, status='pass'):
        report = runtime.review_template(run, 'A001', 'main')
        for key, check in report['checks'].items():
            check.update(status=status, evidence='测试模拟的 ' + key + ' 检查证据；不代表真实商品验证。')
        return report

    def submit(self, run, data):
        p = self.root / 'review.json'
        p.write_text(json.dumps(data, ensure_ascii=False))
        return runtime.review(run, 'A001', 'main', p)

    def test_png_dimensions_and_corruption(self):
        info = runtime.inspect_image(self.original)
        self.assertEqual((info['width'], info['height']), (12, 10))
        self.assertFalse(info['has_alpha_channel'])
        self.original.write_bytes(self.original.read_bytes()[:-4])
        with self.assertRaises(runtime.WorkflowError): runtime.inspect_image(self.original)

    def test_relative_inputs_unique_runs_and_immutable_snapshot(self):
        run1, run2 = self.make_run(), self.make_run()
        self.assertNotEqual(run1, run2)
        _, plan, _ = runtime.load_run(run1)
        ref = plan['products'][0]['references'][0]
        self.assertEqual(runtime.file_hash(Path(run1) / ref['snapshot']), runtime.file_hash(self.original))

    def test_unknown_fact_cannot_be_used_in_shot(self):
        self.request['products'][0]['facts'][0]['status'] = 'unknown'
        with self.assertRaises(runtime.WorkflowError): self.make_run()

    def test_style_is_not_a_product_reference(self):
        self.request['products'][0]['references'][0]['role'] = 'style'
        with self.assertRaises(runtime.WorkflowError): self.make_run()

    def test_style_reference_cannot_supply_product_claim(self):
        self.request['products'][0]['references'].append({'id': 'style', 'role': 'style', 'path': 'source.png'})
        self.request['products'][0]['facts'][0]['source']['ref'] = 'style'
        with self.assertRaises(runtime.WorkflowError): self.make_run()

    def test_duplicate_skus_rejected_before_creating_run(self):
        self.request['products'].append(copy.deepcopy(self.request['products'][0]))
        with self.assertRaises(runtime.WorkflowError): self.make_run()
        self.assertFalse((self.root / 'runs').exists())

    def test_path_traversal_identifier_rejected(self):
        self.request['products'][0]['sku'] = '../outside'
        with self.assertRaises(runtime.WorkflowError): self.make_run()

    def test_original_change_prevents_resume(self):
        run = self.make_run()
        png(self.original, color=(1, 2, 3))
        with self.assertRaises(runtime.WorkflowError): runtime.status_run(run)

    def test_moved_original_can_use_immutable_snapshot(self):
        run = self.make_run()
        self.original.unlink()
        self.assertEqual(runtime.status_run(run)['jobs'][0]['next_action'], 'generate')

    def test_plan_change_invalidates_state(self):
        run = self.make_run()
        p = Path(run) / 'plan.json'
        data = json.loads(p.read_text()); data['brand'] = {'background': 'new'}
        p.write_text(json.dumps(data))
        with self.assertRaises(runtime.WorkflowError): runtime.status_run(run)

    def test_text_file_cannot_be_recorded_as_image(self):
        run = self.make_run()
        with self.assertRaises(runtime.WorkflowError): runtime.record(run, 'A001', 'main', self.prompt, self.prompt)
        self.assertEqual(runtime.status_run(run)['jobs'][0]['renders'], 0)

    def test_repeat_record_is_idempotent_and_waits_for_review(self):
        run = self.make_run()
        self.record(run)
        self.assertTrue(self.record(run)['idempotent'])
        job = runtime.status_run(run)['jobs'][0]
        self.assertEqual((job['renders'], job['next_action']), (1, 'review'))

    def test_empty_review_template_rejected(self):
        run = self.make_run(); self.record(run)
        with self.assertRaises(runtime.WorkflowError): self.submit(run, runtime.review_template(run, 'A001', 'main'))

    def test_unknown_review_not_exported_as_approved(self):
        run = self.make_run(); self.record(run)
        self.assertEqual(self.submit(run, self.report(run, 'unknown'))['review_status'], 'inconclusive')
        with self.assertRaises(runtime.WorkflowError): runtime.export_run(run, self.root / 'delivery')
        export = runtime.export_run(run, self.root / 'drafts', drafts=True)
        self.assertTrue(export['drafts'])

    def test_missing_reference_in_review_is_rejected(self):
        run = self.make_run(); self.record(run)
        report = self.report(run); report['reference_sha256'] = {}
        with self.assertRaises(runtime.WorkflowError): self.submit(run, report)

    def test_old_review_cannot_review_new_candidate(self):
        run = self.make_run(); self.record(run)
        report = self.report(run)
        png(self.image, color=(90, 91, 92)); self.record(run)
        with self.assertRaises(runtime.WorkflowError): self.submit(run, report)

    def test_pass_is_not_user_selection(self):
        run = self.make_run(); self.record(run)
        result = self.submit(run, self.report(run))
        self.assertEqual((result['review_status'], result['selection']), ('reviewed_candidate', 'not_selected'))
        selected = runtime.select(run, 'A001', 'main', 'selected', '测试中的显式选择')
        self.assertEqual(selected['selection'], 'selected')

    def test_fail_does_not_pass_and_revisions_preserve_files(self):
        run = self.make_run(); first = self.record(run)['revision']
        report = self.report(run); report['checks']['identity']['status'] = 'fail'
        self.submit(run, report)
        self.assertEqual(runtime.status_run(run)['jobs'][0]['next_action'], 'revise')
        png(self.image, color=(80, 81, 82)); second = self.record(run)['revision']
        self.assertNotEqual(first['file'], second['file'])
        self.assertTrue((Path(run) / first['file']).exists())
        self.assertEqual(second['review_status'], 'not_reviewed')

    def test_changed_output_invalidates_previous_review(self):
        run = self.make_run(); first = self.record(run)['revision']; self.submit(run, self.report(run))
        png(Path(run) / first['file'], color=(99, 99, 99))
        with self.assertRaises(runtime.WorkflowError): runtime.status_run(run)
        with self.assertRaises(runtime.WorkflowError): runtime.export_run(run, self.root / 'delivery')

    def test_failure_persists_and_limit_does_not_silently_retry(self):
        self.request['products'][0]['shots'][0]['max_renders'] = 1
        run = self.make_run()
        runtime.log_failure(run, 'A001', 'main', 'failed', '模拟实际出图失败')
        self.assertEqual(runtime.status_run(run)['jobs'][0]['next_action'], 'needs_input')
        with self.assertRaises(runtime.WorkflowError): self.record(run)

    def test_default_one_attempt_does_not_block_remaining_shots(self):
        first_shot = self.request['products'][0]['shots'][0]
        first_shot.pop('max_renders')
        scene = copy.deepcopy(first_shot)
        scene.update(id='scene', kind='scene')
        self.request['products'][0]['shots'].append(scene)
        run = self.make_run()
        self.record(run)
        self.submit(run, self.report(run, 'fail'))
        jobs = runtime.status_run(run)['jobs']
        self.assertEqual([(j['max_renders'], j['next_action']) for j in jobs],
                         [(1, 'needs_input'), (1, 'generate')])
        png(self.image, color=(90, 91, 92))
        with self.assertRaises(runtime.WorkflowError): self.record(run)
        runtime.record(run, 'A001', 'scene', self.image, self.prompt)
        jobs = runtime.status_run(run)['jobs']
        self.assertEqual([(j['renders'], j['status']) for j in jobs],
                         [(1, 'needs_revision'), (1, 'not_reviewed')])

    def test_csv_draft_has_one_attempt_per_shot(self):
        csv_file = self.root / 'catalog.csv'
        csv_file.write_text('sku,name,image\nA001,product,source.png\n')
        imported = self.root / 'imported.json'
        runtime.import_csv(csv_file, imported)
        run = runtime.init_run(imported, self.root / 'runs')['run']
        jobs = runtime.status_run(run)['jobs']
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(job['max_renders'] == 1 for job in jobs))
        runtime.log_failure(run, 'A001', 'packshot', 'failed', '模拟第一次调用失败')
        with self.assertRaises(runtime.WorkflowError):
            runtime.record(run, 'A001', 'packshot', self.image, self.prompt)
        self.assertEqual(runtime.status_run(run)['jobs'][1]['next_action'], 'generate')

    def test_export_escapes_html_omits_private_source_paths_and_lists_pending(self):
        shot = copy.deepcopy(self.request['products'][0]['shots'][0]); shot['id'] = 'scene'
        self.request['products'][0]['shots'].append(shot)
        run = self.make_run(); self.record(run); self.submit(run, self.report(run))
        out = runtime.export_run(run, self.root / 'delivery')
        page = Path(out['preview']).read_text()
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        data = json.loads((Path(out['directory']) / 'manifest.json').read_text())
        self.assertEqual(data['omitted'][0]['shot'], 'scene')
        self.assertNotIn(str(self.root), json.dumps(data))
        with zipfile.ZipFile(out['archive']) as z:
            self.assertIn('index.html', z.namelist())
            self.assertFalse(any(n.startswith('/') or '..' in Path(n).parts for n in z.namelist()))
        with self.assertRaises(runtime.WorkflowError): runtime.export_run(run, self.root / 'delivery')

    def test_unknown_profile_rule_is_not_a_pass(self):
        p = self.root / 'profile.json'
        p.write_text(json.dumps({'name': '测试', 'scope': '项目', 'basis': '测试要求', 'checked_at': '2026-09-15', 'rules': {'min_width': 10, 'pure_white_background': True}}))
        self.assertEqual(runtime.check_profile(self.image, p)['verdict'], 'inconclusive')

    def test_export_preserves_dotted_version_name(self):
        run = self.make_run(); self.record(run); self.submit(run, self.report(run))
        out = runtime.export_run(run, self.root / 'delivery.v1.0')
        self.assertTrue(out['archive'].endswith('delivery.v1.0.zip'))

    def test_symlink_snapshot_outside_run_rejected(self):
        run = self.make_run(); _, plan, _ = runtime.load_run(run)
        ref = Path(run) / plan['products'][0]['references'][0]['snapshot']
        ref.unlink(); ref.symlink_to(self.original)
        with self.assertRaises(runtime.WorkflowError): runtime.status_run(run)

    def test_select_old_revision_exports_exact_chosen_file(self):
        run = self.make_run(); first = self.record(run)['revision']; self.submit(run, self.report(run))
        png(self.image, color=(70, 72, 74)); self.record(run); self.submit(run, self.report(run))
        runtime.select(run, 'A001', 'main', 'selected', '用户选用第一版', version=1)
        result = runtime.export_run(run, self.root / 'old-version')
        item = json.loads((Path(result['directory']) / 'manifest.json').read_text())['items'][0]
        self.assertEqual((item['version'], item['sha256']), (1, first['sha256']))
        self.assertEqual(runtime.status_run(run)['jobs'][0]['selected_version'], 1)

    def test_select_replaces_choice_but_not_history(self):
        run = self.make_run(); self.record(run); self.submit(run, self.report(run))
        runtime.select(run, 'A001', 'main', 'selected', '第一版')
        png(self.image, color=(44, 45, 46)); self.record(run); self.submit(run, self.report(run))
        runtime.select(run, 'A001', 'main', 'selected', '改选第二版', version=2)
        data = runtime.show_job(run, 'A001', 'main')
        self.assertEqual([r['selection'] for r in data['history']], ['not_selected', 'selected'])
        self.assertEqual(len(data['history']), 2)

    def test_selected_only_does_not_export_unselected_candidate(self):
        run = self.make_run(); self.record(run); self.submit(run, self.report(run))
        with self.assertRaises(runtime.WorkflowError): runtime.export_run(run, self.root / 'delivery', selected_only=True)

    def test_explicit_review_of_history_version(self):
        run = self.make_run(); self.record(run); old = self.report(run)
        png(self.image, color=(55, 56, 57)); self.record(run)
        p = self.root / 'old-review.json'; p.write_text(json.dumps(old))
        runtime.review(run, 'A001', 'main', p, version=1)
        data = runtime.show_job(run, 'A001', 'main')
        self.assertEqual([r['review_status'] for r in data['history']], ['reviewed_candidate', 'not_reviewed'])

    def test_review_does_not_erase_explicit_user_choice(self):
        run = self.make_run(); self.record(run)
        runtime.select(run, 'A001', 'main', 'selected', '用户明确暂用未审核图', allow_unreviewed=True)
        r = self.submit(run, self.report(run, 'unknown'))
        self.assertEqual(r['selection'], 'selected')
        self.assertEqual(r['review_status'], 'inconclusive')
        with self.assertRaises(runtime.WorkflowError): runtime.export_run(run, self.root / 'not-approved')

    def test_text_cannot_be_not_applicable_when_requested(self):
        self.request['products'][0]['shots'][0]['text'] = ['测试文案']
        run = self.make_run(); self.record(run); report = self.report(run)
        report['checks']['text']['status'] = 'not_applicable'
        with self.assertRaises(runtime.WorkflowError): self.submit(run, report)

    def test_interrupted_state_write_rolls_back_new_image(self):
        run = self.make_run()
        with patch.object(runtime, 'save_json', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError): self.record(run)
        self.assertFalse(list((Path(run) / 'images').rglob('*.png')))
        self.assertEqual(self.record(run)['revision']['version'], 1)

    def test_import_csv_preserves_quoted_fields_and_unknown_claims(self):
        csv_file = self.root / 'supplier.csv'
        csv_file.write_text('\ufeffsku,name,image,description\nA001,"收纳盒, 蓝色",source.png,"容量未知, 待确认"\n', encoding='utf-8')
        request = self.root / 'imported.json'
        runtime.import_csv(csv_file, request, 'packshot,scene')
        data = json.loads(request.read_text())
        self.assertEqual(data['products'][0]['name'], '收纳盒, 蓝色')
        self.assertEqual(data['products'][0]['facts'][0]['status'], 'unknown')
        self.assertEqual(len(data['products'][0]['shots']), 2)
        self.assertTrue(runtime.init_run(request, self.root / 'runs')['run'])

    def test_import_csv_rejects_duplicates_without_output(self):
        csv_file = self.root / 'duplicate.csv'
        csv_file.write_text('sku,name,image\nA001,one,source.png\nA001,two,source.png\n')
        request = self.root / 'bad.json'
        with self.assertRaises(runtime.WorkflowError): runtime.import_csv(csv_file, request)
        self.assertFalse(request.exists())

    def test_import_csv_does_not_fetch_urls(self):
        csv_file = self.root / 'remote.csv'
        csv_file.write_text('sku,name,image\nA001,one,https://example.com/x.png\n')
        with self.assertRaises(runtime.WorkflowError): runtime.import_csv(csv_file, self.root / 'bad.json')

    def test_show_supplies_verified_snapshot_and_current_versions(self):
        run = self.make_run(); self.record(run)
        data = runtime.show_job(run, 'A001', 'main')
        self.assertEqual(data['references'][0]['role'], 'product')
        self.assertTrue(Path(data['references'][0]['path']).is_file())
        self.assertEqual(data['history'][0]['review_status'], 'not_reviewed')


if __name__ == '__main__': unittest.main()
