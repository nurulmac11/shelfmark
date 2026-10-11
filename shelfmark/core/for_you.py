""" "For You" recommendations built from the books already in the library folder.

Seeds are the ebooks found under the download destination (EPUB metadata when
available, otherwise the "Author - Title" folder/file name). Each seed is
resolved on Open Library, then three kinds of rows are built:

* "More from <author>" - popular books by authors you already have, via the
  configured metadata provider (so cards behave like normal search results).
* "Because you have books on <subject>" - Open Library subjects shared by your
  books, ranked by how many of your books carry them.
* "Trending on Hardcover" - when the Hardcover provider is enabled.

Books you already have are filtered out. Results are cached and rebuilt when
the library folder changes.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import threading
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from shelfmark.core.cache import get_metadata_cache
from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataSearchOptions,
    SortOrder,
    get_configured_provider,
    get_provider,
    get_provider_kwargs,
    is_provider_enabled,
)

logger = setup_logger(__name__)

EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".djvu", ".fb2", ".cbz", ".cbr"}
MAX_SEED_FILES = 200
MAX_AUTHOR_ROWS = 6
MIN_BOOKS_PER_ROW = 5
MAX_SUBJECT_ROWS = 3
BOOKS_PER_ROW = 18
CACHE_TTL_SECONDS = 6 * 60 * 60
OPENLIBRARY_BASE_URL = "https://openlibrary.org"
_OL_SEARCH_FIELDS = (
    "key,title,author_name,first_publish_year,cover_i,isbn,publisher,language,subject,"
    "ratings_average,ratings_count"
)

# Open Library subjects that say nothing about a book's topic.
_GENERIC_SUBJECTS = {
    "accessible_book",
    "protected_daisy",
    "in_library",
    "lending_library",
    "open_library_staff_picks",
    "fiction",
    "nonfiction",
    "history",
    "general",
    "textbooks",
    "juvenile_literature",
    "large_type_books",
    "english_language",
    "translations_into_english",
    "biography",
    "criticism_and_interpretation",
    "study_and_teaching",
    "reference",
    "readers",
    "classics",
    "literature",
    "adaptations",
    "long_now_manual_for_civilization",
    "authors_and_publishers",
    "dramatic_works_by_one_author",
    "fictional_works_by_one_author",
    "fiction_general",
    "philosophy",
    "science",
    "psychology",
    "open_syllabus_project",
    "new_york_times_reviewed",
    "popular_print_disabled_books",
    "staff_picks",
}
_SUBJECT_NOISE = re.compile(
    r"(\(|_\d{4}|^\d+$|nyt:|award|language_materials|_fiction$|in_fiction$)"
)

_build_lock = threading.Lock()


@dataclass
class Seed:
    title: str
    author: str | None
    work_id: str | None = None
    author_name: str | None = None
    subjects: list[str] = field(default_factory=list)


def _normalize(text: str | None) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    text = re.split(r"\s[:_]\s|:\s", text)[0]
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _author_key(name: str | None) -> str:
    name = (name or "").strip()
    if "," in name:
        last, first = name.split(",", 1)
        name = f"{first} {last}"
    name = re.sub(r"\b[a-z]\.\s*", " ", name.lower())  # drop initials like "T."
    return re.sub(r"[^a-z]+", " ", name).strip()


def _library_dir() -> Path:
    return Path(str(config.get("DESTINATION", "") or "/books"))


def _library_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in EBOOK_EXTENSIONS
            and not path.name.startswith(".")
        ):
            files.append(path)
            if len(files) >= MAX_SEED_FILES:
                break
    return files


def _epub_metadata(path: Path) -> tuple[str | None, str | None]:
    ns = {
        "c": "urn:oasis:names:tc:opendocument:xmlns:container",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//c:rootfile", ns)
            if rootfile is None:
                return None, None
            opf = ET.fromstring(zf.read(rootfile.attrib["full-path"]))
            title = opf.find(".//dc:title", ns)
            creator = opf.find(".//dc:creator", ns)
            return (
                title.text.strip() if title is not None and title.text else None,
                creator.text.strip() if creator is not None and creator.text else None,
            )
    except OSError, KeyError, zipfile.BadZipFile, ET.ParseError:
        return None, None


def _name_metadata(path: Path) -> tuple[str, str | None]:
    """Guess (title, author) from "Author - Title (year)" folder or file names."""
    for name in (path.parent.name, path.stem):
        cleaned = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", name).strip()
        if " - " in cleaned:
            author, title = cleaned.split(" - ", 1)
            if author and title and len(author) < 60:
                return title.strip(), author.strip()
    return re.sub(r"[_\-]+", " ", path.stem).strip(), None


def _read_seeds(files: list[Path]) -> list[Seed]:
    seeds: dict[str, Seed] = {}
    for path in files:
        title = author = None
        if path.suffix.lower() == ".epub":
            title, author = _epub_metadata(path)
        if not title:
            title, guessed_author = _name_metadata(path)
            author = author or guessed_author
        key = _normalize(title)
        if key and key not in seeds:
            seeds[key] = Seed(title=title, author=author)
    return list(seeds.values())


def _openlibrary() -> Any:
    return get_provider("openlibrary", **get_provider_kwargs("openlibrary"))


def _ol_get(provider: Any, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    from shelfmark.download.network import get_ssl_verify
    from shelfmark.metadata_providers.openlibrary import _rate_limiter

    _rate_limiter.wait_if_needed()
    try:
        response = provider.session.get(
            f"{OPENLIBRARY_BASE_URL}{path}",
            params=params,
            timeout=15,
            verify=get_ssl_verify(OPENLIBRARY_BASE_URL),
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - network/JSON errors just skip this lookup
        logger.debug("For You: Open Library request %s failed: %s", path, exc)
        return None


def _resolve_seed(provider: Any, seed: Seed) -> None:
    params: dict[str, Any] = {
        "title": _normalize(seed.title) or seed.title,
        "limit": 1,
        "fields": "key,title,author_name,subject_key",
    }
    if seed.author:
        params["author"] = seed.author
    data = _ol_get(provider, "/search.json", params)
    docs = (data or {}).get("docs") or []
    if not docs and seed.author:
        params.pop("author")
        data = _ol_get(provider, "/search.json", params)
        docs = (data or {}).get("docs") or []
    if not docs:
        return
    doc = docs[0]
    seed.work_id = str(doc.get("key", "")).rsplit("/", 1)[-1] or None
    seed.author_name = (doc.get("author_name") or [seed.author])[0]
    seed.subjects = list(doc.get("subject_key") or [])


def _subject_label(subject_key: str) -> str:
    words = subject_key.replace("_", " ").strip()
    if words.endswith(" theory of"):  # Open Library inverts these: "knowledge, theory of"
        words = f"theory of {words[: -len(' theory of')]}"
    return words.capitalize()


def _subject_rows(provider: Any, seeds: list[Seed]) -> list[tuple[str, str, list[BookMetadata]]]:
    counts = Counter(
        subject
        for seed in seeds
        for subject in set(seed.subjects)
        if subject not in _GENERIC_SUBJECTS and not _SUBJECT_NOISE.search(subject)
    )
    chosen: list[str] = [subj for subj, n in counts.most_common() if n >= 2][:MAX_SUBJECT_ROWS]
    for seed in seeds:  # round-robin: the most widespread subject of each remaining book
        if len(chosen) >= MAX_SUBJECT_ROWS:
            break
        candidates = [s for s in seed.subjects if s in counts and s not in chosen]
        if candidates and not any(s in chosen for s in seed.subjects):
            chosen.append(max(candidates, key=lambda subj: counts[subj]))
    rows = []
    for subject in chosen:
        # Search (sorted by reading-log activity) ranks far better than the subject
        # page, which orders by edition count and favours old public-domain titles.
        data = _ol_get(
            provider,
            "/search.json",
            {
                "q": f"subject_key:{subject}",
                "sort": "readinglog",
                "limit": BOOKS_PER_ROW + 10,
                "fields": _OL_SEARCH_FIELDS,
            },
        )
        books = [b for d in (data or {}).get("docs") or [] if (b := provider._parse_search_doc(d))]
        if books:
            rows.append(
                (
                    f"subject:{subject}",
                    f"Because you have books on {_subject_label(subject)}",
                    books,
                )
            )
    return rows


def _author_rows(seeds: list[Seed]) -> list[tuple[str, str, list[BookMetadata]]]:
    provider = get_configured_provider(content_type="ebook")
    if not provider or not provider.is_available():
        return []
    authors = Counter(s.author_name or s.author for s in seeds if (s.author_name or s.author))
    rows = []
    for author, _count in authors.most_common(MAX_AUTHOR_ROWS):
        try:
            result = provider.search_paginated(
                MetadataSearchOptions(
                    query="",
                    sort=SortOrder.POPULARITY,
                    limit=BOOKS_PER_ROW + 6,
                    fields={"author": author},
                )
            )
        except Exception as exc:  # noqa: BLE001 - one failing author shouldn't break the page
            logger.debug("For You: author search for %s failed: %s", author, exc)
            continue
        wanted = _author_key(author)
        books = [b for b in result.books if any(_author_key(a) == wanted for a in b.authors)]
        if books:
            display = (
                author if "," not in author else " ".join(reversed(author.split(",", 1))).strip()
            )
            rows.append((f"author:{wanted}", f"More from {display}", books))
    return rows


_TRENDING_QUERY = """
query ForYouTrending($from: date!, $to: date!, $limit: Int!) {
  books_trending(from: $from, to: $to, limit: $limit, offset: 0) { ids }
}
"""
_BOOKS_BY_IDS_QUERY = """
query ForYouBooks($ids: [Int!]) {
  books(where: {id: {_in: $ids}}) {
    id title subtitle slug release_date headline description pages
    cached_image cached_tags cached_contributors
    contributions(where: {contribution: {_eq: "Author"}}) { author { name } }
    default_physical_edition { isbn_10 isbn_13 }
    featured_book_series { position series { id name primary_books_count } }
  }
}
"""


def _trending_row() -> list[tuple[str, str, list[BookMetadata]]]:
    if not is_provider_enabled("hardcover"):
        return []
    hardcover = get_provider("hardcover", **get_provider_kwargs("hardcover"))
    if not hardcover.is_available():
        return []
    today = datetime.datetime.now(tz=datetime.UTC).date()
    data = hardcover._execute_query(
        _TRENDING_QUERY,
        {
            "from": str(today - datetime.timedelta(days=30)),
            "to": str(today),
            "limit": BOOKS_PER_ROW + 6,
        },
    )
    ids = ((data or {}).get("books_trending") or {}).get("ids") or []
    if not ids:
        return []
    books_data = hardcover._execute_query(_BOOKS_BY_IDS_QUERY, {"ids": ids})
    by_id = {b.get("id"): b for b in (books_data or {}).get("books") or []}
    books = []
    for book_id in ids:  # keep trending order
        if book_id in by_id:
            try:
                books.append(hardcover._parse_book(by_id[book_id]))
            except AttributeError, KeyError, TypeError, ValueError:
                continue
    return [("trending", "Trending on Hardcover", books)] if books else []


def _is_owned(book: BookMetadata, owned_titles: set[str]) -> bool:
    title = _normalize(book.title)
    if not title:
        return False
    padded = f" {title} "
    return any(
        title == t
        or title.startswith(t + " ")
        or t.startswith(title + " ")
        or (len(t.split()) >= 2 and f" {t} " in padded)
        for t in owned_titles
    )


def _book_dict(book: BookMetadata) -> dict[str, Any]:
    from shelfmark.core.utils import transform_cover_url

    data = asdict(book)
    if data.get("cover_url"):
        data["cover_url"] = transform_cover_url(
            data["cover_url"], f"{book.provider}_{book.provider_id}"
        )
    return data


def _build(files: list[Path]) -> dict[str, Any]:
    seeds = _read_seeds(files)
    openlibrary = _openlibrary()
    for seed in seeds:
        _resolve_seed(openlibrary, seed)

    owned = {_normalize(s.title) for s in seeds} | {
        _normalize(s.title.split(":")[0]) for s in seeds
    }
    owned.discard("")

    raw_rows: list[tuple[str, str, list[BookMetadata]]] = []
    for builder in (
        _trending_row,
        lambda: _author_rows(seeds),
        lambda: _subject_rows(openlibrary, seeds),
    ):
        try:
            raw_rows.extend(builder())
        except Exception as exc:  # noqa: BLE001 - a broken row source shouldn't break the page
            logger.warning("For You: building a row failed: %s", exc)

    seen: set[str] = set()
    rows = []
    for key, title, books in raw_rows:
        picked = []
        for book in books:
            ident = f"{book.provider}:{book.provider_id}"
            norm = _normalize(book.title)
            if ident in seen or norm in seen or _is_owned(book, owned):
                continue
            seen.update({ident, norm})
            picked.append(_book_dict(book))
            if len(picked) >= BOOKS_PER_ROW:
                break
        if len(picked) >= MIN_BOOKS_PER_ROW:
            rows.append({"key": key, "title": title, "books": picked})

    return {"rows": rows, "seed_count": len(seeds)}


def get_for_you(refresh: bool = False) -> dict[str, Any]:
    """Recommendation rows for the current library, cached until the library changes."""
    files = _library_files(_library_dir())
    fingerprint = hashlib.sha256("\n".join(str(p) for p in files).encode()).hexdigest()[:16]
    cache_key = f"for_you:{fingerprint}"
    cache = get_metadata_cache()
    if not refresh and (cached := cache.get(cache_key)) is not None:
        return cached  # type: ignore[return-value]
    with _build_lock:
        if not refresh and (cached := cache.get(cache_key)) is not None:
            return cached  # type: ignore[return-value]
        result = _build(files)
        cache.set(cache_key, result, CACHE_TTL_SECONDS)
        return result
