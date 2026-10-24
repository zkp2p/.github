"""Patch the concrete Ubuntu 24.04 shared Brotli ABI before subsequent steps."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

os.umask(0o077)


def output(args):
    return subprocess.check_output(args, text=True).strip()


def packages():
    text = output(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}:${Architecture}\t${Version}\t${db:Status-Status}\n",
        ]
    )
    return {
        name: version
        for name, version, state in (line.split("\t") for line in text.splitlines())
        if state == "installed"
    }


def probe():
    # A fresh process is required after dpkg: an already-loaded SONAME is cached.
    code = """import ctypes,ctypes.util,json
library=ctypes.util.find_library('brotlidec')
if library!='libbrotlidec.so.1': raise SystemExit('unsupported native Brotli SONAME')
lib=ctypes.CDLL(library)
lib.BrotliDecoderVersion.restype=ctypes.c_uint32
lib.BrotliDecoderErrorString.argtypes=[ctypes.c_int]
lib.BrotliDecoderErrorString.restype=ctypes.c_char_p
print(json.dumps({'version':lib.BrotliDecoderVersion(),'blockSwitch':lib.BrotliDecoderErrorString(-17).decode()}))"""
    return json.loads(output([sys.executable, "-c", code]))


def symbols(path, static=False):
    command = ["nm", "-g" if static else "-D", "--defined-only", str(path)]
    return {
        fields[-1]
        for line in output(command).splitlines()
        if len(fields := line.split()) == 3
    }


release = dict(
    line.split("=", 1)
    for line in Path("/etc/os-release").read_text().splitlines()
    if "=" in line
)
if (
    release.get("ID", "").strip('"') != "ubuntu"
    or release.get("VERSION_ID", "").strip('"') != "24.04"
):
    raise SystemExit("Native Brotli bootstrap requires Ubuntu 24.04")
current = probe()
if current["blockSwitch"] == "_ERROR_FORMAT_BLOCK_SWITCH":
    print(json.dumps({"nativeBrotli": current, "changed": False}))
    sys.exit(0)
if current["version"] != 0x1001000:
    raise SystemExit(
        "Native Brotli bootstrap requires the inspected 1.1.0 ABI or an already-fixed decoder"
    )
for tool in (
    "gcc",
    "cmake",
    "ninja",
    "xz",
    "nm",
    "readelf",
    "dpkg-deb",
    "dpkg-architecture",
    "timeout",
):
    if not shutil.which(tool):
        raise SystemExit("Native Brotli build prerequisite missing: " + tool)

before = packages()
evidence = Path(
    tempfile.mkdtemp(prefix="brotli-security.", dir=os.environ["RUNNER_TEMP"])
)
(evidence / "packages-before.json").write_text(json.dumps(before, indent=2) + "\n")
(evidence / "native-before.json").write_text(json.dumps(current) + "\n")
builder = Path(__file__).parent / "vendor/build-brotli-security-packages.sh"
if (
    hashlib.sha256(builder.read_bytes()).hexdigest()
    != "5586dec12babe9d6aadf0f46a9aaa794fb9ea2b71baa2eba6e4a14b875253313"
):
    raise SystemExit("Vendored Brotli package builder checksum mismatch")
with (evidence / "build.log").open("w") as log:
    subprocess.run(
        [
            "timeout",
            "--kill-after=10",
            "240",
            "bash",
            str(builder),
            str(evidence / "packages"),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
    )

architecture = output(["dpkg", "--print-architecture"])
multiarch = output(["dpkg-architecture", "-qDEB_HOST_MULTIARCH"])
runtime = evidence / f"packages/libbrotli1_1.1.1-0peer1_{architecture}.deb"
development = evidence / f"packages/libbrotli-dev_1.1.1-0peer1_{architecture}.deb"
stage = evidence / "stage"
for package in (runtime, development):
    subprocess.run(["dpkg-deb", "-x", str(package), str(stage)], check=True)
for name in ("brotlicommon", "brotlidec", "brotlienc"):
    soname = f"lib{name}.so.1"
    installed = Path("/usr/lib") / multiarch / soname
    candidate = stage / "usr/lib" / multiarch / soname
    if not symbols(installed) <= symbols(candidate):
        raise SystemExit("Native Brotli update removes exported ABI symbols: " + soname)
    if f"[{soname}]" not in output(["readelf", "-d", str(candidate)]):
        raise SystemExit("Native Brotli update changes SONAME: " + soname)
    static = stage / "usr/lib" / multiarch / f"lib{name}.a"
    if not static.is_file():
        raise SystemExit("Native Brotli development archive missing: " + static.name)
    old_static = Path("/usr/lib") / multiarch / static.name
    if old_static.exists() and not symbols(old_static, static=True) <= symbols(
        static, static=True
    ):
        raise SystemExit(
            "Native Brotli update removes static ABI symbols: " + static.name
        )
if any(name.split(":")[0] == "libbrotli-dev" for name in before):
    for filename in output(["dpkg-query", "-L", "libbrotli-dev"]).splitlines():
        if (
            filename.endswith((".a", ".h", ".so", ".pc"))
            and not (stage / filename.lstrip("/")).exists()
        ):
            raise SystemExit("Native Brotli development file removed: " + filename)

with (evidence / "install.log").open("w") as log:
    subprocess.run(
        ["sudo", "-n", "dpkg", "-i", str(runtime), str(development)],
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
    )
after = packages()
(evidence / "packages-after.json").write_text(json.dumps(after, indent=2) + "\n")
delta = {
    name: [before.get(name), after.get(name)]
    for name in before.keys() | after.keys()
    if before.get(name) != after.get(name)
}
if {name.split(":")[0] for name in delta} != {"libbrotli1", "libbrotli-dev"} or any(
    value[1] != "1.1.1-0peer1" for value in delta.values()
):
    raise SystemExit("Native Brotli package transaction changed an unexpected package")
fixed = probe()
if (
    fixed["version"] != 0x1001001
    or fixed["blockSwitch"] != "_ERROR_FORMAT_BLOCK_SWITCH"
):
    raise SystemExit("Native Brotli decoder did not load the fixed library")
curl = output(["curl", "-V"])
if "brotli/1.1.1" not in curl:
    raise SystemExit("curl did not load fixed native Brotli 1.1.1")
receipt = {
    "nativeBrotli": fixed,
    "changed": True,
    "packageDelta": delta,
    "abiPreserved": True,
    "developmentFilesPreserved": True,
    "curl": curl.splitlines()[0],
}
(evidence / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
print("Private native build/inventory evidence: " + str(evidence))
