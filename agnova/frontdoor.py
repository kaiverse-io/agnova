#!/usr/bin/env python3
"""Front door — a local plaintext entrance to a Buzz relay.

**Why this exists.** In a Claude Code remote environment, outbound traffic is
supposed to go through an agent proxy. There are two ways to get there:

  * ask the proxy for a tunnel (`CONNECT`), or
  * dial the host directly and be transparently intercepted.

A WebSocket upgrade succeeds on the first path (`101 Switching Protocols`) and
is refused on the second (`403 Forbidden`). `buzz-cli` takes the first path
because reqwest honours `HTTPS_PROXY`; `buzz-acp` takes the second because
`tokio-tungstenite` never reads it. Same host, same network, same relay — one
library asks and the other doesn't.

That is the *entire* reason `buzz-acp` could not run here. Not policy, not TLS
roots, not the relay. Three earlier rounds concluded "the proxy blocks
WebSockets"; none of them tried a tunnelled upgrade.

So: listen in plaintext on loopback, correct the request, and carry it out over
a proper tunnel.

    buzz-acp ──ws://127.0.0.1──▶ front door ──CONNECT + TLS──▶ relay

Loopback is exempt from the proxy and plaintext means no CA trust question, so
the upstream harness runs **stock** — no fork, no patched build, no
`/etc/hosts` entry, no certificates.

Three corrections are applied, all for the same underlying reason — the client
believes it is talking to `127.0.0.1`, and the relay checks that belief:

1. **`Host:`** — rewritten to the relay's real hostname. A wrong `Host` is
   rejected with 403 by the CDN in front of the relay, so this is required, not
   cosmetic.
2. **`Authorization:`** — NIP-98 tokens are bound to the exact request URL, and
   the relay deliberately does not alias loopback to anything. A token minted
   for `http://127.0.0.1:PORT/query` can never verify upstream, so the front
   door discards it and mints its own.
3. **The NIP-42 `AUTH` frame** — its `relay` tag must name the relay's own
   public URL, which the relay reconstructs from its config and the bound
   tenant. A client connected to loopback signs the loopback URL and is
   rejected with `auth-required: verification failed`. The front door rewrites
   that one tag and re-signs, preserving every other tag (notably a NIP-OA
   `auth` tag, without which an owner-attested agent loses its attestation).

2 and 3 are why the front door needs the agent key. It only ever re-signs
events that already carry that agent's pubkey — anything else is passed
through untouched, so this can never launder another identity.

Everything else is byte-for-byte passthrough.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import ssl
import struct
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agnova import nostr  # noqa: E402

# Enough for any request head buzz-acp sends; a client that exceeds it is
# malfunctioning, and refusing beats buffering without bound.
MAX_HEAD_BYTES = 64 * 1024
# Bound on the proxy handshake and the upstream TLS setup.
CONNECT_TIMEOUT = 30
IO_CHUNK = 64 * 1024


class Upstream:
    """Where the relay actually is, and how to reach it."""

    def __init__(self, relay_url: str, proxy_url: str | None, ca_bundle: str | None):
        parsed = urlparse(relay_url)
        self.host = parsed.hostname
        if not self.host:
            raise ValueError(f"relay URL has no host: {relay_url!r}")
        # ws/wss are the same wire as http/https; the scheme only decides TLS.
        self.tls = parsed.scheme in ("wss", "https")
        self.port = parsed.port or (443 if self.tls else 80)
        self.proxy = urlparse(proxy_url) if proxy_url else None
        self.ca_bundle = ca_bundle

    @property
    def origin(self) -> str:
        """Scheme + host as the relay sees itself — what NIP-98 must be signed against."""
        scheme = "https" if self.tls else "http"
        default = 443 if self.tls else 80
        hostpart = self.host if self.port == default else f"{self.host}:{self.port}"
        return f"{scheme}://{hostpart}"

    @property
    def host_header(self) -> str:
        default = 443 if self.tls else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"

    @property
    def ws_origin(self) -> str:
        """The relay's own WebSocket URL — what a NIP-42 `relay` tag must say.

        The relay compares this string (normalised) against the tag, and the
        comparison is scheme-sensitive, so `https://` would not do.
        """
        scheme = "wss" if self.tls else "ws"
        default = 443 if self.tls else 80
        hostpart = self.host if self.port == default else f"{self.host}:{self.port}"
        return f"{scheme}://{hostpart}"

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a connection to the relay, tunnelling through the proxy if there is one."""
        if self.proxy:
            # Every step here is bounded. A proxy that accepts the TCP
            # connection and then never answers CONNECT would otherwise park
            # this coroutine forever, leaking one connection per attempt — and
            # a flaky proxy path is the exact condition this file exists for.
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.proxy.hostname, self.proxy.port),
                timeout=CONNECT_TIMEOUT,
            )
            writer.write(
                f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n\r\n".encode()
            )
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)
            while (await asyncio.wait_for(reader.readline(), timeout=CONNECT_TIMEOUT)).strip():
                pass
            if b" 200" not in status:
                writer.close()
                detail = status.decode(errors="replace").strip()
                raise ConnectionError(f"proxy refused CONNECT: {detail}")
        else:
            # No proxy configured — a normal host. The front door is then a
            # plain relay-to-TLS adapter, which still works and keeps one code
            # path rather than two.
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=CONNECT_TIMEOUT
            )

        if self.tls:
            ctx = ssl.create_default_context(cafile=self.ca_bundle)
            await asyncio.wait_for(
                writer.start_tls(ctx, server_hostname=self.host), timeout=CONNECT_TIMEOUT
            )
        return reader, writer


