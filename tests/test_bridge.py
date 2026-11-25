import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
import ponte
from state_io import atomic_json


class BridgeSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(ponte, "PONTE_HOME", self.root),
                        patch.object(ponte, "CANAIS_PATH", self.root / "canais.json"),
                        patch.object(ponte, "SESSIONS_PATH", self.root / "sessions")]
        for p in self.patches: p.start()
        # Accidental remote traffic or real credential reads must fail every test.
        self.auth = patch.object(ponte, "load_auth", side_effect=AssertionError("No real auth in tests"))
        self.auth.start()

    def tearDown(self):
        self.auth.stop()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_remote_origin_checked_before_credentials(self):
        for url in ("http://grok.com/rest/app-chat/a", "https://example.invalid/rest/app-chat/a", "https://grok.com@example.invalid/rest/app-chat/a", "https://grok.com:444/rest/app-chat/a"):
            with self.subTest(url=url), self.assertRaises(ponte.PonteError):
                ponte.grok_request("GET", url)

    def test_redirects_rejected(self):
        request = Request("https://grok.com/rest/app-chat/a", headers={"Authorization": "Bearer synthetic"})
        with self.assertRaises(ponte.PonteError):
            ponte.NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://example.invalid/")

    def test_ids_prevent_traversal(self):
        for value in ("../secret", "../../../../auth.json", "valid-id\n", "", "a/bbbbbbbb"):
            with self.subTest(value=value), self.assertRaises(ponte.PonteError):
                ponte.require_id(value)

    def test_planner_requires_explicit_send_authorization(self):
        with self.assertRaises(ponte.PonteError):
            ponte.planejar_cli([])

    def test_detects_synthetic_secret_without_printing(self):
        # Construct fixtures so the repository contains no token-shaped literals.
        for value in ("gh" + "p_" + "X"*36, "-----BEGIN " + "PRIVATE KEY-----", "password=" + "test-value-long"):
            with self.assertRaises(ponte.PonteError):
                ponte.assert_safe_outbound(value)

    def test_channel_pair_isolation(self):
        session, web_a, web_b = "session-123", "conversation-a", "conversation-b"
        ponte.append_knowledge(session, web_a, "test", "A-only")
        ponte.append_knowledge(session, web_b, "test", "B-only")
        ponte.save_channels([{"id": "channel-123", "cliSessionId": session, "webConversationId": web_b}])
        with patch.object(ponte, "latest_assistant_reply", return_value=("", None)), \
             patch.object(ponte, "post_conversation", return_value=(web_a, "plan-a")) as post, \
             contextlib.redirect_stderr(io.StringIO()):
            ponte.ask_planner("report-a", web_a, session)
        message = post.call_args.args[0]
        self.assertIn("A-only", message)
        self.assertNotIn("B-only", message)
        self.assertNotIn("plan-a", ponte.knowledge_for_prompt(session, web_b))

    def test_export_preview_excludes_directory_and_tools(self):
        detail = {"title": "Demo", "cwd": "synthetic-private-dir", "toolCount": 1,
                  "messages": [{"role": "user", "text": "hello"}, {"role": "tool", "text": "private-tool-output"}]}
        with patch.object(ponte, "cli_messages", return_value=detail) as mock:
            preview = ponte.export_preview("session-123")
        self.assertNotIn("synthetic-private-dir", preview["message"])
        self.assertNotIn("private-tool-output", preview["message"])
        self.assertEqual(len(preview["hash"]), 64)
        self.assertFalse(mock.call_args.kwargs["include_tools"])

    def test_stale_preview_never_sends(self):
        with patch.object(ponte, "export_preview", return_value={"message": "new", "hash": "newhash"}), \
             patch.object(ponte, "post_conversation") as post, self.assertRaises(ponte.PonteError):
            ponte.send_to_web("session-123", "oldhash")
        post.assert_not_called()

    def test_valid_preview_sends_exact_text(self):
        with patch.object(ponte, "export_preview", return_value={"message": "reviewed", "hash": "samehash"}), \
             patch.object(ponte, "find_cli", return_value={"title": "Demo"}), \
             patch.object(ponte, "remember_link"), \
             patch.object(ponte, "post_conversation", return_value=("conversation-123", "ok")) as post:
            ponte.send_to_web("session-123", "samehash")
        self.assertEqual(post.call_args.args[0], "reviewed")

    def test_local_summary_outside_root_is_rejected(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"info": {"id": "session-123"}}))
        self.assertIsNone(ponte.read_summary(outside))

    def test_atomic_json_failure_preserves_previous_channels(self):
        ponte.save_channels([{"id": "channel-old"}])
        with patch("state_io.os.replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError): ponte.save_channels([{"id": "channel-new"}])
        self.assertEqual(ponte.load_channels(), [{"id": "channel-old"}])

    def test_concurrent_channel_updates_are_not_lost(self):
        failures = []
        def update(number):
            try:
                ponte.open_channel(f"session-{number:03d}", f"conversation-{number:03d}")
            except Exception as exc:
                failures.append(type(exc).__name__)
        with patch.object(ponte, "find_cli", side_effect=lambda value: {"id": value, "title": "Synthetic"}), \
             patch.object(ponte, "web_title", return_value="Synthetic"), \
             patch.object(ponte, "write_channel_marker"), \
             patch.object(ponte, "remember_channel_opened"):
            threads = [threading.Thread(target=update, args=(n,)) for n in range(8)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=5)
        self.assertEqual(failures, [])
        self.assertEqual(len(ponte.load_channels()), 8)


if __name__ == "__main__":
    unittest.main()
