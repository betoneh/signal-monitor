#!/usr/bin/env python3
"""Resolve a free-text model request into a validated Signal Monitor model entry."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
REPO_DIR = PROJECT_DIR / "repos" / "signal-monitor"
SETTINGS_PATH = REPO_DIR / "settings.json"
CHAT_ID = "-1003658657415"
THREAD_ID = "29"
DEFAULT_RESOLVER_MODEL = "anthropic/claude-sonnet-4-6"

sys.path.insert(0, str(SCRIPT_DIR))
from x_editorial import calc_cost, call_llm_routed, load_dotenv  # noqa: E402


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


@dataclass
class Candidate:
    provider: str
    runtime_id: str
    raw_id: str
    label: str
    pricing: dict[str, float]
    score: float = 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_token(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def extract_json_block(raw: str) -> str:
    raw = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


def call_json_prompt(prompt: str, model: str, usage: UsageTotals | None = None) -> Any:
    raw, model_used, input_tokens, output_tokens, cache_read, cache_write = call_llm_routed(prompt, model)
    if usage is not None:
        usage.add(model_used, input_tokens, output_tokens, cache_read, cache_write)
    return json.loads(extract_json_block(raw))


def escape_markdown(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text or "")


def format_usage_cost(usage: UsageTotals, prefix: str = "API cost") -> str:
    if usage.calls <= 0:
        return f"{prefix}: n/a"
    token_str = f"{usage.input_tokens/1000:.1f}k in / {usage.output_tokens/1000:.1f}k out"
    return f"{prefix}: ${usage.cost_usd:.2f} · {token_str}"


def post_telegram(lines: list[str]) -> None:
    load_dotenv(PROJECT_DIR / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("WARNING: TELEGRAM_BOT_TOKEN missing; skipping Telegram notification", file=sys.stderr)
        return
    body = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "parse_mode": "MarkdownV2",
            "text": "\n".join(lines),
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


def humanize_model_name(model_id: str, provider: str) -> str:
    name = model_id.replace("_", " ").replace("/", " ").replace("-", " ")
    name = re.sub(r"\b4 7\b", "4.7", name)
    name = re.sub(r"\b4 6\b", "4.6", name)
    name = re.sub(r"\b5 4\b", "5.4", name)
    name = re.sub(r"\b5 2\b", "5.2", name)
    words = [word.upper() if word in {"gpt", "glm", "xai"} else word.capitalize() for word in name.split()]
    label = " ".join(words) or model_id
    if provider == "venice":
        label = f"{label} (Venice)"
    elif provider == "anthropic" and not label.lower().startswith("claude"):
        label = f"Claude {label}"
    elif provider == "ollama":
        label = f"{label} (local)"
    elif provider == "openclaw":
        label = "OpenClaw (local)"
    return label


def infer_pricing(provider: str, model_id: str) -> dict[str, float]:
    match = model_id.lower()
    if provider == "anthropic":
        if "opus" in match:
            return {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 3.75}
        if "sonnet" in match:
            return {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 0.75}
        if "haiku" in match:
            return {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 0.2}
    if provider == "xai":
        if "grok-4-1" in match or "grok-41" in match:
            return {"input": 0.23, "output": 0.57, "cache_read": 0.06, "cache_write": 0.0}
        if "grok-4" in match:
            return {"input": 3.0, "output": 15.0, "cache_read": 0.0, "cache_write": 0.0}
        if "grok-3" in match:
            return {"input": 3.0, "output": 15.0, "cache_read": 0.0, "cache_write": 0.0}
    return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def fetch_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load_venice_candidates() -> list[Candidate]:
    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("VENICE_API_KEY", "")
    if not api_key:
        return []
    data = fetch_json("https://api.venice.ai/api/v1/models", {"Authorization": f"Bearer {api_key}"})
    items = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        pricing = item.get("model_spec", {}).get("pricing", {})
        items.append(
            Candidate(
                provider="venice",
                runtime_id=f"venice/{model_id}",
                raw_id=model_id,
                label=humanize_model_name(model_id, "venice"),
                pricing={
                    "input": float(pricing.get("input", {}).get("usd", 0) or 0),
                    "output": float(pricing.get("output", {}).get("usd", 0) or 0),
                    "cache_read": float(pricing.get("cache_input", {}).get("usd", 0) or 0),
                    "cache_write": 0.0,
                },
            )
        )
    return items


def load_xai_candidates() -> list[Candidate]:
    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        return []
    data = fetch_json("https://api.x.ai/v1/models", {"Authorization": f"Bearer {api_key}"})
    items = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        items.append(
            Candidate(
                provider="xai",
                runtime_id=f"xai/{model_id}",
                raw_id=model_id,
                label=humanize_model_name(model_id, "xai"),
                pricing=infer_pricing("xai", model_id),
            )
        )
    return items


def load_anthropic_candidates() -> list[Candidate]:
    load_dotenv(PROJECT_DIR / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []
    data = fetch_json(
        "https://api.anthropic.com/v1/models",
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    items = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        items.append(
            Candidate(
                provider="anthropic",
                runtime_id=f"anthropic/{model_id}",
                raw_id=model_id,
                label=item.get("display_name") or humanize_model_name(model_id, "anthropic"),
                pricing=infer_pricing("anthropic", model_id),
            )
        )
    return items


def load_ollama_candidates() -> list[Candidate]:
    try:
        output = subprocess.check_output(["ollama", "list"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    items = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        model_id = parts[0]
        items.append(
            Candidate(
                provider="ollama",
                runtime_id=f"ollama/{model_id}",
                raw_id=model_id,
                label=humanize_model_name(model_id, "ollama"),
                pricing={"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
            )
        )
    return items


def load_candidates() -> list[Candidate]:
    items = [
        Candidate(
            provider="openclaw",
            runtime_id="openclaw",
            raw_id="openclaw",
            label="OpenClaw (local)",
            pricing={"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        )
    ]
    items.extend(load_anthropic_candidates())
    items.extend(load_venice_candidates())
    items.extend(load_xai_candidates())
    items.extend(load_ollama_candidates())
    return items


def detect_provider_hints(request: str) -> set[str]:
    text = normalize_token(request)
    hints: set[str] = set()
    if "venice" in text:
        hints.add("venice")
    if "anthropic" in text or "claude" in text:
        hints.add("anthropic")
    if "xai" in text or "grok" in text:
        hints.add("xai")
    if "ollama" in text or "local" in text:
        hints.add("ollama")
    if "openclaw" in text:
        hints.add("openclaw")
    return hints


def request_tokens(request: str) -> list[str]:
    stopwords = {
        "de",
        "del",
        "la",
        "el",
        "the",
        "model",
        "modelo",
        "quiero",
        "agrega",
        "agregar",
        "add",
        "please",
        "por",
        "favor",
        "use",
        "usar",
    }
    tokens = [token for token in normalize_token(request).split() if token and token not in stopwords]
    return tokens


def score_candidate(candidate: Candidate, request: str, hints: set[str], tokens: list[str]) -> float:
    haystack = normalize_token(f"{candidate.provider} {candidate.runtime_id} {candidate.raw_id} {candidate.label}")
    score = SequenceMatcher(None, normalize_token(request), haystack).ratio() * 100
    if hints:
        score += 30 if candidate.provider in hints else -25
    for token in tokens:
        if token in haystack:
            score += 10 + min(len(token), 8)
    return score


def choose_candidate(request: str, resolver_model: str, usage: UsageTotals) -> Candidate:
    candidates = load_candidates()
    if not candidates:
        raise RuntimeError("No model catalogs were available from configured providers")

    hints = detect_provider_hints(request)
    tokens = request_tokens(request)
    scoped = [candidate for candidate in candidates if not hints or candidate.provider in hints]
    if not scoped:
        raise RuntimeError("No candidates matched the requested provider")

    for candidate in scoped:
        candidate.score = score_candidate(candidate, request, hints, tokens)
    scoped.sort(key=lambda item: item.score, reverse=True)
    top = scoped[:15]
    prompt = f"""You are selecting a single AI model for Signal Monitor.

