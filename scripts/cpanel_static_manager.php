<?php

declare(strict_types=1);

// One-time authenticated endpoint. The runner supplies only paths and an operation ID.
$config = __CONFIG__;
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
$lock = null;
$authenticated = false;
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
    validateConfig($config);
    $lock = fopen(privateRoot($config).'/deploy.lock', 'c');
    if ($lock === false || !flock($lock, LOCK_EX)) {
        throw new RuntimeException('Cannot lock static deployment');
    }
    $result = match ($config['action']) {
        'activate' => switchRelease($config),
        'restore' => restoreRelease($config),
        'finalize' => finalizeRelease($config),
        default => throw new RuntimeException('Unknown static deployment action'),
    };
    echo json_encode(['ok' => true] + $result, JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    if (http_response_code() !== 403) {
        http_response_code(500);
    }
    error_log('Managed static deployment: '.$error->getMessage());
    echo json_encode(['ok' => false, 'error' => $authenticated ? $error->getMessage() : 'Forbidden']);
} finally {
    if (is_resource($lock)) {
        flock($lock, LOCK_UN);
        fclose($lock);
    }
    @unlink($config['token_file']);
    @unlink(__FILE__);
}

function privateRoot(array $config): string
{
    return $config['home'].'/'.$config['project'].'-static';
}

function releaseRoot(array $config): string
{
    return privateRoot($config).'/releases';
}

function sharedRoot(array $config): string
{
    return privateRoot($config).'/shared';
}

function releaseId(string $name): bool
{
    return preg_match('/^[a-f0-9]{12}-[0-9]+-[0-9]+$/', $name) === 1;
}

function validateConfig(array $config): void
{
    if (!preg_match('/^[a-z][a-z0-9]{0,19}$/', $config['project']) ||
        !preg_match('/^[a-f0-9]{24}$/', $config['operation']) ||
        !in_array($config['action'], ['activate', 'restore', 'finalize'], true)) {
        throw new RuntimeException('Invalid static deployment configuration');
    }
    $home = realpath($config['home']);
    $public = realpath($config['public']);
    $private = privateRoot($config);
    if ($home === false || $public === false || !str_starts_with($public.'/', $home.'/') ||
        str_starts_with($public.'/', $private.'/') || is_link($config['public'])) {
        throw new RuntimeException('Invalid static document root');
    }
    if (is_link($private) || is_link(releaseRoot($config)) || is_link(sharedRoot($config)) ||
        !is_dir(releaseRoot($config))) {
        throw new RuntimeException('Static release directory is missing or linked');
    }
    if (!is_dir(sharedRoot($config)) && !mkdir(sharedRoot($config), 0700, true)) {
        throw new RuntimeException('Cannot create static shared directory');
    }
}

function releasePath(array $config, string $name): string
{
    if (!releaseId($name)) {
        throw new RuntimeException('Invalid static release ID');
    }
    $path = releaseRoot($config).'/'.$name;
    $root = realpath(releaseRoot($config));
    $resolved = realpath($path);
    if ($root === false || $resolved === false || is_link($path) ||
        !str_starts_with($resolved.'/', $root.'/')) {
        throw new RuntimeException('Static release path is missing or linked');
    }
    return $path;
}

function releaseFiles(array $config, string $name): array
{
    $path = releasePath($config, $name);
    $meta = $path.'/release.json';
    $site = $path.'/site';
    if (!is_file($meta) || is_link($meta) || !is_dir($site) || is_link($site)) {
        throw new RuntimeException('Static release is incomplete');
    }
    $settings = json_decode((string) file_get_contents($meta), true, 512, JSON_THROW_ON_ERROR);
    if (!in_array($settings['mode'] ?? null, ['next', 'spa', 'files'], true)) {
        throw new RuntimeException('Invalid static routing mode');
    }
    $files = [];
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($site,
        FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::SELF_FIRST);
    foreach ($iterator as $entry) {
        if ($entry->isLink()) {
            throw new RuntimeException('Linked static build path is unsupported');
        }
        if (!$entry->isFile()) {
            continue;
        }
        $relative = substr($entry->getPathname(), strlen($site) + 1);
        if (preg_match('/(?:^|\/)(?:\.htaccess|\.user\.ini|\.env(?:\.[^\/]*)?|\.git|\.github|[^\/]+\.(?:php|phtml|phar|cgi|pl))(?:\/|$)/i', $relative)) {
            throw new RuntimeException('Static release contains a reserved public path');
        }
        safePublicPath($config, $relative);
        $files[$relative] = $entry->getPathname();
    }
    ksort($files);
    if (!isset($files['index.html'])) {
        throw new RuntimeException('Static release has no index.html');
    }
    return ['mode' => $settings['mode'], 'files' => $files];
}

