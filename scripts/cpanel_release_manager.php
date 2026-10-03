<?php

declare(strict_types=1);

// One-time endpoint uploaded by the deployment runner. Configuration is generated
// by the runner and contains only paths, an action, and a random operation ID.
$config = __CONFIG__;
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$authenticated = false;
$lock = null;
try {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
        throw new RuntimeException('Method not allowed');
    }
    $expected = require $config['token_file'];
    $provided = $_SERVER['HTTP_X_LOGICSTRAND_DEPLOY_TOKEN'] ?? '';
    if (!is_string($expected) || !is_string($provided) || !hash_equals($expected, $provided)) {
        http_response_code(403);
        throw new RuntimeException('Forbidden');
    }
    $authenticated = true;
    checkConfig($config);
    $lock = fopen($config['home'].'/'.$config['project'].'-app/deploy.lock', 'c');
    if ($lock === false || !flock($lock, LOCK_EX)) {
        throw new RuntimeException('Cannot lock deployment state');
    }
    $result = match ($config['action']) {
        'activate' => beginSwitch($config, false),
        'rollback' => beginSwitch($config, true),
        'restore' => restoreSwitch($config),
        'finalize' => finalizeSwitch($config),
        default => throw new RuntimeException('Unknown deployment action'),
    };
    echo json_encode(['ok' => true] + $result, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    if (http_response_code() !== 403) {
        http_response_code(500);
    }
    error_log('Managed Laravel deployment: '.$error->getMessage());
    echo json_encode(['ok' => false, 'error' => $authenticated ? $error->getMessage() : 'Forbidden']);
} finally {
    if (is_resource($lock)) {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
    @unlink($config['token_file']);
    @unlink(__FILE__);
}

function checkConfig(array $config): void
{
    if (!preg_match('/^[a-z][a-z0-9]{0,19}$/', $config['project']) ||
        !preg_match('/^[a-f0-9]{24}$/', $config['operation']) ||
        !in_array($config['action'], ['activate', 'rollback', 'restore', 'finalize'], true)) {
        throw new RuntimeException('Invalid deployment configuration');
    }
    $home = realpath($config['home']);
    $public = realpath($config['public']);
    if ($home === false || $public === false || !str_starts_with($public.'/', $home.'/') ||
        str_contains($public, '/'.$config['project'].'-app/')) {
        throw new RuntimeException('Document root is outside the account or overlaps private releases');
    }
    $private = $config['home'].'/'.$config['project'].'-app';
    $releases = $private.'/releases';
    $shared = $private.'/shared';
    if (!is_dir($shared) && !mkdir($shared, 0700, true) && !is_dir($shared)) {
        throw new RuntimeException('Cannot create private shared directory');
    }
    if (is_link($private) || is_link($releases) || is_link($shared) ||
        !is_dir($releases) || !is_dir($shared)) {
        throw new RuntimeException('Private release or shared directory is missing or linked');
    }
}

function releaseRoot(array $config): string
{
    return $config['home'].'/'.$config['project'].'-app/releases';
}

function sharedRoot(array $config): string
{
    return $config['home'].'/'.$config['project'].'-app/shared';
}

function releaseId(string $name): bool
{
    return preg_match('/^[a-f0-9]{12}-[0-9]+-[0-9]+$/', $name) === 1;
}

function releasePath(array $config, string $name): string
{
    if (!releaseId($name)) {
        throw new RuntimeException('Invalid release ID');
    }
    $path = releaseRoot($config).'/'.$name;
    $root = realpath(releaseRoot($config));
    $resolved = realpath($path);
    if ($root === false || $resolved === false || is_link($path) ||
        !str_starts_with($resolved.'/', $root.'/')) {
        throw new RuntimeException('Release path is unavailable or linked');
    }
    return $path;
}

function completeRelease(array $config, string $name): bool
{
    try {
        $path = releasePath($config, $name);
    } catch (Throwable) {
        return false;
    }
    foreach (['bootstrap', 'public', 'vendor', 'vendor/composer'] as $directory) {
        if (!is_dir($path.'/'.$directory) || is_link($path.'/'.$directory)) {
            return false;
        }
    }
    foreach (['artisan', '.env', 'bootstrap/app.php', 'public/index.php',
        'vendor/autoload.php', 'vendor/composer/autoload_real.php'] as $file) {
        if (!is_file($path.'/'.$file) || is_link($path.'/'.$file)) {
            return false;
        }
        $resolved = realpath($path.'/'.$file);
        if ($resolved === false || !str_starts_with($resolved, realpath($path).'/')) {
            return false;
        }
    }
    return true;
}

function activeRelease(array $config): ?string
{
    $index = $config['public'].'/index.php';
    if (!is_file($index) || is_link($index)) {
        return null;
    }
    $content = file_get_contents($index);
    if ($content === false || !str_contains($content, 'Managed Laravel deploy: '.$config['project'])) {
        return null;
    }
    $root = preg_quote(releaseRoot($config).'/', '/');
    if (!preg_match('/\$release\s*=\s*[\'\"]'.$root.'([a-f0-9]{12}-[0-9]+-[0-9]+)[\'\"];/', $content, $match)) {
        throw new RuntimeException('Managed entry point has an invalid release pointer');
    }
    return $match[1];
}

function olderLegacyRelease(string $candidate, ?string $active): bool
{
    if ($active === null || !releaseId($candidate) || !releaseId($active)) {
        return false;
    }
    preg_match('/^[a-f0-9]{12}-([0-9]+)-([0-9]+)$/', $candidate, $candidateParts);
    preg_match('/^[a-f0-9]{12}-([0-9]+)-([0-9]+)$/', $active, $activeParts);
    return [(int) $candidateParts[1], (int) $candidateParts[2]] <
        [(int) $activeParts[1], (int) $activeParts[2]];
}

function historyPath(array $config): string
{
    return sharedRoot($config).'/deployment-history.json';
}

function history(array $config): array
{
    $path = historyPath($config);
    $actual = activeRelease($config);
    if (is_file($path)) {
        if (is_link($path)) {
            throw new RuntimeException('Deployment history is linked');
        }
        $data = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (!is_array($data) || ($data['version'] ?? null) !== 1 ||
            ($data['project'] ?? null) !== $config['project'] ||
            ($data['domain'] ?? null) !== $config['domain'] ||
            ($data['active'] ?? null) !== $actual || !is_array($data['previous'] ?? null)) {
            throw new RuntimeException('Deployment history disagrees with the live entry point');
        }
        return $data;
    }
    // Import only complete legacy releases. Incomplete failed uploads are never
    // offered as rollback targets and are removed after a later healthy deploy.
    $older = [];
    foreach (new DirectoryIterator(releaseRoot($config)) as $entry) {
        $name = $entry->getFilename();
        if ($entry->isDir() && !$entry->isLink() && $name !== $actual && olderLegacyRelease($name, $actual) && completeRelease($config, $name)) {
            $older[$name] = $entry->getMTime();
        }
    }
    arsort($older);
    return ['version' => 1, 'project' => $config['project'], 'domain' => $config['domain'],
        'active' => $actual, 'previous' => array_slice(array_keys($older), 0, 7)];
}

function pendingPath(array $config): string
{
    $base = sharedRoot($config).'/deploy-pending';
    if (is_link($base) || is_link($base.'/'.$config['operation'])) {
        throw new RuntimeException('Linked deployment backup path is unsupported');
    }
    return $base.'/'.$config['operation'];
}

function writeAtomic(string $path, string $content): void
{
    if (is_link($path)) {
        throw new RuntimeException('Refusing to replace linked file');
    }
    $temp = $path.'.managed-'.bin2hex(random_bytes(6));
    if (file_put_contents($temp, $content) === false || !rename($temp, $path)) {
        @unlink($temp);
        throw new RuntimeException('Cannot write deployment file');
    }
    @chmod($path, 0600);
}

function beginSwitch(array $config, bool $rollback): array
{
    $state = history($config);
    $previous = $state['active'];
    if ($rollback) {
        $steps = (int) ($config['steps_back'] ?? 0);
        if ($steps < 1 || $steps > 7 || !isset($state['previous'][$steps - 1])) {
            throw new RuntimeException('Requested rollback is not retained');
        }
        $target = $state['previous'][$steps - 1];
    } else {
        $target = $config['release'];
    }
    if (!completeRelease($config, $target)) {
        throw new RuntimeException('Target release is incomplete');
    }
    if ($previous === null && !$rollback && is_file($config['public'].'/index.php') &&
        !($config['allow_index_replace'] ?? false)) {
        throw new RuntimeException('An unrelated index.php already exists; explicit replacement is required');
    }
    if (!$rollback) {
        runMigrations($config, $target);
    }
    $pending = pendingPath($config);
    if (file_exists($pending) || is_link($pending)) {
        throw new RuntimeException('Operation ID already exists');
    }
    if (!is_dir(dirname($pending)) && !mkdir(dirname($pending), 0700, true)) {
        throw new RuntimeException('Cannot create pending deployment directory');
    }
    if (!mkdir($pending, 0700)) {
        throw new RuntimeException('Cannot create deployment backup');
    }
    $files = desiredFiles($config, $target);
    $manifest = ['kind' => $rollback ? 'rollback' : 'activate', 'target' => $target,
        'previous' => $previous, 'files' => []];
    foreach ($files as $relative => $source) {
        $destination = safePublicPath($config, $relative);
        $old = is_file($destination);
        $backup = hash('sha256', $relative);
        if ($old && !copy($destination, $pending.'/'.$backup)) {
            throw new RuntimeException('Cannot back up public file');
        }
        $manifest['files'][$relative] = [
            'old' => $old, 'old_hash' => $old ? hash_file('sha256', $destination) : null,
            'new_hash' => is_string($source) && str_starts_with($source, '@content:')
                ? hash('sha256', substr($source, 9)) : hash_file('sha256', $source),
            'backup' => $backup,
        ];
    }
    writeAtomic($pending.'/manifest.json', json_encode($manifest, JSON_THROW_ON_ERROR));
    try {
        foreach ($files as $relative => $source) {
            $destination = safePublicPath($config, $relative);
            if (!is_dir(dirname($destination)) && !mkdir(dirname($destination), 0755, true)) {
                throw new RuntimeException('Cannot create public asset directory');
            }
            $content = is_string($source) && str_starts_with($source, '@content:')
                ? substr($source, 9) : file_get_contents($source);
            if ($content === false) {
                throw new RuntimeException('Cannot read release asset');
            }
            writeAtomic($destination, $content);
            @chmod($destination, 0644);
        }
    } catch (Throwable $error) {
        restoreSwitch($config);
        throw $error;
    }
    return ['release' => $target, 'previous' => $previous];
}

function runMigrations(array $config, string $target): void
{
    if (PHP_VERSION_ID < 80300) {
        throw new RuntimeException('PHP 8.3 or newer is required');
    }
    foreach (['pdo_mysql', 'mbstring', 'fileinfo', 'openssl'] as $extension) {
        if (!extension_loaded($extension)) {
            throw new RuntimeException('Missing PHP extension: '.$extension);
        }
    }
    $path = releasePath($config, $target);
    foreach (['app/private', 'framework/cache/data', 'framework/sessions',
        'framework/testing', 'framework/views', 'logs'] as $directory) {
        $destination = sharedRoot($config).'/storage/'.$directory;
        if (!is_dir($destination) && !mkdir($destination, 0755, true) && !is_dir($destination)) {
            throw new RuntimeException('Cannot create shared storage');
        }
    }
    require_once $path.'/vendor/autoload.php';
    $app = require $path.'/bootstrap/app.php';
    $app->useStoragePath(sharedRoot($config).'/storage');
    $kernel = $app->make(\Illuminate\Contracts\Console\Kernel::class);
    $status = $kernel->call('migrate', ['--force' => true, '--no-interaction' => true]);
    if ($status !== 0) {
        throw new RuntimeException('Database migration failed: '.$kernel->output());
    }
}

function desiredFiles(array $config, string $target): array
{
    $release = releasePath($config, $target);
    $sourceRoot = $release.'/public';
    $files = [];
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($sourceRoot,
        FilesystemIterator::SKIP_DOTS));
    foreach ($iterator as $item) {
        if ($item->isLink()) {
            throw new RuntimeException('Linked public asset is unsupported');
        }
        if (!$item->isFile()) {
            continue;
        }
        $relative = substr($item->getPathname(), strlen($sourceRoot) + 1);
        if (in_array($relative, ['index.php', '.htaccess', 'hot'], true)) {
            continue;
        }
        safePublicPath($config, $relative);
        $files[$relative] = $item->getPathname();
    }
    $files['.htaccess'] = '@content:'.managedHtaccess($config);
    $files['index.php'] = '@content:'.managedIndex($config, $target);
    return $files;
}

