import os
import socket
import sys
import threading

# pythonw has no console: give stdout/stderr somewhere to go, and keep a crash log
if sys.stdout is None or sys.stderr is None:
    _d = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    os.makedirs(_d, exist_ok=True)
    log = open(os.path.join(_d, "error.log"), "a", encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log

import webview

import app as appmod
from app import app


class Api:
    """Exposed to the page as window.pywebview.api (the embedded browser can't save downloads itself)."""

    def open_url(self, url):
        import webbrowser
        if url.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)

    def save_records_pdf(self):
        import sqlite3
        conn = sqlite3.connect(appmod.DB_PATH); conn.row_factory = sqlite3.Row
        try:
            data, err = appmod.build_records_pdf(conn)
        finally:
            conn.close()
        if err:
            return {"error": err}
        path = webview.windows[0].create_file_dialog(webview.SAVE_DIALOG, save_filename="생기부.pdf",
                                                     file_types=("PDF (*.pdf)",))
        if not path:
            return {"cancelled": True}
        path = path if isinstance(path, str) else path[0]
        with open(path, "wb") as f:
            f.write(data)
        return {"path": path}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    port = free_port()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, threaded=True), daemon=True).start()
    webview.create_window("생기부 면접 대비", f"http://127.0.0.1:{port}", width=1100, height=720,
                          min_size=(800, 500), background_color="#1e1e1e", js_api=Api())
    webview.start()
