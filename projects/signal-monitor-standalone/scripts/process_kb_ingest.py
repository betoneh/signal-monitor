#!/usr/bin/env python3
"""Ingest a pending KB URL into the private KB and optionally create deep dives."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
REPO_DIR = PROJECT_DIR / "repos" / "signal-monitor"
KB_ROOT = Path.home() / "clawd" / "projects" / "kb"
KB_ENTRIES_DIR = KB_ROOT / "entries"
KB_INDEX_PATH = KB_ROOT / "index.md"
DD_DIR = PROJECT_DIR / "data" / "deep-dives"
COOKIES_PATH = PROJECT_DIR / "data" / "cookies.json"
CHAT_ID = "-1003658657415"
THREAD_ID = "29"

X_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


sys.path.insert(0, str(SCRIPT_DIR))
from x_editorial import calc_cost, call_llm_routed, load_dotenv  # noqa: E402


try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None

try:
    from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
except Exception:  # pragma: no cover
    YouTubeTranscriptApi = None


@dataclass
class SourcePayload:
    title: str
    source_url: str
    source_type: str
    full_text: str
    author: str = ""


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    last_model: str = ""

    def add(self, model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cache_write_tokens += cache_write
        self.cost_usd += calc_cost(model, input_tokens, output_tokens, cache_read, cache_write)
        self.calls += 1
        self.last_model = model


class TextExtractor(HTMLParser):
    """Very small HTML-to-text fallback."""

    BLOCK_TAGS = {"article", "section", "main", "div", "p", "li", "ul", "ol", "h1", "h2", "h3", "blockquote", "br"}
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        cleaned = normalize_ws(data)
        if not cleaned:
            return
        if self.in_title and not self.title:
            self.title = cleaned
        self.parts.append(cleaned)


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-") or "kb-entry"


def fetch_json(url: str, headers: dict[str, str] | None = None, method: str = "GET", data: bytes | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "ignore")


def html_to_text(html: str) -> tuple[str, str]:
    if BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "footer", "nav", "form"]):
            tag.decompose()
        title = ""
        for candidate in (
            soup.find("meta", property="og:title"),
            soup.find("meta", attrs={"name": "twitter:title"}),
            soup.find("title"),
            soup.find("h1"),
        ):
            if not candidate:
                continue
            if getattr(candidate, "get", None):
                title = normalize_ws(candidate.get("content") or candidate.get_text(" ", strip=True))
            else:
                title = normalize_ws(candidate.get_text(" ", strip=True))
            if title:
                break
        root = soup.find("article") or soup.find("main") or soup.body or soup
        chunks: list[str] = []
        for tag in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
            text = normalize_ws(tag.get_text(" ", strip=True))
            if text:
                chunks.append(text)
        if not chunks:
            chunks = [line for line in (normalize_ws(line) for line in root.get_text("\n").splitlines()) if line]
        return title, "\n\n".join(chunks)

    parser = TextExtractor()
    parser.feed(html)
    lines = [line for line in (normalize_ws(line) for line in "".join(parser.parts).splitlines()) if line]
    return parser.title, "\n\n".join(lines)


def strip_html(html: str) -> str:
    return normalize_ws(re.sub(r"<[^>]+>", " ", html))


def get_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def extract_json_block(raw: str) -> str:
    raw = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


def call_json_prompt(prompt: str, model: str, usage: UsageTotals | None = None) -> Any:
    raw, model_used, input_tokens, output_tokens, cache_read, cache_write = call_llm_routed(prompt, model)
    if usage is not None:
        usage.add(model_used, input_tokens, output_tokens, cache_read, cache_write)
    block = extract_json_block(raw)
    return json.loads(block)


def get_x_guest_token() -> str:
    data = fetch_json(
        "https://api.x.com/1.1/guest/activate.json",
        headers={
            "authorization": f"Bearer {X_BEARER}",
            "user-agent": "Mozilla/5.0",
            "origin": "https://x.com",
            "referer": "https://x.com/",
        },
        method="POST",
        data=b"",
    )
    return data["guest_token"]


def load_x_auth_headers() -> dict[str, str]:
    if not COOKIES_PATH.exists():
        return {}
    try:
        cookies = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
        jar = {item["name"]: item["value"] for item in cookies if item.get("name") and item.get("value")}
        cookie = "; ".join(
            f"{key}={value}"
            for key, value in jar.items()
            if key in {"auth_token", "ct0", "twid", "lang", "kdt"}
        )
        if not cookie or not jar.get("ct0"):
            return {}
        return {
            "cookie": cookie,
            "x-csrf-token": jar["ct0"],
            "x-twitter-auth-type": "OAuth2Session",
        }
    except Exception:
        return {}


def json_decoder_from_assignment(text: str, marker: str) -> Any | None:
    idx = text.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
        return obj
    except Exception:
        return None


def recursively_find_article_text(obj: Any) -> str:
    if isinstance(obj, dict):
        plain = obj.get("plain_text")
        if isinstance(plain, str) and len(plain) > 500:
            return normalize_ws(plain)
        content_state = obj.get("content_state")
        if isinstance(content_state, dict):
            blocks = content_state.get("blocks")
            text = blocks_to_text(blocks)
            if text:
                return text
        blocks = obj.get("blocks")
        text = blocks_to_text(blocks)
        if text:
            return text
        for value in obj.values():
            text = recursively_find_article_text(value)
            if text:
                return text
    elif isinstance(obj, list):
        for value in obj:
            text = recursively_find_article_text(value)
            if text:
                return text
    return ""


def collect_block_text(block: Any) -> list[str]:
    if isinstance(block, str):
        text = normalize_ws(block)
        return [text] if text else []
    if isinstance(block, list):
        out: list[str] = []
        for item in block:
            out.extend(collect_block_text(item))
        return out
    if isinstance(block, dict):
        if isinstance(block.get("text"), str):
            text = normalize_ws(block["text"])
            if text:
                return [text]
        out: list[str] = []
        for value in block.values():
            out.extend(collect_block_text(value))
        return out
    return []


def blocks_to_text(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        text = normalize_ws(" ".join(collect_block_text(block)))
        if text and text not in seen:
            seen.add(text)
            paragraphs.append(text)
    joined = "\n\n".join(paragraphs)
    return joined if len(joined) > 500 else ""


def fetch_x_article_full_text(article_rest_id: str, tweet_id: str) -> str:
    headers = {
        "authorization": f"Bearer {X_BEARER}",
        "user-agent": "Mozilla/5.0",
        "referer": "https://x.com/",
    }
    headers.update(load_x_auth_headers())
    if "cookie" not in headers:
        headers["x-guest-token"] = get_x_guest_token()

    features = {
        "articles_preview_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "withArticleRichContentState": True,
        "withArticlePlainText": True,
        "withArticleSummaryText": True,
        "withArticleVoiceOver": False,
    }
    variables = {
        "tweetId": tweet_id,
        "includePromotedContent": True,
        "withVoice": True,
        "withCommunity": True,
    }
    query = urllib.parse.urlencode(
        {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        }
    )
    gql_url = f"https://x.com/i/api/graphql/fHLDP3qFEjnTqhWBVvsREg/TweetResultByRestId?{query}"

    try:
        data = fetch_json(gql_url, headers=headers)
        text = recursively_find_article_text(data)
        if text:
            return text
    except Exception:
        pass

    article_url = f"https://x.com/i/article/{article_rest_id}"
    try:
        html = fetch_text(article_url, headers=headers)
        state = json_decoder_from_assignment(html, "__INITIAL_STATE__=")
        if state is not None:
            text = recursively_find_article_text(state)
            if text:
                return text
        _, text = html_to_text(html)
        if text:
            return text
    except Exception:
        pass
    return ""


def extract_tweet_id(url: str) -> str:
    match = re.search(r"/status/(\d+)", url)
    if not match:
        raise ValueError(f"Could not extract tweet id from {url}")
    return match.group(1)


def fetch_x_source(url: str) -> SourcePayload:
    tweet_id = extract_tweet_id(url)
    tweet = fetch_json(
        f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=foo",
        headers={"user-agent": "Mozilla/5.0"},
    )
    user = tweet.get("user") or {}
    author = normalize_ws(user.get("name") or user.get("screen_name") or "")
    article = tweet.get("article") or {}
    if article.get("rest_id"):
        article_url = f"https://x.com/i/article/{article['rest_id']}"
        full_text = fetch_x_article_full_text(article["rest_id"], tweet_id) or normalize_ws(article.get("preview_text") or "")
        return SourcePayload(
            title=normalize_ws(article.get("title") or "X Article"),
            source_url=article_url,
            source_type="article / X Article",
            full_text=full_text,
            author=author,
        )

    text = normalize_ws(tweet.get("text") or "")
    urls = tweet.get("entities", {}).get("urls", [])
    expanded = [normalize_ws(item.get("expanded_url") or item.get("display_url") or "") for item in urls]
    expanded = [item for item in expanded if item]
    if expanded:
        text = text + "\n\nLinks:\n" + "\n".join(expanded)
    title = text[:80] + ("..." if len(text) > 80 else "")
    return SourcePayload(
        title=title or f"X Post by {author or 'unknown'}",
        source_url=url,
        source_type="tweet / X post",
        full_text=text,
        author=author,
    )


def extract_youtube_id(url: str) -> str:
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([^&\n?#]+)",
        r"youtube\.com/watch.*v=([^&\n?#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract YouTube id from {url}")


def fetch_youtube_source(url: str) -> SourcePayload:
    if YouTubeTranscriptApi is None:
        raise RuntimeError("youtube_transcript_api is not installed")
    video_id = extract_youtube_id(url)
    transcript_items: list[Any]
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        transcript_items = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "es"])
    else:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript_obj = transcript_list.find_transcript(["en", "es"])
        transcript_items = list(transcript_obj.fetch())
    full_text_parts = []
    for item in transcript_items:
        if isinstance(item, dict):
            text = item.get("text", "")
        else:
            text = getattr(item, "text", "")
        text = normalize_ws(text)
        if text:
            full_text_parts.append(text)
    full_text = " ".join(full_text_parts)
    if not full_text:
        raise RuntimeError(f"Transcript fetch returned no text for YouTube video {video_id}")
    html = fetch_text(url, headers={"user-agent": "Mozilla/5.0"})
    title, _ = html_to_text(html)
    return SourcePayload(
        title=title or f"YouTube Video {video_id}",
        source_url=url,
        source_type="video",
        full_text=full_text,
    )


def fetch_pdf_source(url: str) -> SourcePayload:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pypdf is not installed") from exc
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
    tmp_path = PROJECT_DIR / "data" / "tmp-kb-ingest.pdf"
    with urllib.request.urlopen(req, timeout=120) as resp:
        tmp_path.write_bytes(resp.read())
    reader = PdfReader(str(tmp_path))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    tmp_path.unlink(missing_ok=True)
    return SourcePayload(
        title=Path(urllib.parse.urlparse(url).path).stem.replace("-", " ").title() or "PDF",
        source_url=url,
        source_type="pdf",
        full_text=text,
    )


def fetch_article_source(url: str) -> SourcePayload:
    html = fetch_text(url, headers={"user-agent": "Mozilla/5.0"})
    title, text = html_to_text(html)
    return SourcePayload(
        title=title or urllib.parse.urlparse(url).netloc,
        source_url=url,
        source_type="article",
        full_text=text,
    )


def fetch_source(url: str) -> SourcePayload:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if "x.com" in host or "twitter.com" in host:
        return fetch_x_source(url)
    if "youtube.com" in host or "youtu.be" in host:
        return fetch_youtube_source(url)
    if parsed.path.lower().endswith(".pdf"):
        return fetch_pdf_source(url)
    return fetch_article_source(url)


def summarize_source(payload: SourcePayload, model: str, usage: UsageTotals | None = None) -> dict[str, Any]:
    excerpt = payload.full_text[:60000]
    prompt = f"""You are preparing a private knowledge-base entry.

