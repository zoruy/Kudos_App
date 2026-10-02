"""Small, dependency-free Kudos application for local/internal deployment."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KUDOS_DB", BASE_DIR / "kudos.db"))
SESSION_TTL_SECONDS = 60 * 60 * 8
MAX_MESSAGE_LENGTH = 500
RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60 * 60
DUPLICATE_WINDOW_SECONDS = 5 * 60


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_message(message: str) -> str:
    return " ".join(message.strip().split()).casefold()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def database(db_path: Path | None = None):
    """Commit successful work and always release the SQLite file handle."""
    db = connect(db_path)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialise_database(db_path: Path | None = None) -> None:
    db_path = db_path or DB_PATH
    with database(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              display_name TEXT NOT NULL,
              email TEXT NOT NULL UNIQUE,
              team TEXT,
              is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
              role TEXT NOT NULL DEFAULT 'employee' CHECK (role IN ('employee', 'admin'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL REFERENCES users(id),
              csrf_token TEXT NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kudos (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sender_id INTEGER NOT NULL REFERENCES users(id),
              recipient_id INTEGER NOT NULL REFERENCES users(id),
              message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 500),
              message_normalized TEXT NOT NULL,
              is_visible INTEGER NOT NULL DEFAULT 1 CHECK (is_visible IN (0, 1)),
              moderated_by INTEGER REFERENCES users(id),
              moderated_at TEXT,
              reason_for_moderation TEXT,
              created_at TEXT NOT NULL,
              deleted_at TEXT,
              deleted_by INTEGER REFERENCES users(id),
              CHECK (sender_id <> recipient_id),
              CHECK (deleted_at IS NULL OR is_visible = 0)
            );
            CREATE INDEX IF NOT EXISTS kudos_feed_idx
              ON kudos(is_visible, deleted_at, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS kudos_sender_idx ON kudos(sender_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS kudos_moderation_idx
              ON kudos(is_visible, deleted_at, created_at DESC);
            CREATE TABLE IF NOT EXISTS kudos_moderation_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kudos_id INTEGER NOT NULL REFERENCES kudos(id),
              action TEXT NOT NULL CHECK (action IN ('hidden', 'restored', 'deleted')),
              actor_id INTEGER NOT NULL REFERENCES users(id),
              reason TEXT,
              created_at TEXT NOT NULL
            );
            """
        )
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO users (display_name, email, team, role) VALUES (?, ?, ?, ?)",
                [
                    ("Avery Chen", "avery.chen@example.internal", "Engineering", "employee"),
                    ("Jordan Patel", "jordan.patel@example.internal", "Design", "employee"),
                    ("Morgan Williams", "morgan.williams@example.internal", "Operations", "employee"),
                    ("Riley Taylor", "riley.taylor@example.internal", "People", "admin"),
                ],
            )


def response(start_response, status: str, body: Any = None, headers=None):
    headers = list(headers or [])
    if body is None:
        start_response(status, headers)
        return [b""]
    if isinstance(body, (dict, list)):
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers.append(("Content-Type", "application/json; charset=utf-8"))
    else:
        raw = str(body).encode()
        headers.append(("Content-Type", "text/html; charset=utf-8"))
    headers.append(("Content-Length", str(len(raw))))
    headers.extend([("X-Content-Type-Options", "nosniff"), ("Cache-Control", "no-store")])
    start_response(status, headers)
    return [raw]


def error(start_response, status: str, code: str, message: str, fields=None):
    return response(start_response, status, {"error": {"code": code, "message": message, "fields": fields or {}}})


def read_json(environ) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length > 20_000:
            raise ValueError
        return json.loads(environ["wsgi.input"].read(length) or b"{}")
    except (ValueError, json.JSONDecodeError):
        raise ValueError("Invalid JSON request body.")


def cookie(environ, name: str) -> str | None:
    for item in environ.get("HTTP_COOKIE", "").split(";"):
        key, _, value = item.strip().partition("=")
        if key == name:
            return value
    return None


def current_user(environ, db: sqlite3.Connection):
    session_id = cookie(environ, "kudos_session")
    if not session_id:
        return None
    row = db.execute(
        """SELECT u.*, s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.id=? AND s.expires_at>? AND u.is_active=1""",
        (session_id, int(time.time())),
    ).fetchone()
    return row


def require_user(environ, start_response, db, admin=False, csrf=False):
    user = current_user(environ, db)
    if not user:
        return None, error(start_response, "401 Unauthorized", "UNAUTHENTICATED", "Sign in is required.")
    if admin and user["role"] != "admin":
        return None, error(start_response, "403 Forbidden", "FORBIDDEN", "Administrator permission is required.")
    if csrf and not hmac.compare_digest(environ.get("HTTP_X_CSRF_TOKEN", ""), user["csrf_token"]):
        return None, error(start_response, "403 Forbidden", "CSRF_INVALID", "The request could not be verified.")
    return user, None


