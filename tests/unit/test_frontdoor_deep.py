from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import coincurve
import pytest

from agnova import frontdoor, nostr


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.drains = 0
        self.tls: tuple[ssl.SSLContext, str] | None = None

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        self.drains += 1

    def close(self) -> None:
        self.closed = True

    async def start_tls(self, ctx: ssl.SSLContext, server_hostname: str) -> None:
        self.tls = (ctx, server_hostname)


class ClosingWriter(Writer):
    def close(self) -> None:
        super().close()
        raise RuntimeError("already gone")


async def reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def secret() -> coincurve.PrivateKey:
    return nostr.load_secret_key("11" * 32)


def test_upstream_proxy_is_reread_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = frontdoor.Upstream("wss://relay.example/socket", "http://old-proxy:8080", "ca.pem")

    assert upstream.host == "relay.example"
    assert upstream.port == 443
    assert upstream.origin == "https://relay.example"
    assert upstream.host_header == "relay.example"
    assert upstream.ws_origin == "wss://relay.example"
    assert upstream.proxy and upstream.proxy.geturl() == "http://old-proxy:8080"

    monkeypatch.setenv("HTTPS_PROXY", "http://new-proxy:9999")
    assert upstream.proxy and upstream.proxy.hostname == "new-proxy"

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("https_proxy", "http://lower-proxy:8888")
    assert upstream.proxy and upstream.proxy.hostname == "lower-proxy"

    direct = frontdoor.Upstream("http://relay.example:8081", None, None)
    assert direct.proxy is None
    assert direct.host_header == "relay.example:8081"
    assert direct.origin == "http://relay.example:8081"
    assert direct.ws_origin == "ws://relay.example:8081"


def test_upstream_rejects_urls_without_hosts() -> None:
    with pytest.raises(ValueError, match="relay URL has no host"):
        frontdoor.Upstream("wss:///missing-host", None, None)


def test_upstream_connects_direct_and_wraps_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str | None, int | None]] = []
    upstream_writer = Writer()
    upstream_reader_holder: list[asyncio.StreamReader] = []

    async def open_connection(host: str | None, port: int | None) -> tuple[asyncio.StreamReader, Writer]:
        calls.append((host, port))
        return upstream_reader_holder[0], upstream_writer

    async def run() -> tuple[asyncio.StreamReader, Writer]:
        upstream_reader_holder.append(await reader_with(b""))
        return await frontdoor.Upstream("wss://relay.example", None, None).connect()

    monkeypatch.setattr(frontdoor.asyncio, "open_connection", open_connection)

    reader, writer = asyncio.run(run())

    assert reader is upstream_reader_holder[0]
    assert writer is upstream_writer
    assert calls == [("relay.example", 443)]
    assert upstream_writer.tls is not None
    assert upstream_writer.tls[1] == "relay.example"


def test_upstream_connects_direct_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream_writer = Writer()

    async def open_connection(host: str | None, port: int | None) -> tuple[asyncio.StreamReader, Writer]:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, upstream_writer

    monkeypatch.setattr(frontdoor.asyncio, "open_connection", open_connection)
    reader, writer = asyncio.run(frontdoor.Upstream("ws://relay.example:80", None, None).connect())
    assert writer.tls is None


def test_upstream_connects_through_proxy_and_reports_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def accepted(
        host: str | None, port: int | None
    ) -> tuple[asyncio.StreamReader, Writer]:
        assert (host, port) == ("proxy.example", 3128)
        return (
            await reader_with(b"HTTP/1.1 200 Connection Established\r\nVia: test\r\n\r\n"),
            Writer(),
        )

    monkeypatch.setattr(frontdoor.asyncio, "open_connection", accepted)
    reader, writer = asyncio.run(
        frontdoor.Upstream("ws://relay.example:80", "http://proxy.example:3128", None).connect()
    )
    assert reader.at_eof()
    assert bytes(writer.data).startswith(b"CONNECT relay.example:80 HTTP/1.1")
    assert writer.tls is None

    refused_writer = Writer()

    async def refused(
        host: str | None, port: int | None
    ) -> tuple[asyncio.StreamReader, Writer]:
        del host, port
        return await reader_with(b"HTTP/1.1 403 Forbidden\r\n\r\n"), refused_writer

    monkeypatch.setattr(frontdoor.asyncio, "open_connection", refused)
    with pytest.raises(ConnectionError, match="proxy refused CONNECT"):
        asyncio.run(
            frontdoor.Upstream("ws://relay.example", "http://proxy.example:3128", None).connect()
        )
    assert refused_writer.closed is True


