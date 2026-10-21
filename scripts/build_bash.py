#!/usr/bin/env python3
"""Build one supported bash release from GNU source, for testing the session on it.
Stdlib only, plus a C toolchain (`cc`, `make`).

    python scripts/build_bash.py 5.2 PREFIX

downloads `bash-<version>.tar.gz` and every official patch for it from ftp.gnu.org,
checks both against the SHA-256 sums pinned in `RELEASES`, builds, and installs to
`PREFIX` (so `PREFIX/bin/bash`). Put `PREFIX/bin` first on `PATH` and the session starts
that bash (`session.py` runs `bash` from `PATH`).

The supported versions are the keys of `RELEASES`: bash 5.1 is the oldest, as the rcfile
sets `PROMPT_COMMAND` as an array. To move to a newer patch level, raise `patches` and
replace `patches_sha256` (the SHA-256 of all the patch files, concatenated in order).
"""

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

MIRROR = "https://ftp.gnu.org/gnu/bash"

RELEASES = {
    "5.1": {
        "patches": 16,
        "tarball_sha256": "cc012bc860406dcf42f64431bcd3d2fa7560c02915a601aba9cd597a39329baa",
        "patches_sha256": "3f115eb5ab883a81bcc14137efa0857a52bee37359b87715bedb7bd02e3438b2",
    },
    "5.2": {
        "patches": 37,
        "tarball_sha256": "a139c166df7ff4471c5e0733051642ee5556c1cc8a4a78f145583c5c81ab32fb",
        "patches_sha256": "682969cc6c73d92728d3341199b59baca2b7a0cd53ee7a3b3b5a6ab7f846c81b",
    },
    "5.3": {
        "patches": 20,
        "tarball_sha256": "0d5cd86965f869a26cf64f4b71be7b96f90a3ba8b3d74e27e8e9d9d5550f31ba",
        "patches_sha256": "67aa3c11559c91dde818a8466a6a0b312de5f42b0c3f6b90cdd61ec4b7c8fd4d",
    },
}


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def _check(what: str, data: bytes, expected: str) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != expected:
        sys.exit(f"build_bash: {what}: SHA-256 {got}, expected {expected}")


def build(version: str, prefix: str) -> None:
    release = RELEASES[version]
    tarball = _fetch(f"{MIRROR}/bash-{version}.tar.gz")
    _check(f"bash-{version}.tar.gz", tarball, release["tarball_sha256"])
    short = version.replace(".", "")
    patches = [
        _fetch(f"{MIRROR}/bash-{version}-patches/bash{short}-{n:03d}")
        for n in range(1, release["patches"] + 1)
    ]
    _check(f"bash-{version} patches 1-{release['patches']}", b"".join(patches),
           release["patches_sha256"])

    with tempfile.TemporaryDirectory(prefix="build-bash-") as work:
        path = os.path.join(work, "bash.tar.gz")
        with open(path, "wb") as fh:
            fh.write(tarball)
        with tarfile.open(path) as tar:
            tar.extractall(work, filter="data")
        src = os.path.join(work, f"bash-{version}")
        for patch in patches:
            subprocess.run(["patch", "-s", "-p0"], input=patch, cwd=src, check=True)
        subprocess.run(["./configure", "--quiet", f"--prefix={os.path.abspath(prefix)}"],
                       cwd=src, check=True)
        subprocess.run(["make", "-s", f"-j{os.cpu_count() or 1}"], cwd=src, check=True)
        subprocess.run(["make", "-s", "install"], cwd=src, check=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in RELEASES:
        print(f"usage: {argv[0]} {{{','.join(RELEASES)}}} PREFIX", file=sys.stderr)
        return 2
    build(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
