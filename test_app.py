import io
import json
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

import app


class KudosApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = app.DB_PATH
        app.DB_PATH = Path(self.temp.name) / "test.db"
        app.initialise_database()

    def tearDown(self):
        app.DB_PATH = self.old_db
        self.temp.cleanup()

    def call(self, method, path, body=b"", headers=None):
        environ = {}
        setup_testing_defaults(environ)
        environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body))})
        for key, value in (headers or {}).items():
            environ[key] = value
        captured = {}
        result = b"".join(app.app(environ, lambda status, hdrs: captured.update(status=status, headers=dict(hdrs))))
        return captured["status"], captured["headers"], result

    def sign_in(self, user_id):
        status, headers, _ = self.call("POST", "/login", f"user_id={user_id}".encode())
        self.assertEqual(status, "303 See Other")
        token = headers["Set-Cookie"].split(";", 1)[0]
        with app.database() as db:
            csrf = db.execute("SELECT csrf_token FROM sessions WHERE id=?", (token.split("=", 1)[1],)).fetchone()[0]
        return token, csrf

    def api(self, method, path, session, csrf, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        return self.call(method, path, body, {"HTTP_COOKIE": session, "HTTP_X_CSRF_TOKEN": csrf})

    def test_employee_can_create_and_view_kudos(self):
        session, csrf = self.sign_in(1)
        status, _, body = self.api("POST", "/api/kudos", session, csrf, {"recipientId": 2, "message": "Thank you for your thoughtful design review."})
        self.assertEqual(status, "201 Created")
        self.assertFalse(json.loads(body)["duplicate"])
        status, _, body = self.api("GET", "/api/kudos", session, csrf)
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["items"][0]["recipient"]["id"], 2)

    def test_dashboard_redirects_anonymous_users_to_login(self):
        status, headers, _ = self.call("GET", "/")
        self.assertEqual(status, "303 See Other")
        self.assertEqual(headers["Location"], "/login")

    def test_duplicate_and_self_kudos_are_rejected_or_reused(self):
        session, csrf = self.sign_in(1)
        payload = {"recipientId": 2, "message": "Thanks!"}
        self.api("POST", "/api/kudos", session, csrf, payload)
        status, _, body = self.api("POST", "/api/kudos", session, csrf, payload)
        self.assertEqual(status, "200 OK")
        self.assertTrue(json.loads(body)["duplicate"])
        status, _, body = self.api("POST", "/api/kudos", session, csrf, {"recipientId": 1, "message": "Me"})
        self.assertEqual(status, "422 Unprocessable Content")
        self.assertIn("recipientId", json.loads(body)["error"]["fields"])

    def test_administrator_hides_kudos_from_public_feed(self):
        employee_session, employee_csrf = self.sign_in(1)
        status, _, body = self.api("POST", "/api/kudos", employee_session, employee_csrf, {"recipientId": 2, "message": "Good job!"})
        kudos_id = json.loads(body)["kudos"]["id"]
        admin_session, admin_csrf = self.sign_in(4)
        status, _, _ = self.api("PATCH", f"/api/admin/kudos/{kudos_id}/visibility", admin_session, admin_csrf, {"isVisible": False, "reason": "Test moderation"})
        self.assertEqual(status, "200 OK")
        status, _, body = self.api("GET", "/api/kudos", employee_session, employee_csrf)
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["items"], [])


if __name__ == "__main__":
    unittest.main()
