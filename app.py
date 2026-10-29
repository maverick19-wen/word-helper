"""Word Helper: ECDICT lookups, WordNet derivations, and LLM examples."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import nltk
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from nltk.corpus import wordnet as wn
from openai import AsyncOpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DICT_DB = DATA_DIR / "ecdict.sqlite3"
CACHE_DB = Path(os.getenv("CACHE_DB_PATH", str(DATA_DIR / "cache.sqlite3")))
NLTK_DATA_DIR = DATA_DIR / "nltk_data"
INDEX_HTML = BASE_DIR / "static" / "index.html"
ACCESS_USERNAME = os.getenv("ACCESS_USERNAME", "wordhelper")
ACCESS_PASSWORD = os.getenv("ACCESS_PASSWORD", "")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("word_helper")

WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")
POS_PREFIX_RE = re.compile(r"^([A-Za-z]{1,6})\.")
SIMPLE_WORD_RE = re.compile(r"[a-z]+(?:['-][a-z]+)*")

POS_NAMES = {
    "n": "名词",
    "v": "动词",
    "vt": "及物动词",
    "vi": "不及物动词",
    "a": "形容词",
    "adj": "形容词",
    "ad": "副词",
    "adv": "副词",
    "prep": "介词",
    "pron": "代词",
    "conj": "连词",
    "num": "数词",
    "art": "冠词",
    "aux": "助动词",
    "int": "感叹词",
    "interj": "感叹词",
    "abbr": "缩写",
}
EXCHANGE_LABELS = {
    "p": "过去式",
    "d": "过去分词",
    "i": "现在分词",
    "3": "第三人称单数",
    "r": "比较级",
    "t": "最高级",
    "s": "复数",
}
FORM_ORDER = ["p", "d", "i", "3", "r", "t", "s"]
WORDNET_POS = {
    "n": ("n.", "名词"),
    "v": ("v.", "动词"),
    "a": ("adj.", "形容词"),
    "s": ("adj.", "形容词"),
    "r": ("adv.", "副词"),
}
WORDNET_POS_ORDER = {"n": 0, "v": 1, "a": 2, "s": 2, "r": 3}

_wordnet_ready = False
_cache_locks: dict[str, asyncio.Lock] = {}


class ExampleGenerationError(Exception):
    """A user-displayable failure while creating an example sentence."""


def _connect_dictionary() -> sqlite3.Connection:
    if not DICT_DB.is_file():
        raise HTTPException(
            status_code=503,
            detail="词典数据库不存在，请先按 README 下载 ECDICT 并运行转换脚本。",
        )
    connection = sqlite3.connect(DICT_DB, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _init_cache_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(CACHE_DB, timeout=10)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS word_cache (
                word       TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        connection.commit()


def _read_cache(word: str) -> dict[str, Any] | None:
    try:
        with closing(sqlite3.connect(CACHE_DB, timeout=10)) as connection:
            row = connection.execute(
                "SELECT payload FROM word_cache WHERE word = ?", (word,)
            ).fetchone()
    except sqlite3.Error:
        logger.exception("Could not read the query cache for %s", word)
        return None
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid cache entry for %s", word)
        return None


def _write_cache(word: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    with closing(sqlite3.connect(CACHE_DB, timeout=10)) as connection:
        connection.execute(
            """
            INSERT INTO word_cache (word, payload) VALUES (?, ?)
            ON CONFLICT(word) DO UPDATE SET
                payload = excluded.payload,
                created_at = datetime('now')
            """,
            (word, data),
        )
        connection.commit()


def _init_wordnet() -> None:
    global _wordnet_ready
    if NLTK_DATA_DIR.is_dir():
        nltk.data.path.insert(0, str(NLTK_DATA_DIR))
    try:
        wn.ensure_loaded()
        _wordnet_ready = True
        logger.info("WordNet loaded")
    except LookupError:
        _wordnet_ready = False
        logger.warning(
            "WordNet data not found. Install it with: "
            "python -m nltk.downloader -d data/nltk_data -x wordnet"
        )


def _split_definitions(translation: str) -> list[str]:
    # Some ECDICT rows contain literal backslash-n sequences, while others
    # contain real line breaks. Handle both without changing the definitions.
    normalized = (
        translation.replace(r"\r\n", "\n")
        .replace(r"\n", "\n")
        .replace(r"\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def _extract_pos(definitions: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in definitions:
        match = POS_PREFIX_RE.match(line)
        if not match:
            continue
        code = match.group(1).lower()
        if code in seen or code not in POS_NAMES:
            continue
        seen.add(code)
        result.append({"code": code, "label": POS_NAMES[code]})
    return result


def _parse_forms(exchange: str) -> tuple[list[dict[str, Any]], set[str]]:
    grouped: dict[str, list[str]] = {}
    for item in exchange.split("/"):
        code, separator, raw_value = item.partition(":")
        if not separator:
            continue
        code = code.strip()
        value = raw_value.strip()
        if code not in EXCHANGE_LABELS or not value:
            continue
        forms = grouped.setdefault(code, [])
        if value not in forms:
            forms.append(value)

    result: list[dict[str, Any]] = []
    all_forms: set[str] = set()
    for code in FORM_ORDER:
        values = grouped.get(code)
        if not values:
            continue
        all_forms.update(value.lower() for value in values)
        result.append(
            {"code": code, "label": EXCHANGE_LABELS[code], "words": values}
        )
    return result, all_forms


def _adverb_candidates(word: str) -> list[str]:
    candidates = {word + "ly"}
    if word.endswith("y") and len(word) > 2:
        candidates.add(word[:-1] + "ily")
    if word.endswith("le"):
        candidates.add(word[:-1] + "y")
    if word.endswith("ic"):
        candidates.add(word + "ally")
    if word.endswith("e"):
        candidates.add(word[:-1] + "ly")
    if word.endswith("ll"):
        candidates.add(word + "y")
    return sorted(candidates)


def _has_pertainym(candidate: str, base: str) -> bool:
    for synset in wn.synsets(candidate):
        for lemma in synset.lemmas():
            if lemma.name().replace("_", " ").lower() != candidate:
                continue
            for related in lemma.pertainyms():
                if related.name().replace("_", " ").lower() == base:
                    return True
    return False


def _wordnet_related_candidates(word: str) -> dict[str, set[str]]:
    """Return candidate related words mapped to their WordNet POS tags."""
    candidates: dict[str, set[str]] = {}

    def add(candidate: str, pos: str) -> None:
        candidate = candidate.strip().lower()
        if (
            not SIMPLE_WORD_RE.fullmatch(candidate)
            or candidate == word
            or pos not in WORDNET_POS
        ):
            return
        candidates.setdefault(candidate, set()).add(pos)

    for synset in wn.synsets(word):
        for lemma in synset.lemmas():
            if lemma.name().replace("_", " ").lower() != word:
                continue
            for related in lemma.derivationally_related_forms():
                raw_name = related.name()
                if "_" in raw_name:
                    continue
                add(raw_name, related.synset().pos())

    # WordNet records many adverb-to-adjective links as "pertainyms" on the
    # adverb, so they are not returned by derivationally_related_forms().
    for candidate in _adverb_candidates(word):
        if not SIMPLE_WORD_RE.fullmatch(candidate):
            continue
        if _has_pertainym(candidate, word):
            add(candidate, "r")

    return candidates


def _related_words(
    base_word: str, excluded: set[str], limit: int = 10
) -> list[dict[str, str]]:
    if not _wordnet_ready:
        return []

    try:
        candidates = _wordnet_related_candidates(base_word)
    except Exception:
        logger.exception("WordNet lookup failed for %s", base_word)
        return []

    candidate_words = [word for word in candidates if word not in excluded]
    if not candidate_words:
        return []

    placeholders = ",".join("?" for _ in candidate_words)
    with closing(_connect_dictionary()) as connection:
        rows = connection.execute(
            f"SELECT word_lower, word FROM dictionary "
            f"WHERE word_lower IN ({placeholders})",
            candidate_words,
        ).fetchall()
    existing = {row["word_lower"]: row["word"] for row in rows}

    selected = [
        (word, pos_tags)
        for word, pos_tags in candidates.items()
        if word in existing and word not in excluded
    ]
    selected.sort(
        key=lambda item: (
            min(WORDNET_POS_ORDER[pos] for pos in item[1]),
            item[0],
        )
    )

    result: list[dict[str, str]] = []
    for word, pos_tags in selected[:limit]:
        labels: list[str] = []
        for pos in sorted(pos_tags, key=WORDNET_POS_ORDER.__getitem__):
            short, label = WORDNET_POS[pos]
            text = f"{short} {label}"
            if text not in labels:
                labels.append(text)
        result.append({"word": existing[word], "pos": " / ".join(labels)})
    return result


def _normalize_query(q: str) -> str:
    raw = q.strip().replace("\u2019", "'")
    if not raw:
        raise HTTPException(status_code=400, detail="请输入英文单词。")
    if len(raw) > 64:
        raise HTTPException(status_code=400, detail="单词太长，请重新输入。")
    if not WORD_RE.fullmatch(raw):
        raise HTTPException(
            status_code=400,
            detail="请输入单个英文单词（可包含连字符或撇号）。",
        )
    return raw.lower()


def _lookup_word(word: str) -> sqlite3.Row:
    with closing(_connect_dictionary()) as connection:
        row = connection.execute(
            """
            SELECT word, phonetic, translation, exchange
            FROM dictionary
            WHERE word_lower = ?
            """,
            (word,),
        ).fetchone()
    if row is None or not row["translation"].strip():
        raise HTTPException(status_code=404, detail="未找到该单词。")
    return row


def _contains_word(sentence: str, word: str) -> bool:
    pattern = r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])"
    return re.search(pattern, sentence, flags=re.IGNORECASE) is not None


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines[1:])
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("No JSON object in response")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Response JSON is not an object")
    return data


def _validate_example(data: dict[str, Any], word: str) -> dict[str, str]:
    sentence = data.get("example")
    translation = data.get("translation")
    if not isinstance(sentence, str) or not isinstance(translation, str):
        raise ValueError("Missing example or translation")
    sentence = re.sub(r"\s+", " ", sentence).strip()
    translation = re.sub(r"\s+", " ", translation).strip()
    if not sentence or len(sentence) > 300:
        raise ValueError("Empty or overly long example")
    if not _contains_word(sentence, word):
        raise ValueError("Example does not contain the requested word")
    if not re.search(r"[\u4e00-\u9fff]", translation):
        raise ValueError("Translation is not Chinese")
    return {"en": sentence, "zh": translation}


async def _generate_example(word: str, pos: list[dict[str, str]]) -> dict[str, str]:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise ExampleGenerationError(
            "未配置例句生成服务，请在 .env 中设置 LLM_API_KEY 后重试。"
        )

    base_url = os.getenv("LLM_BASE_URL", "").strip() or None
    model = os.getenv("LLM_MODEL", "").strip() or "gpt-4o-mini"
    pos_text = ", ".join(f"{item['code']}. {item['label']}" for item in pos)
    if not pos_text:
        pos_text = "not specified"

    system_prompt = (
        "You write short, natural English example sentences and their Chinese "
        "translations for a learner's dictionary. Return only a JSON object."
    )
    user_prompt = (
        f'English word: "{word}"\n'
        f"Part of speech: {pos_text}\n\n"
        "Write ONE short, natural, intermediate-level English sentence that "
        f'contains the exact word "{word}" (not another form of it). Then give '
        "an accurate, natural Simplified Chinese translation.\n"
        'Return exactly: {"example": "...", "translation": "..."}'
    )

    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=30.0, max_retries=1
    )
    last_validation_error: Exception | None = None
    try:
        for _ in range(2):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.6,
                )
            except Exception:
                logger.exception("LLM request failed for %s", word)
                raise ExampleGenerationError(
                    "例句生成服务暂时不可用，请稍后重试。"
                ) from None

            try:
                content = response.choices[0].message.content or ""
            except (IndexError, AttributeError) as exc:
                last_validation_error = exc
                logger.warning("LLM returned no usable choice for %s", word)
                continue

            try:
                return _validate_example(_extract_json_object(content), word)
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                last_validation_error = exc
                logger.warning("Rejected LLM example for %s: %s", word, exc)

        raise ExampleGenerationError(
            "未能生成符合要求的例句，请稍后重试。"
        ) from last_validation_error
    finally:
        await client.close()


def _get_word_lock(word: str) -> asyncio.Lock:
    lock = _cache_locks.get(word)
    if lock is None:
        lock = asyncio.Lock()
        _cache_locks[word] = lock
    return lock


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _init_cache_db()
    _init_wordnet()
    if not DICT_DB.is_file():
        logger.warning("ECDICT database not found at %s", DICT_DB)
    yield


app = FastAPI(
    title="Word Helper",
    description="本地 ECDICT 词典 + WordNet 派生词 + LLM 例句",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def optional_access_guard(request: Request, call_next):
    """Protect public deployments with one shared HTTP Basic credential."""
    if not ACCESS_PASSWORD or request.url.path == "/health":
        return await call_next(request)

    unauthorized = Response(
        content="需要访问口令。",
        status_code=401,
        media_type="text/plain; charset=utf-8",
        headers={"WWW-Authenticate": 'Basic realm="Word Helper", charset="UTF-8"'},
    )
    authorization = request.headers.get("Authorization", "")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return unauthorized
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return unauthorized
    username, separator, password = decoded.partition(":")
    if (
        separator
        and secrets.compare_digest(username, ACCESS_USERNAME)
        and secrets.compare_digest(password, ACCESS_PASSWORD)
    ):
        return await call_next(request)
    return unauthorized


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(INDEX_HTML)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    if not DICT_DB.is_file():
        raise HTTPException(status_code=503, detail="ECDICT 数据库不可用")
    return {"status": "ok", "dictionary": True, "wordnet": _wordnet_ready}


@app.get("/api/word")
async def query_word(q: str = Query(..., description="要查询的英文单词")) -> dict[str, Any]:
    normalized = _normalize_query(q)
    async with _get_word_lock(normalized):
        cached = _read_cache(normalized)
        if cached is not None:
            cached["cached"] = True
            return cached

        entry = _lookup_word(normalized)
        definitions = _split_definitions(entry["translation"])
        forms, form_words = _parse_forms(entry["exchange"])
        pos = _extract_pos(definitions)
        related = _related_words(
            normalized, {normalized, *form_words}
        )

        result: dict[str, Any] = {
            "word": entry["word"],
            "phonetic": entry["phonetic"],
            "definitions": definitions,
            "pos": pos,
            "forms": forms,
            "related": related,
            "example": None,
            "example_error": None,
            "cached": False,
        }

        try:
            result["example"] = await _generate_example(entry["word"], pos)
            try:
                _write_cache(normalized, result)
            except sqlite3.Error:
                logger.exception("Could not write the query cache for %s", normalized)
        except ExampleGenerationError as exc:
            result["example_error"] = str(exc)
        except Exception:
            logger.exception("Unexpected example generation error for %s", normalized)
            result["example_error"] = "例句生成失败，请稍后重试。"

        return result