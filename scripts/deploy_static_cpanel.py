#!/usr/bin/env python3
"""Deploy a built static site to cPanel without shell access."""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import sys
import urllib.parse
import zipfile
from pathlib import Path

if __package__:
    from . import deploy_cpanel as cpanel
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import deploy_cpanel as cpanel

ROOT = Path(os.environ.get('PROJECT_ROOT') or Path(__file__).resolve().parents[1]).resolve()
RELEASE_ID = re.compile(r'[a-f0-9]{12}-[0-9]+-[0-9]+\Z')
FORBIDDEN_NAMES = {'.htaccess', '.user.ini', '.env', '.git', '.github'}
FORBIDDEN_SUFFIXES = {'.php', '.phtml', '.phar', '.cgi', '.pl'}


def output_directory(configured: str) -> Path:
    if configured in ('', 'auto'):
        candidates = [name for name in ('dist', 'out') if (ROOT / name / 'index.html').is_file()]
        if len(candidates) != 1:
            raise ValueError('Expected exactly one built dist or out directory with index.html; set output_dir explicitly')
        configured = candidates[0]
    if configured.startswith('/') or '\\' in configured:
        raise ValueError('output_dir must be relative to the repository')
    parts = configured.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('output_dir contains an unsafe path segment')
    path = ROOT
    for part in parts:
        path /= part
        if path.is_symlink():
            raise ValueError('output_dir cannot contain symlinks')
    if not path.is_dir() or not (path / 'index.html').is_file() or (path / 'index.html').is_symlink():
        raise ValueError('output_dir must contain a regular index.html')
    return path


def routing_mode(configured: str) -> str:
    if configured not in ('auto', 'next', 'spa', 'files'):
        raise ValueError('routing_mode must be auto, next, spa, or files')
    if configured != 'auto':
        return configured
    package_file = ROOT / 'package.json'
    if package_file.is_file():
        package = json.loads(package_file.read_text())
        if any('next' in package.get(section, {}) for section in ('dependencies', 'devDependencies')):
            return 'next'
    return 'spa'


def archive_release(output: Path, project: str, release: str, mode: str) -> bytes:
    prefix = f'{project}-static/releases/{release}/'
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(prefix + 'release.json', json.dumps({'mode': mode}))
            for current, directories, filenames in os.walk(output, followlinks=False):
                parent = Path(current)
                for name in directories:
                    if (parent / name).is_symlink():
                        raise ValueError('Static build contains a linked directory')
                for name in filenames:
                    path = parent / name
                    if path.is_symlink() or not path.is_file():
                        raise ValueError('Static build contains a linked or unsupported file')
                    relative = path.relative_to(output)
                    if any(part in FORBIDDEN_NAMES or part.startswith('.env.') or
                           Path(part).suffix.lower() in FORBIDDEN_SUFFIXES for part in relative.parts):
                        raise ValueError(f'Static build contains a server-executable or reserved path: {relative}')
                    archive.write(path, prefix + 'site/' + relative.as_posix())
        return buffer.getvalue()


def health_check(site_url: str) -> None:
    cpanel.health_check(site_url, os.environ.get('CPANEL_HEALTH_PATH') or '/')



def protected_document_roots(base: str, user: str, token: str, public: str, domain: str) -> list[str]:
    query = urllib.parse.urlencode({
        'cpanel_jsonapi_user': user, 'cpanel_jsonapi_apiversion': '2',
        'cpanel_jsonapi_module': 'DomainLookup', 'cpanel_jsonapi_func': 'getdocroots',
    })
    raw = cpanel.request(base + '/json-api/cpanel?' + query,
                         headers={'Authorization': f'cpanel {user}:{token}'})
    result = json.loads(raw).get('cpanelresult', {})
    entries = result.get('data')
    if result.get('event', {}).get('result') != 1 or not isinstance(entries, list):
        raise RuntimeError('Could not verify other cPanel document roots')
    prefix = public.rstrip('/') + '/'
    nested = set()
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('docroot'), str) or
                not isinstance(entry.get('domain'), str)):
            raise RuntimeError('Invalid cPanel document-root listing')
        root = entry['docroot'].rstrip('/')
        if root == public.rstrip('/') and entry.get('domain', '').lower() != domain:
            raise ValueError('Another domain shares this document root; static deployment would affect it')
        if root.startswith(prefix):
            nested.add(root[len(prefix):])
    return sorted(nested)


