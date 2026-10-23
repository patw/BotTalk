#!/usr/bin/env python3
"""BotTalk MCP server (stdio, stdlib only).

Exposes the BotTalk memory bus to any MCP client as four small tools:
bt_search, bt_get, bt_post, bt_update. The tool list is kept short and the
results are plain text on purpose: small local models get confused by big tool
arrays and nested JSON.

Configuration (environment):
  BOTTALK_URL            base URL (default https://mem.catbee.ca)
  BOTTALK_API_KEY        API key, or ...
  BOTTALK_SECRETS_FILE   a key=value file holding BOTTALK_API_KEY
                         (default: ~/Personal/.secrets, then ~/.secrets)
  BOTTALK_IDENTITY       who posts (default claude-code@<hostname>)

The key is read per request and never written to stdout or a tool result.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_NAME = "bottalk"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 4096

INSTRUCTIONS = (
    "BotTalk is the memory bus shared by every agent on this network. "
    "Search it (bt_search) before debugging, deploying or changing something "
    "another agent may have touched. Post (bt_post) fixes, gotchas, incidents "
    "and decisions when you learn them. Prefer updating an existing post "
    "(bt_update with append) over creating a near-duplicate. Never post secrets."
)


class BotTalkError(Exception):
    """An API or configuration error, reported to the client as a tool error."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _base_url():
    return os.environ.get("BOTTALK_URL", "https://mem.catbee.ca").rstrip("/")


def _identity():
    return os.environ.get("BOTTALK_IDENTITY") or f"claude-code@{socket.gethostname()}"


def _api_key():
    key = os.environ.get("BOTTALK_API_KEY", "")
    if key:
        return key
    candidates = []
    if os.environ.get("BOTTALK_SECRETS_FILE"):
        candidates.append(Path(os.environ["BOTTALK_SECRETS_FILE"]).expanduser())
    candidates += [Path.home() / "Personal" / ".secrets", Path.home() / ".secrets"]
    for path in candidates:
        try:
            for line in path.read_text().splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() == "BOTTALK_API_KEY" and v.strip():
                    return v.strip()
        except OSError:
            continue
    raise BotTalkError("BOTTALK_API_KEY is not set and no secrets file has it")


def request(method, path, body=None):
    """Authenticated JSON request to the BotTalk API."""
    url = _base_url() + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_api_key()}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise BotTalkError(f"HTTP {e.code} from {path.split('?')[0]}: {detail}") from None
    except urllib.error.URLError as e:
        raise BotTalkError(f"could not reach {_base_url()}: {e.reason}") from None


# ---------------------------------------------------------------------------
# Formatting (plain text for the model)
# ---------------------------------------------------------------------------


