"""Self-update from GitHub releases.

check()  -> asks the GitHub API for the latest release and compares it with version.__version__
start()  -> (exe builds only) downloads the release's .exe in a thread, then swaps it in and relaunches
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from version import GITHUB_REPO, __version__

FROZEN = getattr(sys, "frozen", False)
_state = {"phase": "idle", "done": 0, "total": 0, "error": ""}      # idle | downloading | installing | error
_lock = threading.Lock()


def _ver(v):
    out = []
    for p in v.lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "interview-prep-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check():
    info = {"current": __version__, "newer": False, "can_apply": FROZEN}
    if GITHUB_REPO.startswith("OWNER/"):
        return {**info, "error": "저장소가 설정되지 않았습니다."}
    try:
        with _get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest") as r:
            rel = json.load(r)
    except Exception as e:                                            # offline, rate limit, no release yet ...
        return {**info, "error": f"업데이트를 확인하지 못했습니다. ({e})"}
    latest = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
    return {**info, "latest": latest.lstrip("vV"), "newer": _ver(latest) > _ver(__version__) and asset is not None,
            "notes": rel.get("body") or "", "url": rel.get("html_url", ""),
            "asset": asset and {"url": asset["browser_download_url"], "size": asset.get("size", 0),
                                "digest": asset.get("digest") or ""}}


def status():
    with _lock:
        return dict(_state)


def _set(**kw):
    with _lock:
        _state.update(kw)


def _run(asset):
    try:
        exe = Path(sys.executable)
        tmp = Path(tempfile.mkdtemp(prefix="ip-update-"))
        new = tmp / "new.exe"
        h = hashlib.sha256()
        _set(phase="downloading", done=0, total=asset.get("size", 0), error="")
        with _get(asset["url"], timeout=30) as r, open(new, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk); h.update(chunk)
                _set(done=_state["done"] + len(chunk))
        if asset.get("size") and new.stat().st_size != asset["size"]:
            raise RuntimeError("다운로드한 파일 크기가 맞지 않습니다.")
        digest = asset.get("digest", "")
        if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
            raise RuntimeError("다운로드한 파일의 해시가 맞지 않습니다.")
        _set(phase="installing")
        # the running exe is locked: a helper script waits until it can be replaced, swaps it, relaunches, deletes itself
        bat = tmp / "update.bat"
        bat.write_text(
            "@echo off\r\nset n=0\r\n:retry\r\n"
            f'move /y "{new}" "{exe}" >nul 2>&1\r\n'
            "if not errorlevel 1 goto done\r\nset /a n+=1\r\nif %n% geq 90 exit /b 1\r\n"
            "ping -n 2 127.0.0.1 >nul\r\ngoto retry\r\n:done\r\n"
            f'start "" "{exe}"\r\n(goto) 2>nul & del "%~f0"\r\n', encoding="mbcs")
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x08000000 | 0x00000008, close_fds=True)   # no window, detached
        threading.Timer(0.8, lambda: os._exit(0)).start()
    except Exception as e:
        _set(phase="error", error=str(e))


def start(asset):
    if not FROZEN:
        return {"error": "exe로 설치한 버전에서만 자동 업데이트할 수 있습니다."}
    with _lock:
        if _state["phase"] in ("downloading", "installing"):
            return {"ok": True}
    threading.Thread(target=_run, args=(asset,), daemon=True).start()
    return {"ok": True}