def parse_head(head: bytes) -> tuple[bytes, list[bytes]]:
    lines = head.rstrip(b"\r\n").split(b"\r\n")
    return lines[0], lines[1:]


def header_value(headers: list[bytes], name: str) -> str | None:
    prefix = name.lower().encode() + b":"
    for line in headers:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip().decode(errors="replace")
    return None


def is_websocket_upgrade(headers: list[bytes]) -> bool:
    upgrade = (header_value(headers, "upgrade") or "").lower()
    return "websocket" in upgrade


OPCODE_TEXT = 0x1


async def read_ws_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes, bytes]:
    """Read one WebSocket frame. Returns (opcode, fin, payload, raw_bytes).

    `raw_bytes` is kept so an untouched frame can be forwarded byte-for-byte
    rather than re-encoded — re-encoding a frame we did not need to change is
    pure risk.
    """
    header = await reader.readexactly(2)
    opcode = header[0] & 0x0F
    fin = bool(header[0] & 0x80)
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    raw = header

    if length == 126:
        ext = await reader.readexactly(2)
        raw += ext
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = await reader.readexactly(8)
        raw += ext
        length = struct.unpack("!Q", ext)[0]

    mask = b""
    if masked:
        mask = await reader.readexactly(4)
        raw += mask

    payload = await reader.readexactly(length)
    raw += payload
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload, raw