@pytest.mark.parametrize("payload", [b"hello", b"x" * 130, b"y" * 70000])
def test_read_ws_frame_decodes_masked_and_extended_lengths(payload: bytes) -> None:
    frame = frontdoor.encode_ws_frame(frontdoor.OPCODE_TEXT, payload, fin=False)

    async def read_frame() -> tuple[int, bool, bytes, bytes]:
        return await frontdoor.read_ws_frame(await reader_with(frame))

    opcode, fin, decoded, raw = asyncio.run(read_frame())

    assert opcode == frontdoor.OPCODE_TEXT
    assert fin is False
    assert decoded == payload
    assert raw == frame


def test_read_ws_frame_preserves_unmasked_frames() -> None:
    raw = b"\x82\x03abc"

    async def read_frame() -> tuple[int, bool, bytes, bytes]:
        return await frontdoor.read_ws_frame(await reader_with(raw))

    opcode, fin, payload, preserved = asyncio.run(read_frame())

    assert (opcode, fin, payload, preserved) == (0x2, True, b"abc", raw)


def test_pipe_copies_until_eof_and_closes_destination() -> None:
    dst = Writer()

    async def run() -> None:
        await frontdoor.pipe(await reader_with(b"one-two"), dst)

    asyncio.run(run())

    assert bytes(dst.data) == b"one-two"
    assert dst.closed is True


def test_pipe_treats_disconnects_as_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    class ResetReader:
        async def read(self, size: int) -> bytes:
            del size
            raise ConnectionResetError

    dst = ClosingWriter()

    asyncio.run(frontdoor.pipe(ResetReader(), dst))

    assert dst.closed is True


def test_rewrite_corrects_host_authorization_and_connection_headers() -> None:
    door = frontdoor.FrontDoor(
        frontdoor.Upstream("wss://relay.example:4443", None, None), secret(), quiet=True
    )
    rewritten = door.rewrite(
        b"POST /query HTTP/1.1",
        [
            b"Host: 127.0.0.1:8443",
            b"Authorization: old-token",
            b"Connection: keep-alive",
            b"Proxy-Connection: keep-alive",
            b"Content-Length: 2",
        ],
        "Nostr fresh",
        websocket=False,
    )

    assert b"Host: relay.example:4443" in rewritten
    assert b"Authorization: Nostr fresh" in rewritten
    assert b"Connection: keep-alive" not in rewritten
    assert b"Proxy-Connection" not in rewritten
    assert rewritten.endswith(b"Connection: close\r\n\r\n")

    websocket = door.rewrite(
        b"GET / HTTP/1.1",
        [b"Host: loopback", b"Connection: Upgrade"],
        None,
        websocket=True,
    )
    assert b"Connection: Upgrade" in websocket
    assert b"Connection: close" not in websocket


def test_resign_auth_ignores_non_auth_and_other_pubkeys(capsys: pytest.CaptureFixture[str]) -> None:
    door = frontdoor.FrontDoor(frontdoor.Upstream("wss://relay.example", None, None), secret())
    other = nostr.sign_event(nostr.load_secret_key("22" * 32), 22242, [["relay", "ws://local"]], "")

    assert door.resign_auth(b"not-json") is None
    assert door.resign_auth(json.dumps(["EVENT", {}]).encode()) is None
    assert door.resign_auth(json.dumps(["AUTH", {"kind": 1}]).encode()) is None
    assert door.resign_auth(json.dumps(["AUTH", other]).encode()) is None
    assert "different pubkey" in capsys.readouterr().out


def test_resign_auth_rewrites_relay_tag_preserving_other_tags() -> None:
    door = frontdoor.FrontDoor(
        frontdoor.Upstream("wss://relay.example/ws", None, None), secret(), quiet=True
    )
    event = nostr.sign_event(
        secret(),
        22242,
        [["relay", "ws://127.0.0.1:8443", "kept"], ["auth", "owner", "cond", "sig"]],
        "challenge",
    )

    replacement = door.resign_auth(json.dumps(["AUTH", event]).encode())

    assert replacement is not None
    message = json.loads(replacement)
    assert message[0] == "AUTH"
    assert ["relay", "wss://relay.example", "kept"] in message[1]["tags"]
    assert ["auth", "owner", "cond", "sig"] in message[1]["tags"]
    assert message[1]["content"] == "challenge"


