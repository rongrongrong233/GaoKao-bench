from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from gaokao_bench.io import write_json


CALIBRE_PDFTOTEXT = Path("/Applications/calibre.app/Contents/utils.app/Contents/MacOS/pdftotext")


PDF_SYMBOL_TRANSLATION = str.maketrans(
    {
        "\uf028": "(",
        "\uf029": ")",
        "\uf02b": "+",
        "\uf02d": "-",
        "\uf02f": "/",
        "\uf03c": "<",
        "\uf03d": "=",
        "\uf03e": ">",
        "\uf049": "∩",
        "\uf04c": "…",
        "\uf056": "△",
        "\uf05b": "[",
        "\uf05d": "]",
        "\uf05e": "⊥",
        "\uf061": "α",
        "\uf063": "χ",
        "\uf06c": "λ",
        "\uf070": "π",
        "\uf071": "θ",
        "\uf07b": "{",
        "\uf07c": "|",
        "\uf07d": "}",
        "\uf07e": "∼",
        "\uf0a2": "'",
        "\uf0a3": "≤",
        "\uf0b0": "°",
        "\uf0b1": "±",
        "\uf0b3": "≥",
        "\uf0b4": "×",
        "\uf0b9": "≠",
        "\uf0bb": "≈",
        "\uf0c6": "∅",
        "\uf0c7": "∩",
        "\uf0cc": "⊂",
        "\uf0ce": "∈",
        "\uf0d0": "∠",
        "\uf0d7": "·",
        "\uf0db": "⇔",
        "\uf0de": "⇒",
        "\uf0e5": "∑",
        "\uf0e6": "",
        "\uf0e7": "",
        "\uf0e8": "",
        "\uf0e9": "[",
        "\uf0ea": "[",
        "\uf0eb": "]",
        "\uf0ec": "{",
        "\uf0ed": "|",
        "\uf0ee": "}",
        "\uf0ef": "",
        "\uf0f6": "",
        "\uf0f7": "",
        "\uf0f8": "",
        "\uf0f9": "[",
        "\uf0fa": "[",
        "\uf0fb": "]",
    }
)


def find_pdftotext() -> Path:
    env_path = os.environ.get("PDFTOTEXT_BIN")
    candidates = [
        Path(env_path) if env_path else None,
        Path(found) if (found := shutil.which("pdftotext")) else None,
        CALIBRE_PDFTOTEXT,
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise RuntimeError("pdftotext not found; set PDFTOTEXT_BIN or install poppler/calibre.")


def clean_pdf_text(text: str, *, preserve_layout: bool = False) -> str:
    text = text.translate(PDF_SYMBOL_TRANSLATION)
    text = text.replace("ð", "∁").replace("@", "≌")
    text = re.sub(r"[\ue000-\uf8ff]", "", text)
    text = re.sub(
        r"第\s*\d+\s*页\s*/\s*共\s*\d+\s*页\s*学科\s*网\s*（\s*北\s*京\s*）\s*股\s*份\s*有限\s*公司",
        " ",
        text,
    )
    text = re.sub(r"第\s*\d+\s*页\s*/\s*共\s*\d+\s*页", " ", text)
    text = re.sub(r"学科\s*网\s*（\s*北\s*京\s*）\s*股\s*份\s*有限\s*公司", " ", text)
    if preserve_layout:
        lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
        text = "\n".join(lines)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_pdf_pages(pdf_path: Path, *, layout: bool = True) -> list[dict[str, Any]]:
    command = [find_pdftotext().as_posix()]
    if layout:
        command.append("-layout")
    command.extend([pdf_path.as_posix(), "-"])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    raw_pages = completed.stdout.split("\f")
    if raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()
    return [
        {"page": index, "text": clean_pdf_text(text, preserve_layout=True)}
        for index, text in enumerate(raw_pages, start=1)
    ]


def extract_pdf_text(pdf_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pages = extract_pdf_pages(pdf_path)
    payload = {
        "source_pdf": pdf_path.as_posix(),
        "page_count": len(pages),
        "pages": pages,
        "method": "pdftotext_layout_v1",
    }
    write_json(output_path, payload)
    return payload

