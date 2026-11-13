#!/usr/bin/env python3
"""Host-side HTTP proxy for the isolated Claude Code runtime.

API credentials are injected on the host. Each upstream API call runs in a
cancellable worker process; disconnecting the client closes its upstream socket.
SSE events are parsed structurally. Refusals are returned to the caller, while
network failures and transient server errors receive bounded retries.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import http.client
import json
import logging
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Coroutine, TypeVar, cast
from urllib.parse import unquote, urlsplit, urlunsplit

LOG = logging.getLogger(__name__)
STREAM_LIMIT = 64 * 1024 * 1024
HEADER_LIMIT = 256 * 1024
UPSTREAM_TIMEOUT = 900
SOCKET_IDLE_TIMEOUT = 60
MAX_SSE_ATTEMPTS = 15
PING_INTERVAL = 5.0
T = TypeVar("T")

GEOIP_HOSTS = {
    "ipinfo.io", "ifconfig.me", "ifconfig.co", "ip-api.com", "api.ipify.org",
    "ipify.org", "icanhazip.com", "ipv4.icanhazip.com", "ident.me", "v4.ident.me",
    "checkip.amazonaws.com", "ip.sb", "api.ip.sb", "cip.cc", "myip.ipip.net",
    "wtfismyip.com", "ipecho.net", "myip.com", "api.myip.com", "ipwhois.app",
    "ipapi.co", "freeipapi.com",
}
TZ_PROFILES = {
    "America/Los_Angeles": {
        "city": "San Francisco", "region": "California", "country": "US",
        "loc": "37.7749,-122.4194", "postal": "94107", "org": "AS13335 Cloudflare, Inc.",
    },
    "America/New_York": {
        "city": "New York", "region": "New York", "country": "US",
        "loc": "40.7128,-74.0060", "postal": "10001", "org": "AS13335 Cloudflare, Inc.",
    },
    "America/Chicago": {
        "city": "Chicago", "region": "Illinois", "country": "US",
        "loc": "41.8781,-87.6298", "postal": "60601", "org": "AS13335 Cloudflare, Inc.",
    },
    "Europe/London": {
        "city": "London", "region": "England", "country": "GB",
        "loc": "51.5074,-0.1278", "postal": "EC1A", "org": "AS13335 Cloudflare, Inc.",
    },
}


def is_geoip_host(host: str) -> bool:
    h = host.lower().strip(".")
    return any(h == g or h.endswith("." + g) for g in GEOIP_HOSTS)


def is_anthropic_host(host: str) -> bool:
    return host.lower().strip(".") == "api.anthropic.com"


def _strip_cache_scope(obj: Any) -> None:
    if isinstance(obj, dict):
        cc = obj.get("cache_control")
        if isinstance(cc, dict):
            cc.pop("scope", None)
        for value in obj.values():
            _strip_cache_scope(value)
    elif isinstance(obj, list):
        for value in obj:
            _strip_cache_scope(value)


def normalize_model_name(model: str) -> str:
    return re.sub(r"\[[^\]]*\]$", "", model.strip())


def map_request_model(req_model: str, main_model: str) -> str:
    model = normalize_model_name(req_model)
    if main_model and (not model or "opus" in model.lower() or "fable" in model.lower()):
        return normalize_model_name(main_model)
    return model


def prepare_messages_body(raw_body: bytes, main_model: str) -> tuple[bytes, bool]:
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeError):
        return raw_body, False
    if not isinstance(payload, dict):
        return raw_body, False
    _strip_cache_scope(payload)
    model = payload.get("model", "")
    mapped = map_request_model(model if isinstance(model, str) else "", main_model)
    if mapped:
        payload["model"] = mapped
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), payload.get("stream") is True


def build_geoip_response(host: str, path: str, headers: dict[str, str],
                         fake_ip: str, tz_val: str) -> bytes:
    prof = TZ_PROFILES.get(tz_val, TZ_PROFILES["America/Los_Angeles"])
    h, p = host.lower(), path.lower()
    want_json = (
        "json" in p or "application/json" in headers.get("accept", "").lower()
        or h in ("api.myip.com", "ipwhois.app", "freeipapi.com")
        or (h.endswith("ipinfo.io") and p in ("", "/") and "curl" in headers.get("user-agent", "").lower())
    )
    if h in ("ifconfig.me", "icanhazip.com", "ipv4.icanhazip.com", "ident.me", "v4.ident.me",
             "checkip.amazonaws.com", "ip.sb", "api.ip.sb", "ipecho.net") and "json" not in p:
        want_json = False
    if h.endswith("ipify.org") and "format=json" not in p:
        want_json = False
    if want_json:
        body = (json.dumps({"ip": fake_ip, **prof, "countryCode": prof["country"],
                            "timezone": tz_val, "status": "success"}, indent=2) + "\n").encode()
        ctype = "application/json; charset=utf-8"
    else:
        body, ctype = f"{fake_ip}\n".encode(), "text/plain; charset=utf-8"
    return http_response(200, body, {"content-type": ctype})


async def upgrade_stream_tls(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             ssl_ctx: ssl.SSLContext, *, server_side: bool = False,
                             server_hostname: str | None = None) -> None:
    # Updating only the writer leaves StreamReader.resume_reading on the old
    # transport, which deadlocks requests larger than its receive buffer.
    transport = writer.transport
    protocol = transport.get_protocol()
    replacement = await asyncio.get_running_loop().start_tls(
        transport, protocol, ssl_ctx, server_side=server_side,
        server_hostname=server_hostname, ssl_handshake_timeout=30,
    )
    if replacement is None:
        raise ConnectionError("TLS transport closed during upgrade")
    cast(Any, writer)._transport = replacement
    cast(Any, reader)._transport = replacement
    cast(Any, protocol)._transport = replacement
    cast(Any, protocol)._replace_writer(writer)


async def read_until_double_crlf(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return b"", b""
        raise ValueError("Incomplete HTTP headers") from exc
    if len(head) > HEADER_LIMIT:
        raise ValueError("HTTP headers too large")
    # readuntil does not consume any request-body or TLS bytes.
    return head, b""


def parse_head(raw: bytes) -> tuple[str, str, dict[str, str]]:
    lines = raw.decode("latin-1").split("\r\n")
    first = lines[0].split()
    if len(first) != 3:
        raise ValueError("Malformed HTTP request line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ValueError("Malformed HTTP header")
        name, value = line.split(":", 1)
        name = name.lower().strip()
        if name in headers and name in ("content-length", "transfer-encoding", "host"):
            raise ValueError("Duplicate framing header")
        headers[name] = value.strip()
    return first[0].upper(), first[1], headers


async def read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    encoding = headers.get("transfer-encoding", "").lower()
    if encoding:
        if encoding != "chunked" or "content-length" in headers:
            raise ValueError("Unsupported request framing")
        body = bytearray()
        while True:
            line = await reader.readuntil(b"\r\n")
            if len(line) > 128:
                raise ValueError("Invalid chunk header")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size < 0 or len(body) + size > STREAM_LIMIT:
                raise ValueError("Request body too large")
            if not size:
                trailer_size = 0
                while True:
                    trailer = await reader.readuntil(b"\r\n")
                    trailer_size += len(trailer)
                    if trailer_size > HEADER_LIMIT:
                        raise ValueError("Trailers too large")
                    if trailer == b"\r\n":
                        return bytes(body)
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise ValueError("Invalid chunk terminator")
    length = headers.get("content-length", "0")
    if not length.isdigit() or int(length) > STREAM_LIMIT:
        raise ValueError("Invalid Content-Length")
    return await reader.readexactly(int(length))


def build_upstream_url(base_url: str, req_path: str) -> str:
    base, request = urlsplit(base_url), urlsplit(req_path)
    if (base.scheme not in ("http", "https") or not base.hostname or base.username
            or base.password or base.query or base.fragment):
        raise ValueError("API base URL must be an HTTP(S) origin with an optional path")
    if request.scheme or request.netloc or not request.path.startswith("/v1/"):
        raise ValueError("Invalid API request path")
    prefix, path = base.path.rstrip("/"), request.path
    if prefix and not (path == prefix or path.startswith(prefix + "/")):
        path = prefix + (path[3:] if prefix.endswith("/v1") else path)
    return urlunsplit((base.scheme, base.netloc, path, request.query, ""))


def upstream_headers(headers: dict[str, str], api_key: str) -> dict[str, str]:
    skip = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding",
            "proxy-authorization", "proxy-connection", "authorization", "x-api-key"}
    skip.update(x.strip().lower() for x in headers.get("connection", "").split(","))
    result = {k: v for k, v in headers.items() if k.lower() not in skip}
    result.update({"x-api-key": api_key, "authorization": f"Bearer {api_key}",
                   "accept-encoding": "identity"})
    return result


@dataclass
class SSEEvent:
    name: str
    payload: Any


class SSEParser:
    """Incremental line parser; dispatch only complete, blank-line-delimited events."""
    def __init__(self) -> None:
        self.pending = bytearray()
        self.name = ""
        self.data: list[bytes] = []

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self.pending.extend(chunk)
        events = []
        consumed = 0
        while True:
            end = self.pending.find(b"\n", consumed)
            if end < 0:
                break
            line = bytes(self.pending[consumed:end]).rstrip(b"\r")
            consumed = end + 1
            if not line:
                if self.data:
                    try:
                        payload = json.loads(b"\n".join(self.data))
                    except (ValueError, UnicodeError):
                        payload = None
                    name = self.name or (payload.get("type", "") if isinstance(payload, dict) else "")
                    events.append(SSEEvent(name, payload))
                self.name, self.data = "", []
                continue
            if line.startswith(b":"):
                continue
            key, _, value = line.partition(b":")
            if value.startswith(b" "):
                value = value[1:]
            if key == b"event":
                self.name = value.decode("utf-8", errors="replace")
            elif key == b"data":
                self.data.append(value)
        del self.pending[:consumed]
        return events


def sse_events(data: bytes) -> list[SSEEvent]:
    return SSEParser().feed(data)


def _sse_terminal_error(data: bytes) -> bool:
    return any(event.name == "error" or (isinstance(event.payload, dict)
               and event.payload.get("type") == "error") for event in sse_events(data))


def _sse_complete(data: bytes) -> bool:
    events = sse_events(data)
    return any(e.name == "message_stop" for e in events) and not any(e.name == "error" for e in events)


def retryable_sse_error(data: bytes) -> bool:
    for event in sse_events(data):
        if event.name != "error" or not isinstance(event.payload, dict):
            continue
        error = event.payload.get("error", {})
        if not isinstance(error, dict):
            continue
        if error.get("type") in ("overloaded_error", "rate_limit_error", "api_error"):
            return True
        message = str(error.get("message", "")).lower()
        if "concurrency limit exceeded" in message or "too many pending requests" in message:
            return True
    return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # The injected API key must not follow a redirect to another service.
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def sync_fetch_upstream(full_url: str, headers: dict[str, str], body: bytes,
                        proxy_url: str | None, method: str = "POST") -> tuple[int, dict[str, str], bytes]:
    handlers = [
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect(),
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(full_url, data=body if body else None,
                                 headers={**headers, "Connection": "close"}, method=method)
    try:
        response = opener.open(req, timeout=SOCKET_IDLE_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    with response as resp:
        status = resp.code
        result_headers = {k.lower(): v for k, v in resp.headers.items()}
        if method == "HEAD":
            return status, result_headers, b""
        parser = SSEParser() if "text/event-stream" in result_headers.get("content-type", "").lower() else None
        data = bytearray()
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                return status, result_headers, bytes(data)
            data.extend(chunk)
            if len(data) > STREAM_LIMIT:
                raise ValueError("Upstream response too large")
            if parser and any(e.name in ("message_stop", "error") for e in parser.feed(chunk)):
                return status, result_headers, bytes(data)


def fetch_worker() -> None:
    """Private subprocess protocol: request JSON on stdin, metadata + raw body out."""
    try:
        request = json.load(sys.stdin.buffer)
        status, headers, body = sync_fetch_upstream(
            request["url"], request["headers"], base64.b64decode(request["body"], validate=True),
            request["proxy"], request["method"],
        )
        metadata = {"status": status, "headers": headers}
    except Exception as exc:
        # Exception text may contain a credential-bearing URL. Send only its class.
        metadata, body = {"error": type(exc).__name__}, b""
    sys.stdout.buffer.write(json.dumps(metadata).encode() + b"\n" + body)


async def fetch_upstream(full_url: str, headers: dict[str, str], body: bytes,
                         proxy_url: str | None, method: str = "POST", *,
                         timeout: float | None = None) -> tuple[int, dict[str, str], bytes]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(Path(__file__).resolve()), "--fetch",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    wire = json.dumps({"url": full_url, "headers": headers, "body": base64.b64encode(body).decode(),
                       "proxy": proxy_url, "method": method}).encode()
    communication = asyncio.create_task(proc.communicate(wire))
    try:
        output, _ = await asyncio.wait_for(asyncio.shield(communication), UPSTREAM_TIMEOUT if timeout is None else timeout)
        metadata_line, separator, data = output.partition(b"\n")
        if proc.returncode != 0 or not separator:
            raise OSError("Upstream worker failed")
        metadata = json.loads(metadata_line)
        if "error" in metadata:
            raise OSError("Upstream transport failed: " + metadata["error"])
        return metadata["status"], metadata["headers"], data
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(communication), 1)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    proc.kill()
        await asyncio.gather(communication, return_exceptions=True)


def http_response(status: int, body: bytes, headers: dict[str, str] | None = None) -> bytes:
    headers = headers or {}
    phrase = http.client.responses.get(status, "Response")
    lines = [f"HTTP/1.1 {status} {phrase}", f"Content-Length: {len(body)}", "Connection: close"]
    for key in ("content-type", "retry-after", "request-id"):
        if key in headers:
            lines.append(f"{key}: {headers[key]}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def error_payload(message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}).encode()


def error_event(status: int, data: bytes = b"") -> bytes:
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError):
        payload = None
    if not isinstance(payload, dict) or payload.get("type") != "error":
        data = error_payload(f"Upstream returned HTTP {status}")
    else:
        data = json.dumps(payload).encode()
    return b"event: error\ndata: " + data + b"\n\n"


async def wait_disconnect(reader: asyncio.StreamReader) -> None:
    try:
        while await reader.read(8192):
            pass
    except (ConnectionError, OSError):
        pass


class ClientDisconnected(Exception):
    pass


async def while_connected(operation: Coroutine[Any, Any, T], disconnected: asyncio.Task[None]) -> T:
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            raise ClientDisconnected()
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def write_sse(writer: asyncio.StreamWriter, data: bytes, *, final: bool = True) -> None:
    for offset in range(0, len(data), 32768):
        chunk = data[offset:offset + 32768]
        writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        await writer.drain()
    if final:
        writer.write(b"0\r\n\r\n")
        await writer.drain()


async def handle_anthropic_tunnel(cr: asyncio.StreamReader, cw: asyncio.StreamWriter,
                                  proxy_url: str | None, server_ssl_ctx: ssl.SSLContext,
                                  real_base_url: str, real_api_key: str, force_model: str) -> None:
    cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await cw.drain()
    await upgrade_stream_tls(cr, cw, server_ssl_ctx, server_side=True)
    head, _ = await asyncio.wait_for(read_until_double_crlf(cr), 30)
    if not head:
        return
    method, req_path, headers = parse_head(head)
    if not urlsplit(req_path).path.startswith("/v1/"):
        cw.write(http_response(200, b"{}", {"content-type": "application/json"}))
        await cw.drain()
        return
    try:
        body = await asyncio.wait_for(read_body(cr, headers), 120)
        full_url = build_upstream_url(real_base_url, req_path)
    except (ValueError, asyncio.IncompleteReadError):
        cw.write(http_response(400, error_payload("Invalid API request"), {"content-type": "application/json"}))
        await cw.drain()
        return
    is_stream = False
    if method == "POST" and urlsplit(req_path).path == "/v1/messages":
        body, is_stream = prepare_messages_body(body, force_model)
    send_headers = upstream_headers(headers, real_api_key)
    disconnected = asyncio.create_task(wait_disconnect(cr))
    ping_task: asyncio.Task[None] | None = None

    async def ping_loop() -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await write_sse(cw, b": ping\n\n", final=False)
        except (ConnectionError, OSError):
            cw.close()

    async def stop_pinging() -> None:
        if ping_task is not None:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)

    try:
        if is_stream:
            cw.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
                     b"Transfer-Encoding: chunked\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
            await cw.drain()
            ping_task = asyncio.create_task(ping_loop())
        for attempt in range(MAX_SSE_ATTEMPTS if is_stream else 5):
            try:
                status, result_headers, data = await while_connected(
                    fetch_upstream(full_url, send_headers, body, proxy_url, method), disconnected)
            except (OSError, asyncio.TimeoutError):
                await while_connected(asyncio.sleep(min(15.0, 1.5 * (attempt + 1))), disconnected)
                continue
            if status in (402, 408, 429) or 500 <= status < 600:
                await while_connected(asyncio.sleep(min(30.0, 1.5 * (attempt + 1))), disconnected)
                continue
            if not is_stream:
                cw.write(http_response(status, data, result_headers))
                await cw.drain()
                return
            if status == 200 and retryable_sse_error(data):
                await while_connected(asyncio.sleep(min(15.0, 1.5 * (attempt + 1))), disconnected)
                continue
            if status != 200:
                result = error_event(status, data)
            elif _sse_complete(data) or _sse_terminal_error(data):
                result = data
            else:
                # JSON errors returned with HTTP 200 are still errors, not SSE.
                try:
                    payload = json.loads(data)
                except (ValueError, UnicodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("type") == "error":
                    result = error_event(status, data)
                else:
                    await while_connected(asyncio.sleep(0.8), disconnected)
                    continue
            await stop_pinging()
            await write_sse(cw, result)
            return
        await stop_pinging()
        failure = error_payload("Upstream retry budget exhausted")
        if is_stream:
            await write_sse(cw, b"event: error\ndata: " + failure + b"\n\n")
        else:
            cw.write(http_response(502, failure, {"content-type": "application/json"}))
            await cw.drain()
    except ClientDisconnected:
        pass
    finally:
        await stop_pinging()
        disconnected.cancel()
        await asyncio.gather(disconnected, return_exceptions=True)


def proxy_settings(proxy_url: str) -> tuple[str, int, ssl.SSLContext | None, str]:
    parsed = urlsplit(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Proxy must use http:// or https://")
    ctx = ssl.create_default_context() if parsed.scheme == "https" else None
    auth = ""
    if parsed.username is not None:
        token = base64.b64encode(f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    return parsed.hostname, parsed.port or (443 if ctx else 80), ctx, auth


async def open_target(host: str, port: int, proxy_url: str | None) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if proxy_url:
        proxy_host, proxy_port, ctx, _ = proxy_settings(proxy_url)
        return await asyncio.wait_for(asyncio.open_connection(
            proxy_host, proxy_port, ssl=ctx, limit=STREAM_LIMIT), 20)
    return await asyncio.wait_for(asyncio.open_connection(host, port, limit=STREAM_LIMIT), 20)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def serve_client(cr: asyncio.StreamReader, cw: asyncio.StreamWriter,
                       proxy_url: str | None, fake_ip: str, tz_val: str,
                       server_ssl_ctx: ssl.SSLContext | None, real_base_url: str,
                       real_api_key: str, force_model: str) -> None:
    uw: asyncio.StreamWriter | None = None
    try:
        head, _ = await asyncio.wait_for(read_until_double_crlf(cr), 15)
        if not head:
            return
        method, target, headers = parse_head(head)
        if method == "CONNECT":
            address = urlsplit("//" + target)
            host, port = address.hostname, address.port or 443
            if not host or address.username or address.path:
                raise ValueError("Invalid CONNECT authority")
            if port == 443 and is_anthropic_host(host):
                if server_ssl_ctx is None:
                    raise ValueError("API TLS certificate missing")
                await handle_anthropic_tunnel(cr, cw, proxy_url, server_ssl_ctx,
                                              real_base_url, real_api_key, force_model)
                return
            if is_geoip_host(host) and server_ssl_ctx is not None:
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await cw.drain()
                await upgrade_stream_tls(cr, cw, server_ssl_ctx, server_side=True)
                inner, _ = await asyncio.wait_for(read_until_double_crlf(cr), 10)
                _, path, inner_headers = parse_head(inner)
                cw.write(build_geoip_response(host, path, inner_headers, fake_ip, tz_val))
                await cw.drain()
                return
            ur, uw = await open_target(host, port, proxy_url)
            if proxy_url:
                _, _, _, auth = proxy_settings(proxy_url)
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                uw.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n{auth}\r\n".encode("latin-1"))
                await uw.drain()
                response_head, _ = await asyncio.wait_for(read_until_double_crlf(ur), 30)
                cw.write(response_head)
                await cw.drain()
                if response_head.split(b" ", 2)[1:2] != [b"200"]:
                    return
            else:
                cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await cw.drain()
            await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
            return

        parsed = urlsplit(target if target.startswith("http://") else "http://" + headers.get("host", "") + target)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username:
            raise ValueError("Unsupported proxy target")
        host, port = parsed.hostname, parsed.port or 80
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        if is_geoip_host(host):
            cw.write(build_geoip_response(host, path, headers, fake_ip, tz_val))
            await cw.drain()
            return
        ur, uw = await open_target(host, port, proxy_url)
        request_target = urlunsplit(("http", parsed.netloc, parsed.path or "/", parsed.query, "")) if proxy_url else path
        lines = [f"{method} {request_target} HTTP/1.1", f"Host: {parsed.netloc}", "Connection: close"]
        for key, value in headers.items():
            if key not in ("host", "connection", "proxy-connection", "proxy-authorization"):
                lines.append(f"{key}: {value}")
        if proxy_url:
            _, _, _, auth = proxy_settings(proxy_url)
            if auth:
                lines.append(auth.rstrip("\r\n"))
        uw.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await uw.drain()
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError) as exc:
        LOG.warning("Proxy request failed: %s", type(exc).__name__)
    finally:
        if uw is not None:
            uw.close()
        cw.close()


async def main() -> None:
    mode, bind_target, up_host, up_port_s, fake_ip, tz_val, ca_dir = sys.argv[1:8]
    proxy_url = os.environ.get("UPSTREAM_PROXY_URL")
    if proxy_url is None:
        proxy_url = f"http://{up_host}:{up_port_s}" if up_host and int(up_port_s or "0") else ""
    if proxy_url:
        proxy_settings(proxy_url)
    real_base_url = os.environ.get("REAL_BASE_URL", "https://api.anthropic.com")
    build_upstream_url(real_base_url, "/v1/messages")
    real_api_key = os.environ.get("REAL_API_KEY", "")
    force_model = os.environ.get("FORCE_CLAUDE_MODEL", "")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(ca_dir, "geoip.crt"), os.path.join(ca_dir, "geoip.key"))
    ctx.set_alpn_protocols(["http/1.1"])
    active: set[asyncio.Task[Any]] = set()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        active.add(task)
        try:
            await serve_client(reader, writer, proxy_url or None, fake_ip, tz_val, ctx,
                               real_base_url, real_api_key, force_model)
        finally:
            active.discard(task)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    if mode == "unix":
        server = await asyncio.start_unix_server(handler, path=bind_target, limit=STREAM_LIMIT)
        os.chmod(bind_target, 0o660)
    elif mode == "tcp":
        server = await asyncio.start_server(handler, "127.0.0.1", 0, limit=STREAM_LIMIT)
        Path(bind_target).write_text(str(server.sockets[0].getsockname()[1]))
    else:
        raise ValueError("Unknown listen mode")
    try:
        async with server:
            await stop.wait()
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--fetch"]:
        fetch_worker()
    else:
        asyncio.run(main())
