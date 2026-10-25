"""SAVI Nutrition desktop launcher.

Two modes:

  hosted (default)  Opens https://admin.savinutritions.online in an app window.
                    Nothing runs on this computer - the Namecheap server and
                    the shared TiDB database do all the work, so every computer
                    sees the same data.

  local             Starts the Flask server on this computer (both portals on
                    port 5000, local SQLite database) and opens it. Use this
                    only when the hosting is unreachable or for offline work.

Pick the mode in one of these ways (highest priority first):
  1. Command line:  desktop_app.py --local   /  desktop_app.py --hosted
  2. Environment:   set SAVI_MODE=local       /  set SAVI_MODE=hosted
  3. Config file:   desktop_config.json  ->  "mode": "local" | "hosted"
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "SAVI Nutrition"
PORT = int(os.environ.get("PORT", "5000"))
LOCAL_URL = "http://127.0.0.1:%d/" % PORT

# Defaults used when desktop_config.json is missing or unreadable.
HOSTED_DEFAULTS = {
    "mode": "hosted",
    "admin_url": "https://admin.savinutritions.online",
    "user_url": "https://user.savinutritions.online",
    "portal": "admin",
    "window_width": 1440,
    "window_height": 900,
}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config():
    data = dict(HOSTED_DEFAULTS)
    path = app_dir() / "desktop_config.json"
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if v not in (None, "")})
        except Exception:
            pass
    return data


def resolve_mode(argv, config):
    for arg in argv[1:]:
        lowered = arg.strip().lower()
        if lowered in ("--local", "local"):
            return "local"
        if lowered in ("--hosted", "hosted"):
            return "hosted"
    env_mode = os.environ.get("SAVI_MODE", "").strip().lower()
    if env_mode in ("local", "hosted"):
        return env_mode
    configured = str(config.get("mode", "")).strip().lower()
    return configured if configured in ("local", "hosted") else "hosted"


def log_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / "SaviNutrition" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def port_open(timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            return True
    except OSError:
        return False


def browser_candidates():
    env = os.environ
    program_files = env.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app_data = env.get("LOCALAPPDATA", "")
    return [
        os.path.join(program_files, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(program_files_x86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe") if local_app_data else "",
        os.path.join(program_files, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(program_files_x86, r"Microsoft\Edge\Application\msedge.exe"),
    ]


def profile_dir(portal):
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / "SaviNutrition" / ("%s-app" % portal)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def open_browser(url, portal="admin", width=1440, height=900, app_window=False):
    """Open `url` in Chrome/Edge. app_window=True removes the address bar."""
    if os.environ.get("SAVI_NO_BROWSER") == "1":
        return
    args_tail = []
    if app_window:
        args_tail = [
            "--app=%s" % url,
            "--user-data-dir=%s" % profile_dir(portal),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=TranslateUI,Translate",
            "--window-size=%d,%d" % (int(width), int(height)),
        ]
    else:
        args_tail = ["--new-window", url]
    for candidate in browser_candidates():
        if candidate and os.path.isfile(candidate):
            try:
                flags = 0
                if os.name == "nt":
                    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    )
                subprocess.Popen([candidate, *args_tail], close_fds=True, creationflags=flags)
                return
            except Exception:
                continue
    try:
        webbrowser.open(url)
    except Exception:
        pass


def notify(message):
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    except Exception:
        pass


def hosted_url(config):
    portal = str(config.get("portal") or "admin").strip().lower()
    if portal not in ("admin", "user"):
        portal = "admin"
    key = "%s_url" % portal
    url = str(config.get(key) or HOSTED_DEFAULTS[key]).strip()
    return url, portal


def run_hosted(config):
    url, portal = hosted_url(config)
    open_browser(
        url,
        portal=portal,
        width=int(config.get("window_width") or 1440),
        height=int(config.get("window_height") or 900),
        app_window=True,
    )
    return 0


def wait_and_open(timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open():
            time.sleep(1.5)
            open_browser(LOCAL_URL)
            return
        time.sleep(0.5)
    notify(
        "SAVI Nutrition could not start the local server.\n\n"
        "A log file was saved here:\n%s\n\n"
        "Close this message and try again." % (log_dir() / "server.log")
    )
    open_browser(LOCAL_URL)


def start_server():
    root = app_dir()
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("HOST", "127.0.0.1")
    log_handle = open(log_dir() / "server.log", "ab")
    log_handle.write(("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
    log_handle.flush()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, str(root / "run_production.py")],
        cwd=str(root),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    return proc, log_handle


def find_pythonw():
    """The installed Python windowless interpreter (for the EXE launcher)."""
    candidates = []
    explicit = os.environ.get("SAVI_PYTHONW", "").strip()
    if explicit:
        candidates.append(explicit)
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.append(os.path.join(local_app_data, r"Python\bin\pythonw.exe"))
        programs = os.path.join(local_app_data, "Programs", "Python")
        if os.path.isdir(programs):
            for name in sorted(os.listdir(programs), reverse=True):
                candidates.append(os.path.join(programs, name, "pythonw.exe"))
    candidates.extend([
        r"C:\Python314\pythonw.exe",
        r"C:\Python313\pythonw.exe",
        r"C:\Python312\pythonw.exe",
    ])
    on_path = shutil.which("pythonw")
    if on_path:
        candidates.append(on_path)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def launch_script_with_pythonw(argv):
    pythonw = find_pythonw()
    if not pythonw:
        notify(
            "SAVI Nutrition could not find Python on this computer.\n\n"
            "Install Python 3.10+ (with Flask) or set SAVI_PYTHONW to the "
            "pythonw.exe path, then try again."
        )
        return 1
    root = app_dir()
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen(
        [pythonw, str(root / "desktop_app.py"), *argv[1:]],
        cwd=str(root),
        creationflags=flags,
        close_fds=True,
    )
    return 0


def run_local():
    if port_open():
        open_browser(LOCAL_URL)
        return 0

    threading.Thread(target=wait_and_open, daemon=True).start()

    proc, log_handle = start_server()
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        return 0
    finally:
        try:
            log_handle.close()
        except Exception:
            pass


def main(argv):
    config = load_config()
    mode = resolve_mode(argv, config)

    if getattr(sys, "frozen", False):
        return launch_script_with_pythonw(argv)

    if mode == "local":
        return run_local()
    return run_hosted(config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