function safePublicPath(array $config, string $relative): string
{
    $protected = json_decode($config['protected_roots_json'] ?? '[]', true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($protected)) {
        throw new RuntimeException('Invalid protected document-root list');
    }
    foreach ($protected as $root) {
        if (!is_string($root) || $root === '' || str_starts_with($relative.'/', rtrim($root, '/').'/')) {
            throw new RuntimeException('Public asset overlaps another domain document root');
        }
    }
    $parts = explode('/', $relative);
    if ($relative === '' || str_starts_with($relative, '/') ||
        count(array_filter($parts, fn ($part) => $part === '' || $part === '.' || $part === '..')) > 0) {
        throw new RuntimeException('Unsafe public asset path');
    }
    $path = $config['public'];
    foreach ($parts as $part) {
        $path .= '/'.$part;
        if (is_link($path)) {
            throw new RuntimeException('Linked public path is unsupported');
        }
    }
    if (is_dir($path)) {
        throw new RuntimeException('Public asset conflicts with a directory');
    }
    return $path;
}

function managedHtaccess(array $config): string
{
    $path = $config['public'].'/.htaccess';
    $existing = is_file($path) ? (string) file_get_contents($path) : '';
    $begin = '# BEGIN Managed Laravel '.$config['project'];
    $end = '# END Managed Laravel '.$config['project'];
    $pattern = '/^'.preg_quote($begin, '/').'\n.*?^'.preg_quote($end, '/').'\n?/ms';
    $existing = preg_replace($pattern, '', $existing);
    if ($existing === null) {
        throw new RuntimeException('Cannot prepare routing rules');
    }
    $hostname = parse_url($config['app_url'], PHP_URL_HOST);
    if (!is_string($hostname) || $hostname === '') {
        throw new RuntimeException('Invalid application URL for routing rules');
    }
    $host = preg_quote($hostname, '/');
    return $begin."\n<IfModule mod_rewrite.c>\nRewriteEngine On\n"
        ."RewriteCond %{HTTP_HOST} ^{$host}(?::[0-9]+)?$ [NC]\n"
        ."RewriteCond %{REQUEST_FILENAME} !-d\nRewriteCond %{REQUEST_FILENAME} !-f\n"
        ."RewriteRule ^ index.php [L]\n</IfModule>\n".$end."\n".ltrim($existing, "\r\n");
}