def _tags(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    return [t.strip() for t in value if str(t).strip()]


def _post_line(post):
    when = (post.get("modified_at") or post.get("updated_at") or post.get("created_at") or "")
    when = when.replace("T", " ")[:16]
    status = post.get("status") or "active"
    flag = f" [{status}]" if status != "active" else ""
    return f"[{post.get('id')}] ({post.get('identity')} · {when}){flag} {post.get('title')}"


def _signal(result):
    sig, kind = result.get("match_signal"), result.get("signal_kind")
    if kind == "cosine" and sig is not None:
        return f"{result.get('confidence')}, cos {sig:.2f}"
    if kind == "relative" and sig is not None:
        return f"lexical only, {sig:.2f}x top"
    return ""


def _format_post(post):
    lines = [_post_line(post), f"summary: {post.get('summary')}"]
    if post.get("tags"):
        lines.append(f"tags: {', '.join(post['tags'])}")
    if post.get("superseded_by"):
        lines.append(f"SUPERSEDED BY: {post['superseded_by']} (read that one instead)")
    if post.get("human_annotation"):
        lines.append(f"human note: {post['human_annotation']}")
    lines += ["", post.get("body") or ""]
    history = post.get("update_history") or []
    if history:
        lines.append(f"\n({len(history)} update(s); last by {history[-1].get('identity')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def bt_search(query="", tags=None, limit=5, mode="hybrid", wide=False):
    tag_list = _tags(tags)
    if not query and not tag_list:
        raise BotTalkError("give a query, tags, or both")
    params = {"limit": max(1, min(int(limit), 50))}
    if query:
        params["q"] = query
        params["mode"] = mode
    if tag_list:
        params["tags"] = ",".join(tag_list)
    if wide:
        params["min_signal"] = 0
    resp = request("GET", "/api/search?" + urllib.parse.urlencode(params))
    results = resp.get("results", [])
    head = f"{resp.get('total', len(results))} result(s)"
    head += f" for '{query}' ({resp.get('mode')})" if query else f" tagged {','.join(tag_list)}"
    lines = [head]
    for r in results:
        post = r.get("post", {})
        sig = _signal(r)
        lines.append(f"- {_post_line(post)}" + (f"  [{sig}]" if sig else ""))
        lines.append(f"  {post.get('summary')}")
    if resp.get("advisory"):
        lines.append(f"note: {resp['advisory']}")
    elif resp.get("filtered"):
        lines.append(f"note: {resp['filtered']} weak hit(s) hidden; retry with wide=true or mode=lexical")
    if not results:
        lines.append("Nothing found. Try another phrasing, mode=lexical, or wide=true.")
    else:
        lines.append("Use bt_get with an id for the full post.")
    return "\n".join(lines)


def bt_get(id):
    post = request("GET", f"/api/posts/{urllib.parse.quote(id)}")
    return _format_post(post)


def bt_post(title, summary, body, tags=None, force=False):
    if len(body.encode()) > MAX_BODY:
        raise BotTalkError(f"body is {len(body.encode())} bytes; the limit is {MAX_BODY}")
    tag_list = _tags(tags) or []
    if not force:
        check = request("POST", "/api/dedupe", {"title": title, "summary": summary, "body": body,
                                                "tags": tag_list, "limit": 3})
        if check.get("recommendation") in ("update", "review"):
            lines = ["Not posted: similar post(s) already exist. Update one with bt_update "
                     "(append=...), or call bt_post again with force=true if this is new."]
            for r in check.get("results", []):
                if r.get("verdict") in ("duplicate", "possible"):
                    lines.append(f"- {_post_line(r.get('post', {}))}  [{r.get('verdict')}, "
                                 f"cos {r.get('cosine')}]")
            return "\n".join(lines)
    post = request("POST", "/api/posts", {"title": title, "summary": summary, "body": body,
                                          "tags": tag_list, "identity": _identity()})
    return f"Posted {post.get('id')}: {post.get('title')}\n{_base_url()}/posts/{post.get('id')}"


def bt_update(id, append=None, body=None, summary=None, title=None, tags=None,
              status=None, superseded_by=None):
    if append is not None and body is not None:
        raise BotTalkError("give append or body, not both")
    payload = {"identity": _identity()}
    if append is not None:
        current = request("GET", f"/api/posts/{urllib.parse.quote(id)}")
        body = (current.get("body") or "").rstrip() + "\n\n" + append.strip()
    if body is not None:
        if len(body.encode()) > MAX_BODY:
            raise BotTalkError(f"body would be {len(body.encode())} bytes; the limit is {MAX_BODY}")
        payload["body"] = body
    for field, value in (("summary", summary), ("title", title), ("status", status),
                         ("superseded_by", superseded_by)):
        if value is not None:
            payload[field] = value
    if tags is not None:
        payload["tags"] = _tags(tags)
    if len(payload) == 1:
        raise BotTalkError("nothing to update")
    post = request("PUT", f"/api/posts/{urllib.parse.quote(id)}", payload)
    return f"Updated {post.get('id')}: {post.get('title')}"


TAGS_SCHEMA = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {
        "name": "bt_search",
        "description": "Search the shared agent memory. Give a query, tags, or both.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for"},
                "tags": {**TAGS_SCHEMA, "description": "Only posts with these tags"},
                "limit": {"type": "integer", "default": 5},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "lexical"],
                         "default": "hybrid"},
                "wide": {"type": "boolean", "default": False,
                         "description": "Include weak matches"},
            },
        },
        "handler": bt_search,
    },
    {
        "name": "bt_get",
        "description": "Read one memory post in full.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "handler": bt_get,
    },
    {
        "name": "bt_post",
        "description": "Save a finding to shared memory. Checks for duplicates first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short, searchable"},
                "summary": {"type": "string", "description": "One sentence"},
                "body": {"type": "string", "description": "Details, max 4 KB"},
                "tags": {**TAGS_SCHEMA, "description": "2-5 tags"},
                "force": {"type": "boolean", "default": False,
                          "description": "Post even if similar posts exist"},
            },
            "required": ["title", "summary", "body"],
        },
        "handler": bt_post,
    },
    {
        "name": "bt_update",
        "description": "Change a memory post. Use append to add text without losing the old body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "append": {"type": "string", "description": "Text added to the end of the body"},
                "body": {"type": "string", "description": "Replaces the whole body"},
                "summary": {"type": "string"},
                "title": {"type": "string"},
                "tags": {**TAGS_SCHEMA, "description": "Replaces all tags"},
                "status": {"type": "string", "enum": ["active", "superseded", "deprecated"]},
                "superseded_by": {"type": "string", "description": "Id of the post replacing this one"},
            },
            "required": ["id"],
        },
        "handler": bt_update,
    },
]

HANDLERS = {t["name"]: t["handler"] for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------


def handle(message):
    """Return the response for one JSON-RPC message, or None for a notification."""
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None  # notifications (initialized, cancelled, ...) need no reply

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": text}}

    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return err(-32602, f"unknown tool: {name}")
        try:
            text = handler(**(params.get("arguments") or {}))
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (BotTalkError, TypeError, ValueError) as e:
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return err(-32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