def main() -> None:
    host = cpanel.required('CPANEL_HOST')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise ValueError('CPANEL_HOST must be a hostname without scheme or port')
    user, token = cpanel.required('CPANEL_USERNAME'), cpanel.required('CPANEL_API_TOKEN')
    home = cpanel.required('CPANEL_HOME').rstrip('/')
    project = cpanel.required('PROJECT_SLUG').lower()
    if not re.fullmatch(r'[a-z][a-z0-9]{0,19}', project):
        raise ValueError('PROJECT_SLUG must be 1-20 lowercase letters or digits')
    site_url = cpanel.required('APP_URL').rstrip('/')
    parsed = urllib.parse.urlparse(site_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('APP_URL must be the HTTPS site origin without a path')
    domain = (os.environ.get('CPANEL_DOMAIN') or parsed.hostname).lower()
    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', domain):
        raise ValueError('CPANEL_DOMAIN must be a valid hosted domain')
    release = (os.environ.get('GITHUB_SHA', '')[:12] + '-' +
               os.environ.get('GITHUB_RUN_ID', '') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1'))
    if not RELEASE_ID.fullmatch(release):
        raise ValueError('Deployment requires a GitHub SHA and numeric run ID')
    output = output_directory(os.environ.get('STATIC_OUTPUT_DIR', 'auto').strip())
    mode = routing_mode(os.environ.get('STATIC_ROUTING_MODE', 'auto').strip())
    archive = archive_release(output, project, release, mode)
    base = f'https://{host}:2083'
    selected = os.environ.get('CPANEL_PUBLIC_DIR', 'auto').strip()
    public = (cpanel.discover_public(base, user, token, home, domain, project)
              if selected in ('', 'auto') else cpanel.safe_public_dir(home, selected, project))
    if public == f'{home}/{project}-static' or public.startswith(f'{home}/{project}-static/'):
        raise ValueError('The static release directory cannot be a document root')
    protected = protected_document_roots(base, user, token, public, domain)
    archive_name = f'{project}-static-{release}.zip'
    release_path = f'{home}/{project}-static/releases/{release}'
    print(f'Uploading static release {release} from {output.relative_to(ROOT)}', flush=True)
    cpanel.upload(base, user, token, home, archive_name, archive)
    cpanel.extract(base, user, token, archive_name, home)
    listing = cpanel.uapi(base, user, token, 'Fileman', 'list_files', {
        'dir': release_path + '/site', 'only_these_files': 'index.html', 'types': 'file',
    })
    entries = listing if isinstance(listing, list) else listing.get('files', []) if isinstance(listing, dict) else []
    if not any(isinstance(item, dict) and item.get('file') == 'index.html' for item in entries):
        raise RuntimeError('Extracted static release is missing site/index.html')
    common = {
        'home': home, 'public': public, 'project': project, 'domain': domain,
        'app_url': site_url, 'operation': secrets.token_hex(12), 'release': release,
        'protected_roots_json': json.dumps(protected),
        'allow_index_replace': os.environ.get('CPANEL_ALLOW_INDEX_REPLACE', 'false').lower() == 'true',
    }
    try:
        cpanel.manager_call(base, user, token, {**common, 'action': 'activate'},
                            manager_script='scripts/cpanel_static_manager.php')
        health_check(site_url)
    except Exception as error:
        try:
            cpanel.manager_call(base, user, token, {**common, 'action': 'restore'},
                                manager_script='scripts/cpanel_static_manager.php')
        except Exception as restore_error:
            raise RuntimeError(f'Static deployment failed and recovery also failed: {restore_error}') from error
        raise
    result = cpanel.finalize_or_recover(base, user, token, common,
                                       manager_script='scripts/cpanel_static_manager.php')
    print(f'Activated static release {release} at {site_url}')
    for warning in result.get('warnings', []):
        print(f'Cleanup warning: {warning}', file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f'Static deployment failed: {exc}', file=sys.stderr)
        sys.exit(1)
