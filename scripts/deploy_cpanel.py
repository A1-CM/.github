#!/usr/bin/env python3
"""Package and activate a Laravel app on cPanel without SSH or deleting remote folders."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import requests
import zipfile
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("PROJECT_ROOT") or TOOLKIT_ROOT).resolve()
REQUIRED_DIRS = ("app", "bootstrap", "public", "vendor")
REQUIRED_FILES = ("artisan", "composer.json", "composer.lock")
SKIP_PARTS = {".git", ".github", "node_modules", "tests", "storage", "__pycache__", ".DS_Store"}
SKIP_PATHS = {"bootstrap/cache", "database/database.sqlite", "public/hot"}


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}")
    return value


def env_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("Environment values must be single-line")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def safe_public_dir(home: str, configured: str, project: str = "logicstrand") -> str:
    home = home.rstrip("/")
    if not home.startswith("/") or not home:
        raise ValueError("CPANEL_HOME must be an absolute directory")
    if not configured or configured.startswith("/") or "\\" in configured:
        raise ValueError("CPANEL_PUBLIC_DIR must be relative to CPANEL_HOME")
    parts = configured.strip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("CPANEL_PUBLIC_DIR contains unsafe path segments")
    if parts[0] == project + "-app":
        raise ValueError("The private application directory cannot be a document root")
    return home + "/" + "/".join(parts)


def build_env(shared: str, domain: str, state: dict) -> str:
    app_url = required("APP_URL")
    parsed = urllib.parse.urlparse(app_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("APP_URL must be the HTTPS site origin without a path")
    app_key = state["app_key"]
    vals = {
        "APP_NAME": os.environ.get("PROJECT_NAME") or os.environ.get("PROJECT_SLUG") or "Laravel", "APP_ENV": "production", "APP_DEBUG": "false",
        "APP_URL": app_url, "APP_KEY": app_key,
        "LOG_CHANNEL": "single", "LOG_LEVEL": "warning",
        "DB_CONNECTION": "mysql", "DB_HOST": os.environ.get("DB_HOST") or "localhost",
        "DB_PORT": os.environ.get("DB_PORT") or "3306",
        "DB_DATABASE": state["database"], "DB_USERNAME": state["db_user"], "DB_PASSWORD": state["db_password"],
        "CACHE_STORE": "database", "SESSION_DRIVER": "database", "QUEUE_CONNECTION": "sync",
        "FILESYSTEM_DISK": "local",
        "MAIL_MAILER": "smtp", "MAIL_SCHEME": os.environ.get("MAIL_SCHEME") or "smtps",
        "MAIL_HOST": os.environ.get("MAIL_HOST") or "mail." + domain,
        "MAIL_PORT": os.environ.get("MAIL_PORT") or "465",
        "MAIL_USERNAME": "no-reply@" + domain,
        "MAIL_PASSWORD": state["mail_password"],
        "MAIL_FROM_ADDRESS": "no-reply@" + domain,
        "MAIL_FROM_NAME": os.environ.get("PROJECT_NAME") or "Laravel",
    }
    if os.environ.get("GROQ_API_KEY"):
        vals["GROQ_API_KEY"] = os.environ["GROQ_API_KEY"]
        vals["GROQ_MODEL"] = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"
    extra = os.environ.get("APP_ENV_EXTRA", "")
    protected = set(vals)
    extra_lines = []
    for line in extra.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in protected:
            raise ValueError("APP_ENV_EXTRA contains an invalid or reserved key")
        extra_lines.append(f"{key}={env_value(value)}")
    return "\n".join([*(f"{key}={env_value(value)}" for key, value in vals.items()), *extra_lines]) + "\n"


def request(url: str, *, data: bytes | None = None, headers: dict | None = None) -> bytes:
    # Match the requests client used by the verified local cPanel script.
    # TLS verification remains enabled; never print response bodies or secrets.
    try:
        response = requests.request(
            "POST" if data is not None else "GET",
            url,
            data=data,
            headers=headers or {},
            timeout=180,
            verify=True,
        )
        response.raise_for_status()
        return response.content
    except requests.exceptions.HTTPError as exc:
        parsed = urllib.parse.urlsplit(url)
        status = exc.response.status_code if exc.response is not None else "unknown"
        endpoint = f"{parsed.hostname}:{parsed.port or 443}{parsed.path}"
        raise RuntimeError(f"HTTP {status} at {endpoint}") from exc
    except requests.exceptions.RequestException as exc:
        parsed = urllib.parse.urlsplit(url)
        endpoint = f"{parsed.hostname}:{parsed.port or 443}{parsed.path}"
        raise RuntimeError(f"Connection failed at {endpoint}: {type(exc).__name__}") from exc


def upload(base: str, user: str, token: str, directory: str, name: str, payload: bytes) -> None:
    boundary = "logicstrand-" + secrets.token_hex(16)
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"dir\"\r\n\r\n{directory}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file-1\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    raw = request(base + "/execute/Fileman/upload_files", data=b"".join(chunks), headers={
        "Authorization": f"cpanel {user}:{token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    answer = json.loads(raw)
    if not answer.get("status") or answer.get("errors"):
        raise RuntimeError("cPanel upload failed: " + str(answer.get("errors") or answer.get("messages")))


def extract(base: str, user: str, token: str, archive: str, home: str) -> None:
    # Tell cPanel exactly which ZIP to extract and where. The ZIP itself contains
    # only the project's private release prefix, never a public_html path.
    archive_path = home.rstrip("/") + "/" + archive
    query = urllib.parse.urlencode({
        "cpanel_jsonapi_user": user, "cpanel_jsonapi_apiversion": "2",
        "cpanel_jsonapi_module": "Fileman", "cpanel_jsonapi_func": "fileop",
        "op": "extract", "sourcefiles": archive_path,
        "destfiles": home, "doubledecode": "1",
    })
    raw = request(base + "/json-api/cpanel?" + query, headers={"Authorization": f"cpanel {user}:{token}"})
    result = json.loads(raw).get("cpanelresult", {})
    entries = result.get("data")
    if (result.get("event", {}).get("result") != 1
            or not isinstance(entries, list) or not entries
            or any(not isinstance(item, dict) or item.get("result") != 1 or item.get("err") for item in entries)):
        raise RuntimeError("cPanel archive extraction failed or returned no file operation result")
    destination = entries[0].get("dest")
    if isinstance(destination, str) and destination not in (home, home.rstrip("/") + "/", "~"):
        raise RuntimeError("cPanel extracted the archive to an unexpected destination")


def make_archive(release: str, shared: str, project: str) -> bytes:
    prefix = f"{project}-app/releases/{release}/"
    for directory in REQUIRED_DIRS:
        if not (ROOT / directory).is_dir():
            raise ValueError(f"Missing build directory: {directory}")
    for name in REQUIRED_FILES:
        if not (ROOT / name).is_file():
            raise ValueError(f"Missing build file: {name}")
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp:
        with zipfile.ZipFile(temp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
            out.writestr(prefix + "bootstrap/cache/", "")
            for current, directories, filenames in os.walk(ROOT, followlinks=False):
                directories[:] = [name for name in directories if name not in SKIP_PARTS and not (Path(current) / name).is_symlink()]
                for name in filenames:
                    file = Path(current) / name
                    if file.is_symlink() or name in SKIP_PARTS or name == ".env" or name.startswith(".env."):
                        continue
                    relative = file.relative_to(ROOT).as_posix()
                    if any(relative == skip or relative.startswith(skip + "/") for skip in SKIP_PATHS):
                        continue
                    out.write(file, prefix + relative)
            out.writestr(prefix + "bootstrap/shared_storage.php", "<?php return " + repr_php(shared + "/storage") + ";\n")
        return Path(temp.name).read_bytes()


def verify_remote_autoload(base: str, user: str, token: str, release_path: str) -> None:
    directory = release_path + "/vendor"
    listing = uapi(base, user, token, "Fileman", "list_files", {
        "dir": directory, "only_these_files": "autoload.php", "types": "file",
    })
    entries = listing if isinstance(listing, list) else listing.get("files", []) if isinstance(listing, dict) else []
    if not any(isinstance(item, dict) and item.get("file") == "autoload.php" for item in entries):
        raise RuntimeError(
            "Archive extraction did not put vendor/autoload.php in the private release. "
            "Check the extracted release and ZIP in cPanel File Manager; activation was not attempted."
        )


def repr_php(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def uapi(base: str, user: str, token: str, module: str, function: str, params: dict | None = None) -> object:
    url = f"{base}/execute/{module}/{function}"
    mutating = function in {"create_database", "create_user", "set_privileges_on_database", "add_pop"}
    encoded = urllib.parse.urlencode(params or {})
    body = encoded.encode() if mutating else None
    if encoded and not mutating:
        url += "?" + encoded
    raw = request(url, data=body, headers={
        "Authorization": f"cpanel {user}:{token}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    response = json.loads(raw)
    result = response.get("result", response)
    if result.get("status") != 1 or result.get("errors"):
        raise RuntimeError(f"cPanel {module}/{function} failed" if function in {"create_user", "add_pop"} else f"cPanel {module}/{function} failed: " + str(result.get("errors") or result.get("messages")))
    return result.get("data")


def named_items(data: object, *fields: str) -> set[str]:
    entries = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
    values = set()
    for entry in entries:
        if isinstance(entry, str):
            values.add(entry)
        elif isinstance(entry, dict):
            for field in fields:
                if isinstance(entry.get(field), str):
                    values.add(entry[field])
                    break
    return values


def discover_public(base: str, user: str, token: str, home: str, domain: str, project: str) -> str:
    data = uapi(base, user, token, "DomainInfo", "single_domain_data", {"domain": domain})
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError(f"cPanel did not return a document root for {domain}")
    root = data.get("documentroot") or data.get("document_root")
    if not isinstance(root, str) or not root.startswith(home.rstrip("/") + "/"):
        raise ValueError(f"Document root for {domain} is outside CPANEL_HOME or unavailable")
    return safe_public_dir(home, root[len(home.rstrip("/")) + 1:], project)


def nested_domain_roots(base: str, user: str, token: str, public: str) -> list[str]:
    """Find other domains whose document roots sit inside this site's root."""
    query = urllib.parse.urlencode({
        "cpanel_jsonapi_user": user, "cpanel_jsonapi_apiversion": "2",
        "cpanel_jsonapi_module": "DomainLookup", "cpanel_jsonapi_func": "getdocroots",
    })
    raw = request(base + "/json-api/cpanel?" + query,
                  headers={"Authorization": f"cpanel {user}:{token}"})
    result = json.loads(raw).get("cpanelresult", {})
    data = result.get("data")
    if result.get("event", {}).get("result") != 1 or not isinstance(data, list):
        raise RuntimeError("Could not verify other cPanel document roots")
    prefix = public.rstrip("/") + "/"
    roots = set()
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("docroot"), str):
            raise RuntimeError("Invalid cPanel document-root listing")
        root = entry["docroot"].rstrip("/")
        if root.startswith(prefix):
            roots.add(root[len(prefix):])
    return sorted(roots)


