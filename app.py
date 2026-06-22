import base64
import json
import os
import re
import subprocess
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote, urlparse, urlunparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dockerize import ensure_docker_stack, has_compose, infer_stack_from_compose
from docker_ops import (
    compose_file_names,
    docker_delete_cleanup,
    docker_down,
    docker_logs,
    docker_ps,
    docker_refresh,
    docker_up,
    fix_shell_scripts,
)
from prereqs import ensure_repo_requirements

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPOS_DIR = os.path.join(BASE_DIR, "repos")
REPOS_JSON = os.path.join(DATA_DIR, "repos.json")
GITLAB_JSON = os.path.join(DATA_DIR, "gitlab.json")
GITHUB_JSON = os.path.join(DATA_DIR, "github.json")
RUNNER_PID_FILE = os.path.join(DATA_DIR, "runner.pid")
OVERRIDE_FILE = ".runner-compose.override.yml"
VERSION = "0.2.0"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(REPOS_DIR, exist_ok=True)


@asynccontextmanager
async def lifespan(_app):
    with open(RUNNER_PID_FILE, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    yield
    if os.path.exists(RUNNER_PID_FILE):
        os.remove(RUNNER_PID_FILE)


app = FastAPI(title="Git Gallery Runner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
_job_lock = threading.Lock()


def load_repos():
    if not os.path.exists(REPOS_JSON):
        return []
    with open(REPOS_JSON, "r", encoding="utf-8") as f:
        repos = json.load(f)
    for repo in repos:
        repo.setdefault("port", 3000)
        repo.setdefault("backend_port", 4000)
        repo.setdefault("branch", "")
        repo.setdefault("gitlab_target", "")
        repo.setdefault("github_username", "")
        repo.setdefault("github_token", "")
        repo.setdefault("docker_stack", None)
        repo.setdefault("auto_dockerized", False)
    return repos


def save_repos(repos):
    with open(REPOS_JSON, "w", encoding="utf-8") as f:
        json.dump(repos, f, indent=2, ensure_ascii=False)


def load_gitlab():
    if not os.path.exists(GITLAB_JSON):
        return {
            "url": "https://gitlab.com",
            "username": "",
            "token": "",
            "default_group": "",
        }
    with open(GITLAB_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("url", "https://gitlab.com")
    data.setdefault("username", "")
    data.setdefault("token", "")
    data.setdefault("default_group", "")
    return data


def save_gitlab(data):
    with open(GITLAB_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_github():
    if not os.path.exists(GITHUB_JSON):
        return {"username": "", "token": ""}
    with open(GITHUB_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("username", "")
    data.setdefault("token", "")
    return data


def save_github(data):
    with open(GITHUB_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def github_credentials(repo):
    username = (repo.get("github_username") or "").strip()
    token = (repo.get("github_token") or "").strip()
    if not token:
        global_cfg = load_github()
        username = username or global_cfg.get("username", "").strip()
        token = global_cfg.get("token", "").strip()
    if token and not username:
        username = "x-access-token"
    if token and token.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")):
        username = "x-access-token"
    return username, token


def clean_github_url(url):
    clean_url = url.strip()
    if clean_url.startswith("git@"):
        match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", clean_url)
        if match:
            host, path = match.groups()
            clean_url = f"https://{host}/{path.strip('/')}.git"
        return clean_url

    parsed = urlparse(clean_url)
    if parsed.scheme not in ("http", "https"):
        return clean_url

    netloc = parsed.netloc.split("@")[-1]
    path = parsed.path or ""
    if path and not path.endswith(".git"):
        path = f"{path.rstrip('/')}.git"
    return urlunparse((parsed.scheme, netloc, path, "", "", ""))


def authenticated_git_url(url, repo):
    username, token = github_credentials(repo)
    if not token:
        return clean_github_url(url)

    clean_url = clean_github_url(url)
    parsed = urlparse(clean_url)
    if parsed.scheme not in ("http", "https"):
        return clean_url

    netloc = parsed.netloc
    auth_netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{netloc}"
    return urlunparse((parsed.scheme, auth_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def git_auth_env(repo):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    _, token = github_credentials(repo)
    if not token:
        return env

    basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: basic {basic}"
    return env


def sanitize_log(text, repo=None):
    if not text:
        return text
    if repo:
        _, token = github_credentials(repo)
        if token:
            text = text.replace(token, "***")
        username = (repo.get("github_username") or "").strip()
        if username:
            text = text.replace(username, "***")
    global_cfg = load_github()
    if global_cfg.get("token"):
        text = text.replace(global_cfg["token"], "***")
    if global_cfg.get("username"):
        text = text.replace(global_cfg["username"], "***")
    text = re.sub(r"https://[^@\s]+@github\.com", "https://***@github.com", text)
    return text


def git_auth_hint(log):
    lowered = log.lower()
    if any(
        phrase in lowered
        for phrase in (
            "authentication failed",
            "invalid username or password",
            "repository not found",
            "could not read from remote",
            "terminal prompts disabled",
            "403",
            "401",
        )
    ):
        return (
            log
            + "\n\n--- GitHub private repo help ---\n"
            + "1. Token را در تنظیمات GitHub یا همین پروژه بگذارید (فقط Token هم کافی است).\n"
            + "2. PAT باید دسترسی Contents: Read (private repos) داشته باشد.\n"
            + "3. آدرس repo: https://github.com/owner/repo.git\n"
        )
    return log


def find_repo(repos, repo_name):
    for repo in repos:
        if repo["name"] == repo_name:
            return repo
    return None


def safe_slug(value):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = value.strip("-._")
    return value or "repo"


def repo_name_from_url(url):
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2:
        owner = safe_slug(parts[-2])
        repo = safe_slug(parts[-1].replace(".git", ""))
        return f"{owner}-{repo}"
    return safe_slug(parts[-1].replace(".git", ""))


def validate_repo_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ["http", "https", "git", "ssh"]:
        return False
    if not parsed.netloc and not url.startswith("git@"):
        return False
    return True


def parse_port(value, default):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    if 1024 <= port <= 65535:
        return port
    return default


def get_repo_ports(repo):
    frontend_port = parse_port(repo.get("port"), 3000)
    backend_port = parse_port(repo.get("backend_port"), 4000)
    return frontend_port, backend_port


def run_cmd(args, cwd=None, env=None):
    process = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        shell=False,
    )
    output = ""
    if process.stdout:
        output += process.stdout
    if process.stderr:
        output += process.stderr
    return process.returncode, output


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def repo_path(repo_name):
    return os.path.join(REPOS_DIR, repo_name)


def compose_names(path):
    return compose_file_names(path)


def write_compose_override(repo):
    path = repo_path(repo["name"])
    frontend_port, backend_port = get_repo_ports(repo)
    stack = infer_stack_from_compose(path)
    repo["docker_stack"] = stack
    override_path = os.path.join(path, OVERRIDE_FILE)

    if stack.get("kind") == "none":
        if os.path.exists(override_path):
            os.remove(override_path)
        return

    if stack.get("kind") == "multi":
        fe_service = stack.get("frontend_service", "frontend")
        be_service = stack.get("backend_service", "backend")
        fe_container = stack.get("frontend_container_port", 3000)
        be_container = stack.get("backend_container_port", 4000)
        fe_env = ""
        be_env = ""
        if stack.get("use_next_env"):
            fe_env = f"""    environment:
      NEXT_PUBLIC_API_URL: /api
      BACKEND_INTERNAL_URL: http://{be_service}:{be_container}
"""
            be_env = f"""    environment:
      FRONTEND_URL: http://localhost:{frontend_port}
"""
        elif fe_service == "admin":
            fe_env = f"""    environment:
      NEXT_PUBLIC_ARTEXX_API_URL: http://localhost:{backend_port}
"""
        override = f"""# Auto-generated by Git Gallery Runner
services:
  {fe_service}:
    ports: !override
      - "{frontend_port}:{fe_container}"
{fe_env}  {be_service}:
    ports: !override
      - "{backend_port}:{be_container}"
{be_env}"""
    elif stack.get("kind") == "single":
        service = stack.get("service", "app")
        container_port = stack.get("container_port", 3000)
        override = f"""# Auto-generated by Git Gallery Runner
services:
  {service}:
    ports: !override
      - "{frontend_port}:{container_port}"
"""
    else:
        if os.path.exists(override_path):
            os.remove(override_path)
        return

    with open(override_path, "w", encoding="utf-8") as f:
        f.write(override)


def clone_or_pull(repo):
    path = repo_path(repo["name"])
    branch = (repo.get("branch") or "").strip()
    clean_url = clean_github_url(repo["url"])
    auth_url = authenticated_git_url(repo["url"], repo)
    env = git_auth_env(repo)
    logs = []

    if os.path.exists(path):
        run_cmd(["git", "remote", "set-url", "origin", clean_url], cwd=path, env=env)
        if branch:
            code, log = run_cmd(["git", "fetch", "origin", branch], cwd=path, env=env)
            logs.append(log)
            if code != 0:
                return False, sanitize_log(git_auth_hint("".join(logs)), repo)
            code, log = run_cmd(["git", "checkout", branch], cwd=path, env=env)
            logs.append(log)
            if code != 0:
                return False, sanitize_log(git_auth_hint("".join(logs)), repo)
            code, log = run_cmd(["git", "pull", "origin", branch], cwd=path, env=env)
        else:
            code, log = run_cmd(["git", "pull", "origin"], cwd=path, env=env)
            if code != 0:
                code, log = run_cmd(["git", "pull"], cwd=path, env=env)
        logs.append(log)
        output = sanitize_log(git_auth_hint("".join(logs)), repo)
        return code == 0, output

    clone_args = ["git", "clone"]
    if branch:
        clone_args.extend(["-b", branch])
    clone_args.extend([auth_url, path])
    code, log = run_cmd(clone_args, cwd=REPOS_DIR, env=env)
    logs.append(log)
    if code == 0 and os.path.exists(path):
        run_cmd(["git", "remote", "set-url", "origin", clean_url], cwd=path, env=env)
    output = sanitize_log(git_auth_hint("".join(logs)), repo)
    return code == 0, output


def prepare_docker_stack(repo):
    path = repo_path(repo["name"])
    ok, stack, log = ensure_docker_stack(path, repo)
    if has_compose(path):
        stack = infer_stack_from_compose(path)
    if stack:
        repo["docker_stack"] = stack
    if ok and "created" in log.lower():
        repo["auto_dockerized"] = True
    return ok, log


def docker_compose(repo, command, refresh=False):
    path = repo_path(repo["name"])
    if not os.path.exists(path):
        return False, "Repo folder does not exist. First run/build the project."

    shell_fix_log = fix_shell_scripts(path)
    write_compose_override(repo)
    files = compose_names(path)
    if not files:
        return False, "docker-compose.yml not found in repo."

    project = repo["name"]

    if command == "up":
        prefix = ""
        if shell_fix_log:
            prefix = "--- shell scripts ---\n" + shell_fix_log
        if refresh:
            ok, log = docker_refresh(path, project, files)
            return ok, prefix + log
        ok, log = docker_up(path, project, files)
        return ok, prefix + log
    if command == "down":
        return docker_down(path, project, files)
    if command == "ps":
        return docker_ps(path, project, files)
    if command == "logs":
        return docker_logs(path, project, files)
    return False, "Unknown docker compose command"


def gitlab_remote_url(gitlab_cfg, target_path):
    base = gitlab_cfg["url"].rstrip("/")
    parsed = urlparse(base)
    host = parsed.netloc or parsed.path
    username = quote(gitlab_cfg["username"], safe="")
    token = quote(gitlab_cfg["token"], safe="")
    target = target_path.strip("/").replace(".git", "")
    return f"{parsed.scheme or 'https'}://{username}:{token}@{host}/{target}.git"


def push_to_gitlab(repo, gitlab_cfg, target_path):
    path = repo_path(repo["name"])
    if not os.path.exists(path):
        return False, "Repo folder does not exist."

    if not gitlab_cfg.get("username") or not gitlab_cfg.get("token"):
        return False, "GitLab username and token are required."

    if not target_path:
        return False, "GitLab target project path is required (e.g. group/project)."

    remote_url = gitlab_remote_url(gitlab_cfg, target_path)
    logs = []

    run_cmd(["git", "remote", "remove", "gitlab"], cwd=path)
    code, log = run_cmd(["git", "remote", "add", "gitlab", remote_url], cwd=path)
    logs.append(log)
    if code != 0:
        return False, "".join(logs)

    branch = (repo.get("branch") or "").strip()
    if branch:
        code, log = run_cmd(["git", "push", "-u", "gitlab", branch], cwd=path)
    else:
        code, log = run_cmd(["git", "push", "-u", "gitlab", "--all"], cwd=path)
    logs.append(log)
    if code != 0:
        return False, "".join(logs)

    code, log = run_cmd(["git", "push", "gitlab", "--tags"], cwd=path)
    logs.append(log)
    return code == 0, "".join(logs)


def update_repo_entry(repo_name, **fields):
    with _job_lock:
        repos = load_repos()
        repo = find_repo(repos, repo_name)
        if not repo:
            return None
        repo.update(fields)
        repo["updated_at"] = now_text()
        save_repos(repos)
        return repo


def execute_repo_job(repo_name, action):
    logs = []
    try:
        repos = load_repos()
        repo = find_repo(repos, repo_name)
        if not repo:
            return

        if action in {"run", "refresh"}:
            ok, git_log = clone_or_pull(repo)
            logs.append(git_log)
            if not ok:
                update_repo_entry(
                    repo_name,
                    status="error",
                    last_action="git clone/pull failed",
                    last_log="".join(logs),
                )
                return

            ok, req_log = ensure_repo_requirements(
                repo_path(repo["name"]),
                (repo.get("branch") or "").strip(),
            )
            logs.append("--- requirements.txt ---\n" + req_log)
            if not ok:
                update_repo_entry(
                    repo_name,
                    status="error",
                    last_action="requirements install failed",
                    last_log="".join(logs),
                )
                return

            ok, dockerize_log = prepare_docker_stack(repo)
            logs.append("--- auto dockerize ---\n" + dockerize_log)
            if not ok:
                update_repo_entry(
                    repo_name,
                    status="error",
                    last_action="dockerize failed",
                    last_log="".join(logs),
                    docker_stack=repo.get("docker_stack"),
                    auto_dockerized=repo.get("auto_dockerized", False),
                )
                return

            ok, compose_log = docker_compose(repo, "up", refresh=(action == "refresh"))
            logs.append("--- docker ---\n" + compose_log)
            update_repo_entry(
                repo_name,
                status="running" if ok else "error",
                last_action="pull + rebuild" if action == "refresh" else "run/build",
                last_log="".join(logs),
                docker_stack=repo.get("docker_stack"),
                auto_dockerized=repo.get("auto_dockerized", False),
            )
            return

        if action == "stop":
            ok, log = docker_compose(repo, "down")
            update_repo_entry(
                repo_name,
                status="stopped" if ok else "error",
                last_action="stop",
                last_log=log,
            )
            return

        if action == "logs":
            ok, log = docker_compose(repo, "logs")
            update_repo_entry(repo_name, last_action="logs", last_log=log)
            return

        if action == "ps":
            ok, log = docker_compose(repo, "ps")
            update_repo_entry(repo_name, last_action="status", last_log=log)
            return

        if action == "restart":
            _, stop_log = docker_compose(repo, "down")
            logs.append(stop_log)
            ok, compose_log = docker_compose(repo, "up")
            logs.append(compose_log)
            update_repo_entry(
                repo_name,
                status="running" if ok else "error",
                last_action="restart",
                last_log="".join(logs),
            )
    except Exception as exc:
        update_repo_entry(
            repo_name,
            status="error",
            last_action=f"{action} failed",
            last_log="".join(logs) + f"\n{exc}",
        )


def start_repo_job(repo_name, action):
    repos = load_repos()
    repo = find_repo(repos, repo_name)
    if not repo:
        return False
    if repo.get("status") == "busy":
        return False

    prev_log = repo.get("last_log") or ""
    update_repo_entry(
        repo_name,
        status="busy",
        last_action=f"{action}...",
        last_log=f"{prev_log}\n\n=== {action} @ {now_text()} ===\n",
    )
    threading.Thread(
        target=execute_repo_job,
        args=(repo_name, action),
        daemon=True,
    ).start()
    return True


def run_repo_action(repo_name, action, async_job=False):
    repos = load_repos()
    repo = find_repo(repos, repo_name)
    if not repo:
        return RedirectResponse("/", status_code=303)

    if async_job and action in {"run", "refresh", "restart"}:
        start_repo_job(repo_name, action)
        return RedirectResponse("/", status_code=303)

    if action in {"run", "refresh"}:
        execute_repo_job(repo_name, action)
        return RedirectResponse("/", status_code=303)

    if action == "stop":
        execute_repo_job(repo_name, "stop")
        return RedirectResponse("/", status_code=303)

    if action == "logs":
        execute_repo_job(repo_name, "logs")
        return RedirectResponse("/", status_code=303)

    if action == "ps":
        execute_repo_job(repo_name, "ps")
        return RedirectResponse("/", status_code=303)

    return RedirectResponse("/", status_code=303)


@app.get("/")
def index(request: Request):
    repos = load_repos()
    gitlab = load_gitlab()
    github = load_github()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "repos": repos, "gitlab": gitlab, "github": github, "version": VERSION},
    )


@app.post("/add")
def add_repo(
    url: str = Form(...),
    port: int = Form(3000),
    backend_port: int = Form(4000),
    branch: str = Form(""),
    gitlab_target: str = Form(""),
    github_username: str = Form(""),
    github_token: str = Form(""),
):
    url = url.strip()
    branch = branch.strip()
    gitlab_target = gitlab_target.strip()
    github_username = github_username.strip()
    github_token = github_token.strip()
    repos = load_repos()

    if not validate_repo_url(url):
        repos.append({
            "name": "invalid-url",
            "url": url,
            "port": parse_port(port, 3000),
            "backend_port": parse_port(backend_port, 4000),
            "branch": branch,
            "gitlab_target": gitlab_target,
            "github_username": github_username,
            "github_token": github_token,
            "status": "error",
            "last_action": "validation failed",
            "last_log": "Invalid git URL",
            "updated_at": now_text(),
        })
        save_repos(repos)
        return RedirectResponse("/", status_code=303)

    name = repo_name_from_url(url)
    existing_names = {repo["name"] for repo in repos}
    original_name = name
    counter = 2
    while name in existing_names:
        name = f"{original_name}-{counter}"
        counter += 1

    repos.append({
        "name": name,
        "url": url,
        "port": parse_port(port, 3000),
        "backend_port": parse_port(backend_port, 4000),
        "branch": branch,
        "gitlab_target": gitlab_target,
        "github_username": github_username,
        "github_token": github_token,
        "docker_stack": None,
        "auto_dockerized": False,
        "status": "idle",
        "last_action": "added",
        "last_log": "Repo added. Click Run / Build.",
        "updated_at": now_text(),
    })
    save_repos(repos)
    return RedirectResponse("/", status_code=303)


@app.post("/update/{repo_name}")
def update_repo(
    repo_name: str,
    port: int = Form(...),
    backend_port: int = Form(...),
    branch: str = Form(""),
    gitlab_target: str = Form(""),
    github_username: str = Form(""),
    github_token: str = Form(""),
):
    repos = load_repos()
    repo = find_repo(repos, repo_name)
    if repo:
        repo["port"] = parse_port(port, 3000)
        repo["backend_port"] = parse_port(backend_port, 4000)
        repo["branch"] = branch.strip()
        repo["gitlab_target"] = gitlab_target.strip()
        if github_username.strip():
            repo["github_username"] = github_username.strip()
        if github_token.strip():
            repo["github_token"] = github_token.strip()
        repo["updated_at"] = now_text()
        save_repos(repos)
    return RedirectResponse("/", status_code=303)


@app.post("/run/{repo_name}")
def run_repo(repo_name: str):
    return run_repo_action(repo_name, "run", async_job=True)


@app.post("/refresh/{repo_name}")
def refresh_repo(repo_name: str):
    return run_repo_action(repo_name, "refresh", async_job=True)


@app.post("/restart/{repo_name}")
def restart_repo(repo_name: str, port: int = Form(...), backend_port: int = Form(...)):
    repos = load_repos()
    repo = find_repo(repos, repo_name)
    if not repo:
        return RedirectResponse("/", status_code=303)

    repo["port"] = parse_port(port, 3000)
    repo["backend_port"] = parse_port(backend_port, 4000)
    save_repos(repos)

    start_repo_job(repo_name, "restart")
    return RedirectResponse("/", status_code=303)


@app.post("/stop/{repo_name}")
def stop_repo(repo_name: str):
    return run_repo_action(repo_name, "stop")


@app.post("/logs/{repo_name}")
def logs_repo(repo_name: str):
    return run_repo_action(repo_name, "logs")


@app.post("/ps/{repo_name}")
def ps_repo(repo_name: str):
    return run_repo_action(repo_name, "ps")


@app.post("/delete/{repo_name}")
def delete_repo(repo_name: str):
    repos = load_repos()
    path = repo_path(repo_name)
    files = compose_names(path)
    docker_delete_cleanup(path, repo_name, files)
    repos = [repo for repo in repos if repo["name"] != repo_name]
    save_repos(repos)
    return RedirectResponse("/", status_code=303)


@app.get("/api/repos")
def api_repos():
    return JSONResponse(load_repos())


@app.post("/gitlab/settings")
def gitlab_settings(
    url: str = Form("https://gitlab.com"),
    username: str = Form(""),
    token: str = Form(""),
    default_group: str = Form(""),
):
    save_gitlab({
        "url": url.strip() or "https://gitlab.com",
        "username": username.strip(),
        "token": token.strip(),
        "default_group": default_group.strip(),
    })
    return RedirectResponse("/", status_code=303)


@app.post("/github/settings")
def github_settings(
    username: str = Form(""),
    token: str = Form(""),
):
    current = load_github()
    save_github({
        "username": username.strip() or current.get("username", ""),
        "token": token.strip() or current.get("token", ""),
    })
    return RedirectResponse("/", status_code=303)


@app.post("/gitlab/push/{repo_name}")
def gitlab_push_repo(repo_name: str, target: str = Form("")):
    repos = load_repos()
    repo = find_repo(repos, repo_name)
    gitlab = load_gitlab()
    if not repo:
        return RedirectResponse("/", status_code=303)

    target_path = target.strip() or repo.get("gitlab_target", "").strip()
    if not target_path and gitlab.get("default_group"):
        target_path = f"{gitlab['default_group'].strip('/')}/{repo['name']}"

    ok, log = push_to_gitlab(repo, gitlab, target_path)
    repo["status"] = repo.get("status", "idle")
    repo["last_action"] = "gitlab push"
    repo["last_log"] = log
    repo["updated_at"] = now_text()
    if not ok:
        repo["status"] = "error"
    save_repos(repos)
    return RedirectResponse("/", status_code=303)


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"
