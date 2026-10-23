"""
Tests for the stdio MCP server (mcp/bottalk_mcp.py), run against the real API
through the test client instead of a mock.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp" / "bottalk_mcp.py"
spec = importlib.util.spec_from_file_location("bottalk_mcp", SERVER_PATH)
bottalk_mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bottalk_mcp)

AUTH = {"Authorization": "Bearer test-api-key-12345"}


@pytest.fixture
def mcp(client: TestClient, monkeypatch):
    """The MCP module with its HTTP layer routed into the test client."""

    def request(method, path, body=None):
        resp = client.request(method, path, json=body, headers=AUTH)
        if resp.status_code >= 400:
            raise bottalk_mcp.BotTalkError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else None

    monkeypatch.setattr(bottalk_mcp, "request", request)
    monkeypatch.setenv("BOTTALK_IDENTITY", "mcp-test")
    return bottalk_mcp


def call(mcp, name, **arguments):
    reply = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
    result = reply["result"]
    return result["content"][0]["text"], result["isError"]


def make_post(client: TestClient, **fields) -> str:
    post = {"title": "Kernel bump", "summary": "Moved to 7.2.11", "tags": ["kernel"],
            "body": "Original body.", "identity": "someone"}
    post.update(fields)
    return client.post("/api/posts", json=post, headers=AUTH).json()["id"]


class TestProtocol:
    def test_initialize_echoes_a_known_version(self, mcp):
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
        assert reply["result"]["protocolVersion"] == "2025-06-18"
        assert reply["result"]["capabilities"] == {"tools": {}}
        assert "BotTalk" in reply["result"]["instructions"]

    def test_initialize_answers_an_unknown_version_with_the_latest(self, mcp):
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "1999-01-01"}})
        assert reply["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]

    def test_notifications_get_no_reply(self, mcp):
        assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_tools_list_is_small_and_has_no_handlers(self, mcp):
        tools = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in tools] == ["bt_search", "bt_get", "bt_post", "bt_update"]
        assert all("handler" not in t for t in tools)
        json.dumps(tools)  # must serialise

    def test_unknown_method_is_an_error(self, mcp):
        reply = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        assert reply["error"]["code"] == -32601

    def test_bad_arguments_are_a_tool_error_not_a_crash(self, mcp):
        text, is_error = call(mcp, "bt_get", nope="x")
        assert is_error and text.startswith("Error:")

    def test_stdio_round_trip(self, tmp_path):
        """The script itself speaks newline-delimited JSON-RPC and survives garbage."""
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        out = subprocess.run([sys.executable, str(SERVER_PATH)], input="\n".join(lines) + "\n",
                             capture_output=True, text=True, timeout=30, check=True).stdout
        replies = [json.loads(line) for line in out.splitlines()]
        assert [r.get("id") for r in replies] == [1, None, 2]
        assert replies[1]["error"]["code"] == -32700


class TestTools:
    def test_search_by_tag_lists_posts(self, mcp, client):
        make_post(client, title="Wifi powersave off", tags=["wifi"])
        text, is_error = call(mcp, "bt_search", tags=["wifi"])
        assert not is_error
        assert "Wifi powersave off" in text and "bt_get" in text

    def test_search_needs_query_or_tags(self, mcp):
        text, is_error = call(mcp, "bt_search")
        assert is_error and "query, tags" in text

    def test_get_shows_the_body_and_supersede_link(self, mcp, client):
        new_id = make_post(client, title="New way")
        old_id = make_post(client, title="Old way", status="superseded", superseded_by=new_id)
        text, _ = call(mcp, "bt_get", id=old_id)
        assert "Original body." in text and f"SUPERSEDED BY: {new_id}" in text

    def test_post_creates_with_identity_when_forced(self, mcp, client):
        text, is_error = call(mcp, "bt_post", title="RDP prototype", summary="Works with FreeRDP",
                              body="Details.", tags="rdp, pengyos", force=True)
        assert not is_error and text.startswith("Posted ")
        post_id = text.split()[1].rstrip(":")
        post = client.get(f"/api/posts/{post_id}", headers=AUTH).json()
        assert post["identity"] == "mcp-test" and post["tags"] == ["rdp", "pengyos"]

    def test_post_refuses_an_oversized_body(self, mcp):
        text, is_error = call(mcp, "bt_post", title="t", summary="s", body="x" * 5000, force=True)
        assert is_error and "4096" in text

    def test_update_append_keeps_the_old_body(self, mcp, client):
        post_id = make_post(client)
        text, is_error = call(mcp, "bt_update", id=post_id, append="Also: 7.2.12 next week.")
        assert not is_error
        body = client.get(f"/api/posts/{post_id}", headers=AUTH).json()["body"]
        assert body == "Original body.\n\nAlso: 7.2.12 next week."

    def test_update_rejects_append_and_body_together(self, mcp, client):
        post_id = make_post(client)
        text, is_error = call(mcp, "bt_update", id=post_id, append="a", body="b")
        assert is_error and "not both" in text

    def test_update_marks_superseded(self, mcp, client):
        old_id, new_id = make_post(client), make_post(client, title="Kernel bump v2")
        call(mcp, "bt_update", id=old_id, status="superseded", superseded_by=new_id)
        post = client.get(f"/api/posts/{old_id}", headers=AUTH).json()
        assert post["status"] == "superseded" and post["superseded_by"] == new_id


class TestDedupeGate:
    def test_post_without_force_reports_a_near_duplicate(self, mcp, client_with_embed, monkeypatch):
        def request(method, path, body=None):
            resp = client_with_embed.request(method, path, json=body, headers=AUTH)
            return resp.json() if resp.content else None

        monkeypatch.setattr(mcp, "request", request)
        summary = "The nightly build host runs out of memory unless Strata is stopped first"
        client_with_embed.post("/api/posts", headers=AUTH, json={
            "title": "Stop Strata before builds", "summary": summary, "tags": ["build"],
            "body": "Strata holds 43 GB.", "identity": "someone"})
        text, is_error = call(mcp, "bt_post", title="Stop Strata before builds",
                              summary=summary, body="Strata holds 43 GB.")
        assert not is_error and text.startswith("Not posted")
        assert "Stop Strata before builds" in text