function managedIndex(array $config, string $target): string
{
    $release = var_export(releasePath($config, $target), true);
    $shared = var_export(sharedRoot($config), true);
    return <<<PHP
<?php
// Managed Laravel deploy: {$config['project']}
use Illuminate\Http\Request;
define('LARAVEL_START', microtime(true));
\$release = {$release};
\$shared = {$shared};
if (file_exists(\$maintenance = \$shared.'/storage/framework/maintenance.php')) require \$maintenance;
require \$release.'/vendor/autoload.php';
\$app = require \$release.'/bootstrap/app.php';
\$app->useStoragePath(\$shared.'/storage');
if (method_exists(\$app, 'handleRequest')) {
    \$app->handleRequest(Request::capture());
} else {
    \$kernel = \$app->make(\Illuminate\Contracts\Http\Kernel::class);
    \$request = Request::capture();
    \$response = \$kernel->handle(\$request);
    \$response->send();
    \$kernel->terminate(\$request, \$response);
}
PHP;
}

function restoreSwitch(array $config): array
{
    $pending = pendingPath($config);
    $manifestFile = $pending.'/manifest.json';
    if (!is_file($manifestFile)) {
        return ['restored' => false];
    }
    $manifest = json_decode((string) file_get_contents($manifestFile), true, 512, JSON_THROW_ON_ERROR);
    foreach (array_reverse($manifest['files'], true) as $relative => $record) {
        $destination = safePublicPath($config, $relative);
        $current = is_file($destination) ? hash_file('sha256', $destination) : null;
        if ($current === $record['old_hash']) {
            continue;
        }
        if ($current !== $record['new_hash']) {
            throw new RuntimeException('Public file changed independently during recovery: '.$relative);
        }
        if ($record['old']) {
            $backup = $pending.'/'.$record['backup'];
            if (!is_file($backup)) {
                throw new RuntimeException('Deployment backup is incomplete');
            }
            writeAtomic($destination, (string) file_get_contents($backup));
            @chmod($destination, 0644);
        } else {
            if (!unlink($destination)) {
                throw new RuntimeException('Cannot remove new public asset during recovery');
            }
        }
    }
    removeTree($pending);
    return ['restored' => true, 'release' => $manifest['previous']];
}

