#!/usr/bin/env python3
"""Start all three services on one GPU and put an HTTPS address in front of each.

    python launch.py

Three uvicorn processes on 8001/8002/8003, three Cloudflare quick tunnels in
front of them, and one block of URLs printed at the end to paste into the app.

Everything here is process management. No model code lives in this file -- the
services are exactly the same programs you would run locally, and they do not
know or care that they are behind a tunnel.

Ctrl-C stops all six processes.
"""
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

SERVICES = [
    ("embedding", "embedding.app:app", 8001),
    ("stt", "stt.app:app", 8002),
    ("llm", "llm.app:app", 8003),
]

TUNNEL = os.environ.get("TUNNEL", "cloudflared").lower()   # cloudflared | ngrok | none


def _on_drive(path: Path) -> bool:
    """Is this path inside a mounted Google Drive?

    It matters more than it looks. Drive is a FUSE filesystem, and it does not
    implement two things we rely on: the byte-range locking SQLite needs, and
    chmod. A database opened there fails with "database is locked" or "disk I/O
    error", and a binary downloaded there stays non-executable.
    """
    return "/drive/" in str(path) or str(path).startswith("/content/drive")


# Somewhere real to keep working files. On Colab that is the local disk, never
# Drive -- even when the code itself is being run from Drive.
if _on_drive(HERE) and Path("/content").is_dir():
    WORK_DIR = Path("/content/edgentrag_work")
else:
    WORK_DIR = HERE
WORK_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = WORK_DIR / "logs"

_processes: list[subprocess.Popen] = []
_urls: dict[str, str] = {}
_lock = threading.Lock()

CF_URL = re.compile(r"https://[-\w]+\.trycloudflare\.com")


# --- pretty ------------------------------------------------------------------

BOLD, DIM, GREEN, RED, YELLOW, OFF = (
    ("\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m")
    if sys.stdout.isatty() else ("",) * 6
)


def say(message: str) -> None:
    print(f"{DIM}[launch]{OFF} {message}", flush=True)


def _env_file_value(key: str) -> str | None:
    """Read one setting out of .env, the way pydantic-settings would."""
    path = HERE / ".env"
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return None


# --- cleanup -----------------------------------------------------------------

def track(process: subprocess.Popen) -> subprocess.Popen:
    """Remember a child so stop_all can kill it. Called from tunnel threads too."""
    with _lock:
        _processes.append(process)
    return process


def stop_all(*_args) -> None:
    # Copy under the lock: a tunnel thread may still be appending while a
    # Ctrl-C is being handled, and iterating a list that grows underneath you
    # raises rather than stopping anything.
    with _lock:
        children = list(_processes)

    for process in children:
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
    deadline = time.time() + 5
    for process in children:
        remaining = max(0, deadline - time.time())
        try:
            process.wait(timeout=remaining)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


atexit.register(stop_all)
signal.signal(signal.SIGINT, lambda *a: (stop_all(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *a: (stop_all(), sys.exit(0)))


# --- cloudflared -------------------------------------------------------------

def ensure_cloudflared() -> str | None:
    """Return a path to the cloudflared binary, downloading it if we must."""
    found = shutil.which("cloudflared")
    if found:
        return found

    # Deliberately NOT next to the code. On Colab the code lives on Drive, and
    # Drive ignores chmod -- the binary would download fine and then refuse to
    # run, reported only as "no tunnel URL".
    local = WORK_DIR / "cloudflared"
    if local.exists() and os.access(local, os.X_OK):
        return str(local)

    if not sys.platform.startswith("linux"):
        # The published macOS build is a .tgz, not a bare binary, and grabbing
        # the Linux one would produce "Exec format error" three tunnels later.
        print(f"{YELLOW}cloudflared is not installed. On this machine, either:{OFF}")
        print(f"{DIM}    brew install cloudflared        (then re-run){OFF}")
        print(f"{DIM}    TUNNEL=none python launch.py    (local only, no tunnels){OFF}")
        return None

    machine = os.uname().machine if hasattr(os, "uname") else "x86_64"
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    url = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
           f"cloudflared-linux-{arch}")

    say(f"downloading cloudflared ({arch}) to {local.parent} …")
    try:
        urllib.request.urlretrieve(url, local)
        local.chmod(0o755)
        if not os.access(local, os.X_OK):
            print(f"{RED}{local} downloaded but is not executable — is it on a "
                  f"filesystem that ignores chmod?{OFF}")
            return None
        return str(local)
    except Exception as exc:
        print(f"{RED}could not download cloudflared: {exc}{OFF}")
        return None


def start_tunnel(name: str, port: int, binary: str) -> None:
    """Run one quick tunnel and scrape the https URL out of its output."""
    log_path = LOG_DIR / f"tunnel-{name}.log"
    handle = open(log_path, "w")
    process = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"],
        stdout=handle, stderr=subprocess.STDOUT,
    )
    track(process)

    # cloudflared prints the URL a second or two after it starts.
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            break
        try:
            match = CF_URL.search(log_path.read_text())
        except FileNotFoundError:
            match = None
        if match:
            with _lock:
                _urls[name] = match.group(0)
            return
        time.sleep(0.5)

    say(f"{RED}no tunnel URL for {name} — see {log_path}{OFF}")