def database_names(base: str, user: str, token: str, project: str) -> tuple[str, str]:
    restrictions = uapi(base, user, token, "Mysql", "get_restrictions")
    if not isinstance(restrictions, dict):
        raise ValueError("cPanel did not return MySQL naming restrictions")
    prefix = restrictions.get("prefix") or ""
    max_user = int(restrictions.get("max_username_length") or 32)
    allowed_suffix = max_user - len(prefix)
    if allowed_suffix < 6:
        raise ValueError("cPanel MySQL user prefix leaves too little room for this project")
    suffix = project if len(project) <= allowed_suffix else project[:allowed_suffix - 5] + "_" + hashlib.sha256(project.encode()).hexdigest()[:4]
    database = os.environ.get("CPANEL_DB_NAME") or prefix + project
    db_user = os.environ.get("CPANEL_DB_USER") or prefix + suffix
    if len(database) > int(restrictions.get("max_database_name_length") or 64) or len(db_user) > max_user:
        raise ValueError("MySQL database or username exceeds cPanel's length limit")
    if not re.fullmatch(r"[A-Za-z0-9_]+", database) or not re.fullmatch(r"[A-Za-z0-9_]+", db_user):
        raise ValueError("Invalid MySQL database or username")
    return database, db_user


def existing_resources(base: str, user: str, token: str, database: str, db_user: str, domain: str) -> tuple[bool, bool, bool]:
    databases = named_items(uapi(base, user, token, "Mysql", "list_databases"), "database", "name")
    users = named_items(uapi(base, user, token, "Mysql", "list_users"), "user", "name")
    mailboxes = named_items(uapi(base, user, token, "Email", "list_pops", {"skip_main": "1"}), "email", "login")
    return database in databases, db_user in users, "no-reply@" + domain in mailboxes


