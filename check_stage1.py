"""Read-only acceptance probe against the running server; no mocked readiness."""

import json
import re

import httpx


def main():
    with httpx.Client(base_url="http://127.0.0.1:8765", trust_env=False, timeout=15) as client:
        page = client.get("/")
        page.raise_for_status()
        assert "<title>AICK Local Agent</title>" in page.text
        token = re.search(r'name="aick-session-token" content="([^"]+)"', page.text)[1]
        for asset in ("styles.css", "app.js", "favicon.svg"):
            client.get(f"/static/{asset}").raise_for_status()
        assert client.get("/api/state").status_code == 403
        headers = {"X-Session-Token": token}
        response = client.get("/api/state", headers=headers)
        response.raise_for_status()
        state = response.json()
        assert state["stage"] == 6
        assert state["meeting_state"] == "not connected"
        assert state["transcript"] == state["actions"] == state["decisions"] == []
        assert client.post("/api/state", headers=headers).status_code == 403
        assert client.post("/api/state", headers=headers | {"Origin": "https://example.invalid"}).status_code == 403
        assert client.post("/api/state", headers=headers | {"Origin": "http://127.0.0.1:8765", "Host": "bad.invalid"}).status_code == 403
        print(json.dumps(state, indent=2))
        print("PASS: page/assets served; transcript/actions/decisions empty; token/Host/Origin checks passed.")
        print("Readiness above is measured now; a ready voice/directory does not imply audio was generated/transcribed.")


if __name__ == "__main__":
    main()
