"""Run `clip live --wait` for a channel in the background, forever (macOS LaunchAgent).

launchd starts it at login and restarts it whenever it exits, so after each stream
ends it goes straight back to waiting for the next one.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .util import slugify

AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.clipper.live."


def _label(url: str) -> str:
    return PREFIX + slugify(url.split("://", 1)[-1], 60)


def _target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _log_path(label: str) -> Path:
    return ROOT / "work" / f"autolive-{label.removeprefix(PREFIX)}.log"


def _require_macos() -> None:
    if sys.platform != "darwin":
        sys.exit("`clip autolive` uses macOS launchd. On Linux/Windows, run "
                 "`clip live <url> --wait` in a loop from systemd, Task Scheduler or a terminal instead.")


def install(url: str, extra: list[str]) -> None:
    _require_macos()
    label = _label(url)
    plist = AGENTS / f"{label}.plist"
    bindir = Path(sys.executable).parent
    log = _log_path(label)
    env_path = ":".join([str(bindir), "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"),
                         "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    data = {
        "Label": label,
        "ProgramArguments": [str(bindir / "clip"), "live", url, "--wait", *extra],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,          # restart after each stream ends -> wait for the next
        "ThrottleInterval": 60,
        "ExitTimeOut": 120,         # time to finish the last clips when stopped
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": env_path, "PYTHONUNBUFFERED": "1"},
    }
    AGENTS.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", _target(label)], capture_output=True)  # replace an older copy
    plist.write_bytes(plistlib.dumps(data))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"Installed {label}\n  runs: clip live {url} --wait {' '.join(extra)}\n  log:  {log}\n"
          f"  stop: uv run clip autolive remove {url}")


def remove(url: str) -> None:
    label = _label(url)
    subprocess.run(["launchctl", "bootout", _target(label)], capture_output=True)
    plist = AGENTS / f"{label}.plist"
    existed = plist.exists()
    plist.unlink(missing_ok=True)
    print(f"Removed {label}" if existed else f"No background job found for {url}")


def agents() -> list[dict]:
    out: list[dict] = []
    if sys.platform != "darwin":
        return out
    for p in sorted(AGENTS.glob(PREFIX + "*.plist")):
        label = p.stem
        args = plistlib.loads(p.read_bytes()).get("ProgramArguments", [])
        r = subprocess.run(["launchctl", "print", _target(label)], capture_output=True, text=True)
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        state = next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("state =")), "not loaded")
        pid = next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("pid =")), "")
        log = _log_path(label)
        tail = []
        if log.exists() and log.stat().st_size:
            with open(log, "rb") as fh:
                fh.seek(max(0, log.stat().st_size - 4000))
                tail = fh.read().decode(errors="replace").strip().splitlines()[-8:]
        out.append({"label": label, "url": args[2] if len(args) > 2 else "", "state": state, "pid": pid,
                    "log": str(log), "tail": tail})
    return out


def status() -> None:
    items = agents()
    if not items:
        print("No autolive jobs installed.")
    for a in items:
        print(f"{a['label']}\n  url:   {a['url']}\n  state: {a['state']} (pid {a['pid'] or '-'})\n"
              f"  log:   {a['log']}\n  last:  {a['tail'][-1] if a['tail'] else ''}")
