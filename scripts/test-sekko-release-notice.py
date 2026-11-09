#!/usr/bin/env python3
"""Local fixture/injected transport tests only; no sockets or external services."""

import contextlib
import hashlib
import io
import hmac
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("notice", ROOT / "actions/sekko-release-notice/notice.py")
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)
SHA = "a" * 40
KEY = "01" * 32  # Public deterministic fixture, never a credential.
CONFIG = {"inbox_url": "https://inbox.example.test/v1/releases", "scope_id": "reviewed-scope",
          "target_id": "pay-api-staging", "surface": "pay-api", "origin": notice.STAGING_ORIGIN,
          "description": "Staging release \u00e9"}
ENV = {"SEKKO_NOTICE_ENABLED": "true", "NOTICE_REPOSITORY": "zkp2p/pay",
       "NOTICE_REF": "releases/staging", "NOTICE_ENVIRONMENT": "staging",
       "NOTICE_ENVIRONMENTS_FILE": "deploy/environments-pay.yml", "NOTICE_MODE": "image",
       "NOTICE_DEPLOY_RESULT": "success", "SEKKO_NOTICE_CONFIG": json.dumps(CONFIG),
       "SEKKO_RELEASE_KEY": KEY, "NOTICE_SOURCE_SHA": SHA, "NOTICE_RUN_ID": "41051883943",
       "NOTICE_RUN_ATTEMPT": "2", "NOTICE_DEPLOYMENT_ID": "1234",
       "NOTICE_COMMAND_ID": "12345678-1234-1234-1234-123456789abc"}
READY = {"success": True, "statusCode": 200, "responseObject": {"ok": True, "commit": SHA,
         "dependencies": {"database": "ok", "redis": "ok"}}}