function finalizeSwitch(array $config): array
{
    $pending = pendingPath($config);
    $manifestFile = $pending.'/manifest.json';
    if (!is_file($manifestFile)) {
        throw new RuntimeException('Pending deployment is missing');
    }
    $manifest = json_decode((string) file_get_contents($manifestFile), true, 512, JSON_THROW_ON_ERROR);
    if (activeRelease($config) !== $manifest['target']) {
        throw new RuntimeException('Live entry point does not match the verified release');
    }
    // A pending switch is expected to differ from the last committed history.
    $path = historyPath($config);
    $old = is_file($path) ? json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR) : null;
    if ($old !== null && ($old['active'] ?? null) !== $manifest['previous']) {
        throw new RuntimeException('Deployment history changed during activation');
    }
    if ($old === null) {
        $older = [];
        foreach (new DirectoryIterator(releaseRoot($config)) as $entry) {
            $name = $entry->getFilename();
            if ($entry->isDir() && !$entry->isLink() && $name !== $manifest['target'] &&
                $name !== $manifest['previous'] && olderLegacyRelease($name, $manifest['previous']) && completeRelease($config, $name)) {
                $older[$name] = $entry->getMTime();
            }
        }
        arsort($older);
        $previousList = array_keys($older);
    } else {
        $previousList = $old['previous'];
    }
    $ordered = array_merge([$manifest['previous']], $previousList);
    $ordered = array_values(array_unique(array_filter($ordered,
        fn ($name) => is_string($name) && $name !== $manifest['target'] && completeRelease($config, $name))));
    $history = ['version' => 1, 'project' => $config['project'], 'domain' => $config['domain'],
        'active' => $manifest['target'], 'previous' => array_slice($ordered, 0, 7)];
    writeAtomic($path, json_encode($history, JSON_THROW_ON_ERROR));
    removeTree($pending);
    $warnings = cleanup($config, $history);
    return ['release' => $history['active'], 'previous' => $history['previous'], 'warnings' => $warnings];
}

