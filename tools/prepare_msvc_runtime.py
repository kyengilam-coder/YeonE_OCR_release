"""Extract app-local CRT DLLs from a pinned, Microsoft-signed redistributable.

Does not install the redistributable or change system runtime files. Uses Windows
expand.exe for CAB extraction after validating the complete signed payload.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import urllib.request
import xml.etree.ElementTree as ET


def prepare(output, cache, lock):
    output, cache = output.resolve(), cache.resolve()
    if output.exists():
        raise ValueError("Use a new CRT output directory")
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads(lock.read_text(encoding="utf-8"))
    def fetch(url, path, expected):
        if not path.is_file():
            with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Official CRT download differs from pinned hash; review lock before updating")
        return path
    exe = fetch(lock["url"], cache / "vc_redist.x64.exe", lock["sha256"])
    license_path = fetch(lock["license_url"], cache / "MSVC-LICENSE.html", lock["license_sha256"])
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
    shutil.copy2(license_path, output / "MSVC-LICENSE.html")
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
