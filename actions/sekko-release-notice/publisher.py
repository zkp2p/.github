"""Inactive trusted CI publisher. Object storage is not a controller queue ACK.

No operation, credential discovery or CLI execution occurs on construction.
Only trusted code may supply activation, qualification and the dedicated OIDC
identity; the notice, action inputs and ambient environment cannot select them.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time

BUCKET = "zkp2p-sekko-releases-411399509833-us-east-1"
PREFIX = "notices/v1/pay-api/staging/"
REGION = "us-east-1"
OWNER = "411399509833"
PUBLICATION_ROLE = "arn:aws:iam::411399509833:role/sekko-release-publisher"
MAX_BODY = 65_536
MAX_OUTPUT = 131_072  # Combined stdout/stderr, never raw logged.
MAX_TOKEN = 16_384
MAX_SCRATCH = 262_144
MAX_FILES = 64
COMMAND_SECONDS = 5
_EVENT = re.compile(r"pay:staging:[1-9][0-9]{0,19}:[1-9][0-9]{0,19}:[a-f0-9]{40}\Z")
_VERSION = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}\Z")


class PublicationError(ValueError):
    """Fixed reason only; never external output or credential text."""


class PublicationInactive(PublicationError):
    pass


class PublicationUnknown(PublicationError):
    """No success proof; this may include a successfully stored object."""


@dataclass(frozen=True)
class OidcPublicationIdentity:
    role_arn: str
    token: bytes = field(repr=False)

    def __post_init__(self):
        if (type(self.role_arn) is not str or self.role_arn != PUBLICATION_ROLE
                or type(self.token) is not bytes or not 1 <= len(self.token) <= MAX_TOKEN
                or not re.fullmatch(rb"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", self.token)):
            raise PublicationError("invalid_publication_identity")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes = field(repr=False)


def object_key(event_id):
    if type(event_id) is not str or not _EVENT.fullmatch(event_id):
        raise PublicationError("invalid_publication_event")
    return PREFIX + hashlib.sha256(event_id.encode("ascii")).hexdigest() + ".json"


def _document(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _stop_group(process):
    # The owned group may outlive its leader. Never use leader exit as proof
    # that descendants stopped, or wait for the leader instead of the group.
    def signal_group(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            if sig == signal.SIGKILL:
                raise PublicationUnknown("publication_unknown") from None
        return True

    def wait_for_group_exit(seconds):
        deadline = time.monotonic() + seconds
        while True:
            process.poll()
            if not signal_group(0):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def kill_and_confirm():
        if signal_group(signal.SIGKILL) and not wait_for_group_exit(1):
            raise PublicationUnknown("publication_unknown")

    try:
        if signal_group(signal.SIGTERM) and not wait_for_group_exit(0.2):
            kill_and_confirm()
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            kill_and_confirm()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise PublicationUnknown("publication_unknown") from None


def _check_scratch(root):
    total, entries, pending = 0, 0, [root]
    while pending:
        with os.scandir(pending.pop()) as children:
            for child in children:
                entries += 1
                info = child.stat(follow_symlinks=False)
                if entries > MAX_FILES:
                    raise PublicationUnknown("publication_unknown")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > MAX_SCRATCH:
                        raise PublicationUnknown("publication_unknown")
                else:
                    raise PublicationUnknown("publication_unknown")


class BoundedCliRunner:
    """Pipes provide backpressure; output never grows an unbounded scratch file."""

    def __call__(self, argv, *, cwd, env):
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, close_fds=True)
        try:
            deadline, count, chunks = time.monotonic() + COMMAND_SECONDS, 0, []
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    _check_scratch(cwd)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PublicationUnknown("publication_unknown")
                    for key, _ in selector.select(min(remaining, 0.05)):
                        raw = os.read(key.fd, min(8192, MAX_OUTPUT + 1 - count))
                        count += len(raw)
                        if count > MAX_OUTPUT:
                            raise PublicationUnknown("publication_unknown")
                        if not raw:
                            selector.unregister(key.fileobj)
                        elif key.fileobj is process.stdout:
                            chunks.append(raw)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicationUnknown("publication_unknown")
                process.wait(timeout=remaining)
                _check_scratch(cwd)
                return CommandResult(process.returncode, b"".join(chunks))
        finally:
            try:
                _stop_group(process)
            finally:
                process.stdout.close()
                process.stderr.close()


def _private_file(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


class AwsCliPublisher:
    """One conditional PUT per call; no reads, discovery, retries or fallback.

    Qualification is an external deployment decision, not inferred from a CLI
    version or a caller-provided identity. The role must be a separately reviewed
    dedicated publication role; this module cannot establish its IAM policy.
    """

    def __init__(self, cli_path, *, expected_version, identity=None, activated=False,
                 qualified=False, runner=None):
        if (not isinstance(cli_path, (str, Path)) or not Path(cli_path).is_absolute()
                or type(expected_version) is not str
                or not re.fullmatch(r"2\.[0-9]+\.[0-9]+", expected_version)
                or type(activated) is not bool or type(qualified) is not bool
                or identity is not None and type(identity) is not OidcPublicationIdentity):
            raise PublicationError("invalid_publication_configuration")
        self.cli_path, self.expected_version = Path(cli_path), expected_version
        self.identity, self.activated, self.qualified = identity, activated, qualified
        self.runner = BoundedCliRunner() if runner is None else runner
        if not callable(self.runner):
            raise PublicationError("invalid_publication_configuration")
        self._lock = threading.Lock()

    def _environment(self, root):
        home = root / "home"
        home.mkdir(mode=0o700)
        config, credentials, token = root / "config", root / "credentials", root / "oidc"
        for path, raw in ((config, b""), (credentials, b""), (token, self.identity.token)):
            _private_file(path, raw)
        return {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C", "LC_ALL": "C", "NO_PROXY": "*",
                "AWS_CONFIG_FILE": str(config), "AWS_SHARED_CREDENTIALS_FILE": str(credentials),
                "BOTO_CONFIG": str(credentials), "AWS_ROLE_ARN": self.identity.role_arn,
                "AWS_WEB_IDENTITY_TOKEN_FILE": str(token), "AWS_ROLE_SESSION_NAME": "sekko-release-publisher",
                "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION, "AWS_PAGER": "",
                "AWS_CLI_AUTO_PROMPT": "off", "AWS_MAX_ATTEMPTS": "1", "AWS_RETRY_MODE": "standard",
                "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true", "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_STS_REGIONAL_ENDPOINTS": "regional"}

    def __call__(self, event_id, body):
        if self.activated is not True or self.qualified is not True or self.identity is None:
            raise PublicationInactive("publication_inactive")
        key = object_key(event_id)
        if type(body) is not bytes or not 1 <= len(body) <= MAX_BODY:
            raise PublicationError("invalid_publication_body")
        try:
            envelope = _document(body)
            if (type(envelope) is not dict or set(envelope) != {"key_id", "purpose", "payload", "signature"}
                    or envelope["key_id"] != "release-ci" or envelope["purpose"] != "release-v1"
                    or type(envelope["payload"]) is not dict or envelope["payload"].get("event_id") != event_id
                    or type(envelope["signature"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", envelope["signature"])):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise PublicationError("invalid_publication_body") from None
        digest = hashlib.sha256(body).digest()
        checksum = base64.b64encode(digest).decode("ascii")
        try:
            # Private, bounded scratch disappears on every normal/error path.
            # Serialized calls do not share body/token/config files.
            with self._lock, tempfile.TemporaryDirectory(prefix="sekko-release-publish-") as directory:
                root = Path(directory)
                env = self._environment(root)
                _private_file(root / "body", body)
                result = self.runner((str(self.cli_path), "--version"), cwd=root, env=env)
                if (type(result) is not CommandResult or type(result.returncode) is not int
                        or result.returncode != 0 or type(result.stdout) is not bytes
                        or not 1 <= len(result.stdout) <= 2048
                        or result.stdout.decode("ascii").split()[0] != f"aws-cli/{self.expected_version}"):
                    raise PublicationUnknown("publication_unknown")
                request = {"Bucket": BUCKET, "Key": key, "IfNoneMatch": "*", "ExpectedBucketOwner": OWNER,
                           "ContentLength": len(body), "ContentType": "application/json",
                           "ChecksumAlgorithm": "SHA256", "ChecksumSHA256": checksum}
                argv = (str(self.cli_path), "s3api", "put-object", "--cli-input-json",
                        json.dumps(request, separators=(",", ":")), "--body", str(root / "body"),
                        "--region", REGION, "--endpoint-url", "https://s3.us-east-1.amazonaws.com",
                        "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt",
                        "--cli-connect-timeout", "2", "--cli-read-timeout", "2")
                result = self.runner(argv, cwd=root, env=env)
                if (type(result) is not CommandResult or type(result.returncode) is not int
                        or result.returncode != 0 or type(result.stdout) is not bytes
                        or not 1 <= len(result.stdout) <= MAX_OUTPUT):
                    raise PublicationUnknown("publication_unknown")
                receipt = _document(result.stdout)
                version = receipt.get("VersionId") if type(receipt) is dict else None
                if (type(version) is not str or version == "null" or not _VERSION.fullmatch(version)
                        or receipt.get("ChecksumSHA256") != checksum
                        or receipt.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT"):
                    raise PublicationUnknown("publication_unknown")
                return {"status": "published", "version_id": version, "body_sha256": digest.hex()}
        except PublicationUnknown:
            raise
        except Exception:
            # Includes lost ACK, process cleanup failure, 409/412, malformed
            # response and local I/O failure. No alternate key or second PUT.
            raise PublicationUnknown("publication_unknown") from None
