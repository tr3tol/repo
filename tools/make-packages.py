#!/usr/bin/env python3
"""Rebuild the package index and the signed Release file.

Run from the repository root after dropping a new build into debs/:

    python tools/make-packages.py

Signing is what makes apt accept the repository: an unsigned flat repository is
rejected outright, and the client then keeps serving a stale index forever.
Pass --no-sign to skip it while testing.
"""

import argparse
import bz2
import email.utils
import gzip
import hashlib
import io
import lzma
import os
import subprocess
import sys
import tarfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEBS = os.path.join(REPO, "debs")
SIGNING_KEY = "tr3tol repo"
PUBLIC_KEY = "tr3tol.gpg"

# Fields worth carrying into the index, in the order package managers expect.
ORDER = [
    "Package", "Version", "Architecture", "Name", "Description", "Author",
    "Maintainer", "Section", "Depends", "Pre-Depends", "Recommends", "Conflicts",
    "Replaces", "Provides", "Breaks", "Tag", "Homepage", "Depiction",
    "SileoDepiction", "Icon", "Installed-Size",
]

INDEX_FILES = ["Packages", "Packages.gz", "Packages.bz2"]


def ar_members(blob):
    """Yield (name, data) for each member of an ar archive."""
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    offset = 8
    while offset + 60 <= len(blob):
        header = blob[offset:offset + 60]
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        yield name, blob[start:start + size]
        offset = start + size + (size % 2)


def read_control(path):
    with open(path, "rb") as handle:
        blob = handle.read()

    for name, data in ar_members(blob):
        if not name.startswith("control.tar"):
            continue
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        elif name.endswith(".xz"):
            data = lzma.decompress(data)

        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                if member.name.lstrip("./") == "control":
                    return tar.extractfile(member).read().decode("utf-8")
    raise ValueError("no control member in %s" % path)


def parse(text):
    fields, key = {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key:            # folded continuation line
            fields[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def build_packages():
    entries = []
    for filename in sorted(os.listdir(DEBS)):
        if not filename.endswith(".deb"):
            continue
        path = os.path.join(DEBS, filename)
        with open(path, "rb") as handle:
            blob = handle.read()

        fields = parse(read_control(path))
        fields["Filename"] = "debs/" + filename
        fields["Size"] = str(len(blob))
        fields["MD5sum"] = hashlib.md5(blob).hexdigest()
        fields["SHA1"] = hashlib.sha1(blob).hexdigest()
        fields["SHA256"] = hashlib.sha256(blob).hexdigest()

        lines = []
        for key in ORDER + ["Filename", "Size", "MD5sum", "SHA1", "SHA256"]:
            if key in fields:
                lines.append("%s: %s" % (key, fields[key]))
        entries.append("\n".join(lines))
        print("indexed %s (%s, %.1f KB)" % (filename, fields.get("Version", "?"),
                                            len(blob) / 1024.0))

    packages = ("\n\n".join(entries) + "\n").encode("utf-8")
    write(os.path.join(REPO, "Packages"), packages)
    write(os.path.join(REPO, "Packages.gz"), gzip.compress(packages))
    write(os.path.join(REPO, "Packages.bz2"), bz2.compress(packages))
    print("wrote %s (%d package(s))" % (", ".join(INDEX_FILES), len(entries)))


def write(path, blob):
    with open(path, "wb") as handle:
        handle.write(blob)


def static_release_fields():
    """Keep Origin, Label and friends editable in the Release file itself."""
    path = os.path.join(REPO, "Release")
    kept = []
    with io.open(path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if not stripped:
                continue
            if stripped[0] in " \t":            # a hash line from a previous run
                continue
            key = stripped.split(":", 1)[0]
            if key in ("MD5Sum", "SHA1", "SHA256", "Date"):
                continue
            kept.append(stripped)
    return kept


def build_release():
    lines = static_release_fields()
    lines.append("Date: " + email.utils.formatdate(time.time(), usegmt=True))

    # apt verifies the indexes against these, so they have to match byte for byte.
    for label, algorithm in (("MD5Sum", hashlib.md5), ("SHA256", hashlib.sha256)):
        lines.append(label + ":")
        for name in INDEX_FILES:
            with open(os.path.join(REPO, name), "rb") as handle:
                blob = handle.read()
            lines.append(" %s %d %s" % (algorithm(blob).hexdigest(), len(blob), name))

    write(os.path.join(REPO, "Release"), ("\n".join(lines) + "\n").encode("utf-8"))
    print("wrote Release with index hashes")


def gpg(*args):
    subprocess.run(["gpg", "--batch", "--yes", "--local-user", SIGNING_KEY] + list(args),
                   cwd=REPO, check=True)


def sign_release():
    release = os.path.join(REPO, "Release")
    gpg("--clearsign", "-o", "InRelease", release)
    gpg("--detach-sign", "--armor", "-o", "Release.gpg", release)
    subprocess.run(["gpg", "--batch", "--yes", "--export", "-o", PUBLIC_KEY, SIGNING_KEY],
                   cwd=REPO, check=True)
    print("signed: InRelease, Release.gpg, public key in %s" % PUBLIC_KEY)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-sign", action="store_true", help="skip signing")
    options = parser.parse_args()

    build_packages()
    build_release()
    if options.no_sign:
        print("skipped signing")
        return
    try:
        sign_release()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        sys.exit("signing failed: %s" % error)


if __name__ == "__main__":
    main()