function cleanup(array $config, array $history): array
{
    $warnings = [];
    $keep = array_fill_keys(array_merge([$history['active']], $history['previous']), true);
    foreach (new DirectoryIterator(releaseRoot($config)) as $entry) {
        $name = $entry->getFilename();
        if (!$entry->isDir() || $entry->isLink() || !releaseId($name) || isset($keep[$name])) {
            continue;
        }
        try {
            removeTree(releasePath($config, $name));
        } catch (Throwable) {
            $warnings[] = 'Could not prune an old managed release';
        }
    }
    $zipPattern = '/^'.preg_quote($config['project'], '/').'-[a-f0-9]{12}-[0-9]+-[0-9]+\.zip$/';
    foreach (new DirectoryIterator($config['home']) as $entry) {
        if ($entry->isFile() && !$entry->isLink() && preg_match($zipPattern, $entry->getFilename())) {
            if (!@unlink($entry->getPathname())) {
                $warnings[] = 'Could not remove a managed staging ZIP';
            }
        }
    }
    return array_values(array_unique($warnings));
}

function removeTree(string $directory): void
{
    if (is_link($directory) || !is_dir($directory)) {
        throw new RuntimeException('Refusing to remove linked or missing directory');
    }
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory,
        FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
    foreach ($iterator as $entry) {
        if ($entry->isDir() && !$entry->isLink()) {
            if (!rmdir($entry->getPathname())) throw new RuntimeException('Cannot remove managed directory');
        } else {
            if (!unlink($entry->getPathname())) throw new RuntimeException('Cannot remove managed file');
        }
    }
    if (!rmdir($directory)) {
        throw new RuntimeException('Cannot remove managed directory');
    }
}