function safePublicPath(array $config, string $relative): string
{
    $parts = explode('/', $relative);
    if ($relative === '' || str_starts_with($relative, '/') ||
        count(array_filter($parts, fn ($part) => $part === '' || $part === '.' || $part === '..')) > 0) {
        throw new RuntimeException('Unsafe static public path');
    }
    $protected = json_decode($config['protected_roots_json'] ?? '[]', true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($protected)) {
        throw new RuntimeException('Invalid protected document-root list');
    }
    foreach ($protected as $root) {
        if (!is_string($root) || $root === '' || str_starts_with($relative.'/', rtrim($root, '/').'/')) {
            throw new RuntimeException('Static asset overlaps another domain document root');
        }
    }
    $path = $config['public'];
    foreach ($parts as $part) {
        $path .= '/'.$part;
        if (is_link($path)) {
            throw new RuntimeException('Linked public path is unsupported');
        }
    }
    if (is_dir($path)) {
        throw new RuntimeException('Static asset conflicts with a public directory');
    }
    return $path;
}

function historyPath(array $config): string
{
    return sharedRoot($config).'/static-history.json';
}

function history(array $config): array
{
    $path = historyPath($config);
    if (!is_file($path) || is_link($path)) {
        if (is_link($path)) {
            throw new RuntimeException('Static history is linked');
        }
        return ['version' => 1, 'project' => $config['project'], 'domain' => $config['domain'],
            'active' => null, 'previous' => [], 'files' => []];
    }
    $state = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($state) || ($state['version'] ?? null) !== 1 ||
        ($state['project'] ?? null) !== $config['project'] ||
        ($state['domain'] ?? null) !== $config['domain'] ||
        !releaseId($state['active'] ?? '') || !is_array($state['previous'] ?? null) ||
        !is_array($state['files'] ?? null)) {
        throw new RuntimeException('Invalid static deployment history');
    }
    $active = releaseFiles($config, $state['active']);
    if (array_keys($active['files']) !== $state['files']) {
        throw new RuntimeException('Static deployment history disagrees with the active release');
    }
    $index = safePublicPath($config, 'index.html');
    if (!is_file($index) || hash_file('sha256', $index) !== hash_file('sha256', $active['files']['index.html'])) {
        throw new RuntimeException('Live static entry point disagrees with deployment history');
    }
    return $state;
}

function pendingPath(array $config): string
{
    $base = sharedRoot($config).'/deploy-pending';
    if (is_link($base) || is_link($base.'/'.$config['operation'])) {
        throw new RuntimeException('Linked static backup path is unsupported');
    }
    return $base.'/'.$config['operation'];
}

function writeAtomic(string $path, string $content, int $permissions = 0644): void
{
    if (is_link($path)) {
        throw new RuntimeException('Refusing to replace linked public file');
    }
    $temp = $path.'.managed-'.bin2hex(random_bytes(6));
    if (file_put_contents($temp, $content) === false || !rename($temp, $path)) {
        @unlink($temp);
        throw new RuntimeException('Cannot write static deployment file');
    }
    @chmod($path, $permissions);
}

function managedHtaccess(array $config, string $mode): string
{
    $path = $config['public'].'/.htaccess';
    $existing = is_file($path) ? (string) file_get_contents($path) : '';
    $begin = '# BEGIN Managed Static '.$config['project'];
    $end = '# END Managed Static '.$config['project'];
    $pattern = '/^'.preg_quote($begin, '/').'\n.*?^'.preg_quote($end, '/').'\n?/ms';
    $existing = preg_replace($pattern, '', $existing);
    if ($existing === null) {
        throw new RuntimeException('Cannot prepare static routing rules');
    }
    $hostname = parse_url($config['app_url'], PHP_URL_HOST);
    if (!is_string($hostname) || $hostname === '') {
        throw new RuntimeException('Invalid application URL');
    }
    $host = preg_quote($hostname, '/');
    $rules = $begin."\n<IfModule mod_rewrite.c>\nRewriteEngine On\n";
    if ($mode !== 'files') {
        $rules .= "RewriteCond %{HTTP_HOST} ^{$host}(?::[0-9]+)?$ [NC]\n";
    }
    if ($mode === 'next') {
        $rules .= "RewriteCond %{REQUEST_FILENAME} !-f\nRewriteCond %{REQUEST_FILENAME} !-d\n";
        $rules .= "RewriteCond %{DOCUMENT_ROOT}/\$1.html -f\nRewriteRule ^(.+?)/?\$ \$1.html [L]\n";
    } elseif ($mode === 'spa') {
        $rules .= "RewriteCond %{REQUEST_FILENAME} !-f\nRewriteCond %{REQUEST_FILENAME} !-d\n";
        $rules .= "RewriteRule ^ index.html [L]\n";
    }
    return $rules."</IfModule>\n".$end."\n".ltrim($existing, "\r\n");
}

