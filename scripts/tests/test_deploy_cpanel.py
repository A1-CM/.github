import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import deploy_cpanel as deploy


class DeploymentTests(unittest.TestCase):
    def test_nested_domain_roots_are_discovered(self):
        payload = json.dumps({'cpanelresult': {'event': {'result': 1}, 'data': [
            {'domain': 'main.example', 'docroot': '/home/account/public_html'},
            {'domain': 'addon.example', 'docroot': '/home/account/public_html/addon.example'},
            {'domain': 'other.example', 'docroot': '/home/account/other'},
        ]}}).encode()
        with patch.object(deploy, 'request', return_value=payload):
            self.assertEqual(deploy.nested_domain_roots('https://cpanel:2083', 'account', 'token',
                                                        '/home/account/public_html'), ['addon.example'])

    def test_health_check_requires_http_200(self):
        with patch.object(deploy.requests, 'get', return_value=type('Response', (), {'status_code': 200})()) as get:
            deploy.health_check('https://example.com')
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
        with patch.object(deploy.requests, 'get', return_value=type('Response', (), {'status_code': 503})()), \
             patch.object(deploy.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Health check failed'):
                deploy.health_check('https://example.com')

    def test_finalize_failure_restores_uncommitted_switch(self):
        with patch.object(deploy, 'manager_call', side_effect=[RuntimeError('finalize failed'),
                                                               {'restored': True, 'finalized': False}]) as call:
            with self.assertRaisesRegex(RuntimeError, 'previous live files were restored'):
                deploy.finalize_or_recover('base', 'user', 'token', {'release': 'target'})
        self.assertEqual([item.args[3]['action'] for item in call.call_args_list], ['finalize', 'restore'])

    def test_finalize_response_loss_keeps_committed_release(self):
        with patch.object(deploy, 'manager_call', side_effect=[RuntimeError('response lost'),
                                                               {'restored': False, 'finalized': True}]):
            result = deploy.finalize_or_recover('base', 'user', 'token', {'release': 'target'})
        self.assertIn('committed', result['warnings'][0])

    def test_generated_env_preserves_special_password_characters(self):
        password = 'a${APP_NAME}$b"\\tail#'
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, '.env').write_text('APP_NAME="LogicStrand"\nPASSWORD=' + deploy.env_value(password) + '\n')
            code = 'require ' + json.dumps(str(deploy.ROOT / 'vendor/autoload.php')) + '; Dotenv\\Dotenv::createImmutable(' + json.dumps(temp) + ')->load(); echo json_encode($_ENV["PASSWORD"]);'
            result = subprocess.run(['php', '-r', code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), password)

    def test_document_root_is_selectable_and_private_path_is_rejected(self):
        self.assertEqual(deploy.safe_public_dir('/home/account', 'public_html'), '/home/account/public_html')
        self.assertEqual(deploy.safe_public_dir('/home/account', 'domains/example.com'), '/home/account/domains/example.com')
        for candidate in ('../outside', '/etc', 'logicstrand-app/releases', 'public_html/../other'):
            with self.assertRaises(ValueError):
                deploy.safe_public_dir('/home/account', candidate)

    def test_domain_lookup_uses_the_actual_cpanel_document_root(self):
        with patch.object(deploy, 'uapi', return_value={'documentroot': '/home/account/addon/example.com'}) as api:
            self.assertEqual(
                deploy.discover_public('https://cpanel:2083', 'account', 'token', '/home/account', 'example.com', 'logicstrand'),
                '/home/account/addon/example.com',
            )
            api.assert_called_once_with('https://cpanel:2083', 'account', 'token', 'DomainInfo', 'single_domain_data', {'domain': 'example.com'})
        with patch.object(deploy, 'uapi', return_value={'documentroot': '/home/other/public_html'}):
            with self.assertRaises(ValueError):
                deploy.discover_public('https://cpanel:2083', 'account', 'token', '/home/account', 'example.com', 'logicstrand')

    def test_archive_includes_custom_laravel_dirs_but_excludes_local_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for directory in ('app', 'bootstrap', 'public', 'vendor', 'Modules', 'storage', 'node_modules'):
                (root / directory).mkdir()
            for name in ('artisan', 'composer.json', 'composer.lock'):
                (root / name).write_text(name)
            (root / 'Modules/Example.php').write_text('<?php')
            (root / 'public/asset.css').write_text('body{}')
            (root / '.env').write_text('SECRET=private')
            (root / '.env.production').write_text('SECRET=private')
            (root / 'storage/log.txt').write_text('private')
            (root / 'node_modules/package.js').write_text('unused')
            with patch.object(deploy, 'ROOT', root):
                payload = deploy.make_archive('release', '/home/account/project-app/shared', 'project')
            archive = root / 'release.zip'
            archive.write_bytes(payload)
            with zipfile.ZipFile(archive) as zipped:
                names = set(zipped.namelist())
                self.assertIn('project-app/releases/release/Modules/Example.php', names)
                self.assertIn('project-app/releases/release/public/asset.css', names)
                self.assertFalse(any(name.endswith(('.env', '.env.production', 'log.txt', 'package.js')) for name in names))

    def test_generated_credentials_persist_and_retry_without_new_passwords(self):
        calls = []
        existing = {'databases': set(), 'users': set(), 'mailboxes': set()}
        saved = {}

        def fake_uapi(base, user, token, module, function, params=None):
            calls.append((module, function, params))
            if function == 'list_files':
                return [{'file': 'logicstrand-deploy-state.json'}] if saved else []
            if function == 'get_file_content':
                return {'content': json.dumps(saved)}
            if function == 'get_restrictions':
                return {'prefix': 'account_', 'max_username_length': 32}
            if function == 'list_databases':
                return [{'database': name} for name in existing['databases']]
            if function == 'list_users':
                return [{'user': name} for name in existing['users']]
            if function == 'list_pops':
                return [{'email': name} for name in existing['mailboxes']]
            if function == 'create_database':
                existing['databases'].add(params['name'])
            if function == 'create_user':
                existing['users'].add(params['name'])
            if function == 'add_pop':
                existing['mailboxes'].add(params['email'] + '@' + params['domain'])
            return None

        def fake_upload(base, user, token, directory, filename, payload):
            self.assertEqual(directory, '/home/account')
            self.assertEqual(filename, 'logicstrand-deploy-state.json')
            saved.update(json.loads(payload))

        with patch.dict(os.environ, {}, clear=True), patch.object(deploy, 'uapi', side_effect=fake_uapi), patch.object(deploy, 'upload', side_effect=fake_upload) as upload, patch.object(deploy, 'chmod_private') as chmod:
            state = deploy.prepare_state('https://cpanel:2083', 'account', 'token', '/home/account', 'logicstrand', 'example.com')
            self.assertTrue(state['app_key'].startswith('base64:'))
            self.assertNotEqual(state['db_password'], state['mail_password'])
            deploy.provision('https://cpanel:2083', 'account', 'token', state)
            self.assertEqual(deploy.prepare_state('https://cpanel:2083', 'account', 'token', '/home/account', 'logicstrand', 'example.com'), state)
            deploy.provision('https://cpanel:2083', 'account', 'token', state)
            upload.assert_called_once()
            self.assertEqual(chmod.call_count, 2)
            with patch.dict(os.environ, {'APP_URL': 'https://example.com', 'PROJECT_NAME': 'LogicStrand'}, clear=True):
                env = deploy.build_env('/home/account/logicstrand-app/shared', 'example.com', state)
            self.assertIn('DB_HOST="localhost"', env)
            self.assertIn('DB_PASSWORD=' + deploy.env_value(state['db_password']), env)
            self.assertIn('MAIL_PASSWORD=' + deploy.env_value(state['mail_password']), env)
            self.assertIn('APP_KEY=' + deploy.env_value(state['app_key']), env)
        self.assertEqual(sum(function == 'create_database' for _, function, _ in calls), 1)
        self.assertEqual(sum(function == 'create_user' for _, function, _ in calls), 1)
        self.assertEqual(sum(function == 'add_pop' for _, function, _ in calls), 1)
        self.assertFalse(any('delete' in function or 'remove' in function for _, function, _ in calls))

    def test_first_run_refuses_existing_accounts_without_saved_credentials(self):
        def fake_uapi(base, user, token, module, function, params=None):
            if function == 'list_files':
                return []
            if function == 'get_restrictions':
                return {'prefix': 'account_'}
            if function == 'list_databases':
                return [{'database': 'account_logicstrand'}]
            return []
        with patch.object(deploy, 'uapi', side_effect=fake_uapi), patch.object(deploy, 'upload') as upload:
            with self.assertRaises(ValueError):
                deploy.prepare_state('https://cpanel:2083', 'account', 'token', '/home/account', 'logicstrand', 'example.com')
            upload.assert_not_called()

    def test_activation_keeps_addon_files_and_existing_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            release = root / 'logicstrand-app/releases/release1'
            public = root / 'public_html'
            shared = root / 'logicstrand-app/shared'
            (release / 'vendor').mkdir(parents=True)
            (release / 'bootstrap').mkdir()
            (release / 'public/build').mkdir(parents=True)
            (release / 'vendor/autoload.php').write_text('<?php')
            (release / 'bootstrap/app.php').write_text('''<?php
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
            (release / 'deploy_auth.php').write_text("<?php return 'token';")
            (release / 'public/build/app.css').write_text('body{}')
            (public / 'addon-domain').mkdir(parents=True)
            (public / 'addon-domain/keep.txt').write_text('keep')
            (public / '.htaccess').write_text('# Existing addon-domain rule\nRewriteRule ^addon-domain/ - [L]\n')
            config = {
                'release': str(release), 'shared': str(shared), 'public': str(public),
                'app_url': 'https://example.com', 'project': 'logicstrand', 'allow_index_replace': False,
            }
            template = (deploy.TOOLKIT_ROOT / 'scripts/cpanel_activate.php').read_text()
            hook = public / '_activate.php'
            hook.write_text(template.replace('__CONFIG__', deploy.php_array(config)))
            command = "$_SERVER['REQUEST_METHOD']='POST'; $_SERVER['HTTP_X_LOGICSTRAND_DEPLOY_TOKEN']='token'; include " + json.dumps(str(hook)) + ';'
            result = subprocess.run(['php', '-r', command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'], result.stdout)
            self.assertEqual((public / 'addon-domain/keep.txt').read_text(), 'keep')
            self.assertIn('Existing addon-domain rule', (public / '.htaccess').read_text())
            self.assertIn('Managed Laravel deploy: logicstrand', (public / 'index.php').read_text())
            self.assertEqual((public / 'build/app.css').read_text(), 'body{}')
            self.assertFalse(hook.exists())


if __name__ == '__main__':
    unittest.main()
