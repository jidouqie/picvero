#!/usr/bin/env python3
"""Build eight self-contained skills and a distributable ZIP from project.json."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_runtime_cache(path, root):
    return path.suffix == '.pyc' and '__pycache__' in path.relative_to(root).parts


def build(root, output=None):
    root = Path(root).resolve()
    config = json.loads((root / 'project.json').read_text())
    prefix = config['prefix']
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', prefix) or len(prefix) > 40:
        raise ValueError('prefix 应为简短的小写英文与短横线名称。')
    entrypoint = config.get('entrypoint', 'main')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', entrypoint) or not (root / 'skill_sources' / (entrypoint + '.json')).is_file():
        raise ValueError('entrypoint 必须对应现有技能源文件。')
    entry_name = prefix
    output = Path(output).resolve() if output else root / 'skills'
    if output.exists():
        marker = output / '.generated.json'
        if not marker.is_file():
            raise ValueError('输出目录不是本构建器生成的目录，不覆盖。')
        old = json.loads(marker.read_text())
        actual = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file() and p != marker and not is_runtime_cache(p, output)}
        if actual != old['files']:
            raise ValueError('生成目录内有手工改动；请保留改动并修改 skill_sources/shared 后再生成。')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.skills-build-', dir=output.parent))
    try:
        for source in sorted((root / 'skill_sources').glob('*.json')):
            spec = json.loads(source.read_text())
            name = entry_name if spec['slug'] == entrypoint else prefix + '-' + spec['slug']
            if len(name) > 64:
                raise ValueError('技能名超过 64 字符。')
            dest = staging / name
            dest.mkdir()
            transform = lambda text: text.replace('{{ENTRY}}', entry_name).replace('{{PREFIX}}', prefix).replace('{{NAME}}', config['name'])
            md = ('---\nname: ' + name + '\ndescription: ' + json.dumps(spec['description'], ensure_ascii=False)
                  + '\nmetadata:\n  short-description: ' + json.dumps(spec['title'], ensure_ascii=False)
                  + '\n---\n\n' + transform(spec['body']) + '\n')
            (dest / 'SKILL.md').write_text(md, encoding='utf-8')
            (dest / 'agents').mkdir()
            display_name = config['name'] if spec['slug'] == entrypoint else config['name'] + ' ' + spec['slug'].replace('-', ' ').title()
            fields = {'display_name': display_name,
                      'short_description': spec['short_description'],
                      'default_prompt': transform(spec['default_prompt'])}
            if not 25 <= len(fields['short_description']) <= 64:
                raise ValueError('short_description 长度应为 25–64 字符：' + name)
            yaml = 'interface:\n' + ''.join('  ' + k + ': ' + json.dumps(v, ensure_ascii=False) + '\n' for k, v in fields.items())
            yaml += 'policy:\n  allow_implicit_invocation: true\n'
            (dest / 'agents/openai.yaml').write_text(yaml, encoding='utf-8')
            for sub in ['scripts', 'references']:
                shutil.copytree(root / 'shared' / sub, dest / sub, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copyfile(root / 'LICENSE', dest / 'LICENSE')
        content = {str(p.relative_to(staging)): sha(p) for p in staging.rglob('*') if p.is_file()}
        (staging / '.generated.json').write_text(json.dumps({'version': config['version'], 'prefix': prefix, 'files': content}, ensure_ascii=False, indent=2) + '\n')
        backup = None
        if output.exists():
            backup = output.with_name(output.name + '.previous')
            if backup.exists():
                raise ValueError('上次构建备份仍在，请先核对：' + str(backup))
            os.replace(output, backup)
        try:
            os.replace(staging, output)
        except Exception:
            if backup:
                os.replace(backup, output)
            raise
        if backup:
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    dist = root / 'dist'
    dist.mkdir(exist_ok=True)
    archive = dist / f'{prefix}-skills-{config["version"]}.zip'
    entry_archive = dist / f'{entry_name}-{config["version"]}.zip'
    for target, source in [(archive, output), (entry_archive, output / entry_name)]:
        temporary = target.with_suffix('.zip.tmp')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(source.rglob('*')):
                if p.is_file() and p.name != '.generated.json' and not is_runtime_cache(p, output):
                    z.write(p, p.relative_to(output))
        os.replace(temporary, target)
    return {'skills': [p.name for p in output.iterdir() if p.is_dir()], 'archive': str(archive),
            'entry_skill': entry_name, 'entry_archive': str(entry_archive), 'prefix': prefix}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output')
    args = parser.parse_args()
    print(json.dumps(build(args.root, args.output), ensure_ascii=False, indent=2))
