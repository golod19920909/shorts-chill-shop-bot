from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import base64

from database.database import Database
from services.gemini_dashboard import GeminiDashboardServer


class GeminiDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp_dir.name) / "dashboard.db"))
        asyncio.run(self.database.initialize())
        self.dashboard = GeminiDashboardServer(
            self.database,
            host="127.0.0.1",
            port=0,
            username="admin",
            password="test-dashboard-password",
        ).start()
        self.base_url = f"http://127.0.0.1:{self.dashboard.port}"

    def tearDown(self) -> None:
        self.dashboard.stop()
        self.temp_dir.cleanup()

    def _auth_header(self, username="admin", password="test-dashboard-password"):
        raw = f"{username}:{password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def test_dashboard_requires_authentication(self) -> None:
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base_url + "/")
        self.assertEqual(error.exception.code, 401)
        self.assertIn("Basic", error.exception.headers["WWW-Authenticate"])

    def test_wrong_password_is_rejected(self) -> None:
        request = Request(
            self.base_url + "/",
            headers={"Authorization": self._auth_header(password="wrong")},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 401)

    def test_authenticated_dashboard_and_usage_api(self) -> None:
        headers = {"Authorization": self._auth_header()}
        page = urlopen(Request(self.base_url + "/", headers=headers)).read().decode()
        self.assertIn("Gemini — расходы и токены", page)

        payload = json.loads(
            urlopen(Request(self.base_url + "/api/usage?period=30", headers=headers))
            .read()
            .decode("utf-8")
        )
        self.assertEqual(payload["totals"]["attempts"], 0)

        requests_payload = json.loads(
            urlopen(Request(self.base_url + "/api/requests?period=30", headers=headers))
            .read()
            .decode("utf-8")
        )
        self.assertEqual(requests_payload["requests"], [])

    def test_unknown_path_returns_404_after_authentication(self) -> None:
        request = Request(
            self.base_url + "/not-found",
            headers={"Authorization": self._auth_header()},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