class NoticeTests(unittest.TestCase):
    def send(self, *, env=None, readiness=None, replies=None):
        self.calls, self.sleeps, self.clocks, self.payloads = [], [], [], []
        replies = list(replies or [(202, b'{"status":"queued"}')])

        def transport(method, url, body=None):
            self.calls.append((method, url, body))
            value = (readiness if readiness is not None else (200, json.dumps(READY).encode())) if method == "GET" else replies.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def now():
            self.clocks.append(True)
            return 1000

        return notice.execute(env or ENV, persist_payload=self.payloads.append,
                              transport=transport, now=now, sleep=self.sleeps.append)

    def test_disabled_and_every_context_gate_make_no_requests(self):
        for name, value in {"SEKKO_NOTICE_ENABLED": "false", "NOTICE_REPOSITORY": "zkp2p/other",
                            "NOTICE_REF": "releases/prod", "NOTICE_ENVIRONMENT": "preprod",
                            "NOTICE_ENVIRONMENTS_FILE": "deploy/environments-support.yml",
                            "NOTICE_MODE": "config", "NOTICE_DEPLOY_RESULT": "failure"}.items():
            with self.subTest(name=name):
                self.assertEqual(self.send(env={**ENV, name: value})["status"], "skipped")
                self.assertEqual(self.calls, [])
        self.assertEqual(notice.execute({}, persist_payload=lambda *_: self.fail("persist"),
                                        transport=lambda *_: self.fail("network"))["status"], "skipped")

    def test_missing_config_or_key_and_invalid_context_never_probe_or_post(self):
        for name, value in (("SEKKO_NOTICE_CONFIG", ""), ("SEKKO_RELEASE_KEY", ""),
                            ("SEKKO_RELEASE_KEY", "aa" * 31), ("SEKKO_RELEASE_KEY", "gg" * 32),
                            ("SEKKO_RELEASE_KEY", "a" * 65), ("NOTICE_SOURCE_SHA", "A" * 40),
                            ("NOTICE_RUN_ID", "10\nforged=value"), ("NOTICE_RUN_ATTEMPT", "0"),
                            ("NOTICE_DEPLOYMENT_ID", ""), ("NOTICE_COMMAND_ID", "bad")):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.send(env={**ENV, name: value})["status"], "failed")
                self.assertEqual(self.calls, [])

    def test_invalid_configs_never_probe_or_post(self):
        configs = [dict(CONFIG, extra="unexpected"), {key: value for key, value in CONFIG.items() if key != "scope_id"}]
        for key, values in {"origin": ["https://api.pay.peer.xyz", "http://api-staging.pay.peer.xyz", "https://127.0.0.1"],
                            "inbox_url": ["http://inbox.example.test/v1/releases", "https://127.0.0.1/v1/releases",
                                          "https://user:pass@inbox.example.test/v1/releases", "https://INBOX.example.test/v1/releases",
                                          "https://inbox.example.test:443/v1/releases", "https://inbox.example.test/v1/releases?q=x",
                                          "https://inbox.example.test/v1/releases#fragment", "https://inbox.example.test/elsewhere"],
                            "scope_id": ["", "x" * 129, "line\nbreak", 1], "target_id": [None],
                            "surface": [{}], "description": ["", "x" * 1001, "control\x00"]}.items():
            configs.extend(dict(CONFIG, **{key: value}) for value in values)
        for config in configs:
            with self.subTest(config=config):
                self.assertEqual(self.send(env={**ENV, "SEKKO_NOTICE_CONFIG": json.dumps(config)})["status"], "failed")
                self.assertEqual(self.calls, [])
        for raw in ('{"scope_id":"one","scope_id":"two"}', '{"value":NaN}', '[]', 'x' * 65537):
            self.assertEqual(self.send(env={**ENV, "SEKKO_NOTICE_CONFIG": raw})["status"], "failed")
            self.assertEqual(self.calls, [])

    def test_exact_consumer_envelope_and_canonical_hmac(self):
        result = self.send()
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(self.calls[0], ("GET", notice.STAGING_ORIGIN + "/readyz", None))
        method, url, body = self.calls[1]
        self.assertEqual((method, url), ("POST", CONFIG["inbox_url"]))
        envelope = json.loads(body)
        payload = {"event_id": f"pay:staging:41051883943:2:{SHA}", "scope_id": "reviewed-scope",
                   "target_id": "pay-api-staging", "surface": "pay-api", "environment": "staging",
                   "origin": notice.STAGING_ORIGIN, "version": SHA, "released_at": 1000,
                   "expires_at": 4600, "availability": "available", "description": "Staging release \u00e9",
                   "change_type": "release"}
        # Keep this independent from the production serializer and signing function.
        canonical_payload = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True,
                                       separators=(",", ":")).encode("ascii")
        self.assertEqual(envelope, {"key_id": "release-ci", "purpose": "release-v1", "payload": payload,
                                   "signature": hmac.new(bytes.fromhex(KEY), b"release-v1\x00" + canonical_payload,
                                                         hashlib.sha256).hexdigest()})
        self.assertEqual(self.payloads, [canonical_payload])
        self.assertEqual(body, json.dumps(envelope, ensure_ascii=True, allow_nan=False, sort_keys=True,
                                        separators=(",", ":")).encode("ascii"))
        self.assertIn(b"\\u00e9", body)
        self.assertLessEqual(len(payload["event_id"]), 128)
        self.assertLessEqual(payload["expires_at"] - payload["released_at"], 86400)
        self.assertEqual(self.clocks, [True])

    def test_every_readiness_assertion_and_oversize_or_malformed_body_blocks_signing(self):
        fixtures = [(code, json.dumps(READY).encode()) for code in (301, 302, 307, 503)]
        for path, value in ((["success"], False), (["statusCode"], 503), (["statusCode"], True),
                            (["responseObject", "ok"], False), (["responseObject", "commit"], "b" * 40),
                            (["responseObject", "dependencies", "database"], "fail"),
                            (["responseObject", "dependencies", "redis"], "fail"), (["responseObject"], None)):
            data = json.loads(json.dumps(READY))
            target = data
            for name in path[:-1]:
                target = target[name]
            target[path[-1]] = value
            fixtures.append((200, json.dumps(data).encode()))
        fixtures.extend([(200, b"{}"), (200, b"not json"), (200, b"x" * 65537), (200, b'{"success":true,"success":false}')])
        for fixture in fixtures:
            with self.subTest(fixture=fixture[0]):
                self.assertEqual(self.send(readiness=fixture)["reason"], "readiness_failed")
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.clocks, [])
        self.assertEqual(self.send(readiness=TimeoutError("sensitive readiness text"))["reason"], "readiness_transport_failed")
        self.assertEqual(len(self.calls), 1)

    def test_bounded_retry_uses_identical_bytes_and_duplicate_is_success(self):
        for transient in (408, 429, 503, OSError("sensitive transport text"), http.client.HTTPException("private")):
            with self.subTest(transient=type(transient)):
                reply = transient if isinstance(transient, Exception) else (transient, b"private response")
                result = self.send(replies=[reply, reply, (200, b'{"status":"duplicate"}')])
                self.assertEqual((result["status"], result["attempts"]), ("delivered", 3))
                self.assertEqual(self.sleeps, [2, 4])
                self.assertEqual(self.clocks, [True])
                self.assertEqual(len({body for method, _, body in self.calls if method == "POST"}), 1)
        result = self.send(replies=[(503, b"private")] * 3)
        self.assertEqual((result["reason"], result["attempts"]), ("delivery_exhausted", 3))

    def test_lost_ack_preserves_exact_unsigned_payload_before_signing_and_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            pending = Path(temp, "sekko-release-notice-pending.json")
            accepted = []
            ordering = []
            real_hmac = notice.hmac.new

            def persist(raw):
                ordering.append("persist")
                notice.write_pending(pending, raw)

            def sign(*args):
                ordering.append("sign")
                self.assertTrue(pending.exists())
                self.assertEqual(args[1], b"release-v1\x00" + pending.read_bytes())
                return real_hmac(*args)

            def transport(method, url, body=None):
                if method == "GET":
                    return 200, json.dumps(READY).encode()
                ordering.append("post")
                envelope = json.loads(body)
                self.assertEqual(pending.read_bytes(), notice.canonical(envelope["payload"]))
                self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
                self.assertFalse(set(json.loads(pending.read_bytes())) & {"key_id", "purpose", "signature", "payload", "inbox_url"})
                if not accepted:
                    accepted.append(body)
                    raise TimeoutError("ACK lost after inbox accepted the release")
                self.assertEqual(body, accepted[0])
                return 200, b'{"status":"duplicate"}'

            with patch.object(notice.hmac, "new", side_effect=sign):
                result = notice.execute(ENV, persist_payload=persist, transport=transport,
                                        now=lambda: 1000, sleep=lambda _: None)
            self.assertEqual((result["status"], result["reason"], result["attempts"]), ("delivered", "duplicate", 2))
            self.assertEqual(ordering, ["persist", "sign", "post", "post"])
            self.assertEqual(json.loads(pending.read_bytes()), json.loads(accepted[0])["payload"])
            self.assertNotIn(KEY.encode(), pending.read_bytes())

    def test_failed_payload_persistence_never_signs_or_posts(self):
        calls = []

        def transport(method, url, body=None):
            calls.append(method)
            return 200, json.dumps(READY).encode()

        def persist(_):
            raise OSError("private filesystem text")

        with patch.object(notice.hmac, "new", side_effect=AssertionError("must not sign")):
            result = notice.execute(ENV, persist_payload=persist, transport=transport, now=lambda: 1000)
        self.assertEqual(result["reason"], "pending_payload_failed")
        self.assertEqual(calls, ["GET"])

    def test_failed_delivery_keeps_payload_and_safe_report_as_separate_files(self):
        with tempfile.TemporaryDirectory() as temp:
            pending = Path(temp, "sekko-release-notice-pending.json")
            attempts = []
            env = {**ENV, "RUNNER_TEMP": temp, "GITHUB_OUTPUT": temp + "/output", "GITHUB_STEP_SUMMARY": temp + "/summary"}

            def transport(method, url, body=None):
                if method == "GET":
                    return 200, json.dumps(READY).encode()
                attempts.append(body)
                raise TimeoutError("lost ACK")

            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(notice.main(transport=transport, now=lambda: 1000, sleep=lambda _: None), 1)
            report_raw = Path(temp, "sekko-release-notice-report.json").read_bytes()
            report = json.loads(report_raw)
            self.assertEqual(report["reason"], "delivery_exhausted")
            self.assertEqual(report["deployment_outcome"], "success")
            self.assertEqual(len(attempts), 3)
            self.assertEqual(len(set(attempts)), 1)
            self.assertEqual(pending.read_bytes(), notice.canonical(json.loads(attempts[0])["payload"]))
            self.assertNotIn(b"description", report_raw)
            self.assertNotIn(b"signature", report_raw + pending.read_bytes())
            self.assertEqual(pending.stat().st_mode & 0o777, 0o600)

    def test_invalid_config_clears_only_exact_stale_pending_file(self):
        with tempfile.TemporaryDirectory() as temp:
            pending = Path(temp, "sekko-release-notice-pending.json")
            other = Path(temp, "another-notice-pending.json")
            other.write_text("preserve other work")
            env = {**ENV, "SEKKO_NOTICE_CONFIG": "invalid", "RUNNER_TEMP": temp,
                   "GITHUB_OUTPUT": temp + "/output", "GITHUB_STEP_SUMMARY": temp + "/summary"}
            for disabled in (False, True):
                pending.write_text("stale old public payload")
                env["SEKKO_NOTICE_ENABLED"] = "false" if disabled else "true"
                with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                    result = notice.main(transport=lambda *_: self.fail("no network"))
                self.assertEqual(result, 0 if disabled else 1)
                self.assertFalse(pending.exists())
                self.assertEqual(other.read_text(), "preserve other work")

    def test_redirects_contract_errors_and_other_server_errors_do_not_retry(self):
        for status in (300, 301, 302, 303, 307, 308, 400, 401, 403, 404, 409, 413, 422, 500, 502, 504):
            with self.subTest(status=status):
                result = self.send(replies=[(status, b"sensitive response")])
                self.assertEqual((result["reason"], result["attempts"]), ("delivery_rejected", 1))
                self.assertEqual(self.sleeps, [])

    def test_only_queued_202_or_duplicate_200_acknowledges_delivery(self):
        for status, body in ((202, b'{"status":"duplicate"}'), (200, b'{"status":"queued"}'),
                             (202, b"bad"), (200, b"{}"), (202, b"x" * 65537)):
            self.assertEqual(self.send(replies=[(status, body)])["status"], "failed")
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(self.sleeps, [])

    def test_transport_direct_tls_no_proxy_redirect_or_body_logging(self):
        connection = unittest.mock.MagicMock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.read.return_value = b"private redirect"
        with patch.object(notice.http.client, "HTTPSConnection", return_value=connection) as tls:
            result = notice.request("POST", CONFIG["inbox_url"], b"signed fixture")
        self.assertEqual(result[0], 302)
        self.assertEqual(tls.call_count, 1)
        self.assertEqual(tls.call_args.args, ("inbox.example.test",))
        self.assertEqual(tls.call_args.kwargs["timeout"], 10)
        self.assertEqual(tls.call_args.kwargs["context"].verify_mode, notice.ssl.CERT_REQUIRED)
        self.assertTrue(tls.call_args.kwargs["context"].check_hostname)
        connection.request.assert_called_once_with("POST", "/v1/releases", body=b"signed fixture",
                                                  headers={"Accept": "application/json", "Cache-Control": "no-cache",
                                                           "Content-Type": "application/json"})
        connection.getresponse.return_value.read.assert_called_once_with(65537)
        connection.close.assert_called_once()

    def test_failure_evidence_is_safe_and_does_not_change_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**ENV, "SEKKO_NOTICE_CONFIG": "private config", "RUNNER_TEMP": temp,
                   "GITHUB_OUTPUT": temp + "/output", "GITHUB_STEP_SUMMARY": temp + "/summary"}
            result = subprocess.run(["python3", "-I", str(ROOT / "actions/sekko-release-notice/notice.py")],
                                    env={**os.environ, **env}, capture_output=True, text=True, timeout=5)
            report = json.loads(Path(temp, "sekko-release-notice-report.json").read_bytes())
            self.assertEqual(result.returncode, 1)
            self.assertEqual(report["deployment_outcome"], "success")
            combined = result.stdout + result.stderr + Path(temp, "summary").read_text() + json.dumps(report)
            for private in (KEY, "private config", "signature", CONFIG["inbox_url"], CONFIG["description"]):
                self.assertNotIn(private, combined)
            self.assertIn("do not redeploy", combined)
            self.assertEqual(Path(temp, "output").read_text(), "status=failed\n")
            self.assertEqual(Path(temp, "sekko-release-notice-report.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
