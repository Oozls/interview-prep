import base64
import hashlib
import json
import time
import mimetypes
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, request, send_from_directory

import sys

FROZEN = getattr(sys, "frozen", False)
BASE = Path(sys._MEIPASS) if FROZEN else Path(__file__).parent      # bundled code, templates, static
# user data must live outside the exe so updates (which replace the exe) never touch it
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "생기부면접대비" if FROZEN else BASE
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "data.db"
UPLOADS.mkdir(exist_ok=True)

app = Flask(__name__, template_folder=str(BASE / "templates"), static_folder=str(BASE / "static"))
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  grade INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,                 -- text | file
  content TEXT NOT NULL DEFAULT '',   -- text body, or text extracted from file (search only)
  filename TEXT NOT NULL DEFAULT '',
  stored TEXT NOT NULL DEFAULT '',
  mime TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  start INTEGER, end INTEGER,         -- text range (text materials)
  page INTEGER,                       -- page (file materials)
  quote TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS universities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  department TEXT NOT NULL DEFAULT '',
  interview_date TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '준비중',
  traits TEXT NOT NULL DEFAULT '',
  strengths TEXT NOT NULL DEFAULT '',
  memo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ocr_cache (   -- never pay twice for the same page image
  hash TEXT PRIMARY KEY,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ocr_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  filename TEXT NOT NULL DEFAULT '',
  page INTEGER NOT NULL,
  model TEXT NOT NULL,
  cached INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  grade INTEGER NOT NULL,
  subject TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  UNIQUE (grade, subject)
);
CREATE TABLE IF NOT EXISTS material_questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_answers (
  question_id INTEGER NOT NULL REFERENCES material_questions(id) ON DELETE CASCADE,
  material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  PRIMARY KEY (question_id, material_id)
);
CREATE TABLE IF NOT EXISTS tips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  university_id INTEGER NOT NULL REFERENCES universities(id) ON DELETE CASCADE,
  body TEXT NOT NULL,
  PRIMARY KEY (question_id, university_id)
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


with sqlite3.connect(DB_PATH) as _c:
    _c.executescript(SCHEMA)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def extract_text(path: Path, mime: str) -> str:
    try:
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
        if mime.startswith("text/") or path.suffix.lower() in {".md", ".csv", ".txt"}:
            raw = path.read_bytes()
            for enc in ("utf-8-sig", "cp949"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    pass
    except Exception:
        pass
    return ""


def remap_notes(conn, mid, old, new):
    """Keep notes attached to the right text after the body was edited."""
    old_n = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    p = 0
    lim = min(len(old_n), len(new))
    while p < lim and old_n[p] == new[p]:
        p += 1
    sfx = 0
    while sfx < lim - p and old_n[-1 - sfx] == new[-1 - sfx]:
        sfx += 1
    old_end = len(old_n) - sfx
    delta = len(new) - len(old_n)
    for n in conn.execute("SELECT * FROM notes WHERE material_id=? AND start IS NOT NULL", (mid,)).fetchall():
        # offsets were measured on the raw text (may contain \r\n): convert to normalized offsets
        a = n["start"] - old[:n["start"]].count("\r\n")
        b = n["end"] - old[:n["end"]].count("\r\n")
        quote = n["quote"].replace("\r\n", "\n")
        if b <= p:
            pass
        elif a >= old_end:
            a, b = a + delta, b + delta
        else:
            hits = [m.start() for m in re.finditer(re.escape(quote), new)] if quote else []
            if hits:
                a = min(hits, key=lambda x: abs(x - a))
                b = a + len(quote)
            else:
                conn.execute("UPDATE notes SET start=NULL, end=NULL, quote=? WHERE id=?", (quote, n["id"]))
                continue
        conn.execute("UPDATE notes SET start=?, end=?, quote=? WHERE id=?", (a, b, quote, n["id"]))


def subj_key(subject: str) -> str:
    """Subjects match ignoring spaces/case, so '수학I' and '수학 I' link up."""
    return "".join(subject.split()).casefold()


def snippet(text: str, terms, width=40):
    low = text.lower()
    for t in terms:
        i = low.find(t)
        if i >= 0:
            a, b = max(0, i - width), min(len(text), i + len(t) + width)
            return ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else "")
    return ""


# ---------- pages ----------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/files/<int:mid>")
def serve_file(mid):
    m = db().execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    if not m or m["kind"] != "file":
        abort(404)
    return send_from_directory(UPLOADS, m["stored"], mimetype=m["mime"] or None,
                               download_name=m["filename"], as_attachment=request.args.get("dl") == "1")