def state_filename(project: str) -> str:
    return project + "-deploy-state.json"


def read_state(base: str, user: str, token: str, home: str, project: str, domain: str) -> dict | None:
    filename = state_filename(project)
    listing = uapi(base, user, token, "Fileman", "list_files", {
        "dir": home, "only_these_files": filename, "types": "file",
    })
    entries = listing if isinstance(listing, list) else listing.get("files", []) if isinstance(listing, dict) else []
    if not any(isinstance(item, dict) and item.get("file") == filename for item in entries):
        return None
    payload = uapi(base, user, token, "Fileman", "get_file_content", {
        "dir": home, "file": filename, "to_charset": "utf-8",
    })
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError("cPanel did not return saved deployment credentials")
    state = json.loads(payload["content"])
    if not isinstance(state, dict) or state.get("version") != 1 or state.get("project") != project or state.get("domain") != domain:
        raise ValueError("Saved deployment state does not match this project and domain")
    for key in ("database", "db_user", "db_password", "mail_password", "app_key"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise ValueError("Saved deployment state is incomplete")
    if not state["app_key"].startswith("base64:"):
        raise ValueError("Saved Laravel app key is invalid")
    return state


def chmod_private(base: str, user: str, token: str, filename: str) -> None:
    query = urllib.parse.urlencode({
        "cpanel_jsonapi_user": user, "cpanel_jsonapi_apiversion": "2",
        "cpanel_jsonapi_module": "Fileman", "cpanel_jsonapi_func": "fileop",
        "op": "chmod", "sourcefiles": filename, "metadata": "0600", "doubledecode": "1",
    })
    raw = request(base + "/json-api/cpanel?" + query, headers={"Authorization": f"cpanel {user}:{token}"})
    result = json.loads(raw).get("cpanelresult", {})
    if result.get("event", {}).get("result") != 1 or any(item.get("result") != 1 for item in result.get("data", [])):
        raise RuntimeError("Could not restrict deployment state file permissions")


def prepare_state(base: str, user: str, token: str, home: str, project: str, domain: str) -> dict:
    state = read_state(base, user, token, home, project, domain)
    if state is not None:
        chmod_private(base, user, token, state_filename(project))
        return state
    database, db_user = database_names(base, user, token, project)
    if any(existing_resources(base, user, token, database, db_user, domain)):
        raise ValueError("An app database, user or no-reply mailbox already exists without saved deployment credentials; import its credentials before automated provisioning")
    state = {
        "version": 1, "project": project, "domain": domain,
        "database": database, "db_user": db_user,
        "db_password": secrets.token_urlsafe(36),
        "mail_password": secrets.token_urlsafe(36),
        "app_key": "base64:" + base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    filename = state_filename(project)
    upload(base, user, token, home, filename, json.dumps(state, separators=(",", ":")).encode())
    chmod_private(base, user, token, filename)
    print("Generated and saved private application, database and mailbox credentials")
    return state


def provision(base: str, user: str, token: str, state: dict) -> None:
    database, db_user, domain = state["database"], state["db_user"], state["domain"]
    has_db, has_user, has_mail = existing_resources(base, user, token, database, db_user, domain)
    if not has_db:
        print(f"Creating MySQL database {database}")
        uapi(base, user, token, "Mysql", "create_database", {"name": database})
    if not has_user:
        print(f"Creating MySQL user {db_user}")
        uapi(base, user, token, "Mysql", "create_user", {"name": db_user, "password": state["db_password"]})
    uapi(base, user, token, "Mysql", "set_privileges_on_database", {
        "database": database, "user": db_user, "privileges": "ALL PRIVILEGES",
    })
    if not has_mail:
        print(f"Creating mailbox no-reply@{domain}")
        uapi(base, user, token, "Email", "add_pop", {
            "domain": domain, "email": "no-reply", "password": state["mail_password"], "quota": "500",
        })


def manager_call(base: str, user: str, api_token: str, config: dict, sensitive: tuple[str, ...] = ()) -> dict:
    """Run one authenticated, self-removing PHP operation in the document root."""
    project, home, public = config["project"], config["home"], config["public"]
    nonce = secrets.token_hex(12)
    hook_token = secrets.token_urlsafe(48)
    token_name = project + "-deploy-token-" + nonce + ".php"
    hook_name = "_" + project + "_manage_" + nonce + ".php"
    token_path = home + "/" + token_name
    config = {**config, "token_file": token_path}
    upload(base, user, api_token, home, token_name,
           ("<?php return " + repr_php(hook_token) + ";\n").encode())
    template = (TOOLKIT_ROOT / "scripts/cpanel_release_manager.php").read_text()
    upload(base, user, api_token, public, hook_name,
           template.replace("__CONFIG__", php_array(config)).encode())
    try:
        response = requests.post(config["app_url"] + "/" + hook_name,
                                 data={"run": "1"},
                                 headers={"X-LogicStrand-Deploy-Token": hook_token},
                                 timeout=180, verify=True)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"{config['action']} request failed: {type(exc).__name__}") from exc
    try:
        answer = response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
        raise RuntimeError(
            f"{config['action']} returned HTTP {response.status_code} without JSON "
            f"(content-type={content_type}, body-bytes={len(response.content)})"
        ) from exc
    if not isinstance(answer, dict):
        raise RuntimeError(f"{config['action']} returned invalid JSON")
    if not response.ok or not answer.get("ok"):
        detail = str(answer.get("error") or "unknown error")
        for secret in (api_token, hook_token, os.environ.get("GROQ_API_KEY", ""), *sensitive):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        detail = " ".join(detail.replace(home, "[CPANEL_HOME]").split())[:300]
        raise RuntimeError(f"{config['action']} HTTP {response.status_code}: {detail}")
    return answer


def health_check(site_url: str) -> None:
    path = os.environ.get("CPANEL_HEALTH_PATH") or "/up"
    if not re.fullmatch(r"/[A-Za-z0-9/_-]*", path) or "//" in path or "/../" in path:
        raise ValueError("CPANEL_HEALTH_PATH must be a simple absolute path")
    url = site_url + path
    for attempt in range(3):
        try:
            response = requests.get(url, params={"deploy_check": secrets.token_hex(6)},
                                    headers={"Cache-Control": "no-cache"}, timeout=15, verify=True, allow_redirects=False)
            if response.status_code == 200:
                return
        except requests.exceptions.RequestException:
            pass
        if attempt < 2:
            time.sleep(5)
    raise RuntimeError(f"Health check failed at {path}")


def finalize_or_recover(base: str, user: str, token: str, config: dict,
                        sensitive: tuple[str, ...] = ()) -> dict:
    try:
        return manager_call(base, user, token, {**config, "action": "finalize"}, sensitive)
    except Exception as error:
        try:
            recovery = manager_call(base, user, token, {**config, "action": "restore"}, sensitive)
        except Exception as recovery_error:
            raise RuntimeError(f"Finalization failed and recovery status is unknown: {recovery_error}") from error
        if recovery.get("finalized"):
            return {"warnings": ["Finalization response failed after the release was committed; cleanup may be incomplete"]}
        if recovery.get("restored"):
            raise RuntimeError("Finalization failed; previous live files were restored") from error
        raise RuntimeError("Finalization failed and no pending backup was found; verify the live release") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy or roll back a Laravel release via cPanel")
    parser.add_argument("--rollback", type=int, metavar="STEPS", help="roll back 1-7 prior activations")
    args = parser.parse_args()
    if args.rollback is not None and not 1 <= args.rollback <= 7:
        raise ValueError("--rollback must be between 1 and 7")
    host = required("CPANEL_HOST")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
        raise ValueError("CPANEL_HOST must be a hostname without scheme or port")
    user, token = required("CPANEL_USERNAME"), required("CPANEL_API_TOKEN")
    home = required("CPANEL_HOME").rstrip("/")
    project = os.environ.get("PROJECT_SLUG", "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9]{0,19}", project):
        raise ValueError("PROJECT_SLUG must be 1-20 lowercase letters or digits")
    site_url = required("APP_URL").rstrip("/")
    parsed = urllib.parse.urlparse(site_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("APP_URL must be the HTTPS site origin without a path")
    domain = (os.environ.get("CPANEL_DOMAIN") or parsed.hostname).lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        raise ValueError("CPANEL_DOMAIN must be a valid hosted domain")
    base = f"https://{host}:2083"
    selected = os.environ.get("CPANEL_PUBLIC_DIR", "auto").strip()
    if selected in ("", "auto"):
        print(f"Discovering document root for {domain}", flush=True)
        public = discover_public(base, user, token, home, domain, project)
    else:
        public = safe_public_dir(home, selected, project)
    protected_roots = nested_domain_roots(base, user, token, public)
    operation = secrets.token_hex(12)
    common = {
        "home": home, "public": public, "project": project, "domain": domain,
        "app_url": site_url, "operation": operation,
        "protected_roots_json": json.dumps(protected_roots),
        "allow_index_replace": os.environ.get("CPANEL_ALLOW_INDEX_REPLACE", "false").lower() == "true",
    }
    if args.rollback is not None:
        print(f"Switching to retained release {args.rollback} step(s) back", flush=True)
        try:
            answer = manager_call(base, user, token, {**common, "action": "rollback", "release": "",
                                                       "steps_back": args.rollback})
            health_check(site_url)
        except Exception as error:
            try:
                manager_call(base, user, token, {**common, "action": "restore", "release": ""})
            except Exception as restore_error:
                raise RuntimeError(f"Rollback failed and recovery also failed: {restore_error}") from error
            raise
        common["release"] = answer["release"]
        result = finalize_or_recover(base, user, token, common)
        print(f"Rolled back to {answer['release']} at {site_url}")
        for warning in result.get("warnings", []):
            print(f"Cleanup warning: {warning}", file=sys.stderr)
        return

    release = (os.environ.get("GITHUB_SHA", "local")[:12] + "-" +
               os.environ.get("GITHUB_RUN_ID", "manual") + "-" +
               os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    if not re.fullmatch(r"[a-f0-9]{12}-[0-9]+-[0-9]+", release):
        raise ValueError("Invalid release ID; deployment requires a GitHub SHA and run ID")
    release_path = home + f"/{project}-app/releases/" + release
    shared = home + f"/{project}-app/shared"
    archive = make_archive(release, shared, project)
    expected_autoload = f"{project}-app/releases/{release}/vendor/autoload.php"
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        if expected_autoload not in package.namelist():
            raise RuntimeError("Build archive is missing vendor/autoload.php")
    state = prepare_state(base, user, token, home, project, domain)
    provision(base, user, token, state)
    archive_name = project + "-" + release + ".zip"
    production_env = build_env(shared, domain, state)
    print(f"Uploading release {release} ({len(archive) // 1024 // 1024} MiB)", flush=True)
    upload(base, user, token, home, archive_name, archive)
    extract(base, user, token, archive_name, home)
    verify_remote_autoload(base, user, token, release_path)
    upload(base, user, token, release_path, ".env", production_env.encode())
    common["release"] = release
    sensitive = (state["db_password"], state["mail_password"], state["app_key"])
    try:
        print("Activating release and checking site health", flush=True)
        manager_call(base, user, token, {**common, "action": "activate"}, sensitive)
        health_check(site_url)
    except Exception as error:
        try:
            manager_call(base, user, token, {**common, "action": "restore"}, sensitive)
        except Exception as restore_error:
            raise RuntimeError(f"Deployment failed and recovery also failed: {restore_error}") from error
        raise
    result = finalize_or_recover(base, user, token, common, sensitive)
    print(f"Activated {release} at {site_url}")
    for warning in result.get("warnings", []):
        print(f"Cleanup warning: {warning}", file=sys.stderr)


def php_array(data: dict) -> str:
    items = [repr_php(key) + " => " + ("true" if value is True else "false" if value is False else str(value) if isinstance(value, int) else repr_php(value)) for key, value in data.items()]
    return "[" + ", ".join(items) + "]"


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)