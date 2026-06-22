"""Install repo requirements.txt into a local venv before Docker build."""

import hashlib
import os
import subprocess
import sys

REPO_VENV_DIR = ".runner-venv"
MARKER_FILE = ".runner-requirements.sha"
SEARCH_DIRS = (".", "backend", "api", "server")


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


def _venv_python(venv_dir):
    if sys.platform == "win32":
        candidate = os.path.join(venv_dir, "Scripts", "python.exe")
    else:
        candidate = os.path.join(venv_dir, "bin", "python")
    return candidate


def _find_requirements_files(repo_path):
    found = []
    for subdir in SEARCH_DIRS:
        req_path = os.path.join(repo_path, subdir, "requirements.txt")
        if os.path.isfile(req_path):
            rel = "requirements.txt" if subdir == "." else os.path.join(subdir, "requirements.txt")
            found.append(rel.replace("\\", "/"))
    return found


def _requirements_fingerprint(repo_path, req_files):
    digest = hashlib.sha256()
    for rel in sorted(req_files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        with open(os.path.join(repo_path, rel.replace("/", os.sep)), "rb") as handle:
            digest.update(handle.read())
        digest.update(b"\0")
    return digest.hexdigest()


def _read_marker(repo_path):
    marker_path = os.path.join(repo_path, MARKER_FILE)
    if not os.path.isfile(marker_path):
        return None
    with open(marker_path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _write_marker(repo_path, fingerprint):
    marker_path = os.path.join(repo_path, MARKER_FILE)
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write(fingerprint)


def ensure_repo_requirements(repo_path, branch=""):
    logs = []
    req_files = _find_requirements_files(repo_path)
    if not req_files:
        logs.append("No requirements.txt found — skipping pip install.")
        return True, "\n".join(logs) + "\n"

    fingerprint = _requirements_fingerprint(repo_path, req_files)
    if _read_marker(repo_path) == fingerprint:
        logs.append("requirements.txt unchanged — skipping pip install.")
        return True, "\n".join(logs) + "\n"

    venv_dir = os.path.join(repo_path, REPO_VENV_DIR)
    python = _venv_python(venv_dir)

    if not os.path.isfile(python):
        logs.append(f"Creating {REPO_VENV_DIR} ...")
        code, output = _run([sys.executable, "-m", "venv", venv_dir], cwd=repo_path)
        logs.append(output.strip())
        if code != 0:
            return False, "\n".join(logs) + "\n"
        python = _venv_python(venv_dir)

    branch_note = f" (branch: {branch})" if branch else ""
    logs.append(f"Installing from requirements.txt{branch_note} ...")

    code, output = _run([python, "-m", "pip", "install", "--upgrade", "pip"], cwd=repo_path)
    logs.append(output.strip())

    for rel in req_files:
        logs.append(f"pip install -r {rel}")
        code, output = _run([python, "-m", "pip", "install", "-r", rel], cwd=repo_path)
        logs.append(output.strip())
        if code != 0:
            return False, "\n".join(logs) + "\n"

    _write_marker(repo_path, fingerprint)
    logs.append("requirements.txt installed.")
    return True, "\n".join(logs) + "\n"