def test_resign_auth_adds_missing_relay_tag() -> None:
    door = frontdoor.FrontDoor(frontdoor.Upstream("ws://relay.example:8080", None, None), secret())
    event = nostr.sign_event(secret(), 22242, [["challenge", "abc"]], "")

    replacement = door.resign_auth(json.dumps(["AUTH", event]).encode())

    assert replacement is not None
    tags = json.loads(replacement)[1]["tags"]
    assert ["relay", "ws://relay.example:8080"] in tags


def test_pump_client_frames_rewrites_first_auth_then_pipes_tail() -> None:
    door = frontdoor.FrontDoor(frontdoor.Upstream("wss://relay.example", None, None), secret())
    event = nostr.sign_event(secret(), 22242, [["relay", "ws://local"]], "")
    frame = frontdoor.encode_ws_frame(frontdoor.OPCODE_TEXT, json.dumps(["AUTH", event]).encode())
    writer = Writer()

    async def run_pump() -> None:
        await door.pump_client_frames(await reader_with(frame + b"tail"), writer)

    asyncio.run(run_pump())

    async def read_out() -> tuple[int, bool, bytes, bytes]:
        return await frontdoor.read_ws_frame(await reader_with(bytes(writer.data[:-4])))

    opcode, _fin, payload, _raw = asyncio.run(read_out())
    assert opcode == frontdoor.OPCODE_TEXT
    assert json.loads(payload)[1]["tags"][0][1] == "wss://relay.example"
    assert bytes(writer.data).endswith(b"tail")
    assert writer.closed is True


def test_pump_client_frames_forwards_unchanged_until_disconnect() -> None:
    door = frontdoor.FrontDoor(frontdoor.Upstream("wss://relay.example", None, None), secret())
    frame = frontdoor.encode_ws_frame(0x2, b"binary")
    writer = Writer()

    async def run() -> None:
        await door.pump_client_frames(await reader_with(frame), writer)

    asyncio.run(run())

    assert bytes(writer.data) == frame
    assert writer.closed is True


def test_handle_closes_bad_client_requests() -> None:
    async def run_case(data: bytes) -> Writer:
        door = frontdoor.FrontDoor(frontdoor.Upstream("wss://relay.example", None, None), secret())
        writer = Writer()
        await door.handle(await reader_with(data), writer)
        return writer

    assert asyncio.run(run_case(b"not-http\r\n\r\n")).closed is True
    oversized = b"GET / HTTP/1.1\r\n" + (b"X: y\r\n" * 20000) + b"\r\n"
    assert asyncio.run(run_case(oversized)).closed is True


def test_handle_returns_502_when_upstream_connect_fails() -> None:
    class FailedUpstream(frontdoor.Upstream):
        async def connect(self) -> tuple[asyncio.StreamReader, Writer]:
            raise OSError("no route")

    door = frontdoor.FrontDoor(FailedUpstream("wss://relay.example", None, None), secret(), quiet=True)
    writer = Writer()

    async def run() -> None:
        await door.handle(
            await reader_with(b"GET / HTTP/1.1\r\nHost: local\r\n\r\n"), writer
        )

    asyncio.run(run())

    assert b"502 Bad Gateway" in bytes(writer.data)
    assert writer.closed is True