def start_ngrok_tunnels() -> None:
    """Three tunnels from ONE agent.

    Not three `ngrok http` processes: a free ngrok account allows a single
    simultaneous agent session, so the second and third would die with
    ERR_NGROK_108. pyngrok drives one agent and opens three tunnels on it,
    which is the shape that works on both free and paid plans.
    """
    token = os.environ.get("NGROK_AUTHTOKEN")
    if not token:
        print(f"{RED}TUNNEL=ngrok needs NGROK_AUTHTOKEN in the environment{OFF}")
        return

    try:
        from pyngrok import conf, ngrok
    except ImportError:
        say("installing pyngrok …")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyngrok"], check=False)
        try:
            from pyngrok import conf, ngrok
        except ImportError as exc:
            print(f"{RED}could not install pyngrok: {exc}{OFF}")
            return

    try:
        conf.get_default().auth_token = token
        for name, _, port in SERVICES:
            url = ngrok.connect(port, "http").public_url
            with _lock:
                _urls[name] = url.replace("http://", "https://")
        # Nothing tracks the agent in _processes, so shut it down explicitly.
        atexit.register(ngrok.kill)
    except Exception as exc:
        print(f"{RED}ngrok failed: {exc}{OFF}")


# --- services ----------------------------------------------------------------

def start_service(name: str, target: str, port: int) -> subprocess.Popen:
    log_path = LOG_DIR / f"{name}.log"
    handle = open(log_path, "w")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", target,
         "--host", "0.0.0.0", "--port", str(port), "--workers", "1"],
        cwd=str(HERE), stdout=handle, stderr=subprocess.STDOUT, env=env,
    )
    track(process)
    return process


def _tail(path, lines: int = 6) -> list[str]:
    """The last few log lines, to save one trip to the log file."""
    try:
        content = path.read_text().strip().splitlines()
    except OSError:
        return []
    return [line[:160] for line in content[-lines:]]


