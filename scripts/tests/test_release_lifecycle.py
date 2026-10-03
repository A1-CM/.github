import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import deploy_cpanel as deploy


class ReleaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.public = self.home / 'public_html'
        self.public.mkdir()
        self.releases = self.home / 'logicstrand-app' / 'releases'
        self.releases.mkdir(parents=True)
        self.shared = self.home / 'logicstrand-app' / 'shared'
        self.shared.mkdir()
        self.template = (deploy.TOOLKIT_ROOT / 'scripts/cpanel_release_manager.php').read_text()
        self.counter = 0
        self.protected_roots = []

    def release(self, sequence, color='blue'):
        name = f'{sequence:012x}-{sequence}-1'
        root = self.releases / name
        for part in ('bootstrap', 'public/images', 'vendor/composer'):
            (root / part).mkdir(parents=True)
        (root / 'artisan').write_text('artisan')
        (root / '.env').write_text('APP_ENV=production')
        (root / 'public/index.php').write_text('<?php')
        (root / 'public/images/brand.txt').write_text(color)
        (root / 'vendor/autoload.php').write_text('<?php')
        (root / 'vendor/composer/autoload_real.php').write_text('<?php')
        (root / 'bootstrap/app.php').write_text('''<?php
return new class {
    public function useStoragePath($path) { return $this; }
    public function make($name) {
        return new class {
            public function call($command, $options) { return 0; }
            public function output() { return ''; }
        };
    }
};
''')
        return name

    def action(self, action, operation=None, release='', steps_back=0, allow_index_replace=False):
        self.counter += 1
        operation = operation or f'{self.counter:024x}'
        token_path = self.home / f'token-{self.counter}.php'
        token_path.write_text("<?php return 'test-token';")
        hook = self.public / f'_hook_{self.counter}.php'
        config = {
            'action': action, 'operation': operation, 'release': release,
            'steps_back': steps_back, 'project': 'logicstrand',
            'home': str(self.home), 'public': str(self.public),
            'domain': 'example.com', 'app_url': 'https://example.com',
            'token_file': str(token_path), 'allow_index_replace': allow_index_replace,
            'protected_roots_json': json.dumps(self.protected_roots),
        }
        hook.write_text(self.template.replace('__CONFIG__', deploy.php_array(config)))
        command = "$_SERVER['REQUEST_METHOD']='POST'; $_SERVER['HTTP_X_LOGICSTRAND_DEPLOY_TOKEN']='test-token'; include " + json.dumps(str(hook)) + ';'
        result = subprocess.run(['php', '-r', command], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(hook.exists())
        self.assertFalse(token_path.exists())
        try:
            answer = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f'Invalid manager response: {result.stdout} {result.stderr}')
        return answer, operation

    def test_first_deploy_cleanup_and_automatic_restore(self):
        original_index = '<?php echo "old site";'
        self.public.joinpath('index.php').write_text(original_index)
        self.public.joinpath('.htaccess').write_text('# existing rules\n')
        addon = self.public / 'addon.example.com'
        addon.mkdir()
        (addon / 'keep.txt').write_text('keep')
        (self.home / 'unrelated.zip').write_text('keep')
        (self.home / 'logicstrand-0000000000ff-255-1.zip').write_text('old staging')
        (self.home / 'logicstrand-0000000000ee-238-1.zip').symlink_to('unrelated.zip')
        one = self.release(1, 'one')
        (self.home / f'logicstrand-{one}.zip').write_text('staging')
        answer, op = self.action('activate', release=one, allow_index_replace=True)
        self.assertTrue(answer['ok'], answer)
        self.assertIn(one, self.public.joinpath('index.php').read_text())
        lint = subprocess.run(['php', '-l', str(self.public / 'index.php')], text=True, capture_output=True)
        self.assertEqual(lint.returncode, 0, lint.stderr)
        self.assertEqual(self.public.joinpath('images/brand.txt').read_text(), 'one')
        answer, _ = self.action('restore', operation=op)
        self.assertTrue(answer['ok'], answer)
        self.assertTrue(answer['restored'])
        self.assertEqual(self.public.joinpath('index.php').read_text(), original_index)
        self.assertFalse(self.public.joinpath('images/brand.txt').exists())
        self.assertEqual(self.public.joinpath('.htaccess').read_text(), '# existing rules\n')
        self.assertTrue(self.home.joinpath(f'logicstrand-{one}.zip').exists())
        self.assertEqual(addon.joinpath('keep.txt').read_text(), 'keep')

        answer, op = self.action('activate', release=one, allow_index_replace=True)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(self.home.joinpath(f'logicstrand-{one}.zip').exists())
        self.assertTrue(self.home.joinpath('unrelated.zip').exists())
        self.assertFalse(self.home.joinpath('logicstrand-0000000000ff-255-1.zip').exists())
        self.assertTrue(self.home.joinpath('logicstrand-0000000000ee-238-1.zip').is_symlink())
        self.assertEqual(addon.joinpath('keep.txt').read_text(), 'keep')
        history = json.loads(self.shared.joinpath('deployment-history.json').read_text())
        self.assertEqual(history['active'], one)

    def test_manual_rollback_and_seven_previous_releases(self):
        names = []
        for number in range(1, 10):
            name = self.release(number, str(number))
            names.append(name)
            answer, op = self.action('activate', release=name)
            self.assertTrue(answer['ok'], answer)
            answer, _ = self.action('finalize', operation=op)
            self.assertTrue(answer['ok'], answer)
        history = json.loads(self.shared.joinpath('deployment-history.json').read_text())
        self.assertEqual(history['active'], names[-1])
        self.assertEqual(history['previous'], list(reversed(names[1:-1])))
        self.assertFalse((self.releases / names[0]).exists())
        self.assertEqual(len([p for p in self.releases.iterdir() if p.is_dir()]), 8)
        answer, op = self.action('rollback', steps_back=7)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['release'], names[1])
        self.assertEqual(self.public.joinpath('images/brand.txt').read_text(), '2')
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        history = json.loads(self.shared.joinpath('deployment-history.json').read_text())
        self.assertEqual(history['active'], names[1])
        self.assertEqual(history['previous'][0], names[-1])
        answer, op = self.action('rollback', steps_back=1)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['release'], names[-1])
        self.assertEqual(self.public.joinpath('images/brand.txt').read_text(), '9')
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(json.loads(self.shared.joinpath('deployment-history.json').read_text())['active'], names[-1])

    def test_nested_addon_root_is_never_overwritten(self):
        addon = self.public / 'addon.example.com'
        addon.mkdir()
        (addon / 'keep.txt').write_text('keep')
        self.protected_roots = ['addon.example.com']
        release = self.release(1)
        target = self.releases / release / 'public' / 'addon.example.com'
        target.mkdir()
        (target / 'keep.txt').write_text('replace')
        answer, _ = self.action('activate', release=release)
        self.assertFalse(answer['ok'], answer)
        self.assertIn('overlaps another domain', answer['error'])
        self.assertEqual((addon / 'keep.txt').read_text(), 'keep')

    def test_incomplete_legacy_release_is_not_retained(self):
        incomplete = self.releases / '000000000003-3-1'
        incomplete.mkdir()
        (incomplete / 'deploy_auth.php').write_text('<?php')
        release = self.release(1)
        answer, op = self.action('activate', release=release)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        history = json.loads(self.shared.joinpath('deployment-history.json').read_text())
        self.assertNotIn(incomplete.name, history['previous'])
        self.assertFalse(incomplete.exists())

    def test_imports_complete_legacy_releases_and_rejects_corrupt_history(self):
        active = self.release(2, 'active')
        answer, op = self.action('activate', release=active)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        history_file = self.shared / 'deployment-history.json'
        history_file.unlink()
        older = self.release(1, 'older')
        incomplete = self.releases / '000000000003-3-1'
        incomplete.mkdir()
        answer, op = self.action('rollback', steps_back=1)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['release'], older)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        self.assertFalse(incomplete.exists())
        history_file.write_text('invalid json')
        answer, _ = self.action('rollback', steps_back=1)
        self.assertFalse(answer['ok'], answer)
        self.assertEqual(self.public.joinpath('images/brand.txt').read_text(), 'older')

    def test_cleanup_skips_symlink_release(self):
        active = self.release(1)
        answer, op = self.action('activate', release=active)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        (self.releases / '000000000002-2-1').symlink_to(outside, target_is_directory=True)
        next_release = self.release(3)
        answer, op = self.action('activate', release=next_release)
        self.assertTrue(answer['ok'], answer)
        answer, _ = self.action('finalize', operation=op)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual((outside / 'keep.txt').read_text(), 'keep')
        self.assertTrue((self.releases / '000000000002-2-1').is_symlink())


if __name__ == '__main__':
    unittest.main()
