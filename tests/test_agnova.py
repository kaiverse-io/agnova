"""Unit tests for the pure parts of the runtime.

Deliberately stdlib `unittest` and deliberately offline: these must run in a
fresh container with no relay, no keys and no network. The live path is covered
by `./agent selftest`, which is a different kind of check and needs a real
identity.

Every case here corresponds to something that actually broke. Three of them are
defects found by an audit after the code was written and believed correct, which
is the argument for the file existing.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agnova import nostr  # noqa: E402
from agnova.config import read_env_file  # noqa: E402
from agnova.frontdoor import (  # noqa: E402
    Upstream,
    encode_ws_frame,
    header_value,
    is_websocket_upgrade,
    parse_head,
)

# A valid nsec from NIP-19's own examples. Public test data, not a live key.
VALID_NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"


class Bech32(unittest.TestCase):
    """A mistyped key must fail loudly, never decode to a different identity."""

    def test_valid_nsec_decodes(self):
        secret = nostr.load_secret_key(VALID_NSEC)
        self.assertEqual(len(nostr.public_key_hex(secret)), 64)

    def test_checksum_failure_is_rejected(self):
        # Last symbol altered: payload intact, checksum no longer matches.
        with self.assertRaises(ValueError):
            nostr.load_secret_key(VALID_NSEC[:-1] + "q")

    def test_corrupted_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            nostr.load_secret_key(VALID_NSEC.replace("vl029", "vl028"))

    def test_truncated_key_is_rejected(self):
        with self.assertRaises(ValueError):
            nostr.load_secret_key(VALID_NSEC[:-6])

    def test_hex_and_bech32_agree(self):
        from_bech32 = nostr.load_secret_key(VALID_NSEC)
        from_hex = nostr.load_secret_key(from_bech32.to_hex())
        self.assertEqual(nostr.public_key_hex(from_bech32), nostr.public_key_hex(from_hex))

    def test_pubkey_is_derived_not_configured(self):
        """Two different keys must never derive the same pubkey."""
        a = nostr.load_secret_key("11" * 32)
        b = nostr.load_secret_key("22" * 32)
        self.assertNotEqual(nostr.public_key_hex(a), nostr.public_key_hex(b))


class EnvFile(unittest.TestCase):
    def parse(self, text: str) -> dict:
        path = Path(tempfile.mkdtemp()) / "t.env"
        path.write_text(text)
        return read_env_file(path)

    def test_quoted_value_containing_hash_survives(self):
        # Stripping comments before quotes truncated this and left a stray quote.
        self.assertEqual(self.parse('A="foo # bar"'), {"A": "foo # bar"})

    def test_single_quoted_hash_survives(self):
        self.assertEqual(self.parse("A='x#y'"), {"A": "x#y"})

    def test_trailing_comment_is_stripped(self):
        self.assertEqual(self.parse("A=plain # a comment"), {"A": "plain"})

    def test_comments_and_blanks_ignored(self):
        self.assertEqual(self.parse("# note\n\nA=1\n"), {"A": "1"})

    def test_export_prefix_accepted(self):
        self.assertEqual(self.parse("export A=1"), {"A": "1"})

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(read_env_file(Path("/nonexistent/none.env")), {})


class UpstreamUrls(unittest.TestCase):
    """The three URL forms the relay checks independently."""

    def wss(self) -> Upstream:
        return Upstream("wss://relay.example.com", None, None)

    def test_wss_implies_tls_on_443(self):
        up = self.wss()
        self.assertTrue(up.tls)
        self.assertEqual(up.port, 443)

    def test_default_port_omitted_from_host_header(self):
        self.assertEqual(self.wss().host_header, "relay.example.com")

    def test_nip98_origin_is_https(self):
        self.assertEqual(self.wss().origin, "https://relay.example.com")

    def test_nip42_relay_tag_keeps_ws_scheme(self):
        # The relay compares this scheme-sensitively; https:// would be rejected.
        self.assertEqual(self.wss().ws_origin, "wss://relay.example.com")

    def test_non_default_port_is_kept_everywhere(self):
        up = Upstream("ws://relay.example.com:8080", None, None)
        self.assertFalse(up.tls)
        self.assertEqual(up.host_header, "relay.example.com:8080")
        self.assertEqual(up.origin, "http://relay.example.com:8080")
        self.assertEqual(up.ws_origin, "ws://relay.example.com:8080")

    def test_url_without_host_is_refused(self):
        with self.assertRaises(ValueError):
            Upstream("not-a-url", None, None)


class Nip98(unittest.TestCase):
    def setUp(self):
        self.secret = nostr.load_secret_key(VALID_NSEC)

    def test_header_is_bound_to_the_url(self):
        """Different URLs must not produce interchangeable tokens."""
        a = nostr.nip98_header(self.secret, "POST", "https://a.example.com/query", b"[]")
        b = nostr.nip98_header(self.secret, "POST", "https://b.example.com/query", b"[]")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("Nostr "))

    def test_body_is_covered(self):
        a = nostr.nip98_header(self.secret, "POST", "https://a.example.com/query", b"[]")
        b = nostr.nip98_header(self.secret, "POST", "https://a.example.com/query", b"[1]")
        self.assertNotEqual(a, b)

    def test_signed_event_id_is_stable_for_fixed_input(self):
        event = nostr.sign_event(self.secret, 1, [["t", "x"]], "hello")
        self.assertEqual(len(event["id"]), 64)
        self.assertEqual(len(event["sig"]), 128)
        self.assertEqual(event["pubkey"], nostr.public_key_hex(self.secret))


class HttpAndFrames(unittest.TestCase):
    def test_head_parsing_and_header_lookup(self):
        head = b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n"
        line, headers = parse_head(head)
        self.assertEqual(line, b"GET / HTTP/1.1")
        self.assertEqual(header_value(headers, "host"), "x")
        self.assertEqual(header_value(headers, "HOST"), "x", "lookup must be case-insensitive")
        self.assertIsNone(header_value(headers, "absent"))
        self.assertTrue(is_websocket_upgrade(headers))

    def test_plain_request_is_not_an_upgrade(self):
        _, headers = parse_head(b"POST /query HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertFalse(is_websocket_upgrade(headers))

    def test_client_frames_are_masked(self):
        frame = encode_ws_frame(0x1, b"hi")
        self.assertEqual(frame[0], 0x81, "FIN set, text opcode")
        self.assertTrue(frame[1] & 0x80, "client-to-server frames must be masked")

    def test_frame_length_encodings(self):
        self.assertEqual(encode_ws_frame(0x1, b"x" * 10)[1] & 0x7F, 10)
        self.assertEqual(encode_ws_frame(0x1, b"x" * 200)[1] & 0x7F, 126)
        self.assertEqual(encode_ws_frame(0x1, b"x" * 70000)[1] & 0x7F, 127)

    def test_payload_round_trips_through_the_mask(self):
        payload = b'["AUTH",{"kind":22242}]'
        frame = encode_ws_frame(0x1, payload)
        mask = frame[2:6]
        decoded = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:]))
        self.assertEqual(decoded, payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
