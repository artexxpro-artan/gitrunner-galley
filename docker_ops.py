import os
import subprocess

OVERRIDE_FILE = ".runner-compose.override.yml"


def fix_shell_scripts(path):
    """Convert CRLF to LF in .sh files so Alpine entrypoints work after Windows git clone."""
    logs = []
    skip_dirs = {".git", "node_modules", ".next", ".runner-venv"}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if not name.endswith(".sh"):
                continue
            file_path = os.path.join(root, name)
            with open(file_path, "rb") as handle:
                data = handle.read()
            if b"\r" not in data:
                continue
            normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            with open(file_path, "wb") as handle:
                handle.write(normalized)
            rel = os.path.relpath(file_path, path)
            logs.append(f"Fixed CRLF line endings: {rel}")
    if not logs:
        return ""
    return "\n".join(logs) + "\n"


def compose_file_names(path):
    names = []
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if os.path.exists(os.path.join(path, name)):
            names.append(name)
            break
    if os.path.exists(os.path.join(path, OVERRIDE_FILE)):
        names.append(OVERRIDE_FILE)
    return names


def _run(args, cwd=None):
    process = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        shell=False,
    )
    output = (process.stdout or "") + (process.stderr or "")
    return process.returncode, output


def _compose_args(project, compose_files):
    args = ["docker", "compose", "-p", project]
    for name in compose_files:
        args.extend(["-f", name])
    return args


def docker_up(path, project, compose_files):
    code, log = _run(_compose_args(project, compose_files) + ["up", "--build", "-d"], cwd=path)
    return code == 0, log


def docker_refresh(path, project, compose_files):
    logs = []
    down_args = _compose_args(project, compose_files) + ["down", "--remove-orphans"]
    _, log = _run(down_args, cwd=path)
    logs.append("--- docker compose down --remove-orphans ---\n" + log)

    _, ids = _run(["docker", "ps", "-a", "--filter", f"name={project}", "-q"], cwd=path)
    if ids.strip():
        _, rm_log = _run(["docker", "rm", "-f", *ids.strip().splitlines()], cwd=path)
        logs.append("--- removing leftover containers ---\n" + rm_log)

    up_args = _compose_args(project, compose_files) + ["up", "--build", "-d"]
    code, log = _run(up_args, cwd=path)
    logs.append("--- docker compose up --build -d ---\n" + log)
    return code == 0, "".join(logs)


def docker_down(path, project, compose_files):
    code, log = _run(_compose_args(project, compose_files) + ["down", "--remove-orphans"], cwd=path)
    return code == 0, log


def docker_ps(path, project, compose_files):
    code, log = _run(_compose_args(project, compose_files) + ["ps"], cwd=path)
    return code == 0, log


def docker_logs(path, project, compose_files):
    code, log = _run(_compose_args(project, compose_files) + ["logs", "--tail", "200"], cwd=path)
    return code == 0, log


def docker_delete_cleanup(path, project, compose_files):
    logs = []
    if compose_files:
        _, log = _run(
            _compose_args(project, compose_files) + ["down", "--remove-orphans", "-v", "--rmi", "local"],
            cwd=path,
        )
        logs.append(log)
    _, ids = _run(["docker", "ps", "-a", "--filter", f"name={project}", "-q"], cwd=path)
    if ids.strip():
        _, rm_log = _run(["docker", "rm", "-f", *ids.strip().splitlines()], cwd=path)
        logs.append(rm_log)
    return True, "".join(logs)
