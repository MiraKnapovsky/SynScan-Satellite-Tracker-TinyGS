#!/usr/bin/env python3
"""Download TinyGS supported satellites and save them as local TLE file."""

import os
import tempfile
from pathlib import Path
from typing import List, Tuple

import requests

TLE_URL = "https://api.tinygs.com/v1/tinygs_supported.txt"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "satellites.tle"


def parse_tle_blocks(raw_text: str) -> Tuple[List[str], int, int]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    output_lines: List[str] = []
    kept = 0
    skipped = 0

    # Očekáváme bloky po 3 řádcích: NAME, "1 ...", "2 ..."
    for idx in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[idx], lines[idx + 1], lines[idx + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            output_lines.extend([name, l1, l2])
            kept += 1
        else:
            skipped += 1

    return output_lines, kept, skipped


def atomic_write_text(path: Path, content: str) -> None:
    out_dir = path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=out_dir, encoding="utf-8") as tf:
        tf.write(content)
        tmp_name = tf.name
    os.replace(tmp_name, path)


def fetch_and_save_all() -> None:
    try:
        response = requests.get(TLE_URL, timeout=30)
        response.raise_for_status()
        lines, kept, skipped = parse_tle_blocks(response.text)
        output_text = "\n".join(lines) + ("\n" if lines else "")
        atomic_write_text(OUTPUT_FILE, output_text)
        print(
            f"Aktualizováno: {kept} satelitů uloženo do {OUTPUT_FILE} "
            f"(přeskočeno bloků: {skipped})"
        )
    except requests.RequestException as exc:
        print(f"Chyba při stahování TLE: {exc}")
    except OSError as exc:
        print(f"Chyba při ukládání TLE: {exc}")


if __name__ == "__main__":
    fetch_and_save_all()
