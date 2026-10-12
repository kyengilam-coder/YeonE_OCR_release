"""클립보드 이미지를 해상도 손실 없이 보관하고 같은 원본의 작업 기록을 재사용한다."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import tempfile

from PIL import Image, ImageOps

from . import runtime


def read_clipboard():
    """Pillow의 이미지/파일 목록/빈 클립보드 결과를 그대로 반환한다."""
    from PIL import ImageGrab
    return ImageGrab.grabclipboard()


def _normalized(image: Image.Image) -> Image.Image:
    if not isinstance(image, Image.Image):
        raise TypeError("스크린샷은 Pillow 이미지여야 합니다.")
    if image.width < 1 or image.height < 1:
        raise ValueError("스크린샷 이미지가 비어 있습니다.")
    # exif_transpose의 기본 동작은 새 이미지 반환이다. 호출자가 소유한 이미지는
    # 회전하거나 닫지 않으며, 투명 팔레트도 RGBA 변환으로 흰 바탕에 합성한다.
    with ImageOps.exif_transpose(image) as oriented:
        with oriented.convert("RGBA") as rgba:
            result = Image.new("RGB", rgba.size, "white")
            with rgba.getchannel("A") as alpha:
                result.paste(rgba, mask=alpha)
    return result


def _fingerprint(image: Image.Image) -> str:
    digest = hashlib.sha256(b"YeonE_OCR screenshot RGB v1\0")
    digest.update(struct.pack(">QQ", *image.size))
    digest.update(image.tobytes())
    return digest.hexdigest()


def _same_original(path: Path, encoded: bytes, identity: str) -> bool:
    """파일 전체와 해독한 픽셀을 모두 확인해 변조된 원본을 재사용하지 않는다."""
    try:
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode) or entry.st_size != len(encoded):
            return False
        data = path.read_bytes()
        if data != encoded:
            return False
        with Image.open(io.BytesIO(data)) as candidate:
            if candidate.format != "PNG" or candidate.mode != "RGB":
                return False
            candidate.load()
            return _fingerprint(candidate) == identity
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def _publish(temporary: Path, destination: Path) -> None:
    """완성한 파일을 원자적으로 공개하며 기존 파일은 절대 교체하지 않는다."""
    if os.name == "nt":
        # Windows rename은 대상이 있으면 실패한다. os.replace는 사용하지 않는다.
        os.rename(temporary, destination)
    else:
        # POSIX rename은 기존 대상을 덮어쓰므로 동일 파일시스템의 hard link 사용.
        os.link(temporary, destination)


def save_screenshot(image: Image.Image, directory: Path | None = None) -> Path:
    """방향/투명도를 정규화한 PNG 경로. 같은 픽셀은 같은 원본·작업 경로를 쓴다.

    같은 이름의 파일이 손상 또는 변경된 경우 그 파일을 보존하고 번호를 붙인
    별도 원본을 만든다. 새 파일은 저장이 모두 끝난 뒤에만 최종 경로에 나타난다.
    """
    target = Path(directory) if directory is not None else runtime.user_data_dir() / "screenshots"
    with _normalized(image) as normalized:
        identity = _fingerprint(normalized)
        buffer = io.BytesIO()
        normalized.save(buffer, format="PNG", compress_level=6, optimize=False)
        encoded = buffer.getvalue()
    target.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        number = 1
        while True:
            suffix = "" if number == 1 else f"_{number}"
            destination = target / f"screenshot-{identity}{suffix}.png"
            if _same_original(destination, encoded, identity):
                return destination
            try:
                destination.lstat()
            except FileNotFoundError:
                pass
            else:
                number += 1
                continue
            if temporary is None:
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".screenshot-", suffix=".tmp",
                                                 dir=target, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            try:
                _publish(temporary, destination)
            except FileExistsError:
                # 다른 프로세스가 먼저 저장했으면 그 파일을 검사한 뒤 재사용한다.
                if _same_original(destination, encoded, identity):
                    return destination
                number += 1
            else:
                return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
