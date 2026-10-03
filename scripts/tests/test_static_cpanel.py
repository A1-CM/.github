import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import deploy_static_cpanel as static
from scripts import deploy_cpanel as cpanel


class StaticBuildTests(unittest.TestCase):
    def test_detects_dist_or_out_and_requires_explicit_choice_if_both_exist(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(static, 'ROOT', Path(temp)):
            root = Path(temp)
            (root / 'dist').mkdir()
            (root / 'dist/index.html').write_text('built')
            self.assertEqual(static.output_directory('auto'), root / 'dist')
            (root / 'out').mkdir()
            (root / 'out/index.html').write_text('exported')
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                static.output_directory('auto')
            self.assertEqual(static.output_directory('out'), root / 'out')
            with self.assertRaises(ValueError):
                static.output_directory('../elsewhere')

    def test_static_health_path_accepts_exported_html(self):
        with patch.object(cpanel.requests, 'get', return_value=type('Response', (), {'status_code': 200})()) as get:
            cpanel.health_check('https://example.com', '/about.html')
            self.assertIn('/about.html', get.call_args.args[0])
        with self.assertRaises(ValueError):
            cpanel.health_check('https://example.com', '/../private')

    def test_shared_document_root_is_rejected_and_nested_roots_are_protected(self):
        payload = {'cpanelresult': {'event': {'result': 1}, 'data': [
            {'domain': 'site.example', 'docroot': '/home/user/public_html'},
            {'domain': 'addon.example', 'docroot': '/home/user/public_html/addon'},
        ]}}
        with patch.object(cpanel, 'request', return_value=json.dumps(payload).encode()):
            self.assertEqual(static.protected_document_roots('base', 'user', 'token',
                                                             '/home/user/public_html', 'site.example'), ['addon'])
        payload['cpanelresult']['data'].append(
            {'domain': 'other.example', 'docroot': '/home/user/public_html'})
        with patch.object(cpanel, 'request', return_value=json.dumps(payload).encode()):
            with self.assertRaisesRegex(ValueError, 'shares this document root'):
                static.protected_document_roots('base', 'user', 'token',
                                                '/home/user/public_html', 'site.example')

    def test_next_detection_and_archive_guards(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(static, 'ROOT', Path(temp)):
            root = Path(temp)
            (root / 'package.json').write_text(json.dumps({'dependencies': {'next': '15.0.0'}}))
            self.assertEqual(static.routing_mode('auto'), 'next')
            output = root / 'out'
            (output / '_next').mkdir(parents=True)
            (output / 'index.html').write_text('home')
            (output / '_next/app.js').write_text('asset')
            payload = static.archive_release(output, 'site', '000000000001-1-1', 'next')
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.assertIn('site-static/releases/000000000001-1-1/site/_next/app.js', archive.namelist())
                self.assertEqual(json.loads(archive.read('site-static/releases/000000000001-1-1/release.json'))['mode'], 'next')
            (output / '.htaccess').write_text('unsafe')
            with self.assertRaisesRegex(ValueError, 'reserved path'):
                static.archive_release(output, 'site', '000000000001-1-1', 'next')
            (output / '.htaccess').unlink()
            (output / '.env.production').write_text('SECRET=private')
            with self.assertRaisesRegex(ValueError, 'reserved path'):
                static.archive_release(output, 'site', '000000000001-1-1', 'next')


class StaticLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.public = self.home / 'public_html'
        self.public.mkdir()
        self.releases = self.home / 'site-static/releases'
        self.releases.mkdir(parents=True)
        self.shared = self.home / 'site-static/shared'
        self.shared.mkdir()
        self.template = (cpanel.TOOLKIT_ROOT / 'scripts/cpanel_static_manager.php').read_text()
        self.counter = 0
        self.protected = []

    def release(self, number, files, mode='spa'):
        name = f'{number:012x}-{number}-1'
        root = self.releases / name
        site = root / 'site'
        site.mkdir(parents=True)
        (root / 'release.json').write_text(json.dumps({'mode': mode}))
        for relative, contents in files.items():
            path = site / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        return name

    def action(self, action, release='', operation=None, allow_index_replace=False):
        self.counter += 1
        operation = operation or f'{self.counter:024x}'
        token = self.home / f'token-{self.counter}.php'
        token.write_text("<?php return 'test-token';")
        hook = self.public / f'_hook_{self.counter}.php'
        config = {
            'action': action, 'operation': operation, 'release': release,
            'project': 'site', 'home': str(self.home), 'public': str(self.public),
            'domain': 'example.com', 'app_url': 'https://example.com',
            'token_file': str(token), 'allow_index_replace': allow_index_replace,
            'protected_roots_json': json.dumps(self.protected),
        }
        hook.write_text(self.template.replace('__CONFIG__', cpanel.php_array(config)))
        command = "$_SERVER['REQUEST_METHOD']='POST'; $_SERVER['HTTP_X_LOGICSTRAND_DEPLOY_TOKEN']='test-token'; include " + json.dumps(str(hook)) + ';'
        result = subprocess.run(['php', '-r', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(hook.exists())
        self.assertFalse(token.exists())
        return json.loads(result.stdout), operation

    def test_restore_and_finalize_preserve_addon_and_unrelated_files(self):
        (self.public / 'index.html').write_text('old site')
        (self.public / '.htaccess').write_text('# unrelated rules\n')
        addon = self.public / 'addon.example.com'
        addon.mkdir()
        (addon / 'keep.txt').write_text('keep')
        (self.public / 'other.txt').write_text('other')
        first = self.release(1, {'index.html': 'first', 'assets/old.js': 'old'})
        answer, op = self.action('activate', first)
        self.assertFalse(answer['ok'])
        self.assertIn('index.html', answer['error'])
        answer, op = self.action('activate', first, allow_index_replace=True)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual((self.public / 'index.html').read_text(), 'first')
        answer, _ = self.action('restore', first, op)
        self.assertTrue(answer['restored'], answer)
        self.assertEqual((self.public / 'index.html').read_text(), 'old site')
        self.assertFalse((self.public / 'assets/old.js').exists())
        self.assertEqual((self.public / '.htaccess').read_text(), '# unrelated rules\n')

        answer, op = self.action('activate', first, allow_index_replace=True)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', first, op)
        self.assertTrue(answer['ok'], answer)
        second = self.release(2, {'index.html': 'second', 'assets/new.js': 'new'}, 'next')
        answer, op = self.action('activate', second)
        self.assertTrue(answer['ok'], answer)
        self.assertFalse((self.public / 'assets/old.js').exists())
        self.assertIn('RewriteRule ^(.+?)/?$', (self.public / '.htaccess').read_text())
        answer, _ = self.action('restore', second, op)
        self.assertTrue(answer['restored'], answer)
        self.assertEqual((self.public / 'assets/old.js').read_text(), 'old')
        self.assertFalse((self.public / 'assets/new.js').exists())
        answer, op = self.action('activate', second)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', second, op)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(json.loads((self.shared / 'static-history.json').read_text())['active'], second)
        self.assertEqual((addon / 'keep.txt').read_text(), 'keep')
        self.assertEqual((self.public / 'other.txt').read_text(), 'other')

    def test_retains_seven_previous_releases_and_cleans_only_managed_zips(self):
        (self.home / 'unrelated.zip').write_text('keep')
        names = []
        for number in range(1, 10):
            release = self.release(number, {'index.html': str(number)})
            names.append(release)
            (self.home / f'site-static-{release}.zip').write_text('staging')
            answer, op = self.action('activate', release)
            self.assertTrue(answer['ok'], answer)
            answer, _ = self.action('finalize', release, op)
            self.assertTrue(answer['ok'], answer)
        history = json.loads((self.shared / 'static-history.json').read_text())
        self.assertEqual(history['active'], names[-1])
        self.assertEqual(history['previous'], list(reversed(names[1:-1])))
        self.assertFalse((self.releases / names[0]).exists())
        self.assertEqual(len(list(self.releases.iterdir())), 8)
        self.assertFalse(any(self.home.glob('site-static-*.zip')))
        self.assertTrue((self.home / 'unrelated.zip').exists())

    def test_nested_domain_collision_is_rejected(self):
        self.protected = ['addon.example.com']
        addon = self.public / 'addon.example.com'
        addon.mkdir()
        (addon / 'keep.txt').write_text('keep')
        release = self.release(1, {'index.html': 'new', 'addon.example.com/keep.txt': 'wrong'})
        answer, _ = self.action('activate', release)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('another domain', answer['error'])
        self.assertEqual((addon / 'keep.txt').read_text(), 'keep')

    def test_committed_release_cannot_be_restored(self):
        release = self.release(1, {'index.html': 'new'})
        answer, op = self.action('activate', release)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', release, op)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('restore', release, op)
        self.assertTrue(answer['finalized'], answer)
        self.assertFalse(answer['restored'])
        self.assertEqual((self.public / 'index.html').read_text(), 'new')


if __name__ == '__main__':
    unittest.main()
