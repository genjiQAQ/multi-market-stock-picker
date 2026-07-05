"""PNG chart generation for picker outputs."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from .models import StockRecord


CJK_FONT_PATHS = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


def generate_charts(records: list[StockRecord], output_dir: Path, top_n: int) -> dict[str, str]:
    scored = [record for record in records if record.is_tradeable and "final_score" in record.scores]
    if not scored:
        return {}
    try:
        import matplotlib.pyplot as plt  # type: ignore
    except ImportError:
        return generate_basic_png_charts(scored, output_dir, top_n)
    configure_matplotlib_cjk(plt)

    scores = [record.scores["final_score"] for record in scored]
    score_path = output_dir / "score_distribution.png"
    plt.figure(figsize=(8, 5))
    plt.hist(scores, bins=min(12, max(3, len(scores))), color="#2563eb", alpha=0.8)
    plt.title("Score Distribution")
    plt.xlabel("Final Score")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(score_path, dpi=150)
    plt.close()

    top = sorted(scored, key=lambda item: item.rank_global or 10**9)[:top_n]
    top_path = output_dir / "top_candidates.png"
    labels = [record.name or record.yahoo_symbol for record in top]
    values = [record.scores["final_score"] for record in top]
    plt.figure(figsize=(10, max(5, len(top) * 0.45)))
    plt.barh(list(reversed(labels)), list(reversed(values)), color="#059669")
    plt.title("Top Research Candidates")
    plt.xlabel("Final Score")
    plt.xlim(0, 100)
    plt.tight_layout()
    plt.savefig(top_path, dpi=150)
    plt.close()

    return {
        "score_distribution": str(score_path),
        "top_candidates": str(top_path),
    }


def configure_matplotlib_cjk(plt) -> None:
    try:
        from matplotlib import font_manager  # type: ignore
    except Exception:
        return
    for font_path in CJK_FONT_PATHS:
        path = Path(font_path)
        if not path.exists():
            continue
        try:
            font_manager.fontManager.addfont(str(path))
            font_name = font_manager.FontProperties(fname=str(path)).get_name()
        except Exception:
            continue
        plt.rcParams["font.family"] = [font_name, "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        return


def generate_basic_png_charts(records: list[StockRecord], output_dir: Path, top_n: int) -> dict[str, str]:
    scores = [record.scores["final_score"] for record in records]
    score_path = output_dir / "score_distribution.png"
    top_path = output_dir / "top_candidates.png"
    write_bar_png(score_path, bucket_counts(scores), width=800, height=420, color=(37, 99, 235))
    top = sorted(records, key=lambda item: item.rank_global or 10**9)[:top_n]
    write_bar_png(top_path, [record.scores["final_score"] for record in top], width=900, height=460, color=(5, 150, 105))
    return {
        "score_distribution": str(score_path),
        "top_candidates": str(top_path),
    }


def bucket_counts(values: list[float]) -> list[float]:
    buckets = [0.0] * 10
    for value in values:
        idx = min(9, max(0, int(value // 10)))
        buckets[idx] += 1
    return buckets


def write_bar_png(path: Path, values: list[float], *, width: int, height: int, color: tuple[int, int, int]) -> None:
    image = bytearray([255, 255, 255] * width * height)
    if values:
        max_value = max(values) or 1
        margin = 36
        gap = 6
        bar_width = max(4, (width - margin * 2 - gap * (len(values) - 1)) // max(1, len(values)))
        for idx, value in enumerate(values):
            bar_height = int((height - margin * 2) * (value / max_value))
            x0 = margin + idx * (bar_width + gap)
            x1 = min(width - margin, x0 + bar_width)
            y0 = height - margin - bar_height
            y1 = height - margin
            fill_rect(image, width, height, x0, y0, x1, y1, color)
    write_png(path, width, height, bytes(image))


def fill_rect(
    image: bytearray,
    width: int,
    height: int,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    color: tuple[int, int, int],
) -> None:
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(width, x1)
    y1 = min(height, y1)
    for y in range(y0, y1):
        for x in range(x0, x1):
            offset = (y * width + x) * 3
            image[offset : offset + 3] = bytes(color)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = b"".join(b"\x00" + rgb[row * width * 3 : (row + 1) * width * 3] for row in range(height))
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress(raw, 9))
    png += png_chunk(b"IEND", b"")
    path.write_bytes(png)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
