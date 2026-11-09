#!/usr/bin/env python3
"""Local fixture/injected transport tests only; no sockets or external services."""

import contextlib
import base64
import hashlib
import io
import hmac
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("notice", ROOT / "actions/sekko-release-notice/notice.py")
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)
PUBLISHER_SPEC = importlib.util.spec_from_file_location("publisher", ROOT / "actions/sekko-release-notice/publisher.py")
publisher = importlib.util.module_from_spec(PUBLISHER_SPEC)
sys.modules[PUBLISHER_SPEC.name] = publisher
PUBLISHER_SPEC.loader.exec_module(publisher)
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
    def send(self, *, env=None, readiness=None, replies=None, sender_now=1000):
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
            return sender_now

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
                   "origin": notice.STAGING_ORIGIN, "version": SHA, "released_at": 995,
                   "expires_at": 4595, "availability": "available", "description": "Staging release \u00e9",
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

    def test_five_second_backdate_is_frozen_and_ttl_is_measured_from_release(self):
        self.assertEqual(self.send(sender_now=1000.9, replies=[(502, b"gateway"),
                             (504, b"gateway timeout"), (202, b'{"status":"queued"}')])["status"], "delivered")
        envelopes = [json.loads(body) for method, _, body in self.calls if method == "POST"]
        payload = envelopes[0]["payload"]
        self.assertEqual((payload["released_at"], payload["expires_at"]), (995, 4595))
        self.assertEqual(payload["expires_at"] - payload["released_at"], 3600)
        self.assertEqual(len(self.clocks), 1)
        self.assertEqual(self.payloads, [notice.canonical(payload)])
        # Independently reproduce the signature for the backdated fixture.
        expected = hmac.new(bytes.fromhex(KEY), b"release-v1\x00" + json.dumps(payload,
                            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                            allow_nan=False).encode("ascii"), hashlib.sha256).hexdigest()
        self.assertEqual({envelope["signature"] for envelope in envelopes}, {expected})

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
        for transient in (408, 429, 502, 503, 504, OSError("sensitive transport text"), http.client.HTTPException("private")):
            with self.subTest(transient=type(transient)):
                reply = transient if isinstance(transient, Exception) else (transient, b"private response")
                result = self.send(replies=[reply, reply, (200, b'{"status":"duplicate"}')])
                self.assertEqual((result["status"], result["attempts"]), ("delivered", 3))
                self.assertEqual(self.sleeps, [2, 4])
                self.assertEqual(self.clocks, [True])
                self.assertEqual(len({body for method, _, body in self.calls if method == "POST"}), 1)
        for status in (408, 429, 502, 503, 504):
            with self.subTest(exhausted_status=status):
                result = self.send(replies=[(status, b"private")] * 3)
                self.assertEqual((result["reason"], result["attempts"]), ("delivery_exhausted", 3))
                self.assertEqual(self.sleeps, [2, 4])
                self.assertEqual(len({body for method, _, body in self.calls if method == "POST"}), 1)

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
        for status in (300, 301, 302, 303, 307, 308, 400, 401, 403, 404, 409, 413, 422, 500):
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


