#!/usr/bin/env python3
"""Convert the ECDICT CSV data into the SQLite database used by Word Helper.

ECDICT CSV download: https://github.com/skywind3000/ECDICT
Only the fields needed by the application are stored.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

REQUIRED_COLUMNS = {"word", "phonetic", "translation", "exchange"}
BATCH_SIZE = 5_000

SCHEMA = """
CREATE TABLE dictionary (
    word_lower  TEXT PRIMARY KEY,
    word        TEXT NOT NULL,
    phonetic    TEXT NOT NULL DEFAULT '',
    translation TEXT NOT NULL,
    exchange    TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 ECDICT 的 ecdict.csv 转换为 Word Helper 使用的 SQLite 词典。"
    )
    parser.add_argument("csv_file", type=Path, help="ECDICT CSV 文件，例如 data/ecdict.csv")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("data/ecdict.sqlite3"),
        help="输出 SQLite 文件（默认: data/ecdict.sqlite3）",
    )
    return parser.parse_args()


def convert(csv_file: Path, output: Path) -> int:
    if not csv_file.is_file():
        raise SystemExit(f"找不到 CSV 文件: {csv_file}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output.with_name(output.name + ".tmp")
    temp_output.unlink(missing_ok=True)

    started = time.monotonic()
    connection = sqlite3.connect(temp_output)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            """
        )
        connection.execute(SCHEMA)

        inserted = 0
        skipped = 0
        batch: list[tuple[str, str, str, str, str]] = []

        with csv_file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
            if missing:
                raise SystemExit(
                    "CSV 缺少必需字段: " + ", ".join(sorted(missing))
                )

            for row in reader:
                word = (row.get("word") or "").strip()
                translation = (row.get("translation") or "").strip()
                if not word or not translation:
                    skipped += 1
                    continue

                batch.append(
                    (
                        word.lower(),
                        word,
                        (row.get("phonetic") or "").strip(),
                        translation,
                        (row.get("exchange") or "").strip(),
                    )
                )
                if len(batch) >= BATCH_SIZE:
                    connection.executemany(
                        "INSERT OR IGNORE INTO dictionary "
                        "(word_lower, word, phonetic, translation, exchange) "
                        "VALUES (?, ?, ?, ?, ?)",
                        batch,
                    )
                    inserted += len(batch)
                    batch.clear()
                    print(f"已处理 {inserted:,} 条...", flush=True)

            if batch:
                connection.executemany(
                    "INSERT OR IGNORE INTO dictionary "
                    "(word_lower, word, phonetic, translation, exchange) "
                    "VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
                inserted += len(batch)

        connection.commit()
        total = connection.execute("SELECT COUNT(*) FROM dictionary").fetchone()[0]
        skipped += inserted - total
        connection.close()
        os.replace(temp_output, output)
    except BaseException:
        connection.close()
        temp_output.unlink(missing_ok=True)
        raise

    elapsed = time.monotonic() - started
    print(
        f"完成: {total:,} 条词条写入 {output} "
        f"(跳过 {skipped:,} 条空词条/小写重复词条，{elapsed:.1f} 秒)"
    )
    return 0


def main() -> int:
    args = parse_args()
    return convert(args.csv_file, args.output)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        sys.exit(130)