def test_handle_rewrites_http_request_and_pipes_response() -> None:
    up_writer = Writer()
    up_reader_holder: list[asyncio.StreamReader] = []

    class Up(frontdoor.Upstream):
        async def connect(self) -> tuple[asyncio.StreamReader, Writer]:
            return up_reader_holder[0], up_writer

    door = frontdoor.FrontDoor(Up("wss://relay.example", None, None), secret(), quiet=True)
    client_writer = Writer()
    request = (
        b"POST /query HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8443\r\n"
        b"Authorization: stale\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n{}"
    )

    async def run() -> None:
        up_reader_holder.append(
            await reader_with(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        )
        await door.handle(await reader_with(request), client_writer)

    asyncio.run(run())

    sent = bytes(up_writer.data)
    assert sent.startswith(b"POST /query HTTP/1.1\r\nHost: relay.example\r\n")
    assert b"Authorization: Nostr " in sent
    assert sent.endswith(b"\r\n\r\n{}")
    assert bytes(client_writer.data).endswith(b"\r\n\r\nok")


def test_handle_websocket_uses_frame_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    up_writer = Writer()
    up_reader_holder: list[asyncio.StreamReader] = []
    pumped: list[tuple[asyncio.StreamReader, Writer]] = []

    class Up(frontdoor.Upstream):
        async def connect(self) -> tuple[asyncio.StreamReader, Writer]:
            return up_reader_holder[0], up_writer

    async def fake_pump(reader: asyncio.StreamReader, writer: Writer) -> None:
        pumped.append((reader, writer))
        writer.close()

    door = frontdoor.FrontDoor(Up("wss://relay.example", None, None), secret(), quiet=True)
    monkeypatch.setattr(door, "pump_client_frames", fake_pump)

    async def run() -> None:
        up_reader_holder.append(
            await reader_with(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
        )
        await door.handle(
            await reader_with(b"GET / HTTP/1.1\r\nHost: local\r\nUpgrade: websocket\r\n\r\n"),
            Writer(),
        )

    asyncio.run(run())

    assert pumped and pumped[0][1] is up_writer
    assert b"Connection: close" not in bytes(up_writer.data)


def test_serve_starts_server_and_logs_route(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Server:
        async def __aenter__(self) -> Server:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def serve_forever(self) -> None:
            raise RuntimeError("stop")

    async def start_server(handler: object, host: str, port: int) -> Server:
        seen["start"] = (handler, host, port)
        return Server()

    monkeypatch.setenv("HTTPS_PROXY", "http://live-proxy:3128")
    monkeypatch.setattr(frontdoor.asyncio, "start_server", start_server)
    door = frontdoor.FrontDoor(
        frontdoor.Upstream("wss://relay.example", "http://proxy:1", None), secret()
    )

    with pytest.raises(RuntimeError, match="stop"):
        asyncio.run(door.serve("127.0.0.1", 9999))

    assert seen["start"][1:] == ("127.0.0.1", 9999)


def test_ca_bundle_prefers_explicit_then_existing_standard_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "explicit.pem"
    standard = tmp_path / "standard.pem"
    explicit.write_text("ca", encoding="utf-8")
    standard.write_text("ca", encoding="utf-8")

    monkeypatch.setenv("BUZZ_CA_BUNDLE", str(explicit))
    assert frontdoor.ca_bundle() == str(explicit)

    monkeypatch.delenv("BUZZ_CA_BUNDLE")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(standard))
    assert frontdoor.ca_bundle() == str(standard)

    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE")
    assert frontdoor.ca_bundle() is None


def test_build_validates_environment_and_constructs_frontdoor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BUZZ_RELAY_URL", raising=False)
    monkeypatch.delenv("BUZZ_PRIVATE_KEY", raising=False)

    with pytest.raises(SystemExit, match="BUZZ_RELAY_URL"):
        frontdoor.build()

    monkeypatch.setenv("BUZZ_RELAY_URL", "wss://relay.example")
    with pytest.raises(SystemExit, match="BUZZ_PRIVATE_KEY"):
        frontdoor.build()

    monkeypatch.setenv("BUZZ_PRIVATE_KEY", "11" * 32)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    door = frontdoor.build(quiet=True)
    assert door.quiet is True
    assert door.upstream.proxy == urlparse("http://proxy.example:3128")
    assert nostr.public_key_hex(door.secret) == nostr.public_key_hex(secret())


def test_main_runs_server_and_swallows_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served: list[tuple[str, int, bool]] = []

    class Door:
        def __init__(self, quiet: bool) -> None:
            self.quiet = quiet

        async def serve(self, host: str, port: int) -> None:
            served.append((host, port, self.quiet))

    def fake_build(
        relay_url: str | None = None, secret_raw: str | None = None, quiet: bool = False
    ) -> Door:
        del relay_url, secret_raw
        return Door(quiet)

    monkeypatch.setattr(frontdoor, "build", fake_build)
    monkeypatch.setattr(frontdoor.sys, "argv", ["frontdoor", "--host", "0.0.0.0", "--port", "9000", "--quiet"])
    frontdoor.main()
    assert served == [("0.0.0.0", 9000, True)]

    def raise_interrupt(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        raise KeyboardInterrupt

    monkeypatch.setattr(frontdoor.asyncio, "run", raise_interrupt)
    monkeypatch.setattr(frontdoor.sys, "argv", ["frontdoor"])
    frontdoor.main()


def test_main_uses_default_port_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[str, int]] = []

    async def fake_serve(host: str, port: int) -> None:
        observed.append((host, port))

    monkeypatch.setenv("BUZZ_FRONTDOOR_PORT", "9777")
    monkeypatch.setattr(frontdoor, "build", lambda quiet=False: SimpleNamespace(serve=fake_serve))
    monkeypatch.setattr(frontdoor.sys, "argv", ["frontdoor"])

    frontdoor.main()

    assert observed == [("127.0.0.1", 9777)]
