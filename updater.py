"""Self-update from GitHub releases.

check()  -> asks the GitHub API for the latest release and compares it with version.__version__
start()  -> (exe builds only) downloads the release .zip in a thread, then overlays it onto the app folder and relaunches.
          data.db / uploads / error.log live in that folder and are never overwritten.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from version import GITHUB_REPO, __version__

FROZEN = getattr(sys, "frozen", False)
API_BASE = os.environ.get("IP_UPDATE_API", "https://api.github.com")      # overridable for offline tests
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
    if GITHUB_REPO.startswith("OWNER/") and API_BASE == "https://api.github.com":
        return {**info, "error": "저장소가 설정되지 않았습니다."}
    try:
        with _get(f"{API_BASE}/repos/{GITHUB_REPO}/releases/latest") as r:
            rel = json.load(r)
    except Exception as e:                                            # offline, rate limit, no release yet ...
        return {**info, "error": f"업데이트를 확인하지 못했습니다. ({e})"}
    latest = rel.get("tag_name", "")
    asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".zip")), None)
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
        app_dir = Path(sys.executable).parent
        tmp = Path(tempfile.mkdtemp(prefix="ip-update-"))
        zpath = tmp / "new.zip"
        h = hashlib.sha256()
        _set(phase="downloading", done=0, total=asset.get("size", 0), error="")
        with _get(asset["url"], timeout=30) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk); h.update(chunk)
                _set(done=_state["done"] + len(chunk))
        if asset.get("size") and zpath.stat().st_size != asset["size"]:
            raise RuntimeError("다운로드한 파일 크기가 맞지 않습니다.")
        digest = asset.get("digest", "")
        if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
            raise RuntimeError("다운로드한 파일의 해시가 맞지 않습니다.")
        _set(phase="installing")
        src = tmp / "src"
        with zipfile.ZipFile(zpath) as z:
            root = (tmp / "src").resolve()
            for n in z.namelist():                                  # zip-slip guard
                if not (root / n).resolve().is_relative_to(root):
                    raise RuntimeError("압축 파일 경로가 올바르지 않습니다.")
            z.extractall(src)
        tops = [p for p in src.iterdir()]
        if len(tops) == 1 and tops[0].is_dir():                     # zip wraps everything in one folder
            src = tops[0]
        if not (src / Path(sys.executable).name).exists():
            raise RuntimeError("압축 파일에 실행 파일이 없습니다.")
        # the running exe is locked: a helper waits for this process to exit, then overlays the new files
        # (user data is excluded), relaunches, and cleans up
        pid, exe = os.getpid(), app_dir / Path(sys.executable).name
        bat = tmp / "update.bat"
        script = [
            "@echo off",
            "set n=0",
            ":wait",
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul',
            "if errorlevel 1 goto go",
            "set /a n+=1",
            "if %n% geq 90 exit /b 1",
            "ping -n 2 127.0.0.1 >nul",
            "goto wait",
            ":go",
            f'if exist "{app_dir}\\_internal" rmdir /s /q "{app_dir}\\_internal"',
            f'robocopy "{src}" "{app_dir}" /E /R:5 /W:1 /XF data.db data.db-wal data.db-shm error.log /XD uploads >nul',
            f'start "" "{exe}"',
            f'(goto) 2>nul & rmdir /s /q "{tmp}"',
        ]
        bat.write_text("\r\n".join(script) + "\r\n", encoding="mbcs")
        # windowed exe has no std handles: give the helper explicit ones, or cmd dies on start
        subprocess.Popen(["cmd", "/c", str(bat)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x08000000 | 0x00000200)      # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        threading.Timer(0.8, lambda: os._exit(0)).start()
    except Exception as e:
        _set(phase="error", error=str(e))


def start(asset):
    if not FROZEN:
        return {"error": "배포(zip) 버전에서만 자동 업데이트할 수 있습니다."}
    with _lock:
        if _state["phase"] in ("downloading", "installing"):
            return {"ok": True}
    threading.Thread(target=_run, args=(asset,), daemon=True).start()
    return {"ok": True}
