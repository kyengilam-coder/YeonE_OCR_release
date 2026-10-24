"""Extract app-local CRT DLLs from a pinned, Microsoft-signed redistributable.

Does not install the redistributable or change system runtime files. Uses Windows
expand.exe for CAB extraction after validating the complete signed payload.
"""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import urllib.request
import xml.etree.ElementTree as ET


def fetch_verified(url, path, expected):
    """Accept only pinned bytes, including after HTTPS redirects or cache repair."""
    path = Path(path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not url.startswith("https://"):
        raise ValueError("Pinned HTTPS CRT download is required")
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        return path
    temporary = path.with_name(path.name + ".download")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "YeonE-OCR-release/1"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
            if not response.geturl().startswith("https://"):
                raise ValueError("Refusing non-HTTPS CRT download redirect")
            shutil.copyfileobj(response, stream)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
            raise ValueError("Official CRT download differs from pinned hash; review lock before updating")
        os.replace(temporary, path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def archived_license(lock, repo):
    """Use reviewed license text, not the changing Microsoft website wrapper.

    The lock records both the official source DOCX hash and the full extracted
    paragraph text hash. No live HTML is accepted as redistribution terms.
    """
    repo = Path(repo).resolve()
    relative = Path(lock["license_archive"])
    path = repo / relative
    if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(repo):
        raise ValueError("Invalid archived CRT license path")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != lock["license_archive_sha256"]:
        raise ValueError("Archived CRT license differs from pinned hash")
    text = content.decode("utf-8")
    plain = " ".join(text.upper().split())
    if (not plain.startswith("MICROSOFT SOFTWARE LICENSE TERMS")
            or "MICROSOFT VISUAL C++" not in plain or "REDISTRIBUTABLE" not in plain
            or "EULA ID: CPP_V14_ENU.1033" not in plain):
        raise ValueError("Archived CRT license is not the reviewed V14 license text")
    return text


def prepare(output, cache, lock):
    output, cache = output.resolve(), cache.resolve()
    if output.exists():
        raise ValueError("Use a new CRT output directory")
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = lock.resolve()
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    license_text = archived_license(lock, lock_path.parent.parent)
    exe = fetch_verified(lock["url"], cache / "vc_redist.x64.exe", lock["sha256"])
    script = ("$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath '" + str(exe).replace("'", "''") + "'; "
              "if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Microsoft Corporation*'){exit 1}")
    subprocess.run([shutil.which("pwsh") or "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode()], check=True)
    data = exe.read_bytes()
    cabinets, position = [], 0
    while True:
        position = data.find(b"MSCF", position)
        if position < 0:
            break
        size = struct.unpack_from("<I", data, position + 8)[0]
        if data[position + 4:position + 8] == b"\0" * 4 and 36 < size <= len(data) - position:
            path = cache / f"container-{len(cabinets)}.cab"
            path.write_bytes(data[position:position + size])
            cabinets.append(path)
        position += 4
    if len(cabinets) != 2:
        raise ValueError("Unexpected signed Burn container layout")
    def expand(cab, folder):
        folder.mkdir(exist_ok=True)
        subprocess.run(["expand.exe", "-F:*", str(cab), str(folder)], check=True, stdout=subprocess.DEVNULL)
    bootstrap, payload, minimum = cache / "bootstrap", cache / "payload", cache / "minimum"
    expand(cabinets[0], bootstrap)
    expand(cabinets[1], payload)
    manifest = ET.parse(bootstrap / "0").getroot()
    entry = next(x for x in manifest if x.tag.endswith("Payload") and
                 x.attrib.get("FilePath") == "packages\\vcRuntimeMinimum_amd64\\cab1.cab")
    name = entry.attrib["SourcePath"]
    if Path(name).name != name:
        raise ValueError("Invalid Microsoft CAB entry")
    expand(payload / name, minimum)
    output.mkdir(parents=True)
    for src in minimum.glob("*.dll_amd64"):
        shutil.copy2(src, output / src.name.removesuffix("_amd64"))
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        if not (output / name).is_file():
            raise ValueError("Required CRT DLL missing")
    license_html = ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    "<title>Microsoft Visual C++ V14 Runtime License</title><pre>"
                    + html.escape(license_text) + "</pre></html>\n")
    (output / "MSVC-LICENSE.html").write_text(license_html, encoding="utf-8", newline="\n")
    provenance = {**lock, "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in output.iterdir() if p.is_file()}}
    (output / "msvc-provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "version": lock["version"], "dlls": len(list(output.glob("*.dll")))}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "packaging/msvc-runtime-lock.json")
    args = parser.parse_args()
    prepare(args.output, args.cache, args.lock)
