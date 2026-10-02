<?php

// Uploaded with a random name for a single deployment, then removed after activation.
declare(strict_types=1);
use Illuminate\Contracts\Console\Kernel;
use Illuminate\Foundation\Application;

$config = (static function (): array {
    return __CONFIG__;
})();

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['ok' => false, 'error' => 'Method not allowed']);
    exit;
}

$expected = require $config['release'].'/deploy_auth.php';
$provided = $_SERVER['HTTP_X_LOGICSTRAND_DEPLOY_TOKEN'] ?? '';
if (! is_string($provided) || ! hash_equals($expected, $provided)) {
    http_response_code(403);
    echo json_encode(['ok' => false, 'error' => 'Forbidden']);
    exit;
}

try {
    if (PHP_VERSION_ID < 80300) {
        throw new RuntimeException('PHP 8.3 or newer is required');
    }
    foreach (['pdo_mysql', 'mbstring', 'fileinfo', 'openssl'] as $extension) {
        if (! extension_loaded($extension)) {
            throw new RuntimeException("Missing PHP extension: {$extension}");
        }
    }

    $release = realpath($config['release']);
    $public = realpath($config['public']);
    if ($release === false || $public === false || ! is_dir($release) || ! is_dir($public)) {
        throw new RuntimeException('Release or public directory is unavailable');
    }
    if (str_starts_with($release.'/', $public.'/') || str_starts_with($public.'/', $release.'/')) {
        throw new RuntimeException('Private release and public directory overlap');
    }
    $index = $public.'/index.php';
    if (is_link($index)) {
        throw new RuntimeException('Refusing to replace a linked index.php');
    }
    $entryMarker = 'Managed Laravel deploy: '.$config['project'];
    if (is_file($index) && ! str_contains((string) file_get_contents($index), $entryMarker) && ! $config['allow_index_replace']) {
        throw new RuntimeException('An unrelated index.php already exists. Set CPANEL_ALLOW_INDEX_REPLACE=true only if replacing that site is intended.');
    }

    $shared = $config['shared'];
    foreach ([
        $shared.'/storage/app/private', $shared.'/storage/framework/cache/data',
        $shared.'/storage/framework/sessions', $shared.'/storage/framework/testing',
        $shared.'/storage/framework/views', $shared.'/storage/logs',
    ] as $directory) {
        if (! is_dir($directory) && ! mkdir($directory, 0755, true) && ! is_dir($directory)) {
            throw new RuntimeException('Cannot create shared storage');
        }
    }
    require_once $release.'/vendor/autoload.php';
    /** @var Application $app */
    $app = require $release.'/bootstrap/app.php';
    $app->useStoragePath($shared.'/storage');
    $kernel = $app->make(Kernel::class);
    $status = $kernel->call('migrate', ['--force' => true, '--no-interaction' => true]);
    if ($status !== 0) {
        throw new RuntimeException('Database migration failed: '.$kernel->output());
    }

    // Copy only files shipped under public/. Existing addon-domain directories are untouched.
    $sourcePublic = $release.'/public';
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($sourcePublic, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST
    );
    foreach ($iterator as $item) {
        $relative = substr($item->getPathname(), strlen($sourcePublic) + 1);
        if ($relative === 'index.php' || $relative === '.htaccess' || $relative === 'hot') {
            continue;
        }
        $destination = $public.'/'.$relative;
        if ($item->isLink() || is_link($destination)) {
            throw new RuntimeException('Linked public assets are not supported');
        }
        if ($item->isDir()) {
            if (is_file($destination) || (! is_dir($destination) && ! mkdir($destination, 0755, true))) {
                throw new RuntimeException('Public asset directory conflicts with an existing file');
            }

            continue;
        }
        $parent = dirname($destination);
        if (! is_dir($parent) && ! mkdir($parent, 0755, true)) {
            throw new RuntimeException('Cannot create public asset directory');
        }
        if (is_dir($destination)) {
            throw new RuntimeException('Public asset conflicts with an existing directory');
        }
        $temp = $destination.'.logicstrand-'.bin2hex(random_bytes(6));
        if (! copy($item->getPathname(), $temp) || ! rename($temp, $destination)) {
            @unlink($temp);
            throw new RuntimeException('Cannot copy public asset');
        }
    }

    $host = parse_url($config['app_url'], PHP_URL_HOST);
    if (! is_string($host) || $host === '') {
        throw new RuntimeException('APP_URL has no host');
    }
    $hostPattern = str_replace('.', '\\.', $host);
    $beginMarker = "# BEGIN Managed Laravel {$config['project']}";
    $endMarker = "# END Managed Laravel {$config['project']}";
    $routeBlock = $beginMarker."\n"
        ."<IfModule mod_rewrite.c>\n"
        ."RewriteEngine On\n"
        ."RewriteCond %{HTTP_HOST} ^{$hostPattern}(?::[0-9]+)?$ [NC]\n"
        ."RewriteCond %{REQUEST_FILENAME} !-d\n"
        ."RewriteCond %{REQUEST_FILENAME} !-f\n"
        ."RewriteRule ^ index.php [L]\n"
        ."</IfModule>\n"
        .$endMarker."\n";
    $htaccess = $public.'/.htaccess';
    if (is_link($htaccess)) {
        throw new RuntimeException('Refusing to change a linked .htaccess');
    }
    $existingRules = is_file($htaccess) ? (string) file_get_contents($htaccess) : '';
    $pattern = '/^'.preg_quote($beginMarker, '/').'\n.*?^'.preg_quote($endMarker, '/').'\n?/ms';
    $existingRules = preg_replace($pattern, '', $existingRules);
    if ($existingRules === null) {
        throw new RuntimeException('Cannot prepare .htaccess');
    }
    $newRules = $routeBlock.ltrim($existingRules, "\r\n");
    writeAtomically($htaccess, $newRules);

    $entry = <<<'ENTRY'
<?php
// __ENTRY_MARKER__
use Illuminate\Http\Request;
define('LARAVEL_START', microtime(true));
$release = __RELEASE__;
$shared = __SHARED__;
if (file_exists($maintenance = $shared.'/storage/framework/maintenance.php')) {
    require $maintenance;
}
require $release.'/vendor/autoload.php';
$app = require $release.'/bootstrap/app.php';
$app->useStoragePath($shared.'/storage');
if (method_exists($app, 'handleRequest')) {
    $app->handleRequest(Request::capture());
} else {
    $kernel = $app->make(\Illuminate\Contracts\Http\Kernel::class);
    $request = Request::capture();
    $response = $kernel->handle($request);
    $response->send();
    $kernel->terminate($request, $response);
}
ENTRY;
    $entry = str_replace(
        ['__RELEASE__', '__SHARED__', '__ENTRY_MARKER__'],
        [var_export($release, true), var_export($shared, true), $entryMarker],
        $entry
    );
    writeAtomically($index, $entry."\n");

    echo json_encode(['ok' => true, 'release' => basename($release)]);
} catch (Throwable $exception) {
    http_response_code(500);
    error_log('LogicStrand deployment failed: '.$exception->getMessage());
    echo json_encode(['ok' => false, 'error' => $exception->getMessage()]);
} finally {
    @unlink(__FILE__);
}

function writeAtomically(string $path, string $contents): void
{
    $temp = $path.'.logicstrand-'.bin2hex(random_bytes(6));
    if (file_put_contents($temp, $contents) === false || ! rename($temp, $path)) {
        @unlink($temp);
        throw new RuntimeException('Cannot update public entry point');
    }
}
