"""Boundary and failure tests. External services are mocked only in this file."""

import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        app.SESSIONS.clear()
        app._health_cache = None
        self.client = TestClient(app.app, base_url=app.ORIGIN)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
        self.headers = {"X-Session-Token": self.token}

    def tearDown(self):
        self.client.close()

    def test_api_requires_matching_session_header_and_cookie(self):
        for headers in ({}, {"X-Session-Token": "forged"}):
            self.assertEqual(self.client.get("/api/state", headers=headers).status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/state", headers=self.headers).status_code, 403)

    def test_unexpected_host_origin_and_cross_site_blocked(self):
        for headers in (
            {"Host": "evil.example:8765"}, {"Host": "127.0.0.1:9999"},
            {"Origin": "https://evil.example"}, {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            self.assertEqual(self.client.get("/api/state", headers=self.headers | headers).status_code, 403)
        self.assertEqual(self.client.get("/", headers={"Host": "localhost:8765"}).status_code, 403)
        response = self.client.get("/", headers=[("Host", app.HOST), ("Host", "evil.example")])
        self.assertEqual(response.status_code, 403)

    def test_mutations_checked_before_routing(self):
        for headers in (
            self.headers,
            self.headers | {"Origin": "https://evil.example"},
            {"Origin": app.ORIGIN},
            self.headers | {"Origin": app.ORIGIN, "Host": "evil.example"},
        ):
            self.assertEqual(self.client.post("/api/state", headers=headers).status_code, 403)
        allowed = self.headers | {"Origin": app.ORIGIN}
        self.assertEqual(self.client.post("/api/state", headers=allowed).status_code, 405)
        self.assertEqual(self.client.post("/api/answer", headers=allowed).status_code, 404)

    def test_top_level_dashboard_link_works_but_cross_site_api_does_not(self):
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        self.assertEqual(self.client.get("/", headers=navigation).status_code, 200)
        self.assertEqual(self.client.get("/api/state", headers=navigation | self.headers).status_code, 403)
        navigation["Sec-Fetch-Dest"] = "iframe"
        self.assertEqual(self.client.get("/", headers=navigation).status_code, 403)

    def test_session_expiry_and_isolation(self):
        with TestClient(app.app, base_url=app.ORIGIN) as other:
            other.get("/")
            self.assertEqual(other.get("/api/state", headers=self.headers).status_code, 403)
        app.SESSIONS[self.token] = 0
        self.assertEqual(self.client.get("/api/state", headers=self.headers).status_code, 403)
        new_page = self.client.get("/")
        self.assertNotIn(f'content="{self.token}"', new_page.text)

    def test_security_headers_static_allowlist_and_no_websocket(self):
        response = self.client.get("/")
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        for path in ("/static/app.py", "/static/%2e%2e/app.py", "/openapi.json", "/docs"):
            self.assertEqual(self.client.get(path).status_code, 404)
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/ws", headers=self.headers):
                self.fail("Stage 1 must not accept WebSockets.")

    def test_empty_data_and_honest_service_failure(self):
        fixture = app.status("not connected", "Unit test: local service unavailable")
        with (patch.object(app, "read_config", return_value=({}, Path("test-config.json"), None)),
              patch.object(app, "check_llama", new=AsyncMock(return_value=fixture)),
              patch.object(app, "check_whisper", return_value=app.status("ready", "Unit test files")),
              patch.object(app, "check_speech", return_value=app.status("error", "Unit test voice failure"))):
            response = self.client.get("/api/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["state"], "error")
        self.assertEqual(data["meeting_state"], "not connected")
        for field in ("transcript", "actions", "decisions"):
            self.assertEqual(data[field], [])
        self.assertEqual(data["services"]["llama"]["state"], "not connected")


class ReadinessTests(unittest.TestCase):
    def test_health_url_is_loopback_and_removes_v1(self):
        self.assertEqual(app.health_url("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/health")
        for value in ("https://example.com/v1", "http://example.com", "http://127.0.0.1.evil.example",
                      "http://user:pass@127.0.0.1", "file:///secret", "http://127.0.0.1/v1?url=evil"):
            with self.assertRaises(ValueError):
                app.health_url(value)

    def test_llama_http_and_payload_failures_not_ready(self):
        actual_client = httpx.AsyncClient
        for code, body in ((200, b'{"status":"ok"}'), (503, b'{"status":"loading model"}'),
                           (200, b'{"status":"loading"}'), (200, b'not json'),
                           (302, b''), (200, b'x' * 8193)):
            transport = httpx.MockTransport(lambda request: httpx.Response(code, content=body))
            def create_client(**kwargs):
                self.assertFalse(kwargs["trust_env"])
                self.assertFalse(kwargs["follow_redirects"])
                return actual_client(transport=transport, **kwargs)
            with patch.object(app.httpx, "AsyncClient", side_effect=create_client):
                result = asyncio.run(app.check_llama({"llm_base_url": "http://127.0.0.1:8080/v1"}))
            self.assertEqual(result["state"], "ready" if code == 200 and body == b'{"status":"ok"}' else "error")

    def test_llama_connection_failure(self):
        with patch.object(app.httpx, "AsyncClient", side_effect=httpx.ConnectError("Unit test offline")):
            result = asyncio.run(app.check_llama({"llm_base_url": "http://127.0.0.1:8080/v1"}))
        self.assertEqual(result["state"], "not connected")

    def test_missing_and_invalid_config(self):
        for error in (FileNotFoundError(), PermissionError(), ValueError("invalid JSON")):
            with patch.object(Path, "read_text", side_effect=error):
                config, _, message = app.read_config()
            self.assertEqual(config, {})
            self.assertTrue(message)
        with patch.object(Path, "read_text", return_value=json.dumps({"python": "local.exe"})):
            self.assertIsNotNone(app.read_config()[2])

    def test_missing_whisper_model(self):
        with patch.object(Path, "is_dir", return_value=False):
            result = app.check_whisper({"asr_model_path": "missing"}, Path("config.json"))
        self.assertEqual(result["state"], "error")
        with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "open", side_effect=FileNotFoundError()):
            result = app.check_whisper({"asr_model_path": "missing"}, Path("config.json"))
        self.assertEqual(result["state"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