# ---------- materials ----------
@app.get("/api/materials")
def list_materials():
    q = request.args.get("q", "").strip().lower()
    grade = request.args.get("grade", "")
    subject = request.args.get("subject", "").strip()
    sql, args = "SELECT * FROM materials WHERE 1=1", []
    if grade != "":
        sql += " AND grade=?"
        args.append(int(grade))
    if subject:
        sql += " AND subject=?"
        args.append(subject)
    rows = db().execute(sql + " ORDER BY id DESC", args).fetchall()
    notes = {}
    for n in db().execute("SELECT * FROM notes").fetchall():
        notes.setdefault(n["material_id"], []).append(n)
    mans = {}
    for a in db().execute("SELECT material_id, body FROM material_answers"):
        mans.setdefault(a["material_id"], []).append(a["body"])
    terms = q.split()
    out = []
    for r in rows:
        ns = notes.get(r["id"], [])
        item = {k: r[k] for k in ("id", "title", "subject", "grade", "kind", "filename", "created_at")}
        item["note_count"] = len(ns)
        item["hit"] = ""
        if terms:
            fields = {
                "제목": r["title"], "본문": r["content"], "파일명": r["filename"],
                "노트": "\n".join(n["body"] + " " + n["quote"] for n in ns),
                "질문 답변": "\n".join(mans.get(r["id"], [])),
            }
            hay = "\n".join(fields.values()).lower()
            if not all(t in hay for t in terms):
                continue
            for label, val in fields.items():
                s = snippet(val, terms)
                if s:
                    item["hit"] = f"[{label}] {s}"
                    break
        out.append(item)
    return jsonify(out)


@app.get("/api/subjects")
def subjects():
    rows = db().execute("SELECT DISTINCT subject FROM materials WHERE subject!='' ORDER BY subject").fetchall()
    return jsonify([r[0] for r in rows])


@app.post("/api/materials")
def create_material():
    f = request.form
    title = f.get("title", "").strip()
    subject = f.get("subject", "").strip()
    grade = int(f.get("grade") or 0)
    upload = request.files.get("file")
    conn = db()
    if upload and upload.filename:
        stored = uuid.uuid4().hex + Path(upload.filename).suffix
        path = UPLOADS / stored
        upload.save(path)
        mime = upload.mimetype or ""
        if mime in ("", "application/octet-stream"):
            mime = mimetypes.guess_type(upload.filename)[0] or mime
        cur = conn.execute(
            "INSERT INTO materials(title,subject,grade,kind,content,filename,stored,mime,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (title or Path(upload.filename).stem, subject, grade, "file", extract_text(path, mime),
             upload.filename, stored, mime, now()))
    else:
        content = f.get("content", "").replace("\r\n", "\n")
        if not content.strip():
            return jsonify(error="텍스트 또는 파일이 필요합니다."), 400
        cur = conn.execute(
            "INSERT INTO materials(title,subject,grade,kind,content,created_at) VALUES(?,?,?,?,?,?)",
            (title or "(제목 없음)", subject, grade, "text", content, now()))
    conn.commit()
    return jsonify(id=cur.lastrowid)


def material_or_404(mid):
    m = db().execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
    if not m:
        abort(404)
    return m


@app.get("/api/materials/<int:mid>")
def get_material(mid):
    m = material_or_404(mid)
    d = {k: m[k] for k in ("id", "title", "subject", "grade", "kind", "filename", "mime", "created_at")}
    d["content"] = m["content"] if m["kind"] == "text" else ""
    d["notes"] = [dict(n) for n in db().execute(
        "SELECT * FROM notes WHERE material_id=? ORDER BY COALESCE(page,0), COALESCE(start,0), id", (mid,))]
    return jsonify(d)


@app.put("/api/materials/<int:mid>")
def update_material(mid):
    m = material_or_404(mid)
    j = request.get_json()
    conn = db()
    if m["kind"] == "text" and j.get("content") is not None:
        content = j["content"].replace("\r\n", "\n")
        if not content.strip():
            return jsonify(error="본문이 비어 있습니다."), 400
        if content != m["content"].replace("\r\n", "\n") or "\r\n" in m["content"]:
            remap_notes(conn, mid, m["content"], content)
            conn.execute("UPDATE materials SET content=? WHERE id=?", (content, mid))
    conn.execute("UPDATE materials SET title=?, subject=?, grade=? WHERE id=?",
                 (j.get("title", m["title"]).strip(), j.get("subject", m["subject"]).strip(), int(j.get("grade", m["grade"]) or 0), mid))
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/materials/<int:mid>")
def delete_material(mid):
    m = material_or_404(mid)
    if m["stored"]:
        (UPLOADS / m["stored"]).unlink(missing_ok=True)
    conn = db()
    conn.execute("DELETE FROM materials WHERE id=?", (mid,))
    conn.commit()
    return jsonify(ok=True)