def encode_ws_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Encode a client-to-server frame. Client frames are always masked."""
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, 0x80 | length])
    elif length < (1 << 16):
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    mask = secrets.token_bytes(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(IO_CHUNK):
            dst.write(data)
            await dst.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


class FrontDoor:
    def __init__(self, upstream: Upstream, secret, quiet: bool = False):
        self.upstream = upstream
        self.secret = secret
        self.quiet = quiet

    def log(self, *parts) -> None:
        if not self.quiet:
            print(*parts, flush=True)

    def rewrite(
        self,
        request_line: bytes,
        headers: list[bytes],
        authorization: str | None,
        websocket: bool,
    ) -> bytes:
        out = [request_line]
        for line in headers:
            lowered = line.lower()
            if lowered.startswith(b"host:"):
                out.append(b"Host: " + self.upstream.host_header.encode())
            elif lowered.startswith(b"authorization:") and authorization is not None:
                out.append(b"Authorization: " + authorization.encode())
            elif not websocket and lowered.startswith((b"connection:", b"proxy-connection:")):
                # Dropped, then replaced below. Keeping a client's keep-alive
                # would be a correctness bug, not an optimisation: the second
                # request on a reused connection never passes through the
                # rewrite above, so it reaches the relay with `Host:
                # 127.0.0.1` and a NIP-98 token signed for loopback. The CDN
                # answers that with 403. One request per connection is the
                # price of every request being corrected.
                continue
            else:
                out.append(line)
        if not websocket:
            out.append(b"Connection: close")
        return b"\r\n".join(out) + b"\r\n\r\n"

    def resign_auth(self, payload: bytes) -> bytes | None:
        """Rewrite a NIP-42 `["AUTH", <event>]` frame for the real relay.

        Returns the replacement payload, or None to forward unchanged — which
        is the answer for every frame that is not an AUTH event signed by this
        agent. Only the `relay` tag changes; challenge, and any NIP-OA `auth`
        tag, are carried across untouched.
        """
        try:
            message = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        if not (isinstance(message, list) and len(message) == 2 and message[0] == "AUTH"):
            return None
        event = message[1]
        if not isinstance(event, dict) or event.get("kind") != 22242:
            return None
        if event.get("pubkey") != nostr.public_key_hex(self.secret):
            # Someone else's identity. Never re-sign it — pass it through and
            # let the relay make its own decision.
            self.log("  AUTH from a different pubkey — forwarding unchanged")
            return None

        tags = [
            [t[0], self.upstream.ws_origin, *t[2:]] if t and t[0] == "relay" else t
            for t in event.get("tags", [])
        ]
        if not any(t and t[0] == "relay" for t in tags):
            tags.append(["relay", self.upstream.ws_origin])
        rewritten = nostr.sign_event(self.secret, 22242, tags, event.get("content", ""))
        self.log(f"  AUTH re-signed for {self.upstream.ws_origin}")
        return json.dumps(["AUTH", rewritten]).encode()

    async def pump_client_frames(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Forward client frames, correcting the AUTH one on its way past.

        Frame parsing stops as soon as AUTH has been handled: after that there
        is nothing left to correct, and raw piping is both faster and less
        likely to mangle something.
        """
        try:
            while True:
                opcode, fin, payload, raw = await read_ws_frame(reader)
                if opcode == OPCODE_TEXT:
                    replacement = self.resign_auth(payload)
                    if replacement is not None:
                        writer.write(encode_ws_frame(OPCODE_TEXT, replacement, fin))
                        await writer.drain()
                        await pipe(reader, writer)
                        return
                writer.write(raw)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionResetError):
            client_writer.close()
            return
        except asyncio.LimitOverrunError:
            client_writer.close()
            return
        if len(head) > MAX_HEAD_BYTES:
            client_writer.close()
            return

        request_line, headers = parse_head(head)
        try:
            method, path, _ = request_line.decode().split(" ", 2)
        except ValueError:
            client_writer.close()
            return

        websocket = is_websocket_upgrade(headers)
        body = b""
        authorization: str | None = None

        if not websocket:
            # Buffer the body so NIP-98's payload hash can cover it. Chunked
            # bodies are passed through unsigned rather than mis-signed —
            # buzz-acp sends length-delimited JSON, so this stays theoretical.
            length = header_value(headers, "content-length")
            if length and length.isdigit():
                body = await client_reader.readexactly(int(length))
            if header_value(headers, "authorization"):
                authorization = nostr.nip98_header(
                    self.secret, method, f"{self.upstream.origin}{path}", body
                )

        try:
            up_reader, up_writer = await self.upstream.connect()
        except Exception as exc:
            self.log(f"  upstream failed: {exc}")
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            try:
                await client_writer.drain()
            except Exception:
                pass
            client_writer.close()
            return

        up_writer.write(self.rewrite(request_line, headers, authorization, websocket) + body)
        await up_writer.drain()
        self.log(f"  {method} {path}{' [ws]' if websocket else ''}")

        # A WebSocket runs until either end hangs up; an HTTP response is
        # delimited by the upstream close. The only direction needing
        # inspection is client-to-relay on a socket, and only until AUTH.
        outbound = (
            self.pump_client_frames(client_reader, up_writer)
            if websocket
            else pipe(client_reader, up_writer)
        )
        await asyncio.gather(outbound, pipe(up_reader, client_writer))

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self.handle, host, port)
        route = f" via {self.upstream.proxy.geturl()}" if self.upstream.proxy else " (direct)"
        self.log(f"front door: {host}:{port} -> {self.upstream.origin}{route}")
        async with server:
            await server.serve_forever()


def ca_bundle() -> str | None:
    """CA bundle for the upstream TLS leg, or None for the system default.

    A TLS-terminating proxy installs its own root, which is not in the system
    store. Such environments conventionally advertise it via SSL_CERT_FILE or
    REQUESTS_CA_BUNDLE, so honour those rather than hardcoding any one vendor's
    path; on a host without a proxy this returns None and the system trust
    store is used.
    """
    explicit = os.environ.get("BUZZ_CA_BUNDLE")
    if explicit:
        return explicit
    for candidate in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def build(
    relay_url: str | None = None, secret_raw: str | None = None, quiet: bool = False
) -> FrontDoor:
    relay_url = relay_url or os.environ.get("BUZZ_RELAY_URL", "")
    if not relay_url:
        raise SystemExit("BUZZ_RELAY_URL is not set")
    secret_raw = secret_raw or os.environ.get("BUZZ_PRIVATE_KEY", "")
    if not secret_raw:
        raise SystemExit("BUZZ_PRIVATE_KEY is not set")
    upstream = Upstream(
        relay_url,
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        ca_bundle(),
    )
    return FrontDoor(upstream, nostr.load_secret_key(secret_raw), quiet=quiet)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    default_port = int(os.environ.get("BUZZ_FRONTDOOR_PORT", "8443"))
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(build(quiet=args.quiet).serve(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
