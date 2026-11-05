#!/usr/bin/env python3
"""Create and inspect a new, private Personal OS using only the Python stdlib."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / 'workspace'
FIELDS = {'workspace_name', 'owner_label', 'agent_name', 'language',
          'address_form', 'timezone', 'code_root'}
MARKER = re.compile(r'\{\{[A-Z_]+\}\}')
LINK = re.compile(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)')


def fail(message):
    raise ValueError(message)


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        fail('use an absolute path or ~, not a relative path')
    return path.resolve()


def template_files():
    files = []
    for path in sorted(SOURCE.rglob('*')):
        if path.is_symlink():
            fail('workspace source must not contain symlinks')
        if path.is_file():
            files.append(path)
    if not files:
        fail('workspace source is missing')
    return files


def load_profile(path):
    profile = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(profile, dict) or set(profile) != FIELDS:
        fail('profile must contain exactly the fields from profile.example.json')
    for name, value in profile.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 240:
            fail('profile values must be nonempty strings of at most 240 characters: '+name)
        if any(ord(c) < 32 for c in value) or any(c in value for c in ('`', '{{', '}}')):
            fail('profile values must be single-line plain text: '+name)
        profile[name] = value.strip()
    try:
        zone = dt.timezone.utc if profile['timezone'] == 'UTC' else ZoneInfo(profile['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        fail('timezone is unavailable; use a valid IANA name with local timezone data, or UTC')
    code_root = absolute_path(profile['code_root'])
    profile['code_root'] = str(code_root)
    return profile, zone, code_root


def source_digest(files):
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(SOURCE).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def init(args):
    profile, zone, code_root = load_profile(args.profile)
    root = absolute_path(args.root)
    # Reject even an empty pre-existing directory and dangling final symlinks.
    if Path(args.root).expanduser().is_symlink() or root.exists():
        fail('target already exists; choose a new directory (nothing was changed)')
    if overlaps(root, REPO) or overlaps(root, code_root):
        fail('OS root must not overlap the code root or starter checkout')
    if not root.parent.is_dir():
        fail('target parent must already be an existing trusted directory')
    files = template_files()
    version = (REPO / 'VERSION').read_text(encoding='utf-8').strip()
    created = dt.datetime.now(zone).date().isoformat()
    replacements = {key.upper(): value for key, value in profile.items()}
    replacements.update(OS_ROOT=str(root), CREATED=created, TEMPLATE_VERSION=version)
    rendered = {}
    for path in files:
        body = path.read_text(encoding='utf-8')
        def replace(match):
            name = match.group()[2:-2]
            if name not in replacements:
                fail('unknown workspace placeholder: '+name)
            return replacements[name]
        body = MARKER.sub(replace, body)
        if '{{' in body or '}}' in body:
            fail('unresolved workspace placeholder in '+path.relative_to(SOURCE).as_posix())
        rendered[path.relative_to(SOURCE)] = body
    manifest = {'schema_version': 1, 'template_version': version,
                'workspace_sha256': source_digest(files), 'created': created,
                'os_root': str(root), 'profile': profile}
    print(json.dumps({'action': 'dry-run' if args.dry_run else 'init', 'root': str(root),
                      'code_root': str(code_root), 'template_version': version,
                      'file_count': len(rendered)+1}, ensure_ascii=False))
    if args.dry_run:
        return
    stage = Path(tempfile.mkdtemp(prefix='.personal-os-init-', dir=str(root.parent)))
    try:
        os.chmod(stage, 0o700)
        for relative, body in rendered.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
            os.chmod(target, 0o600)
        (stage / '.personal-os.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        os.chmod(stage / '.personal-os.json', 0o600)
        for directory in stage.rglob('*'):
            if directory.is_dir():
                os.chmod(directory, 0o700)
        # Recheck just before rename. Use a trusted parent (see INSTALL.md).
        if root.exists() or root.is_symlink():
            fail('target appeared during installation; nothing was overwritten')
        stage.rename(root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print('Created a new OS. Run check and the new-session acceptance procedure.')


def safe_core_path(root, relative):
    path = root / relative
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            return None
    return path


def check(args):
    root = absolute_path(args.root)
    problems = []
    files = template_files()
    manifest_path = safe_core_path(root, Path('.personal-os.json'))
    if manifest_path is None or not manifest_path.is_file():
        fail('installation manifest is missing or symlinked')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    version = (REPO/'VERSION').read_text(encoding='utf-8').strip()
    if manifest.get('schema_version') != 1 or manifest.get('template_version') != version:
        fail('use the starter version recorded in the installation manifest')
    if manifest.get('os_root') != str(root):
        problems.append('installation moved: review manifest and machine-local paths before use')
    for source in files:
        relative = source.relative_to(SOURCE)
        path = safe_core_path(root, relative)
        if path is None or not path.is_file():
            problems.append(relative.as_posix()+': missing file or symlink (not followed)')
            continue
        body = path.read_text(encoding='utf-8')
        if MARKER.search(body):
            problems.append(relative.as_posix()+': unresolved template placeholder')
        if path.suffix != '.md':
            continue
        for link in LINK.findall(body):
            if link.startswith('<') and link.endswith('>'):
                link = link[1:-1]
            url = urlsplit(link)
            if url.scheme or url.netloc or not url.path:
                continue
            # Validate file links without reading their target contents.
            target = (path.parent/unquote(url.path)).resolve()
            if target != root and root not in target.parents:
                problems.append(relative.as_posix()+': local link leaves the OS')
            elif not target.exists():
                problems.append(relative.as_posix()+': broken local link')
    if problems:
        for problem in problems:
            print('ERROR: '+problem, file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({'ok': True, 'template_version': version,
                      'core_files_checked': len(files),
                      'scope': 'core files, placeholders, local links; no client/API/private-content check'},
                     ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('init', help='create a new private OS; never overwrite an existing root')
    create.add_argument('--root', required=True)
    create.add_argument('--profile', required=True, type=Path)
    create.add_argument('--dry-run', action='store_true')
    create.set_defaults(func=init)
    inspect = commands.add_parser('check', help='inspect core files and local links without network access')
    inspect.add_argument('root')
    inspect.set_defaults(func=check)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError) as error:
        # Never echo profile contents or a traceback that could contain private data.
        if isinstance(error, OSError):
            print('ERROR: filesystem operation failed; check paths and permissions', file=sys.stderr)
        else:
            print('ERROR: '+str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