# ---------- notes ----------
@app.post("/api/materials/<int:mid>/notes")
def create_note(mid):
    m = material_or_404(mid)
    j = request.get_json()
    body = j.get("body", "").strip()
    if not body:
        return jsonify(error="노트 내용이 비었습니다."), 400
    start = end = page = None
    quote = ""
    if m["kind"] == "text":
        start, end = int(j["start"]), int(j["end"])
        quote = m["content"][start:end]
    else:
        page = int(j.get("page") or 1)
        if j.get("start") is not None and j.get("end") is not None:      # text range inside that PDF page
            start, end, quote = int(j["start"]), int(j["end"]), str(j.get("quote") or "")[:2000]
    conn = db()
    cur = conn.execute("INSERT INTO notes(material_id,start,end,page,quote,body,created_at) VALUES(?,?,?,?,?,?,?)",
                       (mid, start, end, page, quote, body, now()))
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.put("/api/notes/<int:nid>")
def update_note(nid):
    conn = db()
    conn.execute("UPDATE notes SET body=? WHERE id=?", (request.get_json().get("body", ""), nid))
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/notes/<int:nid>")
def delete_note(nid):
    conn = db()
    conn.execute("DELETE FROM notes WHERE id=?", (nid,))
    conn.commit()
    return jsonify(ok=True)


# ---------- universities ----------
UNI_FIELDS = ("name", "department", "interview_date", "status", "traits", "strengths", "memo")


@app.get("/api/universities")
def list_unis():
    rows = db().execute("SELECT * FROM universities ORDER BY interview_date='', interview_date, id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/universities")
def create_uni():
    j = request.get_json()
    if not j.get("name", "").strip():
        return jsonify(error="대학명이 필요합니다."), 400
    conn = db()
    cur = conn.execute(f"INSERT INTO universities({','.join(UNI_FIELDS)}) VALUES({','.join('?' * len(UNI_FIELDS))})",
                       [j.get(k) or ("준비중" if k == "status" else "") for k in UNI_FIELDS])
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.put("/api/universities/<int:uid>")
def update_uni(uid):
    j = request.get_json()
    conn = db()
    conn.execute(f"UPDATE universities SET {','.join(k + '=?' for k in UNI_FIELDS)} WHERE id=?",
                 [j.get(k) or ("준비중" if k == "status" else "") for k in UNI_FIELDS] + [uid])
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/universities/<int:uid>")
def delete_uni(uid):
    conn = db()
    conn.execute("DELETE FROM universities WHERE id=?", (uid,))
    conn.commit()
    return jsonify(ok=True)


# ---------- settings (OpenRouter) ----------
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"   # ~$0.10/M in, $0.40/M out: ~$0.0005 per scanned page
OCR_PAGE_LIMIT = 40
EST_USD_PER_PAGE = 0.0006
MIN_TEXT_CHARS = 30       # a page with fewer extracted characters is treated as scanned


def get_setting(key, default=""):
    r = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def api_key():
    return get_setting("openrouter_key") or os.environ.get("OPENROUTER_API_KEY", "")


@app.get("/api/settings")
def read_settings():
    return jsonify(has_key=bool(api_key()), model=get_setting("model", DEFAULT_MODEL))


