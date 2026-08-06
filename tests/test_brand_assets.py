"""The brand assets are a published byte contract, not incidental files.

HACS validation fails its `brands` check unless the repository ships
`custom_components/<domain>/brand/icon.png`, and Home Assistant's Brands Proxy
API serves the pair as a square 256x256 icon plus its 512x512 @2x variant
(`custom_components/avisoscat/brand/README.md`). Both consumers run outside this
repository, so these tests parse the PNG headers and assert that contract.
"""

import struct
from pathlib import Path

import pytest

BRAND_DIR = Path(__file__).resolve().parents[1] / "custom_components/avisoscat/brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> tuple[int, int]:
    """Return `(width, height)` from a PNG's IHDR chunk.

    Kept dependency-free on purpose: the integration itself has no runtime
    requirements and the dev environment carries no imaging library.
    """
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    assert header[12:16] == b"IHDR", f"{path.name} does not start with an IHDR chunk"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize(
    ("filename", "expected_size"),
    [("icon.png", 256), ("icon@2x.png", 512)],
)
def test_brand_icon_is_a_square_png_of_the_expected_size(
    filename: str, expected_size: int
) -> None:
    """Each brand icon exists and is a square PNG at its documented size."""
    path = BRAND_DIR / filename
    assert path.is_file(), f"{filename} is missing: HACS brands validation fails"

    width, height = png_size(path)
    assert (width, height) == (expected_size, expected_size)