Return ONLY valid JSON with this schema:
{{
  "title": "final title including author/source when useful",
  "type": "article | video | podcast | book | paper | pdf | thread (X/Twitter) | tweet / X post",
  "tags": ["tag1", "tag2"],
  "summary": ["paragraph 1", "paragraph 2", "paragraph 3"],
  "key_takeaways": ["point 1", "point 2"]
}}

Rules:
- Keep 6 to 10 concise tags, lowercase, kebab-case when needed.
- Summary must be 2 to 3 sharp analytical paragraphs.
- Key takeaways must be 5 to 8 bullets.
- Use the source title when it is already strong.
- If an author is obvious, include it in the title.

Source title: {payload.title}
Source type guess: {payload.source_type}
Author: {payload.author or "unknown"}
URL: {payload.source_url}

Full text excerpt:
{excerpt}
"""
    return call_json_prompt(prompt, model, usage=usage)


def load_kb_index() -> str:
    if not KB_INDEX_PATH.exists():
        return "# KB Index\n\n*Last updated: " + get_today() + "*\n\n## Entries\n"
    return KB_INDEX_PATH.read_text(encoding="utf-8")


def parse_kb_entry(entry_path: Path) -> tuple[SourcePayload, dict[str, Any]]:
    text = entry_path.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    source_match = re.search(r"^- \*\*Source:\*\*\s+(.+)$", text, re.MULTILINE)
    type_match = re.search(r"^- \*\*Type:\*\*\s+(.+)$", text, re.MULTILINE)
    tags_match = re.search(r"^- \*\*Tags:\*\*\s+(.+)$", text, re.MULTILINE)
    summary_match = re.search(r"## Summary\s+(.+?)\s+## Key Takeaways", text, re.DOTALL)
    takeaways_match = re.search(r"## Key Takeaways\s+(.+?)\s+## Full Text / Transcript", text, re.DOTALL)
    full_match = re.search(r"## Full Text / Transcript\s+(.+)$", text, re.DOTALL)

    summary = [normalize_ws(line) for line in (summary_match.group(1).splitlines() if summary_match else []) if normalize_ws(line)]
    takeaways = [
        normalize_ws(re.sub(r"^-+\s*", "", line))
        for line in (takeaways_match.group(1).splitlines() if takeaways_match else [])
        if normalize_ws(line)
    ]
    tags = [slugify(part.strip()) for part in (tags_match.group(1).split(",") if tags_match else []) if normalize_ws(part)]
    payload = SourcePayload(
        title=normalize_ws(title_match.group(1) if title_match else entry_path.stem.replace("-", " ").title()),
        source_url=normalize_ws(source_match.group(1) if source_match else ""),
        source_type=normalize_ws(type_match.group(1) if type_match else "article"),
        full_text=(full_match.group(1).strip() if full_match else ""),
    )
    meta = {
        "title": payload.title,
        "type": payload.source_type,
        "tags": tags,
        "summary": summary,
        "key_takeaways": takeaways,
    }
    return payload, meta


def resolve_kb_entry_path(entry_ref: str) -> Path:
    raw = normalize_ws(entry_ref)
    if not raw:
        raise RuntimeError("KB entry reference is empty")

    candidate = raw
    if candidate.endswith(".md"):
        path = KB_ENTRIES_DIR / candidate
        if path.exists():
            return path
        candidate = candidate[:-3]

    if candidate.startswith("#"):
        candidate = candidate[1:]

    if candidate.isdigit():
        number = int(candidate)
        text = load_kb_index()
        match = re.search(rf"\*\*#{number}\*\* \[[^\]]+\]\(entries/([^)]+)\)", text)
        if not match:
            raise RuntimeError(f"Could not resolve KB entry #{number}")
        path = KB_ENTRIES_DIR / Path(match.group(1)).name
        if not path.exists():
            raise RuntimeError(f"Resolved KB entry #{number}, but file is missing: {path}")
        return path

    stem = candidate[:-3] if candidate.endswith(".md") else candidate
    direct = KB_ENTRIES_DIR / f"{stem}.md"
    if direct.exists():
        return direct

    slug = slugify(candidate)
    slug_path = KB_ENTRIES_DIR / f"{slug}.md"
    if slug_path.exists():
        return slug_path

    raise RuntimeError(f"Could not resolve KB entry reference: {entry_ref}")


def next_kb_number(index_text: str) -> int:
    matches = [int(num) for num in re.findall(r"\*\*#(\d+)\*\*", index_text)]
    return (max(matches) + 1) if matches else 1


def resolve_kb_number_from_slug(slug: str) -> int:
    text = load_kb_index()
    match = re.search(rf"\*\*#(\d+)\*\* \[[^\]]+\]\(entries/{re.escape(slug)}\.md\)", text)
    if match:
        return int(match.group(1))
    return next_kb_number(text) - 1


def update_kb_index(slug: str, title: str, entry_type: str, tags: list[str], date_added: str) -> int:
    text = load_kb_index()
    kb_number = next_kb_number(text)
    line = f"- **#{kb_number}** [{title}](entries/{slug}.md) — {entry_type} · {', '.join(tags)} — {date_added}"
    if "## Entries" not in text:
        text = text.rstrip() + "\n\n## Entries\n"
    text = re.sub(r"\*Last updated: [^*]+\*", f"*Last updated: {date_added}*", text)
    text = text.rstrip() + "\n" + line + "\n"
    KB_INDEX_PATH.write_text(text, encoding="utf-8")
    return kb_number


def write_kb_entry(payload: SourcePayload, meta: dict[str, Any]) -> tuple[str, int, Path]:
    title = normalize_ws(meta.get("title") or payload.title)
    entry_type = normalize_ws(meta.get("type") or payload.source_type)
    tags = [slugify(tag).replace("-", "-") for tag in meta.get("tags") or []]
    if not tags:
        tags = ["uncategorized"]
    date_added = get_today()
    author_suffix = ""
    if payload.author and payload.author.lower() not in title.lower():
        author_suffix = f" — {payload.author}"
    summary = [normalize_ws(part) for part in (meta.get("summary") or []) if normalize_ws(part)]
    takeaways = [normalize_ws(part) for part in (meta.get("key_takeaways") or []) if normalize_ws(part)]
    slug = slugify(title)
    entry_path = KB_ENTRIES_DIR / f"{slug}.md"

    body = "\n".join(
        [
            f"# {title}{author_suffix}",
            "",
            f"- **Source:** {payload.source_url}",
            f"- **Type:** {entry_type}",
            f"- **Tags:** {', '.join(tags)}",
            f"- **Date added:** {date_added}",
            "",
            "## Summary",
            "",
            *summary,
            "",
            "## Key Takeaways",
            "",
            *[f"- {point}" for point in takeaways],
            "",
            "## Full Text / Transcript",
            "",
            payload.full_text.strip(),
            "",
        ]
    )
    entry_path.write_text(body, encoding="utf-8")
    kb_number = update_kb_index(slug, title, entry_type, tags, date_added)
    return slug, kb_number, entry_path


def ingest_with_openclaw(url: str) -> tuple[str, int, Path, SourcePayload, dict[str, Any]]:
    before = {path.name for path in KB_ENTRIES_DIR.glob("*.md")}
    cmd = [
        "/opt/homebrew/bin/openclaw",
        "agent",
        "--local",
        "--thinking",
        "medium",
        "--timeout",
        "1800",
        "--message",
        (
            "Use the knowledge-base skill. Add this URL to the private KB only. "
            "Fetch the complete source content, do not generate deep dives, and do not publish anything. "
            f"URL: {url}"
        ),
    ]
    subprocess.run(cmd, cwd=str(PROJECT_DIR), check=True, capture_output=True, text=True)

    after_paths = sorted(KB_ENTRIES_DIR.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    created = [path for path in after_paths if path.name not in before]
    entry_path = created[0] if created else (after_paths[0] if after_paths else None)
    if entry_path is None:
        raise RuntimeError("OpenClaw ingest did not produce a KB entry")

    slug = entry_path.stem
    kb_number = resolve_kb_number_from_slug(slug)
    payload, meta = parse_kb_entry(entry_path)
    return slug, kb_number, entry_path, payload, meta


def load_existing_kb_entry(entry_ref: str) -> tuple[str, int, Path, SourcePayload, dict[str, Any]]:
    entry_path = resolve_kb_entry_path(entry_ref)
    slug = entry_path.stem
    kb_number = resolve_kb_number_from_slug(slug)
    payload, meta = parse_kb_entry(entry_path)
    return slug, kb_number, entry_path, payload, meta


def next_deep_dive_id() -> int:
    max_id = 0
    for file in DD_DIR.glob("*.json"):
        match = re.match(r"(\d+)-", file.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return max_id + 1


def generate_deep_dives(
    payload: SourcePayload,
    meta: dict[str, Any],
    slug: str,
    model: str,
    count: int,
    usage: UsageTotals | None = None,
) -> list[Path]:
    if count <= 0:
        return []

    created: list[Path] = []
    used_titles: list[str] = []
    next_id = next_deep_dive_id()
    remaining = count
    source_excerpt = payload.full_text[:70000]

    while remaining > 0:
        batch_size = min(5, remaining)
        prompt = f"""You are writing deep dives for Signal Monitor from a KB source.

