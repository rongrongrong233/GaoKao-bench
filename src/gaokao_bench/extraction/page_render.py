from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from gaokao_bench.io import write_json


def find_pdftoppm() -> Path:
    found = shutil.which("pdftoppm")
    if found:
        return Path(found)
    raise RuntimeError("pdftoppm not found; install poppler with `brew install poppler`.")


def render_pdf_pages(
    pdf_path: Path,
    output_dir: Path,
    *,
    dpi: int = 160,
    force: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists() and not force:
        manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("dpi", 0)) == int(dpi):
            return manifest

    for old in output_dir.glob("page-*.png"):
        old.unlink()

    prefix = output_dir / "page"
    command = [
        find_pdftoppm().as_posix(),
        "-png",
        "-r",
        str(dpi),
        pdf_path.as_posix(),
        prefix.as_posix(),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)

    pages = []
    for index, path in enumerate(sorted(output_dir.glob("page-*.png")), start=1):
        normalized = output_dir / f"page-{index:03d}.png"
        if path != normalized:
            path.rename(normalized)
        pages.append({"page": index, "image_path": normalized.as_posix(), "dpi": dpi})

    manifest = {
        "source_pdf": pdf_path.as_posix(),
        "page_count": len(pages),
        "dpi": dpi,
        "pages": pages,
        "method": "pdftoppm_png_v1",
    }
    write_json(manifest_path, manifest)
    return manifest


def page_image_map(manifest: dict[str, Any]) -> dict[int, Path]:
    return {int(row["page"]): Path(row["image_path"]) for row in manifest.get("pages", [])}