@app.put("/api/settings")
def write_settings():
    j = request.get_json()
    conn = db()
    if j.get("api_key") is not None and j["api_key"].strip():
        conn.execute("INSERT INTO settings(key,value) VALUES('openrouter_key',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (j["api_key"].strip(),))
    if j.get("clear_key"):
        conn.execute("DELETE FROM settings WHERE key='openrouter_key'")
    model = (j.get("model") or "").strip() or DEFAULT_MODEL
    conn.execute("INSERT INTO settings(key,value) VALUES('model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))
    conn.commit()
    return jsonify(ok=True)


# ---------- PDF -> text (free local extraction first, paid OCR only for scanned pages) ----------
OCR_PROMPT = ("Transcribe all text in this image exactly as written, in reading order. "
              "Keep the original language (Korean etc.). Write every mathematical expression, formula, "
              "fraction, sum, integral, subscript/superscript and Greek letter in LaTeX: $...$ inline, $$...$$ for displayed equations. "
              "Keep tables as plain text rows. Output only the transcription: no commentary, no markdown fences.")



def ocr_image(jpeg: bytes, key: str, model: str):
    body = json.dumps({
        "model": model, "temperature": 0, "max_tokens": 4000, "usage": {"include": True},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        ]}],
    }).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "X-Title": "saenggibu-interview"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenRouter 오류 {e.code}: {detail}")
    text = (j["choices"][0]["message"].get("content") or "").strip()
    u = j.get("usage") or {}
    return text, float(u.get("cost") or 0), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def or_get(path, key):
    req = urllib.request.Request("https://openrouter.ai/api/v1" + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("data") or {}


_models_cache = {"at": 0, "rows": []}
PAGE_IN_TOK, PAGE_OUT_TOK = 1500, 700   # rough tokens for one scanned page (image in, transcribed text out)


@app.get("/api/openrouter/models")
def openrouter_models():
    """Vision-capable (image in, text out) models with prices; public endpoint, cached for an hour."""
    if time.time() - _models_cache["at"] > 3600 or not _models_cache["rows"]:
        try:
            with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
                data = json.load(r)["data"]
        except Exception as e:
            return jsonify(error=f"모델 목록을 불러오지 못했습니다: {e}"), 502
        rows = []
        for m in data:
            a = m.get("architecture") or {}
            if m["id"].endswith(":batch"):     # batch-API-only variants, not usable for a live request
                continue
            if "image" not in (a.get("input_modalities") or []) or (a.get("output_modalities") or []) != ["text"]:
                continue
            try:
                pin = float(m["pricing"]["prompt"]) * 1e6
                pout = float(m["pricing"]["completion"]) * 1e6
            except (KeyError, TypeError, ValueError):
                continue
            if pin < 0 or pout < 0:       # router/meta entries with sentinel prices
                continue
            rows.append({"id": m["id"], "name": m.get("name") or m["id"], "in": round(pin, 4), "out": round(pout, 4),
                         "context": m.get("context_length") or 0,
                         "est_page": round((pin * PAGE_IN_TOK + pout * PAGE_OUT_TOK) / 1e6, 5)})
        rows.sort(key=lambda r: (r["est_page"], r["id"]))
        _models_cache.update(at=time.time(), rows=rows)
    return jsonify(_models_cache["rows"])


@app.get("/api/openrouter/usage")
def openrouter_usage():
    """Remote numbers (needs the key) + our own local log of every OCR call."""
    conn = db()
    tot = conn.execute("SELECT COALESCE(SUM(cost),0) c, COALESCE(SUM(1-cached),0) paid, COALESCE(SUM(cached),0) hit "
                       "FROM ocr_log").fetchone()
    recent = [dict(r) for r in conn.execute("SELECT * FROM ocr_log ORDER BY id DESC LIMIT 30")]
    out = {"local": {"cost": round(tot["c"], 5), "paid_pages": tot["paid"], "cached_pages": tot["hit"], "recent": recent},
           "key": None, "credits": None, "errors": []}
    key = api_key()
    if not key:
        return jsonify(out)
    try:
        out["key"] = or_get("/key", key)
    except Exception as e:
        out["errors"].append(f"키 정보 조회 실패: {e}")
    try:   # account-wide balance; OpenRouter may require a management key for this one
        c = or_get("/credits", key)
        out["credits"] = {"total": c.get("total_credits"), "used": c.get("total_usage")}
    except Exception:
        pass
    return jsonify(out)


PUA_RE = re.compile("[\ue000-\uf8ff\ufffd]+")


def garbled(t: str) -> bool:
    """Text layer exists but formulas/glyphs did not survive: private-use or replacement chars, '(cid:N)'."""
    flat = "".join(t.split())
    if not flat:
        return False
    bad = sum(len(m) for m in PUA_RE.findall(flat)) + sum(1 for ch in flat if ord(ch) < 32)
    return bad >= 3 or "(cid:" in t


def run_extraction(data: bytes, filename: str, ocr: bool, force: bool):
    """Free local text extraction first; paid OCR only for scanned/garbled pages (or every page if `force`).
    Returns (result_dict, None) or (None, (message, http_status))."""
    import pymupdf as fitz
    ext = Path(filename or "").suffix.lower().lstrip(".")
    doc = None
    if ext in ("txt", "md", "csv"):
        text = ""
        for enc in ("utf-8-sig", "cp949"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        texts = [text.replace("\r\n", "\n").strip()]
    else:
        try:
            doc = fitz.open(stream=data, filetype=ext or "pdf")
        except Exception:
            return None, ("이 형식은 텍스트로 변환할 수 없습니다. (PDF·이미지·txt/md/csv 지원)", 400)
        texts = [p.get_text().replace("\r\n", "\n").strip() for p in doc]
    scanned = [i for i, t in enumerate(texts) if doc is not None and (len(t) < MIN_TEXT_CHARS or garbled(t))]
    out = {"pages": len(texts), "scanned": [i + 1 for i in scanned], "ocr_pages": [], "cost_usd": 0,
           "est_cost_usd": round(min(len(scanned), OCR_PAGE_LIMIT) * EST_USD_PER_PAGE, 4),
           "est_all_usd": round(min(len(texts), OCR_PAGE_LIMIT) * EST_USD_PER_PAGE, 4),
           "model": get_setting("model", DEFAULT_MODEL), "has_key": bool(api_key()), "warning": ""}

    if doc is not None and (scanned or force) and ocr:
        key = api_key()
        if not key:
            return None, ("OpenRouter API 키가 설정되지 않았습니다.", 400)
        model = out["model"]
        wanted = list(range(len(texts))) if force else scanned
        targets = wanted[:OCR_PAGE_LIMIT]
        if len(wanted) > OCR_PAGE_LIMIT:
            out["warning"] = f"비용 보호를 위해 앞의 {OCR_PAGE_LIMIT}장만 OCR했습니다."
        conn, jobs = db(), []
        for i in targets:
            page = doc[i]
            scale = min(2.0, 1400 / max(page.rect.width, page.rect.height))   # small image = fewer tokens
            jpeg = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("jpeg", jpg_quality=80)
            hsh = hashlib.sha256((model + OCR_PROMPT).encode() + jpeg).hexdigest()
            row = conn.execute("SELECT text FROM ocr_cache WHERE hash=?", (hsh,)).fetchone()
            if row:
                texts[i] = row["text"]
                out["ocr_pages"].append(i + 1)
                conn.execute("INSERT INTO ocr_log(ts,filename,page,model,cached) VALUES(?,?,?,?,1)",
                             (now(), filename or "", i + 1, model))
            else:
                jobs.append((i, hsh, jpeg))
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda j: ocr_image(j[2], key, model), jobs))
        except Exception as e:
            return None, (str(e), 502)
        for (i, hsh, _), (text, cost, ptok, ctok) in zip(jobs, results):
            texts[i] = text
            out["ocr_pages"].append(i + 1)
            out["cost_usd"] += cost
            conn.execute("INSERT OR REPLACE INTO ocr_cache(hash,text) VALUES(?,?)", (hsh, text))
            conn.execute("INSERT INTO ocr_log(ts,filename,page,model,cached,cost,prompt_tokens,completion_tokens) "
                         "VALUES(?,?,?,?,0,?,?,?)", (now(), filename or "", i + 1, model, cost, ptok, ctok))
        conn.commit()
        out["cost_usd"] = round(out["cost_usd"], 5)

    left = 0
    for i, t in enumerate(texts):
        if (i + 1) not in out["ocr_pages"] and PUA_RE.search(t):
            texts[i], n = PUA_RE.subn("[수식]", t)
            left += n
    out["formulas_unread"] = left
    # where each page starts in the joined text (so page notes can be placed when converting file -> text)
    parts, starts, pos = [], [], 0
    for i, t in enumerate(texts):
        if not t:
            continue
        if parts:
            pos += 2
        starts.append({"page": i + 1, "start": pos, "end": pos + len(t)})
        parts.append(t)
        pos += len(t)
    out["text"] = "\n\n".join(parts)
    out["page_starts"] = starts
    return out, None