def wait_for_health(port: int, process: subprocess.Popen, timeout: int = 180) -> bool:
    """Wait for a service to answer, or for it to die trying.

    Checking the process matters: a service that fails on import is never going
    to answer, and waiting the full timeout for each of three would mean nine
    minutes of silence before anything useful is printed.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


# --- main --------------------------------------------------------------------

def _settle_chroma_dir() -> str:
    """Decide where the vector database goes, and keep it off Drive.

    Chroma is SQLite underneath, and SQLite needs byte-range locking that
    Google Drive's FUSE layer does not provide. Point it at Drive and the first
    /embed fails with "database is locked" or "disk I/O error" -- a confusing
    way to lose an afternoon, because everything else about Drive works.

    The index is disposable: it is rebuilt by re-uploading the documents. So
    local disk is the right home even though the runtime will eventually take
    it away.
    """
    configured = os.environ.get("CHROMA_DIR") or _env_file_value("CHROMA_DIR")

    if configured and _on_drive(Path(configured).resolve()):
        print(f"{RED}CHROMA_DIR is on Google Drive: {configured}{OFF}")
        print(f"{YELLOW}  Chroma is SQLite, and Drive cannot do the file locking")
        print(f"  SQLite needs. Overriding it.{OFF}")
        configured = None

    if not configured and WORK_DIR != HERE:
        # We are running from Drive; the default "./_chroma" would land there.
        configured = str(WORK_DIR / "_chroma")

    if not configured:
        configured = str((HERE / "_chroma").resolve())

    os.environ["CHROMA_DIR"] = configured
    return configured


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    chroma_dir = _settle_chroma_dir()

    print(f"\n{BOLD}EdgentRAG — three services, one GPU{OFF}\n")

    has_gpu = False
    try:
        import torch

        has_gpu = torch.cuda.is_available()
        if has_gpu:
            name = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / 1024**3
            print(f"  GPU  {GREEN}{name}{OFF}  ({total:.0f} GB)")
        else:
            print(f"  GPU  {YELLOW}none found — everything will run on CPU and be slow{OFF}")
    except ImportError:
        print(f"  GPU  {YELLOW}torch not installed yet{OFF}")
    except Exception as exc:
        # A broken CUDA install raises OSError or RuntimeError here. Report it
        # rather than dying before a single service has started.
        print(f"  GPU  {YELLOW}could not be inspected: {exc}{OFF}")

    # A .env left over from running these on a laptop will pin DEVICE=cpu, and
    # then a rented A100 sits idle while everything crawls. That is a miserable
    # thing to debug, so say it out loud rather than letting it pass.
    if has_gpu:
        pinned = (os.environ.get("DEVICE") or _env_file_value("DEVICE") or "auto").lower()
        if pinned == "cpu":
            print(f"       {RED}but DEVICE=cpu is set — the GPU will not be used.{OFF}")
            print(f"       {YELLOW}Remove DEVICE from .env (the default, 'auto', "
                  f"picks the GPU).{OFF}")

    if WORK_DIR != HERE:
        print(f"  code {DIM}{HERE}{OFF}")
        print(f"  work {DIM}{WORK_DIR}{OFF}  "
              f"{DIM}(off Drive: SQLite and chmod do not work there){OFF}")
    print(f"  index {DIM}{chroma_dir}{OFF}")
    print()

    started: dict[str, subprocess.Popen] = {}
    for name, target, port in SERVICES:
        started[name] = start_service(name, target, port)
        say(f"started {name} on :{port}")

    print()
    all_up = True
    for name, _, port in SERVICES:
        ok = wait_for_health(port, started[name])
        mark = f"{GREEN}up{OFF}" if ok else f"{RED}did not come up{OFF}"
        print(f"  {name:10s} :{port}  {mark}")
        if not ok:
            all_up = False
            print(f"             {DIM}see {LOG_DIR / (name + '.log')}{OFF}")
            tail = _tail(LOG_DIR / f"{name}.log")
            for line in tail:
                print(f"             {DIM}{line}{OFF}")
    print()

    if not all_up:
        print(f"{YELLOW}Not everything started. Tunnels for a dead service are no")
        print(f"use, so fix the above first.{OFF}\n")
        stop_all()
        return 1

    # --- tunnels -------------------------------------------------------------
    if TUNNEL == "none":
        say("TUNNEL=none — services are local only")
    elif TUNNEL == "ngrok":
        say("opening ngrok tunnels …")
        start_ngrok_tunnels()
    else:
        binary = ensure_cloudflared()
        if binary:
            say("opening https tunnels …")
            threads = [threading.Thread(target=start_tunnel, args=(n, p, binary), daemon=True)
                       for n, _, p in SERVICES]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

    # --- the bit you copy ----------------------------------------------------
    print()
    if _urls:
        print(f"{BOLD}Paste these three into the app's start screen{OFF}\n")
        width = max(len(n) for n in _urls)
        for name, _, _port in SERVICES:
            url = _urls.get(name)
            if url:
                print(f"  {name.upper():>{width}}   {GREEN}{url}{OFF}")
            else:
                print(f"  {name.upper():>{width}}   {RED}(no tunnel){OFF}")
        print()
        print(f"{DIM}  These addresses die when this command stops or the Colab runtime")
        print(f"  is recycled. Restarting gives you three new ones.{OFF}")
    else:
        print(f"{YELLOW}No public URLs. The services are still running locally on")
        print(f"8001/8002/8003 — useful if you are testing on the same machine.{OFF}")

    print(f"\n{DIM}logs: {LOG_DIR}      Ctrl-C to stop everything{OFF}\n")

    # Hold the terminal open; if a service dies, say so rather than sitting mute.
    try:
        while True:
            time.sleep(5)
            for (name, _, _port), process in zip(SERVICES, _processes[:3]):
                if process.poll() is not None:
                    print(f"{RED}{name} exited (code {process.returncode}) — "
                          f"see {LOG_DIR / (name + '.log')}{OFF}")
                    return 1
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
