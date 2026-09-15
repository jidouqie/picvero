#!/usr/bin/env python3
"""Install generated skills to an explicit local directory; no network or config edits."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil


def install(source, dest, links=False):
    source, dest = Path(source).resolve(), Path(dest).expanduser().resolve()
    if source == dest or dest.is_relative_to(source):
        raise ValueError('安装目标不能位于技能源目录内。')
    skills = [p for p in sorted(source.iterdir()) if p.is_dir() and (p / 'SKILL.md').is_file()]
    if not skills:
        raise ValueError('没有找到可安装技能。')
    for p in skills:
        target = dest / p.name
        if target.is_symlink() and target.resolve() == p and links:
            continue
        if target.exists() or target.is_symlink():
            raise ValueError('已存在同名技能，不覆盖：' + str(target))
    dest.mkdir(parents=True, exist_ok=True)
    installed = []
    for p in skills:
        target = dest / p.name
        if target.is_symlink() and target.resolve() == p:
            installed.append(str(target))
            continue
        if links:
            target.symlink_to(os.path.relpath(p, dest), target_is_directory=True)
        else:
            shutil.copytree(p, target)
        installed.append(str(target))
    return {'installed': installed, 'mode': 'symlinks' if links else 'self-contained copies'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', default=str(Path(__file__).resolve().parents[1] / 'skills'))
    p.add_argument('--dest', required=True)
    p.add_argument('--link', action='store_true', help='Use relative symlinks for repository-scoped activation')
    a = p.parse_args()
    print(json.dumps(install(a.source, a.dest, a.link), ensure_ascii=False, indent=2))
