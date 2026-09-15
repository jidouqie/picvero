import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


build = module('build', 'tools/build.py')
install = module('install', 'tools/install.py')


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        for p in ['project.json', 'LICENSE']:
            shutil.copyfile(ROOT / p, self.root / p)
        for p in ['skill_sources', 'shared']:
            shutil.copytree(ROOT / p, self.root / p, ignore=shutil.ignore_patterns('__pycache__'))

    def tearDown(self): self.temp.cleanup()

    def test_single_skill_is_self_contained_after_extracting_zip(self):
        result = build.build(self.root)
        self.assertEqual(len(result['skills']), 8)
        dest = self.root / 'single'
        with zipfile.ZipFile(result['archive']) as z:
            for info in z.infolist():
                if info.filename.startswith('picvero-image/'):
                    z.extract(info, dest)
        skill = dest / 'picvero-image'
        process = subprocess.run([sys.executable, str(skill / 'scripts/runtime.py'), '--help'], capture_output=True)
        self.assertEqual(process.returncode, 0)
        for p in skill.rglob('*.md'):
            for ref in re.findall(r'\]\(([^)]+)\)', p.read_text()):
                if not ref.startswith(('https://', 'http://', '#')):
                    self.assertTrue((p.parent / ref).is_file(), (p, ref))

    def test_prefix_change_rebuilds_every_skill_and_prompt(self):
        build.build(self.root)
        p = self.root / 'project.json'; data = json.loads(p.read_text()); data.update(prefix='futurebrand', name='后续品牌')
        p.write_text(json.dumps(data))
        result = build.build(self.root)
        self.assertEqual(set(result['skills']), {'futurebrand', 'futurebrand-image', 'futurebrand-suite',
                         'futurebrand-localize', 'futurebrand-brand', 'futurebrand-batch',
                         'futurebrand-review', 'futurebrand-listing'})
        self.assertFalse(any(p.name == 'picvero' or p.name.startswith('picvero-') for p in (self.root / 'skills').iterdir()))
        for name in result['skills']:
            metadata = (self.root / 'skills' / name / 'agents/openai.yaml').read_text()
            self.assertEqual(re.findall(r'\$([a-z0-9-]+)', metadata), [name])
            display = re.search(r'^  display_name: (.+)$', metadata, re.MULTILINE)
            capability = name.removeprefix('futurebrand').lstrip('-')
            self.assertEqual(json.loads(display.group(1)), '后续品牌' + (' ' + capability.title() if capability else ''))
            frontmatter = (self.root / 'skills' / name / 'SKILL.md').read_text().split('---', 2)[1]
            source = json.loads((self.root / 'skill_sources' / ((capability or 'main') + '.json')).read_text())
            short = re.search(r'^  short-description: (.+)$', frontmatter, re.MULTILINE)
            self.assertEqual(json.loads(short.group(1)), source['title'])
        self.assertEqual(result['entry_skill'], 'futurebrand')
        self.assertEqual(Path(result['entry_archive']).name, 'futurebrand-' + data['version'] + '.zip')
        with zipfile.ZipFile(result['entry_archive']) as z:
            self.assertTrue(all(n.startswith('futurebrand/') for n in z.namelist()))

    def test_entry_archive_prepares_run_without_other_skills(self):
        result = build.build(self.root)
        dest = self.root / 'entry-only'
        with zipfile.ZipFile(result['entry_archive']) as z:
            z.extractall(dest)
        skill = dest / result['entry_skill']
        self.assertEqual([p.name for p in dest.iterdir()], [result['entry_skill']])
        for p in skill.rglob('*.md'):
            for ref in re.findall(r'\]\(([^)]+)\)', p.read_text()):
                if not ref.startswith(('https://', 'http://', '#')):
                    self.assertTrue((p.parent / ref).is_file(), (p, ref))
        original = self.root / 'product.png'
        shutil.copyfile(ROOT / 'examples/synthetic-product.png', original)
        request = self.root / 'request.json'
        request.write_text(json.dumps({'schema_version': 1, 'products': [{
            'sku': 'ENTRY01', 'name': '入口独立安装测试夹具',
            'references': [{'id': 'original', 'role': 'product', 'path': str(original)}],
            'facts': [], 'invariants': [],
            'shots': [{'id': 'main', 'kind': 'packshot', 'goal': '测试任务准备，不代表模型推断或生图',
                       'text': [], 'fact_ids': [], 'aspect': '1:1', 'max_renders': 3}]}]}))
        runtime = skill / 'scripts/runtime.py'
        process = subprocess.run([sys.executable, str(runtime), 'init', '--request', str(request),
                                  '--runs', str(self.root / 'runs')], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        run = Path(json.loads(process.stdout)['result']['run'])
        self.assertEqual((run / 'inputs/ENTRY01/original.png').read_bytes(), original.read_bytes())
        process = subprocess.run([sys.executable, str(runtime), 'status', '--run', str(run)],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        job = json.loads(process.stdout)['result']['jobs'][0]
        self.assertEqual((job['sku'], job['next_action'], job['renders']), ('ENTRY01', 'generate', 0))

    def test_generated_manual_changes_are_not_overwritten(self):
        build.build(self.root)
        path = self.root / 'skills/picvero-image/SKILL.md'; path.write_text(path.read_text() + '\nuser edit\n')
        with self.assertRaises(ValueError): build.build(self.root)
        self.assertIn('user edit', path.read_text())

    def test_python_runtime_cache_does_not_block_rebuild(self):
        build.build(self.root)
        cache = self.root / 'skills/picvero-review/scripts/__pycache__'
        cache.mkdir(); (cache / 'runtime.cpython-314.pyc').write_bytes(b'generated bytecode cache')
        result = build.build(self.root)
        self.assertEqual(len(result['skills']), 8)
        with zipfile.ZipFile(result['archive']) as z:
            self.assertFalse(any('__pycache__' in name for name in z.namelist()))

    def test_install_collision_is_preserved(self):
        build.build(self.root)
        dest = self.root / 'installed'; dest.mkdir(); (dest / 'picvero-image').mkdir(); (dest / 'picvero-image/keep.txt').write_text('user')
        with self.assertRaises(ValueError): install.install(self.root / 'skills', dest)
        self.assertEqual((dest / 'picvero-image/keep.txt').read_text(), 'user')
        self.assertFalse((dest / 'picvero').exists())

    def test_project_symlink_activation_is_repeatable(self):
        build.build(self.root)
        a = install.install(self.root / 'skills', self.root / '.agents/skills', links=True)
        b = install.install(self.root / 'skills', self.root / '.agents/skills', links=True)
        self.assertEqual(a, b)
        self.assertEqual(len(a['installed']), 8)


if __name__ == '__main__': unittest.main()