class PublisherTests(unittest.TestCase):
    EVENT = f"pay:staging:41051883943:2:{SHA}"
    TOKEN = b"synthetic.ci_oidc.fixture"  # Public fake, never a usable identity.
    ROLE = "arn:aws:iam::411399509833:role/sekko-release-publisher"  # Proposed binding, not provisioned here.

    def setUp(self):
        self.calls, self.roots, self.saved = [], [], []
        self.body = notice.canonical({"key_id": "release-ci", "purpose": "release-v1",
                                      "payload": {"event_id": self.EVENT}, "signature": "1" * 64})
        self.checksum = base64.b64encode(hashlib.sha256(self.body).digest()).decode()
        self.reply = publisher.CommandResult(0, notice.canonical({"VersionId": "immutable-version+1",
                                            "ChecksumSHA256": self.checksum, "ChecksumType": "FULL_OBJECT"}))

    def runner(self, argv, *, cwd, env):
        self.calls.append((argv, dict(env)))
        self.roots.append(cwd)
        self.assertTrue(cwd.is_dir())
        self.assertEqual(cwd.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])
        self.assertEqual(Path(env["AWS_CONFIG_FILE"]).read_bytes(), b"")
        self.assertEqual(Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_bytes(), b"")
        self.assertEqual(Path(env["AWS_WEB_IDENTITY_TOKEN_FILE"]).read_bytes(), self.TOKEN)
        for name in ("body", "config", "credentials", "oidc"):
            self.assertEqual((cwd / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((cwd / "body").read_bytes(), self.body)
        if argv[1:] == ("--version",):
            return publisher.CommandResult(0, b"aws-cli/2.31.20 Python/3 fixture\n")
        self.saved.append((cwd / "body").read_bytes())
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def client(self, **changes):
        options = {"expected_version": "2.31.20", "activated": True, "qualified": True,
                   "identity": publisher.OidcPublicationIdentity(self.ROLE, self.TOKEN), "runner": self.runner}
        options.update(changes)
        return publisher.AwsCliPublisher("/qualified/aws", **options)

    def test_inactive_import_construction_and_each_gate_never_execute_cli(self):
        for changes in ({"activated": False}, {"qualified": False}, {"identity": None}):
            with self.subTest(changes=changes):
                client = self.client(**changes)
                self.assertEqual(self.calls, [])
                with self.assertRaises(publisher.PublicationInactive):
                    client(self.EVENT, self.body)
                self.assertEqual(self.calls, [])
        with patch.object(publisher.subprocess, "Popen", side_effect=AssertionError("no CLI")):
            publisher.AwsCliPublisher("/qualified/aws", expected_version="2.31.20")

    def test_strict_trusted_configuration_and_no_static_identity_interface(self):
        for changes in ({"expected_version": "latest"}, {"expected_version": "2.31.20\n"},
                        {"activated": 1}, {"qualified": "true"}, {"identity": {"AWS_ACCESS_KEY_ID": "fake"}},
                        {"runner": 1}):
            with self.subTest(changes=changes), self.assertRaises(publisher.PublicationError):
                self.client(**changes)
        with self.assertRaises(publisher.PublicationError):
            publisher.AwsCliPublisher("aws", expected_version="2.31.20")
        for role, token in ((self.ROLE.replace("411399509833", "000000000000"), self.TOKEN),
                            ("arn:aws:iam::411399509833:role/peer-deploy", self.TOKEN),
                            (self.ROLE, b"static-key"), (self.ROLE, self.TOKEN * 2000),
                            (self.ROLE + "\n", self.TOKEN), (self.ROLE, "token")):
            with self.subTest(role=role), self.assertRaises(publisher.PublicationError):
                publisher.OidcPublicationIdentity(role, token)
        self.assertNotIn(self.TOKEN.decode(), repr(publisher.OidcPublicationIdentity(self.ROLE, self.TOKEN)))

    def test_exact_frozen_bytes_fixed_hash_destination_and_one_conditional_put(self):
        receipt = self.client()(self.EVENT, self.body)
        self.assertEqual(receipt, {"status": "published", "version_id": "immutable-version+1",
                                  "body_sha256": hashlib.sha256(self.body).hexdigest()})
        self.assertEqual(len(self.calls), 2)
        argv, env = self.calls[1]
        self.assertEqual(argv[:3], ("/qualified/aws", "s3api", "put-object"))
        request = json.loads(argv[argv.index("--cli-input-json") + 1])
        self.assertEqual(request, {"Bucket": "zkp2p-sekko-releases-411399509833-us-east-1",
                                  "Key": "notices/v1/pay-api/staging/" + hashlib.sha256(self.EVENT.encode()).hexdigest() + ".json",
                                  "ExpectedBucketOwner": "411399509833", "IfNoneMatch": "*",
                                  "ContentLength": len(self.body), "ContentType": "application/json",
                                  "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": self.checksum})
        self.assertEqual(argv[argv.index("--region") + 1], "us-east-1")
        self.assertEqual(argv[argv.index("--endpoint-url") + 1], "https://s3.us-east-1.amazonaws.com")
        self.assertEqual(self.saved, [self.body])
        self.assertTrue(all(not root.exists() for root in self.roots))
        for private in (self.body.decode(), self.TOKEN.decode()):
            self.assertNotIn(private, repr(argv) + repr(receipt))

    def test_clean_environment_ignores_ambient_profiles_keys_proxies_endpoints_and_roles(self):
        ambient = {"AWS_PROFILE": "personal", "AWS_ACCESS_KEY_ID": "fake-personal", "AWS_SECRET_ACCESS_KEY": "fake",
                   "AWS_SESSION_TOKEN": "fake", "AWS_ROLE_ARN": "deploy-role", "AWS_ENDPOINT_URL": "http://127.0.0.1",
                   "HTTPS_PROXY": "http://127.0.0.1", "HTTP_PROXY": "http://127.0.0.1", "ALL_PROXY": "private",
                   "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1", "PYTHONPATH": "ambient", "BOTO_CONFIG": "ambient"}
        with patch.dict(os.environ, ambient):
            self.client()(self.EVENT, self.body)
        env = self.calls[1][1]
        for name in ambient:
            if name not in ("AWS_ROLE_ARN", "BOTO_CONFIG"):
                self.assertNotIn(name, env)
        self.assertEqual(env["AWS_ROLE_ARN"], self.ROLE)
        self.assertEqual(env["AWS_EC2_METADATA_DISABLED"], "true")
        self.assertEqual(env["AWS_MAX_ATTEMPTS"], "1")
        self.assertEqual(env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"], "true")
        self.assertEqual(env["NO_PROXY"], "*")
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_invalid_or_mismatched_event_and_envelope_do_not_execute_cli(self):
        values = [(self.EVENT + "/evil", self.body), (self.EVENT, b""), (self.EVENT, b"x" * 65537),
                  (self.EVENT, bytearray(self.body)), (self.EVENT, b"{}"), (self.EVENT, b"not-json"),
                  (self.EVENT, self.body.replace(b"release-v1", b"scope-v1")),
                  (self.EVENT, self.body.replace(self.EVENT.encode(), b"wrong")),
                  (self.EVENT, self.body.replace(b'"purpose":"release-v1"', b'"purpose":"release-v1","purpose":"release-v1"'))]
        for event, body in values:
            with self.subTest(length=len(body)), self.assertRaises(publisher.PublicationError):
                self.client()(event, body)
        self.assertEqual(self.calls, [])

    def test_body_hard_limit_and_original_whitespace_bytes_are_preserved(self):
        self.body += b" " * (65536 - len(self.body))
        self.checksum = base64.b64encode(hashlib.sha256(self.body).digest()).decode()
        self.reply = publisher.CommandResult(0, notice.canonical({"VersionId": "v", "ChecksumSHA256": self.checksum}))
        self.client()(self.EVENT, self.body)
        self.assertEqual(self.saved, [self.body])

    def test_409_412_timeout_lost_ack_and_nonzero_results_are_unknown_without_retry(self):
        for reply in (TimeoutError("private lost ACK"), OSError("private"),
                      publisher.CommandResult(409, b"private conflict"), publisher.CommandResult(412, b"private exists"),
                      publisher.CommandResult(1, b"private credentials"), publisher.CommandResult(True, b"{}")):
            self.calls, self.saved = [], []
            self.reply = reply
            with self.subTest(reply=type(reply)), self.assertRaisesRegex(publisher.PublicationUnknown, "^publication_unknown$"):
                self.client()(self.EVENT, self.body)
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(self.saved, [self.body])
            self.assertTrue(all(not root.exists() for root in self.roots))

    def test_missing_malformed_or_mismatched_immutable_proof_never_succeeds(self):
        valid = {"VersionId": "v", "ChecksumSHA256": self.checksum}
        fixtures = [b"{}", b"[]", b"not-json", b"x" * (publisher.MAX_OUTPUT + 1),
                    b'{"VersionId":"v","VersionId":"v"}', b'{"VersionId":NaN}']
        for version in (None, "", "null", True, "x" * 1025, "v\n"):
            fixtures.append(notice.canonical({**valid, "VersionId": version}))
        fixtures += [notice.canonical({"VersionId": "v"}), notice.canonical({**valid, "ChecksumSHA256": "wrong"}),
                     notice.canonical({**valid, "ChecksumType": "COMPOSITE"})]
        for raw in fixtures:
            self.calls = []
            self.reply = publisher.CommandResult(0, raw)
            with self.subTest(rawlen=len(raw)), self.assertRaises(publisher.PublicationUnknown):
                self.client()(self.EVENT, self.body)
            self.assertEqual(len(self.calls), 2)

    def test_version_pin_failure_prevents_put_and_cleans_scratch(self):
        for reply in (publisher.CommandResult(0, b"aws-cli/2.31.21 fixture"), publisher.CommandResult(0, b""),
                      publisher.CommandResult(1, b"private"), publisher.CommandResult(0, b"x" * 2049)):
            roots = []
            def runner(argv, *, cwd, env):
                roots.append(cwd)
                self.assertEqual(argv[1:], ("--version",))
                return reply
            with self.assertRaises(publisher.PublicationUnknown):
                self.client(runner=runner)(self.EVENT, self.body)
            self.assertTrue(roots)
            self.assertTrue(all(not root.exists() for root in roots))

    def test_notice_injected_seam_preserves_gates_signing_and_no_https_post(self):
        calls, saved, clocks, published = [], [], [], []
        config = {key: value for key, value in CONFIG.items() if key != "inbox_url"}
        env = {**ENV, "SEKKO_NOTICE_CONFIG": json.dumps(config)}
        def transport(method, url, body=None):
            calls.append((method, url))
            self.assertEqual(method, "GET")
            return 200, notice.canonical(READY)
        def publish(event, body):
            self.assertEqual(saved, [notice.canonical(json.loads(body)["payload"])])
            self.assertEqual(event, self.EVENT)
            signed = hmac.new(bytes.fromhex(KEY), b"release-v1\x00" + saved[0], hashlib.sha256).hexdigest()
            self.assertEqual(json.loads(body)["signature"], signed)
            published.append(body)
            return {"status": "published", "version_id": "v", "body_sha256": hashlib.sha256(body).hexdigest()}
        result = notice.execute(env, persist_payload=saved.append, transport=transport,
                                publisher=publish, now=lambda: clocks.append(True) or 1000,
                                sleep=lambda _: self.fail("no publication retry"))
        self.assertEqual((result["status"], result["reason"], result["attempts"], result["deployment_outcome"]),
                         ("published", "controller_intake_unconfirmed", 1, "success"))
        self.assertEqual(len(published), 1)
        self.assertEqual(clocks, [True])
        self.assertEqual(calls, [("GET", notice.STAGING_ORIGIN + "/readyz")])
        for gate in ("SEKKO_NOTICE_ENABLED", "NOTICE_DEPLOY_RESULT"):
            skipped = notice.execute({**env, gate: "false"}, persist_payload=lambda *_: self.fail("persist"),
                                     publisher=lambda *_: self.fail("publish"), transport=lambda *_: self.fail("probe"))
            self.assertEqual(skipped["status"], "skipped")
        failed = notice.execute(env, persist_payload=lambda *_: self.fail("persist"),
                                publisher=lambda *_: self.fail("publish"), transport=lambda *_: (503, b"private"))
        self.assertEqual(failed["reason"], "readiness_failed")

    def test_unknown_publication_and_invalid_receipt_keep_successful_deployment_and_safe_evidence(self):
        for outcome in (TimeoutError("private token and signed envelope"), None, {"status": "queued"},
                        {"status": "published", "version_id": "null", "body_sha256": "wrong"}):
            with self.subTest(outcome=type(outcome)), tempfile.TemporaryDirectory() as temp:
                attempts = []
                def publish(event, body):
                    attempts.append(body)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                config = {key: value for key, value in CONFIG.items() if key != "inbox_url"}
                env = {**ENV, "SEKKO_NOTICE_CONFIG": json.dumps(config), "RUNNER_TEMP": temp,
                       "GITHUB_OUTPUT": temp + "/output", "GITHUB_STEP_SUMMARY": temp + "/summary"}
                with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as captured:
                    code = notice.main(transport=lambda *_: (200, notice.canonical(READY)), publisher=publish,
                                       now=lambda: 1000, sleep=lambda _: self.fail("retry"))
                report = json.loads(Path(temp, "sekko-release-notice-report.json").read_bytes())
                self.assertEqual(code, 1)
                self.assertEqual((report["reason"], report["deployment_outcome"], report["attempts"]),
                                 ("publication_unknown", "success", 1))
                self.assertEqual(len(attempts), 1)
                self.assertEqual(Path(temp, "sekko-release-notice-pending.json").read_bytes(),
                                 notice.canonical(json.loads(attempts[0])["payload"]))
                combined = captured.getvalue() + json.dumps(report) + Path(temp, "summary").read_text()
                for private in (KEY, "private", "signature", CONFIG["description"], CONFIG["inbox_url"]):
                    self.assertNotIn(private, combined)
                self.assertNotIn("queued", combined)

    def test_published_main_reports_storage_only_without_controller_ack(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {key: value for key, value in CONFIG.items() if key != "inbox_url"}
            env = {**ENV, "SEKKO_NOTICE_CONFIG": json.dumps(config), "RUNNER_TEMP": temp,
                   "GITHUB_OUTPUT": temp + "/output", "GITHUB_STEP_SUMMARY": temp + "/summary"}
            def publish(event, body):
                return {"status": "published", "version_id": "v", "body_sha256": hashlib.sha256(body).hexdigest()}
            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(notice.main(transport=lambda *_: (200, notice.canonical(READY)),
                                             publisher=publish, now=lambda: 1000), 0)
            report = json.loads(Path(temp, "sekko-release-notice-report.json").read_bytes())
            self.assertEqual((report["status"], report["reason"]), ("published", "controller_intake_unconfirmed"))
            self.assertEqual(Path(temp, "output").read_text(), "status=published\n")
            self.assertIn("controller intake unconfirmed", Path(temp, "summary").read_text())
            self.assertEqual(captured.getvalue(), "")

    def test_bounded_runner_uses_pipes_no_shell_and_cleans_even_exited_leader(self):
        process = unittest.mock.MagicMock(pid=123456789, returncode=0)
        streams = []
        for raw in (b"ack", b"private stderr"):
            reader, writer = os.pipe()
            os.write(writer, raw)
            os.close(writer)
            streams.append(os.fdopen(reader, "rb"))
        process.stdout, process.stderr = streams
        try:
            with tempfile.TemporaryDirectory() as scratch, \
                 patch.object(publisher.subprocess, "Popen", return_value=process) as popen, \
                 patch.object(publisher.os, "killpg", side_effect=ProcessLookupError()) as kill:
                result = publisher.BoundedCliRunner()(("/qualified/aws", "--version"), cwd=Path(scratch), env={})
            self.assertEqual(result, publisher.CommandResult(0, b"ack"))
            self.assertNotIn("private", repr(result))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertTrue(popen.call_args.kwargs["close_fds"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            kill.assert_called_once_with(process.pid, publisher.signal.SIGTERM)
            self.assertTrue(all(stream.closed for stream in streams))
        finally:
            for stream in streams:
                stream.close()

    def test_runner_output_overflow_and_deadline_stop_owned_group(self):
        for limit, seconds in ((2, 5), (100, 0)):
            process = unittest.mock.MagicMock(pid=123456789, returncode=0)
            streams = []
            for raw in (b"oversize", b"private"):
                reader, writer = os.pipe()
                os.write(writer, raw)
                os.close(writer)
                streams.append(os.fdopen(reader, "rb"))
            process.stdout, process.stderr = streams
            try:
                with tempfile.TemporaryDirectory() as scratch, \
                     patch.object(publisher, "MAX_OUTPUT", limit), patch.object(publisher, "COMMAND_SECONDS", seconds), \
                     patch.object(publisher.subprocess, "Popen", return_value=process), \
                     patch.object(publisher, "_stop_group") as stop:
                    with self.assertRaises(publisher.PublicationUnknown):
                        publisher.BoundedCliRunner()(("/qualified/aws",), cwd=Path(scratch), env={})
                stop.assert_called_once_with(process)
                self.assertTrue(all(stream.closed for stream in streams))
            finally:
                for stream in streams:
                    stream.close()

    def test_group_cleanup_kills_remaining_descendants_independently_of_leader(self):
        process = unittest.mock.MagicMock(pid=123456789)
        process.poll.return_value = 0
        with patch.object(publisher.time, "monotonic", side_effect=range(20)), \
             patch.object(publisher.os, "killpg", side_effect=[None, None, None, ProcessLookupError()]) as kill:
            publisher._stop_group(process)
        self.assertEqual(kill.call_args_list, [unittest.mock.call(process.pid, publisher.signal.SIGTERM),
                                              unittest.mock.call(process.pid, 0),
                                              unittest.mock.call(process.pid, publisher.signal.SIGKILL),
                                              unittest.mock.call(process.pid, 0)])
        process.wait.assert_called_once_with(timeout=1)
        with patch.object(publisher.time, "monotonic", side_effect=range(20)), \
             patch.object(publisher.os, "killpg", side_effect=PermissionError()):
            with self.assertRaises(publisher.PublicationUnknown):
                publisher._stop_group(process)

    def test_cleanup_cannot_confirm_success_while_group_remains_after_kill(self):
        process = unittest.mock.MagicMock(pid=123456789)
        process.poll.return_value = 0
        with patch.object(publisher.time, "monotonic", side_effect=range(20)), \
             patch.object(publisher.time, "sleep"), patch.object(publisher.os, "killpg") as kill:
            with self.assertRaisesRegex(publisher.PublicationUnknown, "^publication_unknown$"):
                publisher._stop_group(process)
        self.assertIn(unittest.mock.call(process.pid, publisher.signal.SIGKILL), kill.call_args_list)
        process.wait.assert_called_once_with(timeout=1)

    def test_valid_storage_receipt_stays_unknown_if_exited_leader_group_survives_kill(self):
        process = unittest.mock.MagicMock(pid=123456789)
        process.poll.return_value = 0
        def runner(argv, *, cwd, env):
            result = self.runner(argv, cwd=cwd, env=env)
            if argv[1:3] == ("s3api", "put-object"):
                publisher._stop_group(process)
            return result
        with patch.object(publisher.time, "monotonic", side_effect=range(20)), \
             patch.object(publisher.time, "sleep"), patch.object(publisher.os, "killpg"):
            with self.assertRaisesRegex(publisher.PublicationUnknown, "^publication_unknown$"):
                self.client(runner=runner)(self.EVENT, self.body)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.saved, [self.body])
        self.assertTrue(all(not root.exists() for root in self.roots))

    def test_scratch_size_entry_count_and_special_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bounded").write_bytes(b"x" * publisher.MAX_SCRATCH)
            publisher._check_scratch(root)
            with (root / "bounded").open("ab") as stream:
                stream.write(b"x")
            with self.assertRaises(publisher.PublicationUnknown):
                publisher._check_scratch(root)
            (root / "bounded").unlink()
            for number in range(publisher.MAX_FILES + 1):
                (root / str(number)).touch()
            with self.assertRaises(publisher.PublicationUnknown):
                publisher._check_scratch(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "link").symlink_to("/nonexistent-fixture")
            with self.assertRaises(publisher.PublicationUnknown):
                publisher._check_scratch(root)


if __name__ == "__main__":
    unittest.main()
