"""Tests for DNA integrity, git memory backend, and checkpoint bundle upload."""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agnova import checkpoint, dna  # noqa: E402
from agnova.memory.git_backend import GitMemoryBackend  # noqa: E402
from agnova.memory.mcp_server import TOOLS, _handle  # noqa: E402


class DnaHash(unittest.TestCase):
    def test_stable_hash_and_mismatch(self):
        home = Path(tempfile.mkdtemp())
        (home / "SOUL.md").write_text("karma\n", encoding="utf-8")
        (home / "AGENTS.md").write_text("thin\n", encoding="utf-8")
        paths = ["SOUL.md", "AGENTS.md"]
        got = dna.compute_hash(home, paths)
        self.assertTrue(got.startswith("sha256:"))
        self.assertEqual(dna.enforce(home, expected=got, paths=paths), got)
        with self.assertRaises(SystemExit):
            dna.enforce(home, expected="sha256:" + "0" * 64, paths=paths)

    def test_unset_hash_is_noop(self):
        home = Path(tempfile.mkdtemp())
        self.assertIsNone(dna.enforce(home, expected=None))


class GitMemory(unittest.TestCase):
    def test_remember_recall_forget_context(self):
        home = Path(tempfile.mkdtemp())
        backend = GitMemoryBackend(home)
        stored = backend.remember([{"content": "learned about dharma", "type": "lesson"}])
        self.assertEqual(len(stored), 1)
        hits = backend.recall("dharma")
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(any("dharma" in h.content.lower() for h in hits))
        self.assertIn("dharma", backend.context(budget=500).lower())
        self.assertTrue(backend.forget(stored[0].id))
        self.assertFalse(backend.forget(stored[0].id))


class McpDispatch(unittest.TestCase):
    def test_tools_list_and_remember(self):
        home = Path(tempfile.mkdtemp())
        backend = GitMemoryBackend(home)
        listed = _handle(backend, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert listed is not None
        self.assertEqual(len(listed["result"]["tools"]), len(TOOLS))
        reply = _handle(
            backend,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "remember",
                    "arguments": {"items": [{"content": "hello memory"}]},
                },
            },
        )
        assert reply is not None
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload[0]["content"], "hello memory")


class BundleUpload(unittest.TestCase):
    def test_upload_headers_and_body(self):
        received: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                received["sha"] = self.headers.get("X-Content-Sha256")
                received["pubkey"] = self.headers.get("X-Agent-Pubkey")
                received["auth"] = self.headers.get("Authorization")
                received["body"] = body
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"version":1}')

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            home = Path(tempfile.mkdtemp())
            subprocess.run(["git", "init"], cwd=home, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "t@example.com"],
                cwd=home,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "t"], cwd=home, check=True, capture_output=True
            )
            (home / "MEMORY.md").write_text("note\n", encoding="utf-8")
            subprocess.run(["git", "add", "MEMORY.md"], cwd=home, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "init"], cwd=home, check=True, capture_output=True
            )
            bundle = home / "x.bundle"
            self.assertTrue(checkpoint.create_bundle(home, bundle))
            body = bundle.read_bytes()
            ok = checkpoint.upload_bundle(
                f"http://127.0.0.1:{port}/upload",
                "tok",
                "ab" * 32,
                bundle,
            )
            self.assertTrue(ok)
            self.assertEqual(received["auth"], "Bearer tok")
            self.assertEqual(received["sha"], hashlib.sha256(body).hexdigest())
            self.assertEqual(received["body"], body)
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
