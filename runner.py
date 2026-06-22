#!/usr/bin/env python3
"""Start/stop Git Gallery Runner as a background service."""

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PID_FILE = os.path.join(DATA_DIR, "runner.pid")
LOG_FILE = os.path.join(DATA_DIR, "runner.log")
HOST = "127.0.0.1"
PORT = 8080


def venv_python():
    if sys.platform == "win32":
        candidate = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
        pythonw = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
    else:
        candidate = os.path.join(BASE_DIR, ".venv", "bin", "python")
    return candidate if os.path.exists(candidate) else sys.executable


def read_pid():
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def health_ok():
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as response:
            return response.read().decode("utf-8").strip() == "ok"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def pid_alive(pid):
    if pid is None:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running():
    if health_ok():
        return True
    pid = read_pid()
    if pid_alive(pid):
        return True
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    return False


def start():
    if is_running():
        print(f"Already running — http://{HOST}:{PORT} (PID {read_pid()})")
        return 0

    os.makedirs(DATA_DIR, exist_ok=True)
    python = venv_python()
    log_handle = open(LOG_FILE, "a", encoding="utf-8")
    log_handle.write("\n--- start ---\n")
    log_handle.flush()

    popen_kwargs = {
        "cwd": BASE_DIR,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(
        [python, "-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT)],
        **popen_kwargs,
    )

    for _ in range(40):
        if health_ok():
            print(f"Started — http://{HOST}:{PORT} (PID {read_pid()})")
            print(f"Log: {LOG_FILE}")
            return 0
        time.sleep(0.25)

    print("Runner process started but health check failed. See log:", LOG_FILE)
    return 1


def stop():
    pid = read_pid()
    if not health_ok() and not pid_alive(pid):
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        print("Runner is not running.")
        return 1

    if pid is not None:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"], check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            print(f"Could not stop PID {pid}: {exc}")

    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

    print(f"Stopped (PID {pid})")
    return 0


def status():
    if is_running():
        print(f"Running — http://{HOST}:{PORT} (PID {read_pid()})")
        return 0
    print("Not running")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Git Gallery Runner background service")
    parser.add_argument("command", choices=["start", "stop", "status", "restart"])
    args = parser.parse_args()

    if args.command == "start":
        return start()
    if args.command == "stop":
        return stop()
    if args.command == "status":
        return status()
    stop()
    return start()


if __name__ == "__main__":
    raise SystemExit(main())
