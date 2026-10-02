#!/usr/bin/env python3
"""Package and activate a Laravel app on cPanel without SSH or deleting remote folders."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
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
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        # Keep query parameters, response bodies, and authentication headers out of logs.
        parsed = urllib.parse.urlsplit(url)
        endpoint = f"{parsed.hostname}:{parsed.port or 443}{parsed.path}"
        hint = (
            " Check that CPANEL_API_TOKEN is a cPanel token for CPANEL_USERNAME "
            "and that CPANEL_HOST is the cPanel server hostname."
            if exc.code == 403 and parsed.path.startswith(("/execute/", "/json-api/"))
            else ""
        )
        raise RuntimeError(f"HTTP {exc.code} at {endpoint}.{hint}") from exc


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


def extract(base: str, user: str, token: str, archive: str) -> None:
    query = urllib.parse.urlencode({
        "cpanel_jsonapi_user": user, "cpanel_jsonapi_apiversion": "2",
        "cpanel_jsonapi_module": "Fileman", "cpanel_jsonapi_func": "fileop",
        "op": "extract", "sourcefiles": archive, "doubledecode": "1",
    })
    raw = request(base + "/json-api/cpanel?" + query, headers={"Authorization": f"cpanel {user}:{token}"})
    result = json.loads(raw).get("cpanelresult", {})
    if result.get("event", {}).get("result") != 1 or any(item.get("result") != 1 for item in result.get("data", [])):
        raise RuntimeError("cPanel archive extraction failed: " + str(result.get("data", [])))


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


def main() -> None:
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
    release = (os.environ.get("GITHUB_SHA", "local")[:12] + "-" + os.environ.get("GITHUB_RUN_ID", "manual") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    if not re.fullmatch(r"[a-zA-Z0-9-]+", release):
        raise ValueError("Invalid release ID")
    base = f"https://{host}:2083"
    selected = os.environ.get("CPANEL_PUBLIC_DIR", "auto").strip()
    if selected in ("", "auto"):
        print(f"Checking cPanel API and discovering document root for {domain}", flush=True)
        public = discover_public(base, user, token, home, domain, project)
    else:
        public = safe_public_dir(home, selected, project)
    release_path = home + f"/{project}-app/releases/" + release
    shared = home + f"/{project}-app/shared"
    archive = make_archive(release, shared, project)
    state = prepare_state(base, user, token, home, project, domain)
    provision(base, user, token, state)
    hook_token = secrets.token_urlsafe(48)
    hook_name = "_" + project + "_activate_" + secrets.token_hex(12) + ".php"
    archive_name = project + "-" + release + ".zip"
    production_env = build_env(shared, domain, state)
    print(f"Uploading release {release} ({len(archive) // 1024 // 1024} MiB) to private home directory")
    upload(base, user, token, home, archive_name, archive)
    print("Extracting release through cPanel File Manager API")
    extract(base, user, token, archive_name)
    print("Uploading private production configuration")
    upload(base, user, token, release_path, ".env", production_env.encode())
    upload(base, user, token, release_path, "deploy_auth.php", ("<?php return " + repr_php(hook_token) + ";\n").encode())
    template = (TOOLKIT_ROOT / "scripts/cpanel_activate.php").read_text()
    config = {
        "release": release_path, "shared": shared, "public": public,
        "app_url": site_url, "project": project, "allow_index_replace": os.environ.get("CPANEL_ALLOW_INDEX_REPLACE", "false").lower() == "true",
    }
    hook = template.replace("__CONFIG__", php_array(config))
    print("Uploading protected activation endpoint")
    upload(base, user, token, public, hook_name, hook.encode())
    hook_url = site_url + "/" + hook_name
    try:
        raw = request(hook_url, data=b"activate=1", headers={
            "X-LogicStrand-Deploy-Token": hook_token,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        answer = json.loads(raw)
        if not answer.get("ok"):
            raise RuntimeError("Activation failed: " + str(answer.get("error", "unknown error")))
    except Exception:
        print("Activation did not complete. The temporary endpoint remains protected by its random name and token.", file=sys.stderr)
        raise
    print(f"Activated {release} at {site_url}")


def php_array(data: dict) -> str:
    items = [repr_php(key) + " => " + ("true" if value is True else "false" if value is False else repr_php(value)) for key, value in data.items()]
    return "[" + ", ".join(items) + "]"


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)