User request:
{request}

Candidate models:
{json.dumps([
    {
        "provider": item.provider,
        "runtime_id": item.runtime_id,
        "raw_id": item.raw_id,
        "label": item.label,
        "score": round(item.score, 2),
    }
    for item in top
], indent=2, ensure_ascii=False)}

Choose the single best exact match. Respect provider, family, and version.
If the exact request is not available in the candidates, return no match instead of picking a nearby version.
Return JSON with:
- runtime_id: selected runtime_id, or empty string when there is no exact match
- reason: one short sentence
- label_override: optional user-facing label, or empty string
"""
    result = call_json_prompt(prompt, resolver_model, usage=usage)
    runtime_id = normalize_ws(str(result.get("runtime_id") or ""))
    if not runtime_id:
        raise RuntimeError(normalize_ws(str(result.get("reason") or "No exact model match found")))
    for candidate in top:
        if candidate.runtime_id == runtime_id:
            label_override = normalize_ws(str(result.get("label_override") or ""))
            if label_override:
                candidate.label = label_override
            return candidate
    raise RuntimeError(f"Resolver returned an unknown candidate: {runtime_id}")


def validate_candidate(candidate: Candidate, usage: UsageTotals) -> str:
    prompt = "Reply with exactly OK."
    try:
        text, model_used, input_tokens, output_tokens, cache_read, cache_write = call_llm_routed(prompt, candidate.runtime_id)
    except BaseException as exc:
        raise RuntimeError(f"Validation call failed for {candidate.runtime_id}: {exc}") from exc
    usage.add(model_used, input_tokens, output_tokens, cache_read, cache_write)
    reply = normalize_ws(text)
    if "ok" not in reply.lower():
        raise RuntimeError(f"Validation response was unexpected: {reply[:120]}")
    return reply


def load_settings() -> dict[str, Any]:
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}


def save_settings(data: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert_available_model(settings: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
    models = settings.get("available_models") or []
    updated = False
    for item in models:
        if item.get("id") == candidate.runtime_id:
            item["label"] = candidate.label
            item["enabled"] = True
            item["pricing"] = candidate.pricing
            updated = True
            break
    if not updated:
        models.append(
            {
                "id": candidate.runtime_id,
                "label": candidate.label,
                "enabled": True,
                "pricing": candidate.pricing,
            }
        )
    models.sort(key=lambda item: item.get("label", item.get("id", "")).lower())
    settings["available_models"] = models
    return settings


def pricing_summary(pricing: dict[str, float]) -> str:
    return (
        f"input ${pricing.get('input', 0):g}/M · "
        f"output ${pricing.get('output', 0):g}/M · "
        f"cache read ${pricing.get('cache_read', 0):g}/M · "
        f"cache write ${pricing.get('cache_write', 0):g}/M"
    )


def send_success_telegram(request: str, candidate: Candidate, usage: UsageTotals) -> None:
    lines = [
        "*Model Added*",
        f"Request: {escape_markdown(request)}",
        f"Label: {escape_markdown(candidate.label)}",
        f"ID: {escape_markdown(candidate.runtime_id)}",
        f"Pricing: {escape_markdown(pricing_summary(candidate.pricing))}",
        "",
        escape_markdown(format_usage_cost(usage)),
    ]
    post_telegram(lines)


def send_failure_telegram(request: str, error: str) -> None:
    lines = [
        "*Model Onboarding Failed*",
        f"Request: {escape_markdown(request)}",
        f"Reason: {escape_markdown(error)}",
    ]
    post_telegram(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve and validate a free-text model request")
    parser.add_argument("--request", help="Free-text model request; defaults to settings.json value")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_settings()
    request = normalize_ws(args.request or settings.get("model_onboarding", {}).get("request") or "")
    if not request:
        print("No model_onboarding request pending.")
        return 0

    resolver_model = settings.get("model") or DEFAULT_RESOLVER_MODEL
    usage = UsageTotals()
    try:
        candidate = choose_candidate(request, resolver_model, usage)
        validate_candidate(candidate, usage)
        payload = {
            "request": "",
            "last_result": f"Added {candidate.runtime_id} at {now_iso()}",
            "updated_at": now_iso(),
        }
        settings["model_onboarding"] = payload
        upsert_available_model(settings, candidate)
        if not args.dry_run:
            save_settings(settings)
            send_success_telegram(request, candidate, usage)
        print(
            json.dumps(
                {
                    "request": request,
                    "model": {
                        "id": candidate.runtime_id,
                        "label": candidate.label,
                        "pricing": candidate.pricing,
                    },
                    "usage": usage.__dict__,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        error = normalize_ws(str(exc)) or exc.__class__.__name__
        settings["model_onboarding"] = {
            "request": "",
            "last_error": error,
            "updated_at": now_iso(),
        }
        if not args.dry_run:
            save_settings(settings)
            try:
                send_failure_telegram(request, error)
            except Exception as tg_exc:
                print(f"WARNING: Telegram notification failed: {tg_exc}", file=sys.stderr)
        print(f"ERROR: {error}", file=sys.stderr)
        return 1 if args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
