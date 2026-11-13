"""Local protocol regressions. No real API credentials or network services needed."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import proxy_bridge as bridge

STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
START = b'event: message_start\ndata: {"type":"message_start","message":{"id":"test","content":[]}}\n\n'


def event(name, payload):
    return b"event: " + name.encode() + b"\ndata: " + json.dumps(payload).encode() + b"\n\n"


class ParserTests(unittest.TestCase):
    def test_fragmented_error_and_stop(self):
        error = event("error", {"type": "error", "error": {"type": "overloaded_error", "message": "x" * 2000}})
        for data in (error, START + error, STOP, START + STOP):
            for fragment in (1, 7, 511, 2048):
                parser = bridge.SSEParser()
                parsed = []
                for offset in range(0, len(data), fragment):
                    parsed.extend(parser.feed(data[offset:offset + fragment]))
                self.assertIn(parsed[-1].name, ("error", "message_stop"))
                self.assertIsInstance(parsed[-1].payload, dict)
        self.assertFalse(bridge._sse_complete(STOP[:-1]))
        self.assertTrue(bridge._sse_complete(STOP.replace(b"\n", b"\r\n")))

    def test_ordinary_text_is_not_an_error(self):
        data = START + event("content_block_delta", {
            "type": "content_block_delta", "delta": {"type": "text_delta", "text":
            'Example log: concurrency limit exceeded; too many pending requests; {"type":"error"}'},
        }) + STOP
        self.assertTrue(bridge._sse_complete(data))
        self.assertFalse(bridge._sse_terminal_error(data))
        self.assertFalse(bridge.retryable_sse_error(data))

    def test_model_mapping_does_not_rewrite_content(self):
        content = 'Discuss claude-opus, {"type":"error"}, and concurrency limit exceeded.'
        for model, expected in (("claude-opus-test[1m]", "main"), ("claude-fable-test", "main"),
                                ("claude-haiku-test", "claude-haiku-test"),
                                ("claude-sonnet-test", "claude-sonnet-test")):
            body = {"model": model, "stream": True, "messages": [{"role": "user", "content": content}]}
            result, streaming = bridge.prepare_messages_body(json.dumps(body).encode(), "main[1m]")
            result = json.loads(result)
            self.assertTrue(streaming)
            self.assertEqual(result["model"], expected)
            self.assertEqual(result["messages"], body["messages"])

    def test_base_url_prefixes_and_query(self):
        for base, expected in (
            ("https://example.test", "/v1/messages"),
            ("https://example.test/v1/", "/v1/messages"),
            ("https://example.test/api", "/api/v1/messages"),
            ("https://example.test/api/v1", "/api/v1/messages"),
        ):
            self.assertEqual(bridge.build_upstream_url(base, "/v1/messages?beta=true"),
                             "https://example.test" + expected + "?beta=true")
        with self.assertRaises(ValueError):
            bridge.build_upstream_url("https://example.test", "https://elsewhere.test/v1/messages")


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_dir = tempfile.TemporaryDirectory(prefix="cc-proxy-cert-")
        cert = Path(cls.cert_dir.name) / "cert.pem"
        key = Path(cls.cert_dir.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=api.anthropic.com",
                        "-addext", "subjectAltName=DNS:api.anthropic.com"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_ctx.load_cert_chain(cert, key)
        cls.client_ctx = ssl.create_default_context(cafile=str(cert))

    @classmethod
    def tearDownClass(cls):
        cls.cert_dir.cleanup()

    async def asyncSetUp(self):
        self.tasks = set()

    async def asyncTearDown(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def server(self, handler):
        async def tracked(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                self.tasks.discard(task)
        server = await asyncio.start_server(tracked, "127.0.0.1", 0, limit=bridge.STREAM_LIMIT)
        try:
            yield server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()

    @asynccontextmanager
    async def api_proxy(self, upstream, *, base_path="", proxy_url=None):
        async def handler(reader, writer):
            await bridge.serve_client(reader, writer, proxy_url, "192.0.2.1", "UTC",
                                      self.server_ctx, f"http://127.0.0.1:{upstream}{base_path}",
                                      "DUMMY_TEST_KEY", "main-model")
        async with self.server(handler) as port:
            yield port

    async def api_connect(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=bridge.STREAM_LIMIT)
        writer.write(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n")
        await writer.drain()
        self.assertIn(b"200", await reader.readuntil(b"\r\n\r\n"))
        await writer.start_tls(self.client_ctx, server_hostname="api.anthropic.com")
        return reader, writer

    async def send_api(self, port, method, path, body=b"", chunked=False):
        reader, writer = await self.api_connect(port)
        framing = "Transfer-Encoding: chunked" if chunked else f"Content-Length: {len(body)}"
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: api.anthropic.com\r\n{framing}\r\n\r\n".encode())
        if chunked:
            for offset in range(0, len(body), 12000):
                part = body[offset:offset + 12000]
                writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            writer.write(b"0\r\n\r\n")
        else:
            writer.write(body)
        await writer.drain()
        try:
            result = await asyncio.wait_for(reader.read(), 5)
            head, _, content = result.partition(b"\r\n\r\n")
            if b"chunked" in head:
                body_reader = asyncio.StreamReader()
                body_reader.feed_data(content)
                body_reader.feed_eof()
                content = await bridge.read_body(body_reader, {"transfer-encoding": "chunked"})
            return head, content
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_non_streaming_methods_large_chunked_body_and_v1_prefix(self):
        requests = []
        async def upstream(reader, writer):
            head, _ = await bridge.read_until_double_crlf(reader)
            method, path, headers = bridge.parse_head(head)
            body = await bridge.read_body(reader, headers)
            requests.append((method, path, body, headers))
            writer.write(bridge.http_response(200, b'{"ok":true}', {"content-type": "application/json"}))
            await writer.drain()
        async with self.server(upstream) as target, self.api_proxy(target, base_path="/v1") as proxy:
            head, body = await self.send_api(proxy, "GET", "/v1/models?limit=2")
            self.assertIn(b"200", head)
            self.assertEqual(body, b'{"ok":true}')
            raw = json.dumps({"model": "custom", "messages": [{"role": "user", "content": "a" * 300000}]}).encode()
            _, body = await self.send_api(proxy, "POST", "/v1/messages", raw, chunked=True)
            self.assertEqual(body, b'{"ok":true}')
        self.assertEqual(requests[0][:2], ("GET", "/v1/models?limit=2"))
        self.assertEqual(requests[1][1], "/v1/messages")
        self.assertEqual(json.loads(requests[1][2]), json.loads(raw))
        self.assertEqual(requests[1][3]["x-api-key"], "DUMMY_TEST_KEY")

    async def test_direct_connect_and_plain_http(self):
        got = asyncio.get_running_loop().create_future()
        async def target(reader, writer):
            data = await reader.readexactly(5)
            got.set_result(data)
            writer.write(data)
            await writer.drain()
        async with self.server(target) as target_port, self.api_proxy(target_port) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
            writer.write(f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\nHost: ignored\r\n\r\nHELLO".encode())
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 2), b"HELLO")
            self.assertEqual(await got, b"HELLO")
            writer.close()
            await writer.wait_closed()
        async def http_target(reader, writer):
            head, _ = await bridge.read_until_double_crlf(reader)
            self.assertEqual(bridge.parse_head(head)[:2], ("GET", "/path?q=1"))
            writer.write(bridge.http_response(200, b"OK"))
            await writer.drain()
        async with self.server(http_target) as target_port, self.api_proxy(target_port) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
            writer.write(f"GET http://127.0.0.1:{target_port}/path?q=1 HTTP/1.1\r\nHost: ignored\r\n\r\n".encode())
            await writer.drain()
            self.assertTrue((await asyncio.wait_for(reader.read(), 2)).endswith(b"OK"))
            writer.close()
            await writer.wait_closed()

    async def test_direct_fetch_ignores_environment_proxy_and_preserves_http_error(self):
        async def upstream(reader, writer):
            await bridge.read_until_double_crlf(reader)
            writer.write(bridge.http_response(403, b'{"reason":"denied"}', {"content-type": "application/json"}))
            await writer.drain()
        async with self.server(upstream) as target:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                                         "https_proxy": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
                status, _, body = await bridge.fetch_upstream(f"http://127.0.0.1:{target}/", {}, b"", None, "GET", timeout=2)
            self.assertEqual((status, body), (403, b'{"reason":"denied"}'))

    async def test_terminal_event_does_not_wait_for_keepalive_eof(self):
        for data in (event("error", {"type": "error", "error": {"type": "overloaded_error", "message": "x" * 2000}}),
                     START + STOP):
            async def upstream(reader, writer):
                await bridge.read_until_double_crlf(reader)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                for offset in range(0, len(data), 13):
                    part = data[offset:offset + 13]
                    writer.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    await writer.drain()
                await reader.read()  # No zero chunk or TCP EOF from this server.
            async with self.server(upstream) as target:
                status, _, result = await bridge.fetch_upstream(f"http://127.0.0.1:{target}/", {}, b"", None, "GET", timeout=2)
                self.assertEqual(status, 200)
                self.assertEqual(result, data)

    async def test_client_disconnect_and_deadline_close_upstream(self):
        connected = asyncio.Event()
        closed = asyncio.Event()
        async def blocked(reader, writer):
            head, _ = await bridge.read_until_double_crlf(reader)
            _, _, headers = bridge.parse_head(head)
            await bridge.read_body(reader, headers)
            connected.set()
            await reader.read()
            closed.set()
        async with self.server(blocked) as target, self.api_proxy(target) as proxy:
            reader, writer = await self.api_connect(proxy)
            body = b'{"model":"custom","stream":true,"messages":[]}'
            writer.write(f"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            await asyncio.wait_for(connected.wait(), 2)
            writer.transport.abort()
            await asyncio.wait_for(closed.wait(), 2)
        connected.clear()
        closed.clear()
        async with self.server(blocked) as target:
            with self.assertRaises(asyncio.TimeoutError):
                await bridge.fetch_upstream(f"http://127.0.0.1:{target}/", {}, b"", None, timeout=0.5)
            await asyncio.wait_for(closed.wait(), 2)

    async def test_stream_keepalive_refusal_and_error_budget(self):
        calls = 0
        refusal = START + event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "refusal"}}) + STOP
        async def upstream(reader, writer):
            nonlocal calls
            calls += 1
            head, _ = await bridge.read_until_double_crlf(reader)
            await bridge.read_body(reader, bridge.parse_head(head)[2])
            await asyncio.sleep(0.12)
            writer.write(bridge.http_response(200, refusal, {"content-type": "text/event-stream"}))
            await writer.drain()
        async with self.server(upstream) as target, self.api_proxy(target) as proxy:
            with patch.object(bridge, "PING_INTERVAL", 0.03):
                head, data = await self.send_api(proxy, "POST", "/v1/messages", b'{"stream":true,"messages":[]}')
            self.assertIn(b"200", head)
            self.assertIn(b": ping\n\n", data)
            self.assertTrue(data.endswith(refusal))
            self.assertEqual(calls, 1)
        async with self.server(upstream) as target, self.api_proxy(target) as proxy:
            with patch.object(bridge, "MAX_SSE_ATTEMPTS", 0):
                _, data = await self.send_api(proxy, "POST", "/v1/messages", b'{"stream":true}')
            self.assertTrue(bridge._sse_terminal_error(data))
            self.assertIn(b"retry budget exhausted", data)

    async def test_upstream_proxy_auth_and_connect(self):
        requests = []
        async def upstream_proxy(reader, writer):
            head, _ = await bridge.read_until_double_crlf(reader)
            method, target, headers = bridge.parse_head(head)
            requests.append((method, target, headers))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            data = await reader.readexactly(4)
            writer.write(data)
            await writer.drain()
        async with self.server(upstream_proxy) as target, self.api_proxy(target, proxy_url=f"http://user:pass@127.0.0.1:{target}") as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy)
            writer.write(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"PING")
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b"PING")
            writer.close()
            await writer.wait_closed()
        self.assertEqual(requests[0][0:2], ("CONNECT", "example.test:443"))
        self.assertEqual(requests[0][2]["proxy-authorization"], "Basic dXNlcjpwYXNz")


if __name__ == "__main__":
    unittest.main()
