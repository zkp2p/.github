#!/usr/bin/env python3
"""Send one bounded notice. No private imports, subprocesses, proxies, or redirects."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import time
from urllib.parse import urlsplit

MAX_BYTES = 65_536
STAGING_ORIGIN = "https://api-staging.pay.peer.xyz"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
RETRYABLE = {408, 429, 503}
ATTEMPTS = 3
TIMEOUT = 10


class NoticeError(ValueError):
    """Fixed reason codes only; exception text never includes external data."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def document(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        raise NoticeError("invalid_document")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise NoticeError("invalid_document")
            result[key] = value
        return result

    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(NoticeError("invalid_document")))
        if type(result) is not dict:
            raise NoticeError("invalid_document")
        canonical(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise NoticeError("invalid_document") from None


def endpoint(value):
    if not isinstance(value, str) or len(value) > 280:
        raise NoticeError("invalid_config")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        valid = (parsed.scheme == "https" and host is not None and parsed.port is None
                 and re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host)
                 and len(host) <= 253 and parsed.username is None and parsed.password is None
                 and value == "https://" + host + "/v1/releases")
    except ValueError:
        valid = False
    if not valid:
        raise NoticeError("invalid_config")
    return value


def configuration(raw, encoded_key):
    config = document(raw.encode("utf-8"))
    if set(config) != {"inbox_url", "scope_id", "target_id", "surface", "origin", "description"}:
        raise NoticeError("invalid_config")
    endpoint(config["inbox_url"])
    if config["origin"] != STAGING_ORIGIN:
        raise NoticeError("invalid_config")
    if any(not isinstance(config[name], str) or not IDENTIFIER.fullmatch(config[name])
           for name in ("scope_id", "target_id", "surface")):
        raise NoticeError("invalid_config")
    description = config["description"]
    if (not isinstance(description, str) or not 1 <= len(description) <= 1000
            or any(ord(c) < 32 and c not in "\n\t" for c in description)):
        raise NoticeError("invalid_config")
    if (not re.fullmatch(r"[a-fA-F0-9]{64,8192}", encoded_key)
            or len(encoded_key) % 2):
        raise NoticeError("invalid_key")
    return config, bytes.fromhex(encoded_key)


def request(method, url, body=None):
    """Direct TLS with default certificate checks; no proxy or redirect handler."""
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=TIMEOUT,
                                            context=ssl.create_default_context())
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, parsed.path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_BYTES + 1)
        return response.status, raw
    finally:
        connection.close()


def ready(status, raw, sha):
    if status != 200:
        return False
    body = document(raw)
    report = body.get("responseObject")
    return (body.get("success") is True and type(body.get("statusCode")) is int
            and body["statusCode"] == 200 and type(report) is dict
            and report.get("ok") is True and report.get("commit") == sha
            and report.get("dependencies") == {"database": "ok", "redis": "ok"})


def eligible(env):
    return all(env.get(name) == value for name, value in {
        "SEKKO_NOTICE_ENABLED": "true", "NOTICE_REPOSITORY": "zkp2p/pay",
        "NOTICE_REF": "releases/staging", "NOTICE_ENVIRONMENT": "staging",
        "NOTICE_ENVIRONMENTS_FILE": "deploy/environments-pay.yml", "NOTICE_MODE": "image",
        "NOTICE_DEPLOY_RESULT": "success",
    }.items())


def execute(env, *, transport=request, now=time.time, sleep=time.sleep):
    report = {"status": "skipped", "reason": "not_eligible", "attempts": 0}
    if not eligible(env):
        return report
    report.update(status="failed", reason="invalid_config", deployment_outcome="success")
    try:
        config, key = configuration(env.get("SEKKO_NOTICE_CONFIG", ""), env.get("SEKKO_RELEASE_KEY", ""))
        sha = env.get("NOTICE_SOURCE_SHA", "")
        run = env.get("NOTICE_RUN_ID", "")
        attempt = env.get("NOTICE_RUN_ATTEMPT", "")
        command = env.get("NOTICE_COMMAND_ID", "")
        deployment = env.get("NOTICE_DEPLOYMENT_ID", "")
        if (not re.fullmatch(r"[a-f0-9]{40}", sha)
                or any(not re.fullmatch(r"[1-9][0-9]{0,19}", item) for item in (run, attempt, deployment))
                or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", command)):
            raise NoticeError("invalid_context")
        event = f"pay:staging:{run}:{attempt}:{sha}"
        report.update(event_id=event, command_id=command, deployment_id=deployment)
        # Never sign availability until independent public ingress proves the serving SHA.
        try:
            status, raw = transport("GET", config["origin"] + "/readyz")
            if not ready(status, raw, sha):
                raise NoticeError("readiness_failed")
        except (ValueError, KeyError, TypeError):
            raise NoticeError("readiness_failed") from None
        released = int(now())
        payload = {name: config[name] for name in ("scope_id", "target_id", "surface", "origin", "description")}
        payload.update(event_id=event, environment="staging", version=sha,
                       released_at=released, expires_at=released + 3600,
                       availability="available", change_type="release")
        signature = hmac.new(key, b"release-v1\x00" + canonical(payload), hashlib.sha256).hexdigest()
        body = canonical({"key_id": "release-ci", "purpose": "release-v1",
                          "payload": payload, "signature": signature})
        # Freeze these bytes once: retries must not change timestamp, ID or signature.
        for number in range(1, ATTEMPTS + 1):
            report["attempts"] = number
            try:
                status, raw = transport("POST", config["inbox_url"], body)
            except (OSError, http.client.HTTPException):
                status, raw = None, b""
            report["http_status"] = status
            if status in (200, 202):
                expected = "queued" if status == 202 else "duplicate"
                if document(raw).get("status") != expected:
                    raise NoticeError("invalid_acknowledgement")
                report.update(status="delivered", reason=expected)
                return report
            if status is not None and status not in RETRYABLE:
                raise NoticeError("delivery_rejected")
            if number < ATTEMPTS:
                sleep(number * 2)
        raise NoticeError("delivery_exhausted")
    except NoticeError as error:
        report["reason"] = str(error)
    except (OSError, http.client.HTTPException):
        report["reason"] = "readiness_transport_failed"
    return report


def main():
    # Evidence contains fixed reason codes and deployment references only: no config,
    # response body, signed envelope, signature, key, or exception details.
    path = Path(os.environ["RUNNER_TEMP"]) / "sekko-release-notice-report.json"
    path.write_bytes(canonical({"status": "failed", "reason": "interrupted", "attempts": 0}) + b"\n")
    path.chmod(0o600)
    report = execute(os.environ)
    path.write_bytes(canonical(report) + b"\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("status=" + report["status"] + "\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        if report["status"] == "failed":
            summary.write("Sekko release-notice delivery gap: **" + report["reason"]
                          + "**. Deployment outcome is separate; do not redeploy to resend. "
                          "See the safe delivery report artifact.\n")
        else:
            summary.write("Sekko release notice: " + report["status"] + ".\n")
    if report["status"] == "failed":
        print("::warning::Sekko release-notice delivery gap; see safe report artifact")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