function switchRelease(array $config): array
{
    $state = history($config);
    $target = $config['release'];
    $release = releaseFiles($config, $target);
    if ($state['active'] === null) {
        if (is_file($config['public'].'/index.php')) {
            throw new RuntimeException('An existing index.php prevents static activation');
        }
        if (is_file($config['public'].'/index.html') && !($config['allow_index_replace'] ?? false)) {
            throw new RuntimeException('An unrelated index.html exists; explicit replacement is required');
        }
    }
    $oldFiles = $state['active'] === null ? [] : releaseFiles($config, $state['active'])['files'];
    $writes = $release['files'];
    $writes['.htaccess'] = '@content:'.managedHtaccess($config, $release['mode']);
    $deletes = array_diff($state['files'], array_keys($release['files']));
    $pending = pendingPath($config);
    if (file_exists($pending) || is_link($pending)) {
        throw new RuntimeException('Static operation ID already exists');
    }
    if (!is_dir(dirname($pending)) && !mkdir(dirname($pending), 0700, true)) {
        throw new RuntimeException('Cannot create static pending directory');
    }
    if (!mkdir($pending, 0700)) {
        throw new RuntimeException('Cannot create static backup');
    }
    $manifest = ['target' => $target, 'previous' => $state['active'], 'files' => []];
    foreach (array_unique(array_merge(array_keys($writes), $deletes)) as $relative) {
        $destination = safePublicPath($config, $relative);
        $old = is_file($destination);
        if (in_array($relative, $deletes, true) && $old &&
            hash_file('sha256', $destination) !== hash_file('sha256', $oldFiles[$relative])) {
            throw new RuntimeException('Managed static asset changed independently: '.$relative);
        }
        $backup = hash('sha256', $relative);
        if ($old && !copy($destination, $pending.'/'.$backup)) {
            throw new RuntimeException('Cannot back up static public file');
        }
        $source = $writes[$relative] ?? null;
        $manifest['files'][$relative] = [
            'old' => $old, 'old_hash' => $old ? hash_file('sha256', $destination) : null,
            'new_hash' => $source === null ? null : (str_starts_with($source, '@content:')
                ? hash('sha256', substr($source, 9)) : hash_file('sha256', $source)),
            'backup' => $backup,
        ];
    }
    writeAtomic($pending.'/manifest.json', json_encode($manifest, JSON_THROW_ON_ERROR), 0600);
    try {
        foreach ($manifest['files'] as $relative => $record) {
            $destination = safePublicPath($config, $relative);
            if ($record['new_hash'] === null) {
                if (is_file($destination) && !unlink($destination)) {
                    throw new RuntimeException('Cannot remove old managed static asset');
                }
                continue;
            }
            if (!is_dir(dirname($destination)) && !mkdir(dirname($destination), 0755, true)) {
                throw new RuntimeException('Cannot create static public directory');
            }
            $source = $writes[$relative];
            $content = str_starts_with($source, '@content:') ? substr($source, 9) : file_get_contents($source);
            if ($content === false) {
                throw new RuntimeException('Cannot read static release file');
            }
            writeAtomic($destination, $content);
        }
    } catch (Throwable $error) {
        restoreRelease($config);
        throw $error;
    }
    return ['release' => $target, 'previous' => $state['active']];
}

function committedRelease(array $config, string $target): bool
{
    $path = historyPath($config);
    if (!releaseId($target) || !is_file($path) || is_link($path)) {
        return false;
    }
    $state = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
    return ($state['active'] ?? null) === $target;
}