@app.post("/api/extract-pdf")
def extract_pdf():
    upload = request.files.get("file")
    if not upload:
        return jsonify(error="PDF 파일이 필요합니다."), 400
    out, err = run_extraction(upload.read(), upload.filename or "a.pdf", request.form.get("ocr") == "1",
                              request.form.get("force") == "1")
    return (jsonify(error=err[0]), err[1]) if err else jsonify(out)


# ---------- change an existing material's format, keeping its notes ----------
def _key(t: str) -> str:
    return re.sub(r"\s+", "", t)


@app.post("/api/materials/<int:mid>/extract")
def extract_stored(mid):
    m = material_or_404(mid)
    if m["kind"] != "file":
        return jsonify(error="파일 자료만 추출할 수 있습니다."), 400
    out, err = run_extraction((UPLOADS / m["stored"]).read_bytes(), m["filename"], request.form.get("ocr") == "1",
                              request.form.get("force") == "1")
    return (jsonify(error=err[0]), err[1]) if err else jsonify(out)


@app.post("/api/materials/<int:mid>/convert-to-text")
def convert_to_text(mid):
    m = material_or_404(mid)
    if m["kind"] != "file":
        return jsonify(error="이미 텍스트 자료입니다."), 400
    j = request.get_json()
    content = (j.get("content") or "").replace("\r\n", "\n")
    if not content.strip():
        return jsonify(error="변환할 텍스트가 비어 있습니다."), 400
    spans = {p["page"]: p for p in j.get("page_starts") or []}
    conn, placed, lost = db(), 0, 0
    for n in conn.execute("SELECT * FROM notes WHERE material_id=?", (mid,)).fetchall():
        sp = spans.get(n["page"]) if n["page"] is not None else None
        body = (f"[원본 {n['page']}쪽] " if n["page"] is not None else "") + n["body"]
        if sp and sp["end"] > sp["start"]:
            a, b = sp["start"], min(sp["end"], sp["start"] + 40)     # anchor on the first line of that page
            conn.execute("UPDATE notes SET start=?, end=?, page=NULL, quote=?, body=? WHERE id=?",
                         (a, b, content[a:b], body, n["id"]))
            placed += 1
        else:
            conn.execute("UPDATE notes SET start=NULL, end=NULL, page=NULL, body=? WHERE id=?", (body, n["id"]))
            lost += 1
    # keep the original file (moved aside) so the conversion is not destructive
    trash = UPLOADS / "_converted"
    trash.mkdir(exist_ok=True)
    src = UPLOADS / m["stored"]
    if src.exists():
        src.replace(trash / m["stored"])
    conn.execute("UPDATE materials SET kind='text', content=?, filename='', stored='', mime='' WHERE id=?",
                 (content, mid))
    conn.commit()
    return jsonify(ok=True, placed=placed, lost=lost, moved_to=f"uploads/_converted/{m['stored']}")