Return ONLY valid JSON as an array of {batch_size} objects.
Each object must have:
- "title": string
- "body_html": valid HTML using only <p>, <em>, <strong>, <a> tags
- "tags": array of 4 to 7 concise lowercase tags

Rules:
- Each deep dive must be distinct and non-overlapping.
- Titles must be punchy and standalone.
- body_html must be 3 to 5 paragraphs.
- Keep each deep dive grounded in the source.
- Do not mention that this came from a KB entry.
- Avoid repeating these existing titles: {json.dumps(used_titles)}

Source title: {meta.get("title") or payload.title}
Source URL: {payload.source_url}
Source tags: {json.dumps(meta.get("tags") or [])}
Source summary: {json.dumps(meta.get("summary") or [])}
Source takeaways: {json.dumps(meta.get("key_takeaways") or [])}

Full text excerpt:
{source_excerpt}
"""
        items = call_json_prompt(prompt, model, usage=usage)
        if not isinstance(items, list):
            raise RuntimeError("Deep dive model output was not a JSON array")
        for item in items:
            title = normalize_ws(item.get("title") or f"Deep Dive {next_id}")
            body_html = normalize_ws(item.get("body_html") or "")
            tags = [slugify(tag) for tag in item.get("tags") or [] if normalize_ws(str(tag))]
            if not title or not body_html:
                raise RuntimeError("Deep dive output missing title or body_html")
            filename = DD_DIR / f"{next_id}-{slugify(title)}.json"
            data = {
                "id": next_id,
                "title": title,
                "source": {
                    "kb_entry": slug,
                    "original": meta.get("title") or payload.title,
                    "url": payload.source_url,
                },
                "body_html": body_html,
                "tags": tags[:7],
                "word_count": len(strip_html(body_html).split()),
                "used_dates": [],
                "created_date": get_today(),
            }
            filename.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            created.append(filename)
            used_titles.append(title)
            next_id += 1
            remaining -= 1
            if remaining <= 0:
                break

    return created


def run_publish_steps() -> None:
    subprocess.run([sys.executable, str(SCRIPT_DIR / "build_hub.py")], cwd=str(PROJECT_DIR), check=True)
    subprocess.run([sys.executable, str(SCRIPT_DIR / "x_push.py")], cwd=str(PROJECT_DIR), check=True)


def escape_markdown(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)


def format_usage_cost(usage: UsageTotals, prefix: str = "API cost") -> str:
    if usage.calls <= 0:
        return f"{prefix}: n/a"
    token_str = f"{usage.input_tokens/1000:.1f}k in / {usage.output_tokens/1000:.1f}k out"
    return f"{prefix}: ${usage.cost_usd:.2f} · {token_str}"


def send_telegram_notification(
    kb_number: int,
    payload: SourcePayload,
    deep_dives: list[Path],
    usage: UsageTotals,
    note: str = "",
) -> None:
    load_dotenv(PROJECT_DIR / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("WARNING: TELEGRAM_BOT_TOKEN missing; skipping Telegram notification", file=sys.stderr)
        return

    lines = [
        f"*KB Updated*",
        f"KB \\#{kb_number}: {escape_markdown(payload.title)}",
    ]
    if deep_dives:
        lines.append("")
        lines.append(f"*Deep Dives \\({len(deep_dives)}\\)*")
        for path in deep_dives:
            data = json.loads(path.read_text(encoding="utf-8"))
            lines.append(f"• \\#{data['id']} — {escape_markdown(data['title'])}")
    else:
        lines.append("")
        lines.append("Deep Dives: none")

    lines.append("")
    lines.append(escape_markdown(format_usage_cost(usage)))
    if note:
        lines.append(escape_markdown(note))

    text = "\n".join(lines)
    body = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "parse_mode": "MarkdownV2",
            "text": text,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram send failed: {result}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process a pending KB ingest job")
    parser.add_argument("--url")
    parser.add_argument("--kb-entry")
    parser.add_argument("--deep-dives", type=int, default=0)
    parser.add_argument("--model", default="anthropic/claude-opus-4-6")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.url) == bool(args.kb_entry):
        parser.error("Use exactly one of --url or --kb-entry")
    return args


def main() -> int:
    args = parse_args()
    payload = None
    meta = None
    slug = ""
    kb_number = 0
    entry_path = None
    usage = UsageTotals()
    note = ""

    if args.kb_entry:
        slug, kb_number, entry_path, payload, meta = load_existing_kb_entry(args.kb_entry)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "kb_entry": args.kb_entry,
                        "resolved_path": str(entry_path),
                        "source": payload.__dict__,
                        "meta": meta,
                    },
                    indent=2,
                    ensure_ascii=False,
                )[:12000]
            )
            return 0
    else:
        try:
            payload = fetch_source(args.url)
            if len(payload.full_text.strip()) < 200:
                raise RuntimeError("Fetched source text is too short; refusing to create KB entry from partial content")
            meta = summarize_source(payload, args.model, usage=usage)
            if args.dry_run:
                print(json.dumps({"source": payload.__dict__, "meta": meta}, indent=2, ensure_ascii=False)[:12000])
                return 0
            slug, kb_number, entry_path = write_kb_entry(payload, meta)
        except Exception as exc:
            if args.dry_run:
                print(f"DRY RUN fallback required: {exc}")
                return 0
            print(f"Falling back to OpenClaw KB ingest: {exc}", file=sys.stderr)
            slug, kb_number, entry_path, payload, meta = ingest_with_openclaw(args.url)
            note = "KB ingest via OpenClaw/local; API cost reflects DD generation only."

    deep_dives = generate_deep_dives(payload, meta, slug, args.model, args.deep_dives, usage=usage)

    print(f"KB #{kb_number}: {entry_path.name}")
    if deep_dives:
        ids = [int(re.match(r"(\d+)-", path.name).group(1)) for path in deep_dives]
        print(f"Deep dives: {len(deep_dives)} ({min(ids)}-{max(ids)})")
    else:
        print("Deep dives: 0")
    print(format_usage_cost(usage))

    run_publish_steps()
    try:
        send_telegram_notification(kb_number, payload, deep_dives, usage, note=note)
    except Exception as exc:
        print(f"WARNING: Telegram notification failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