def kudos_dict(row, include_moderation=False):
    result = {
        "id": row["id"], "sender": {"id": row["sender_id"], "displayName": row["sender_name"]},
        "recipient": {"id": row["recipient_id"], "displayName": row["recipient_name"]},
        "message": row["message"], "createdAt": row["created_at"],
    }
    if include_moderation:
        result.update({"isVisible": bool(row["is_visible"]), "moderatedBy": row["moderated_by"],
                       "moderatedAt": row["moderated_at"], "reasonForModeration": row["reason_for_moderation"]})
    return result


KUDOS_SELECT = """SELECT k.*, s.display_name sender_name, r.display_name recipient_name
FROM kudos k JOIN users s ON s.id=k.sender_id JOIN users r ON r.id=k.recipient_id"""


def encode_cursor(created_at: str, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{created_at}|{row_id}".encode()).decode().rstrip("=")


def decode_cursor(value: str):
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        created_at, row_id = decoded.rsplit("|", 1)
        return created_at, int(row_id)
    except Exception:
        raise ValueError("Invalid pagination cursor.")


def page_kudos(db, where: str, params: list, query: dict, moderation=False):
    try:
        limit = min(max(int(query.get("limit", ["20"])[0]), 1), 20)
    except ValueError:
        limit = 20
    cursor = query.get("cursor", [None])[0]
    if cursor:
        created, row_id = decode_cursor(cursor)
        where += " AND (k.created_at < ? OR (k.created_at = ? AND k.id < ?))"
        params += [created, created, row_id]
    rows = db.execute(KUDOS_SELECT + " WHERE " + where + " ORDER BY k.created_at DESC, k.id DESC LIMIT ?", params + [limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [kudos_dict(r, moderation) for r in rows],
            "nextCursor": encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if has_more else None}


def html_page(title: str, content: str, user=None, csrf="") -> str:
    nav = ""
    if user:
        admin_link = '<a href="/admin">Moderation</a>' if user["role"] == "admin" else ""
        nav = f'<nav><a href="/">Dashboard</a>{admin_link}<span>{html.escape(user["display_name"])}</span><form method="post" action="/logout"><button>Sign out</button></form></nav>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="csrf-token" content="{html.escape(csrf)}"><title>{html.escape(title)}</title><link rel="stylesheet" href="/static/styles.css"></head><body>{nav}<main>{content}</main><script src="/static/app.js"></script></body></html>"""


def app(environ, start_response):
    initialise_database()
    method, path = environ["REQUEST_METHOD"], environ.get("PATH_INFO", "/")
    query = parse_qs(environ.get("QUERY_STRING", ""))
    with database() as db:
        if method == "GET" and path == "/static/styles.css":
            data = (BASE_DIR / "static" / "styles.css").read_bytes()
            start_response("200 OK", [("Content-Type", "text/css; charset=utf-8"), ("Content-Length", str(len(data)))])
            return [data]
        if method == "GET" and path == "/static/app.js":
            data = (BASE_DIR / "static" / "app.js").read_bytes()
            start_response("200 OK", [("Content-Type", "application/javascript; charset=utf-8"), ("Content-Length", str(len(data)))])
            return [data]
        if method == "GET" and path == "/login":
            users = db.execute("SELECT id, display_name, role FROM users WHERE is_active=1 ORDER BY display_name").fetchall()
            options = "".join(f'<option value="{u["id"]}">{html.escape(u["display_name"])} ({u["role"]})</option>' for u in users)
            body = f'<section class="auth"><h1>Internal Kudos</h1><p>Development sign-in</p><form method="post" action="/login"><label>Employee<select name="user_id">{options}</select></label><button>Sign in</button></form></section>'
            return response(start_response, "200 OK", html_page("Sign in", body))
        if method == "POST" and path == "/login":
            size = int(environ.get("CONTENT_LENGTH") or 0)
            form = parse_qs(environ["wsgi.input"].read(size).decode())
            try: user_id = int(form.get("user_id", [""])[0])
            except ValueError: return response(start_response, "303 See Other", "", [("Location", "/login")])
            user = db.execute("SELECT id FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
            if not user: return response(start_response, "303 See Other", "", [("Location", "/login")])
            session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (session_id, user_id, csrf, int(time.time()) + SESSION_TTL_SECONDS))
            return response(start_response, "303 See Other", "", [("Location", "/"), ("Set-Cookie", f"kudos_session={session_id}; HttpOnly; SameSite=Lax; Path=/")])
        if method == "POST" and path == "/logout":
            sid = cookie(environ, "kudos_session")
            if sid: db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            return response(start_response, "303 See Other", "", [("Location", "/login"), ("Set-Cookie", "kudos_session=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/")])

        if method == "GET" and path == "/":
            user = current_user(environ, db)
            if not user:
                return response(start_response, "303 See Other", "", [("Location", "/login")])
            body = '''<header><h1>Celebrate great work</h1><p>Send a colleague a note of appreciation.</p></header><section class="panel"><h2>Give kudos</h2><form id="kudos-form"><label for="recipient">Colleague</label><select id="recipient" required><option value="">Choose a colleague</option></select><label for="message">Message <span id="count">0/500</span></label><textarea id="message" maxlength="500" required></textarea><p id="form-error" class="error" role="alert"></p><button id="submit-kudos">Send kudos</button><p id="form-success" class="success" role="status"></p></form></section><section><h2>Recent kudos</h2><div id="feed" aria-live="polite"></div><button id="load-more" hidden>Load more</button></section>'''
            return response(start_response, "200 OK", html_page("Kudos dashboard", body, user, user["csrf_token"]))
        if method == "GET" and path == "/admin":
            user = current_user(environ, db)
            if not user:
                return response(start_response, "303 See Other", "", [("Location", "/login")])
            if user["role"] != "admin":
                return response(start_response, "403 Forbidden", html_page("Forbidden", "<h1>Forbidden</h1><p>Administrator permission is required.</p>", user, user["csrf_token"]))
            body = '''<header><h1>Kudos moderation</h1><p>Hide, restore, or delete content that does not meet internal standards.</p></header><section><label for="status-filter">Show<select id="status-filter"><option value="all">Visible and hidden</option><option value="visible">Visible</option><option value="hidden">Hidden</option></select></label><div id="moderation-list"></div></section>'''
            return response(start_response, "200 OK", html_page("Kudos moderation", body, user, user["csrf_token"]))

        if method == "GET" and path == "/api/kudos/recipients":
            user, failed = require_user(environ, start_response, db)
            if failed: return failed
            q = query.get("q", [""])[0].strip()
            rows = db.execute("SELECT id, display_name, team FROM users WHERE is_active=1 AND id<>? AND (display_name LIKE ? OR email LIKE ?) ORDER BY display_name LIMIT 20", (user["id"], f"%{q}%", f"%{q}%")).fetchall()
            return response(start_response, "200 OK", {"items": [{"id": r["id"], "displayName": r["display_name"], "team": r["team"]} for r in rows]})
        if method == "GET" and path == "/api/kudos":
            user, failed = require_user(environ, start_response, db)
            if failed: return failed
            try: return response(start_response, "200 OK", page_kudos(db, "k.is_visible=1 AND k.deleted_at IS NULL", [], query))
            except ValueError as exc: return error(start_response, "400 Bad Request", "INVALID_CURSOR", str(exc))
        if method == "POST" and path == "/api/kudos":
            user, failed = require_user(environ, start_response, db, csrf=True)
            if failed: return failed
            try: payload = read_json(environ); recipient_id = int(payload.get("recipientId")); message = payload.get("message", "")
            except (ValueError, TypeError): return error(start_response, "400 Bad Request", "MALFORMED_REQUEST", "Recipient and message are required.")
            if not isinstance(message, str): return error(start_response, "422 Unprocessable Content", "VALIDATION_ERROR", "Please correct the highlighted fields.", {"message": "Message must be text."})
            message = message.strip()
            fields = {}
            if not message: fields["message"] = "Message is required."
            if len(message) > MAX_MESSAGE_LENGTH: fields["message"] = "Message must be 500 characters or fewer."
            if recipient_id == user["id"]: fields["recipientId"] = "You cannot give kudos to yourself."
            recipient = db.execute("SELECT id FROM users WHERE id=? AND is_active=1", (recipient_id,)).fetchone()
            if not recipient: fields["recipientId"] = "Choose an active colleague."
            if fields: return error(start_response, "422 Unprocessable Content", "VALIDATION_ERROR", "Please correct the highlighted fields.", fields)
            cutoff = datetime.fromtimestamp(time.time() - RATE_WINDOW_SECONDS, timezone.utc).isoformat(timespec="seconds")
            if db.execute("SELECT COUNT(*) FROM kudos WHERE sender_id=? AND created_at>=?", (user["id"], cutoff)).fetchone()[0] >= RATE_LIMIT:
                return error(start_response, "429 Too Many Requests", "RATE_LIMITED", "You can send up to 10 kudos per hour. Please try again later.")
            normalized, duplicate_cutoff = normalize_message(message), datetime.fromtimestamp(time.time() - DUPLICATE_WINDOW_SECONDS, timezone.utc).isoformat(timespec="seconds")
            dup = db.execute(KUDOS_SELECT + " WHERE k.sender_id=? AND k.recipient_id=? AND k.message_normalized=? AND k.created_at>=?", (user["id"], recipient_id, normalized, duplicate_cutoff)).fetchone()
            if dup: return response(start_response, "200 OK", {"kudos": kudos_dict(dup), "duplicate": True})
            created = now()
            cur = db.execute("INSERT INTO kudos (sender_id, recipient_id, message, message_normalized, created_at) VALUES (?, ?, ?, ?, ?)", (user["id"], recipient_id, message, normalized, created))
            row = db.execute(KUDOS_SELECT + " WHERE k.id=?", (cur.lastrowid,)).fetchone()
            return response(start_response, "201 Created", {"kudos": kudos_dict(row), "duplicate": False})

        if method == "GET" and path == "/api/admin/kudos":
            user, failed = require_user(environ, start_response, db, admin=True)
            if failed: return failed
            status = query.get("status", ["all"])[0]
            where = "k.deleted_at IS NULL"
            if status == "visible": where += " AND k.is_visible=1"
            elif status == "hidden": where += " AND k.is_visible=0"
            elif status != "all": return error(start_response, "400 Bad Request", "INVALID_STATUS", "Status must be visible, hidden, or all.")
            try: return response(start_response, "200 OK", page_kudos(db, where, [], query, True))
            except ValueError as exc: return error(start_response, "400 Bad Request", "INVALID_CURSOR", str(exc))
        if path.startswith("/api/admin/kudos/"):
            user, failed = require_user(environ, start_response, db, admin=True, csrf=True)
            if failed: return failed
            parts = path.split("/")
            try: kudos_id = int(parts[4])
            except (ValueError, IndexError): return error(start_response, "404 Not Found", "NOT_FOUND", "Kudos was not found.")
            row = db.execute(KUDOS_SELECT + " WHERE k.id=?", (kudos_id,)).fetchone()
            if not row: return error(start_response, "404 Not Found", "NOT_FOUND", "Kudos was not found.")
            if method == "PATCH" and len(parts) == 6 and parts[5] == "visibility":
                try: payload = read_json(environ); visible = payload["isVisible"]
                except (ValueError, KeyError): return error(start_response, "400 Bad Request", "MALFORMED_REQUEST", "isVisible is required.")
                if not isinstance(visible, bool): return error(start_response, "422 Unprocessable Content", "VALIDATION_ERROR", "isVisible must be true or false.")
                reason = str(payload.get("reason", "")).strip()
                if not visible and not reason: return error(start_response, "422 Unprocessable Content", "VALIDATION_ERROR", "A moderation reason is required.", {"reason": "Provide a reason when hiding kudos."})
                if len(reason) > 500: return error(start_response, "422 Unprocessable Content", "VALIDATION_ERROR", "Reason must be 500 characters or fewer.", {"reason": "Reason is too long."})
                if row["deleted_at"]: return error(start_response, "409 Conflict", "DELETED", "Deleted kudos cannot be restored.")
                action = "restored" if visible else "hidden"
                db.execute("UPDATE kudos SET is_visible=?, moderated_by=?, moderated_at=?, reason_for_moderation=? WHERE id=?", (int(visible), user["id"], now(), None if visible else reason, kudos_id))
                db.execute("INSERT INTO kudos_moderation_audit (kudos_id, action, actor_id, reason, created_at) VALUES (?, ?, ?, ?, ?)", (kudos_id, action, user["id"], reason or None, now()))
                updated = db.execute(KUDOS_SELECT + " WHERE k.id=?", (kudos_id,)).fetchone()
                return response(start_response, "200 OK", {"kudos": kudos_dict(updated, True)})
            if method == "DELETE" and len(parts) == 5:
                if not row["deleted_at"]:
                    db.execute("UPDATE kudos SET is_visible=0, deleted_at=?, deleted_by=? WHERE id=?", (now(), user["id"], kudos_id))
                    db.execute("INSERT INTO kudos_moderation_audit (kudos_id, action, actor_id, created_at) VALUES (?, 'deleted', ?, ?)", (kudos_id, user["id"], now()))
                return response(start_response, "204 No Content")
        return error(start_response, "404 Not Found", "NOT_FOUND", "The requested resource was not found.")


if __name__ == "__main__":
    initialise_database()
    print("Kudos is running at http://127.0.0.1:8000")
    make_server("127.0.0.1", 8000, app).serve_forever()