@app.post("/api/materials/<int:mid>/convert-to-file")
def convert_to_file(mid):
    m = material_or_404(mid)
    if m["kind"] != "text":
        return jsonify(error="이미 파일 자료입니다."), 400
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="변환할 파일을 선택하세요."), 400
    stored = uuid.uuid4().hex + Path(upload.filename).suffix
    path = UPLOADS / stored
    upload.save(path)
    mime = upload.mimetype or ""
    if mime in ("", "application/octet-stream"):
        mime = mimetypes.guess_type(upload.filename)[0] or mime
    # page text of the new file, used to find which page each note's quote lives on
    page_keys = []
    if path.suffix.lower() == ".pdf":
        try:
            import pymupdf as fitz
            page_keys = [_key(p.get_text()) for p in fitz.open(str(path))]
        except Exception:
            page_keys = []
    conn, found, lost = db(), 0, 0
    for n in conn.execute("SELECT * FROM notes WHERE material_id=?", (mid,)).fetchall():
        qk, page = _key(n["quote"] or ""), None
        for size in (40, 20, 10):
            if page is None and len(qk) >= min(size, 8):
                needle = qk[:size]
                page = next((i + 1 for i, pk in enumerate(page_keys) if needle in pk), None)
        if page is None:
            lost += 1
        else:
            found += 1
        conn.execute("UPDATE notes SET start=NULL, end=NULL, page=? WHERE id=?", (page or 1, n["id"]))
    # the text is kept as the searchable content of the file
    conn.execute("UPDATE materials SET kind='file', filename=?, stored=?, mime=? WHERE id=?",
                 (upload.filename, stored, mime, mid))
    conn.commit()
    return jsonify(ok=True, placed=found, unmatched=lost, pdf=bool(page_keys))


# ---------- 생기부 records (per grade & subject) ----------
@app.get("/api/records")
def list_records():
    q = request.args.get("q", "").strip().lower()
    grade = request.args.get("grade", "")
    sql, args = "SELECT * FROM records WHERE 1=1", []
    if grade != "":
        sql += " AND grade=?"
        args.append(int(grade))
    rows = db().execute(sql + " ORDER BY grade, subject", args).fetchall()
    terms = q.split()
    out = []
    for r in rows:
        hay = (r["subject"] + "\n" + r["content"]).lower()
        if all(t in hay for t in terms):
            d = dict(r)
            d["materials"] = [{"id": m["id"], "title": m["title"]} for m in db().execute(
                "SELECT id, title, subject FROM materials WHERE grade=? ORDER BY id", (r["grade"],))
                if subj_key(m["subject"]) == subj_key(r["subject"])]
            out.append(d)
    return jsonify(out)