function restoreRelease(array $config): array
{
    $pending = pendingPath($config);
    $manifestFile = $pending.'/manifest.json';
    if (!is_file($manifestFile)) {
        return ['restored' => false, 'finalized' => committedRelease($config, $config['release'] ?? '')];
    }
    $manifest = json_decode((string) file_get_contents($manifestFile), true, 512, JSON_THROW_ON_ERROR);
    if (committedRelease($config, $manifest['target'])) {
        return ['restored' => false, 'finalized' => true];
    }
    $path = historyPath($config);
    if (is_file($path)) {
        $state = json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        if (($state['active'] ?? null) !== $manifest['previous']) {
            throw new RuntimeException('Static history changed during recovery');
        }
    }
    foreach (array_reverse($manifest['files'], true) as $relative => $record) {
        $destination = safePublicPath($config, $relative);
        $current = is_file($destination) ? hash_file('sha256', $destination) : null;
        if ($current === $record['old_hash']) {
            continue;
        }
        if ($current !== $record['new_hash']) {
            throw new RuntimeException('Static file changed independently during recovery: '.$relative);
        }
        if ($record['old']) {
            $backup = $pending.'/'.$record['backup'];
            if (!is_file($backup)) {
                throw new RuntimeException('Static backup is incomplete');
            }
            writeAtomic($destination, (string) file_get_contents($backup));
        } elseif (is_file($destination) && !unlink($destination)) {
            throw new RuntimeException('Cannot remove new static asset during recovery');
        }
    }
    removeTree($pending);
    return ['restored' => true, 'finalized' => false, 'release' => $manifest['previous']];
}

function finalizeRelease(array $config): array
{
    $pending = pendingPath($config);
    $manifestFile = $pending.'/manifest.json';
    if (!is_file($manifestFile)) {
        throw new RuntimeException('Pending static deployment is missing');
    }
    $manifest = json_decode((string) file_get_contents($manifestFile), true, 512, JSON_THROW_ON_ERROR);
    $target = $manifest['target'];
    $release = releaseFiles($config, $target);
    foreach ($release['files'] as $relative => $source) {
        $public = safePublicPath($config, $relative);
        if (!is_file($public) || hash_file('sha256', $public) !== hash_file('sha256', $source)) {
            throw new RuntimeException('Live static files disagree with the verified release');
        }
    }
    $path = historyPath($config);
    $old = is_file($path) ? json_decode((string) file_get_contents($path), true, 512, JSON_THROW_ON_ERROR) : null;
    if (($old['active'] ?? null) !== $manifest['previous']) {
        throw new RuntimeException('Static history changed during finalization');
    }
    $ordered = array_values(array_unique(array_filter(array_merge([$manifest['previous']], $old['previous'] ?? []),
        fn ($name) => is_string($name) && $name !== $target && completeRelease($config, $name))));
    $state = ['version' => 1, 'project' => $config['project'], 'domain' => $config['domain'],
        'active' => $target, 'previous' => array_slice($ordered, 0, 7),
        'files' => array_keys($release['files'])];
    writeAtomic($path, json_encode($state, JSON_THROW_ON_ERROR), 0600);
    removeTree($pending);
    return ['release' => $target, 'warnings' => cleanup($config, $state)];
}

function completeRelease(array $config, string $name): bool
{
    try {
        releaseFiles($config, $name);
        return true;
    } catch (Throwable) {
        return false;
    }
}

function cleanup(array $config, array $state): array
{
    $warnings = [];
    $keep = array_fill_keys(array_merge([$state['active']], $state['previous']), true);
    foreach (new DirectoryIterator(releaseRoot($config)) as $entry) {
        $name = $entry->getFilename();
        if (!$entry->isDir() || $entry->isLink() || !releaseId($name) || isset($keep[$name])) {
            continue;
        }
        try {
            removeTree(releasePath($config, $name));
        } catch (Throwable) {
            $warnings[] = 'Could not prune an old static release';
        }
    }
    $pattern = '/^'.preg_quote($config['project'], '/').'-static-[a-f0-9]{12}-[0-9]+-[0-9]+\.zip$/';
    foreach (new DirectoryIterator($config['home']) as $entry) {
        if ($entry->isFile() && !$entry->isLink() && preg_match($pattern, $entry->getFilename()) &&
            !@unlink($entry->getPathname())) {
            $warnings[] = 'Could not remove a static staging ZIP';
        }
    }
    return array_values(array_unique($warnings));
}

function removeTree(string $directory): void
{
    if (!is_dir($directory) || is_link($directory)) {
        throw new RuntimeException('Refusing to remove linked or missing static directory');
    }
    $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($directory,
        FilesystemIterator::SKIP_DOTS), RecursiveIteratorIterator::CHILD_FIRST);
    foreach ($iterator as $entry) {
        if ($entry->isDir() && !$entry->isLink()) {
            if (!rmdir($entry->getPathname())) throw new RuntimeException('Cannot remove static directory');
        } elseif (!unlink($entry->getPathname())) {
            throw new RuntimeException('Cannot remove static file');
        }
    }
    if (!rmdir($directory)) {
        throw new RuntimeException('Cannot remove static directory');
    }
}
