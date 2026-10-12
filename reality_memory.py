
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone


def log_reality_event(event, **fields):
    """Emit a structured event without interrupting a request."""
    try:
        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        print(json.dumps(record, default=str), flush=True)
    except Exception:
        pass


def client_fingerprint(scope):
    """
    Optional pseudonymous client fingerprint.
    Requires REALITY_MEMORY_SALT in Render environment.
    Never trusts X-Forwarded-For supplied by an unknown client.
    """
    salt = os.environ.get("REALITY_MEMORY_SALT", "")
    if not salt:
        return None

    client = scope.get("client")
    if not client or not client[0]:
        return None

    raw = str(client[0]).encode("utf-8")
    return hmac.new(
        salt.encode("utf-8"),
        raw,
        hashlib.sha256,
    ).hexdigest()[:20]


class MCPObservationMiddleware:
    """
    ASGI wrapper around an existing MCP application.

    Observes JSON-RPC method names without changing the
    incoming request body or outgoing response.
    """

    def __init__(self, app, service):
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        http_method = scope.get("method", "")
        if http_method != "POST":
            await self.app(scope, receive, send)
            return

        chunks = []
        total = 0
        too_large = False

        # Read the original ASGI body once and replay it
        # unchanged to the MCP application.
        while True:
            message = await receive()

            if message["type"] == "http.disconnect":
                return

            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")
            chunks.append(message)
            total += len(chunk)

            if total > 65536:
                too_large = True

            if not message.get("more_body", False):
                break

        rpc_method = None
        tool_name = None
        rpc_id = None

        if not too_large:
            try:
                body = b"".join(
                    part.get("body", b"") for part in chunks
                )
                payload = json.loads(body)

                if isinstance(payload, dict):
                    rpc_method = payload.get("method")
                    rpc_id = payload.get("id")

                    params = payload.get("params", {})
                    if isinstance(params, dict):
                        tool_name = params.get("name")
            except (ValueError, UnicodeDecodeError):
                pass

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(chunks):
                message = chunks[replay_index]
                replay_index += 1
                return message
            return await receive()

        status_code = None

        async def observe_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status")
            await send(message)

        try:
            await self.app(scope, replay_receive, observe_send)
        finally:
            if rpc_method in (
                "initialize",
                "tools/list",
                "tools/call",
            ):
                log_reality_event(
                    "mcp_protocol_observation",
                    service=self.service,
                    method=rpc_method,
                    tool=tool_name,
                    rpc_id=rpc_id,
                    http_status=status_code,
                    client_fingerprint=client_fingerprint(scope),
                    evidence_level="protocol_request",
                )