def build_records_pdf(conn):
    """Every 생기부 record as one PDF, grouped by grade then subject. Returns (bytes, None) or (None, message)."""
    import html as _html
    rows = conn.execute("SELECT * FROM records ORDER BY grade, subject").fetchall()
    if not rows:
        return None, "내보낼 생기부가 없습니다."
    # AppleSDGothicNeoL00 (the app's UI font) is installed per-user; fall back to system fonts
    dirs = [Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    fonts = reg = bold = None
    for d, r, b in [(dirs[0], "AppleSDGothicNeoL.ttf", "AppleSDGothicNeoB.ttf"),
                    (dirs[1], "AppleSDGothicNeoL.ttf", "AppleSDGothicNeoB.ttf"),
                    (dirs[1], "malgun.ttf", "malgunbd.ttf"), (dirs[1], "NanumGothic.ttf", "NanumGothic.ttf")]:
        if (d / r).exists():
            fonts, reg, bold = d, r, (b if (d / b).exists() else r)
            break
    if fonts is None:
        return None, "한글 폰트를 찾을 수 없습니다."
    css = (f'@font-face {{ font-family: kr; src: url({reg}); }}'
           f'@font-face {{ font-family: kr; font-weight: bold; src: url({bold}); }}'
           'body { font-family: kr; font-size: 10pt; line-height: 1.6; color: #111; }'
           'h1 { font-size: 18pt; margin: 0 0 4pt; } h2 { font-size: 13pt; margin: 16pt 0 2pt; border-bottom: 1px solid #888; padding-bottom: 2pt; }'
           'h3 { font-size: 11pt; margin: 12pt 0 3pt; } p { margin: 0 0 5pt; } .m { color: #666; font-size: 9pt; }')
    by_grade = {}
    for r in rows:
        by_grade.setdefault(r["grade"], []).append(r)
    parts = [f'<h1>생기부</h1><p class="m">{datetime.now():%Y-%m-%d} · 과목 {len(rows)}개</p>']
    for g in [1, 2, 3, 0]:
        if g not in by_grade:
            continue
        parts.append(f'<h2>{"%d학년" % g if g else "기타"}</h2>')
        for r in by_grade[g]:
            parts.append(f'<h3>{_html.escape(r["subject"])}</h3>')
            paras = [t.strip() for t in re.split(r"\n+", r["content"]) if t.strip()]
            parts.append("".join(f"<p>{_html.escape(t)}</p>" for t in paras) or '<p class="m">(내용 없음)</p>')
    import pymupdf as fitz
    import io
    story = fitz.Story("".join(parts), user_css=css, archive=fitz.Archive(str(fonts)))
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (54, 54, -54, -60)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    # page numbers
    doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
    for i, page in enumerate(doc):
        page.insert_text((mediabox.width / 2 - 8, mediabox.height - 30), f"{i + 1} / {len(doc)}",
                         fontsize=8, fontname="helv", color=(0.4, 0.4, 0.4))
    try:
        doc.subset_fonts()
    except Exception:
        pass
    return doc.tobytes(garbage=3, deflate=True), None


@app.get("/api/records/export.pdf")
def export_records_pdf():
    from flask import Response
    data, err = build_records_pdf(db())
    if err:
        return jsonify(error=err), 400
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''%EC%83%9D%EA%B8%B0%EB%B6%80.pdf"})


@app.post("/api/records")
def save_record():
    """Create, or overwrite the entry for the same grade+subject (id given -> edit that entry)."""
    j = request.get_json()
    subject = j.get("subject", "").strip()
    if not subject:
        return jsonify(error="과목을 입력하세요."), 400
    grade = int(j.get("grade") or 0)
    content = j.get("content", "").replace("\r\n", "\n")
    conn = db()
    dup = conn.execute("SELECT id FROM records WHERE grade=? AND subject=?", (grade, subject)).fetchone()
    rid = j.get("id")
    if dup and dup["id"] != rid:
        return jsonify(error=f"{grade}학년 '{subject}' 기록이 이미 있습니다. 해당 항목을 수정하세요."), 400
    if rid:
        conn.execute("UPDATE records SET grade=?, subject=?, content=?, updated_at=? WHERE id=?",
                     (grade, subject, content, now(), rid))
    else:
        cur = conn.execute("INSERT INTO records(grade,subject,content,updated_at) VALUES(?,?,?,?)",
                           (grade, subject, content, now()))
        rid = cur.lastrowid
    conn.commit()
    return jsonify(id=rid)


@app.delete("/api/records/<int:rid>")
def delete_record(rid):
    conn = db()
    conn.execute("DELETE FROM records WHERE id=?", (rid,))
    conn.commit()
    return jsonify(ok=True)


@app.get("/api/materials/<int:mid>/record")
def material_record(mid):
    m = material_or_404(mid)
    key = subj_key(m["subject"])
    for r in db().execute("SELECT * FROM records WHERE grade=?", (m["grade"],)):
        if key and subj_key(r["subject"]) == key:
            return jsonify(record=dict(r))
    return jsonify(record=None)


@app.get("/api/record-subjects")
def record_subjects():
    rows = db().execute(
        "SELECT subject FROM records UNION SELECT subject FROM materials WHERE subject!='' ORDER BY subject")
    return jsonify([r[0] for r in rows])


# ---------- per-material questions & answers ----------
@app.get("/api/materials/<int:mid>/questions")
def list_material_questions(mid):
    material_or_404(mid)
    rows = db().execute(
        "SELECT q.id, q.text, COALESCE(a.body, '') AS answer FROM material_questions q "
        "LEFT JOIN material_answers a ON a.question_id=q.id AND a.material_id=? ORDER BY q.id", (mid,))
    return jsonify([dict(r) for r in rows])


@app.post("/api/material-questions")
def create_material_question():
    text = request.get_json().get("text", "").strip()
    if not text:
        return jsonify(error="질문을 입력하세요."), 400
    conn = db()
    cur = conn.execute("INSERT INTO material_questions(text) VALUES(?)", (text,))
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.put("/api/material-questions/<int:qid>")
def update_material_question(qid):
    text = request.get_json().get("text", "").strip()
    if not text:
        return jsonify(error="질문을 입력하세요."), 400
    conn = db()
    conn.execute("UPDATE material_questions SET text=? WHERE id=?", (text, qid))
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/material-questions/<int:qid>")
def delete_material_question(qid):
    conn = db()
    conn.execute("DELETE FROM material_questions WHERE id=?", (qid,))
    conn.commit()
    return jsonify(ok=True)


@app.put("/api/material-questions/<int:qid>/answers/<int:mid>")
def save_material_answer(qid, mid):
    body = request.get_json().get("body", "")
    conn = db()
    if body.strip():
        conn.execute("INSERT INTO material_answers(question_id,material_id,body) VALUES(?,?,?) "
                     "ON CONFLICT(question_id,material_id) DO UPDATE SET body=excluded.body", (qid, mid, body))
    else:
        conn.execute("DELETE FROM material_answers WHERE question_id=? AND material_id=?", (qid, mid))
    conn.commit()
    return jsonify(ok=True)


# ---------- common questions & per-university answers ----------
@app.get("/api/questions")
def list_questions():
    conn = db()
    answers = {}
    for a in conn.execute("SELECT * FROM answers"):
        answers.setdefault(a["question_id"], {})[str(a["university_id"])] = a["body"]
    return jsonify([{"id": q["id"], "text": q["text"], "answers": answers.get(q["id"], {})}
                    for q in conn.execute("SELECT * FROM questions ORDER BY id")])


@app.post("/api/questions")
def create_question():
    text = request.get_json().get("text", "").strip()
    if not text:
        return jsonify(error="질문을 입력하세요."), 400
    conn = db()
    cur = conn.execute("INSERT INTO questions(text) VALUES(?)", (text,))
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.put("/api/questions/<int:qid>")
def update_question(qid):
    text = request.get_json().get("text", "").strip()
    if not text:
        return jsonify(error="질문을 입력하세요."), 400
    conn = db()
    conn.execute("UPDATE questions SET text=? WHERE id=?", (text, qid))
    conn.commit()
    return jsonify(ok=True)


@app.delete("/api/questions/<int:qid>")
def delete_question(qid):
    conn = db()
    conn.execute("DELETE FROM questions WHERE id=?", (qid,))
    conn.commit()
    return jsonify(ok=True)


@app.put("/api/questions/<int:qid>/answers/<int:uid>")
def save_answer(qid, uid):
    body = request.get_json().get("body", "")
    conn = db()
    if body.strip():
        conn.execute("INSERT INTO answers(question_id,university_id,body) VALUES(?,?,?) "
                     "ON CONFLICT(question_id,university_id) DO UPDATE SET body=excluded.body", (qid, uid, body))
    else:
        conn.execute("DELETE FROM answers WHERE question_id=? AND university_id=?", (qid, uid))
    conn.commit()
    return jsonify(ok=True)


# ---------- interview tip notes ----------
@app.get("/api/tips")
def list_tips():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM tips ORDER BY id")])


@app.post("/api/tips")
def create_tip():
    d = request.get_json(silent=True) or {}
    conn = db()
    cur = conn.execute("INSERT INTO tips(title,content,updated_at) VALUES(?,?,?)",
                       (d.get("title", ""), d.get("content", ""), now()))
    conn.commit()
    return jsonify(id=cur.lastrowid)


@app.put("/api/tips/<int:tid>")
def update_tip(tid):
    d = request.get_json()
    conn = db()
    conn.execute("UPDATE tips SET title=?, content=?, updated_at=? WHERE id=?",
                 (d.get("title", ""), d.get("content", ""), now(), tid))
    conn.commit()
    return jsonify(ok=True, updated_at=now())


@app.delete("/api/tips/<int:tid>")
def delete_tip(tid):
    conn = db()
    conn.execute("DELETE FROM tips WHERE id=?", (tid,))
    conn.commit()
    return jsonify(ok=True)


# ---------- self-update (GitHub releases) ----------
import updater


@app.get("/api/update/check")
def update_check():
    return jsonify(updater.check())


@app.post("/api/update/apply")
def update_apply():
    info = updater.check()          # never trust a client-supplied URL: re-resolve the asset from the release
    if not info.get("newer") or not info.get("asset"):
        return jsonify(error=info.get("error") or "새 버전이 없습니다."), 400
    r = updater.start(info["asset"])
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.get("/api/update/status")
def update_status():
    return jsonify(updater.status())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
