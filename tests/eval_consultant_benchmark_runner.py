# -*- coding: utf-8 -*-
"""Blind, provider-neutral replay runner for the active Option C interpreter.

This script is opt-in and makes paid API calls only when explicitly run with
at least two provider credentials configured. It never reads .env, Telegram,
AliExpress, or production databases. Credentials are read from process
environment variables only. No retries or fallback calls are performed.

Example (PowerShell):
  $env:GEMINI_API_KEY = "..."
  $env:OPENAI_API_KEY = "..."
  python tests/eval_consultant_benchmark_runner.py --output eval-results.json

For Qwen, also set QWEN_API_KEY and QWEN_BASE_URL to the exact workspace
endpoint supplied by Alibaba Model Studio. Never put keys in command-line
arguments or commit them.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import statistics
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from services.ai_service import AIService, CONVERSATION_TURN_PROMPT
from tests.eval_consultant_dialogue_dataset import DIALOGUE_CASES, DATASET_VERSION


TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "action", "confidence", "product_type", "marketplace_terms",
        "question", "response", "slot_updates",
    ],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["new_search", "refine", "correction", "alternatives", "compare", "clarify", "respond"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "product_type": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "marketplace_terms": {"type": "array", "items": {"type": "string"}},
        "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "response": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "slot_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["operation", "kind", "target", "value"],
                "properties": {
                    "operation": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "kind": {"type": "string", "enum": ["use_case", "compatibility", "feature", "exclusion", "preference"]},
                    "target": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "value": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Provider:
    key: str
    provider: str
    model: str
    api_key: str
    base_url: str | None = None


@dataclass
class ModelResponse:
    text: str
    usage: dict[str, int | None]
    duration_ms: float
    http_status: int | None = None
    finish_reason: str | None = None
    resolved_model: str | None = None


def configured_providers() -> list[Provider]:
    """Return only explicitly configured providers; never read dotenv files."""
    providers: list[Provider] = []
    specs = [
        ("gemini", "google", os.getenv("GEMINI_API_KEY"), os.getenv("EVAL_GEMINI_MODEL", "gemini-3.8-flash"), None),
        ("gemini_lite", "google", os.getenv("GEMINI_API_KEY"), os.getenv("EVAL_GEMINI_LITE_MODEL", "gemini-3.1-flash-lite"), None),
        ("openai", "openai", os.getenv("OPENAI_API_KEY"), os.getenv("EVAL_OPENAI_MODEL", "gpt-6-luna"), None),
        ("anthropic", "anthropic", os.getenv("ANTHROPIC_API_KEY"), os.getenv("EVAL_ANTHROPIC_MODEL", "claude-haiku-5-5"), None),
        ("deepseek", "deepseek", os.getenv("DEEPSEEK_API_KEY"), os.getenv("EVAL_DEEPSEEK_MODEL", "deepseek-flash"), None),
        ("qwen", "qwen", os.getenv("QWEN_API_KEY"), os.getenv("EVAL_QWEN_MODEL", "qwen3.5-flash"), os.getenv("QWEN_BASE_URL")),
    ]
    for key, provider, api_key, model, base_url in specs:
        if api_key and (provider != "qwen" or base_url):
            providers.append(Provider(key, provider, model, api_key, base_url))
    return providers


def _usage(**values: Any) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for key, value in values.items():
        result[key] = value if isinstance(value, int) and value >= 0 else None
    return result


def _extract_openai_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks)


def _qwen_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    if base.endswith("/compatible-mode/v1"):
        return base + "/chat/completions"
    return base + "/compatible-mode/v1/chat/completions"


async def call_provider(provider: Provider, prompt: str, timeout_seconds: float) -> ModelResponse:
    """One provider attempt. No retries, fallback, prompt logging or key logging."""
    started = time.perf_counter()
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if provider.provider == "google":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{provider.model}:generateContent"
            response = await client.post(
                url,
                headers={"x-goog-api-key": provider.api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
            )
        elif provider.provider == "openai":
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json={
                    "model": provider.model,
                    "input": prompt,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": "conversation_turn",
                            "strict": True,
                            "schema": TURN_SCHEMA,
                        }
                    },
                },
            )
        elif provider.provider == "anthropic":
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": provider.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": provider.model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                    "output_config": {"format": {"type": "json_schema", "schema": TURN_SCHEMA}},
                },
            )
        elif provider.provider == "deepseek":
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json={
                    "model": provider.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2048,
                },
            )
        elif provider.provider == "qwen":
            response = await client.post(
                _qwen_url(provider.base_url or ""),
                headers={"Authorization": f"Bearer {provider.api_key}"},
                json={
                    "model": provider.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2048,
                },
            )
        else:  # pragma: no cover - closed provider registry
            raise ValueError(f"Unsupported provider: {provider.provider}")

    duration_ms = (time.perf_counter() - started) * 1000
    payload = response.json() if response.content else {}
    if response.is_error:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        error_code = error.get("code") or error.get("type") if isinstance(error, dict) else None
        raise ProviderHTTPError(
            response.status_code,
            str(error_code or "provider_error"),
            duration_ms=duration_ms,
        )

    if provider.provider == "google":
        candidates = payload.get("candidates", []) or []
        parts = (candidates[0].get("content", {}).get("parts", []) if candidates else [])
        text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
        meta = payload.get("usageMetadata", {}) or {}
        candidate_tokens = meta.get("candidatesTokenCount")
        thought_tokens = meta.get("thoughtsTokenCount")
        billable_output = (
            None if candidate_tokens is None and thought_tokens is None
            else (candidate_tokens or 0) + (thought_tokens or 0)
        )
        usage = _usage(
            input_tokens=meta.get("promptTokenCount"),
            # Gemini reports candidate and thought tokens separately. Both
            # are billable output; keep the split for analysis but count the
            # sum exactly once in output_tokens.
            output_tokens=billable_output,
            candidate_output_tokens=candidate_tokens,
            reasoning_tokens=thought_tokens,
            cached_input_tokens=meta.get("cachedContentTokenCount"),
        )
        finish = candidates[0].get("finishReason") if candidates else None
    elif provider.provider == "openai":
        text = _extract_openai_text(payload)
        meta = payload.get("usage", {}) or {}
        input_details = meta.get("input_tokens_details", {}) or {}
        output_details = meta.get("output_tokens_details", {}) or {}
        usage = _usage(
            input_tokens=meta.get("input_tokens"),
            output_tokens=meta.get("output_tokens"),
            reasoning_tokens=output_details.get("reasoning_tokens"),
            cached_input_tokens=input_details.get("cached_tokens"),
        )
        finish = payload.get("status")
    elif provider.provider == "anthropic":
        text = "\n".join(
            block.get("text", "")
            for block in payload.get("content", []) or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        meta = payload.get("usage", {}) or {}
        base_input = meta.get("input_tokens")
        cache_read = meta.get("cache_read_input_tokens")
        cache_write = meta.get("cache_creation_input_tokens")
        total_input = (
            None if base_input is None and cache_read is None and cache_write is None
            else (base_input or 0) + (cache_read or 0) + (cache_write or 0)
        )
        usage = _usage(
            # Anthropic reports uncached, cache-read and cache-write input
            # categories separately; normalize input_tokens to their total.
            input_tokens=total_input,
            output_tokens=meta.get("output_tokens"),
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        finish = payload.get("stop_reason")
    else:
        choices = payload.get("choices", []) or []
        message = choices[0].get("message", {}) if choices else {}
        text = message.get("content", "") if isinstance(message, dict) else ""
        meta = payload.get("usage", {}) or {}
        prompt_details = meta.get("prompt_tokens_details", {}) or {}
        completion_details = meta.get("completion_tokens_details", {}) or {}
        usage = _usage(
            input_tokens=meta.get("prompt_tokens"),
            output_tokens=meta.get("completion_tokens"),
            reasoning_tokens=completion_details.get("reasoning_tokens"),
            cached_input_tokens=prompt_details.get("cached_tokens") or meta.get("prompt_cache_hit_tokens"),
        )
        finish = choices[0].get("finish_reason") if choices else None

    return ModelResponse(
        text=text if isinstance(text, str) else "",
        usage=usage,
        duration_ms=duration_ms,
        http_status=response.status_code,
        finish_reason=str(finish) if finish is not None else None,
        resolved_model=str(
            payload.get("model") or payload.get("modelVersion") or provider.model
        ),
    )


class ProviderHTTPError(RuntimeError):
    def __init__(self, status: int, code: str, *, duration_ms: float | None = None):
        super().__init__(f"HTTP {status} ({code})")
        self.status = status
        self.code = code
        self.duration_ms = duration_ms


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).casefold().split())


def _canonical_updates(items: Any) -> list[tuple[str, str, str | None, str | None]]:
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append((
            str(item.get("operation") or ""),
            str(item.get("kind") or ""),
            _norm(item.get("target")),
            _norm(item.get("value")),
        ))
    return sorted(result)


def score_decision(expected: dict[str, Any], actual: dict[str, Any] | None) -> dict[str, Any]:
    """Score accepted application decisions; retain dimensions separately."""
    if not isinstance(actual, dict):
        return {
            "accepted": False, "action_exact": False, "product_type_exact": False,
            "slot_updates_exact": False, "retrieval_exact": False, "all_exact": False,
        }
    action_exact = actual.get("action") == expected.get("action")
    expected_type = _norm(expected.get("product_type"))
    actual_type = _norm(actual.get("product_type"))
    product_type_exact = expected_type == actual_type
    slot_updates_exact = _canonical_updates(actual.get("slot_updates")) == _canonical_updates(expected.get("slot_updates"))
    retrieval = actual.get("action") in {"new_search", "refine", "correction", "alternatives"}
    retrieval_exact = expected.get("retrieval") is None or retrieval == expected.get("retrieval")
    return {
        "accepted": True,
        "action_exact": action_exact,
        "product_type_exact": product_type_exact,
        "slot_updates_exact": slot_updates_exact,
        "retrieval_exact": retrieval_exact,
        "all_exact": action_exact and product_type_exact and slot_updates_exact and retrieval_exact,
    }


def _cost_usd(usage: dict[str, int | None], rate: dict[str, float] | None) -> float | None:
    if not rate or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    total_input = int(usage["input_tokens"] or 0)
    cached = min(total_input, int(usage.get("cached_input_tokens") or 0))
    remaining = total_input - cached
    cache_write = min(remaining, int(usage.get("cache_write_tokens") or 0))
    uncached = remaining - cache_write
    input_rate = float(rate["input_per_million"])
    output_rate = float(rate["output_per_million"])
    if cached and "cached_input_per_million" not in rate:
        return None
    if cache_write and "cache_write_per_million" not in rate:
        return None
    cached_rate = float(rate.get("cached_input_per_million", input_rate))
    write_rate = float(rate.get("cache_write_per_million", input_rate))
    return (
        uncached * input_rate + cached * cached_rate + cache_write * write_rate
        + int(usage["output_tokens"] or 0) * output_rate
    ) / 1_000_000


def _load_rates(path: str | None) -> dict[str, dict[str, float]]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Rates file must contain a JSON object keyed by model ID")
    return data


def _safe_error(exc: BaseException) -> dict[str, Any]:
    # Do not store exception messages: SDK/HTTP errors can echo request details.
    return {
        "type": type(exc).__name__,
        "http_status": getattr(exc, "status", getattr(getattr(exc, "response", None), "status_code", None)),
        "provider_code": getattr(exc, "code", None),
    }


def validate_provider_roster(providers: list[Provider]) -> None:
    if len({provider.provider for provider in providers}) < 2:
        raise SystemExit("Configure credentials for at least two distinct providers; two Google models alone are not a cross-provider comparison.")


async def run_benchmark(
    providers: list[Provider],
    *,
    output_path: Path,
    map_path: Path,
    repeat: int,
    timeout_seconds: float,
    seed: int,
    rates: dict[str, dict[str, float]],
) -> None:
    validate_provider_roster(providers)

    rng = random.Random(seed)
    shuffled = providers[:]
    rng.shuffle(shuffled)
    aliases = {provider.key: chr(ord("A") + index) for index, provider in enumerate(shuffled)}
    alias_map = {
        aliases[p.key]: {
            "provider": p.provider,
            "requested_model": p.model,
            "provider_key": p.key,
            "output_mode": {
                "google": "json_mime_type",
                "openai": "strict_json_schema",
                "anthropic": "json_schema",
                "deepseek": "json_object",
                "qwen": "json_object",
            }[p.provider],
            "resolved_models": [],
        }
        for p in providers
    }

    tasks = []
    for provider in providers:
        for case in DIALOGUE_CASES:
            for turn_index, turn in enumerate(case["turns"], start=1):
                for repetition in range(1, repeat + 1):
                    tasks.append((provider, case, turn_index, turn, repetition))
    rng.shuffle(tasks)

    results: list[dict[str, Any]] = []
    for provider, case, turn_index, turn, repetition in tasks:
        ai = AIService(
            api_key="benchmark-placeholder",
            model=provider.model,
            fast_model=provider.model,
            fallback_model=provider.model,
            # Match the production 8.5s turn deadline while the adapter below
            # enforces one primary attempt with the configured 4s default.
            timeout_seconds=8.5,
        )
        call_record: dict[str, Any] = {}

        async def injected_generate(contents, *, models=None, operation="generate"):
            try:
                response = await call_provider(provider, str(contents), timeout_seconds)
            except Exception as exc:
                call_record["duration_ms"] = getattr(exc, "duration_ms", None)
                call_record["http_status"] = getattr(exc, "status", None)
                raise
            call_record.update({
                "raw_text": response.text,
                "usage": response.usage,
                "duration_ms": response.duration_ms,
                "http_status": response.http_status,
                "finish_reason": response.finish_reason,
                "resolved_model": response.resolved_model,
            })
            resolved = response.resolved_model
            if resolved and resolved not in alias_map[aliases[provider.key]]["resolved_models"]:
                alias_map[aliases[provider.key]]["resolved_models"].append(resolved)
            return response.text

        # Reuse the exact production prompt builder and the exact application
        # validation path, but replace only the transport for this isolated run.
        ai._generate = injected_generate  # type: ignore[method-assign]
        started = time.perf_counter()
        try:
            decision = await ai.decide_conversation_turn(
                turn["user_text"], context=turn["context"]
            )
            wall_ms = (time.perf_counter() - started) * 1000
            score = score_decision(turn["expected"], decision)
            status = "accepted" if decision is not None else "rejected_by_application_validator"
            error = None
        except Exception as exc:  # noqa: BLE001 - one provider failure must not stop the corpus
            wall_ms = (time.perf_counter() - started) * 1000
            decision = None
            score = score_decision(turn["expected"], None)
            status = "provider_error"
            error = _safe_error(exc)

        usage = call_record.get("usage", {})
        results.append({
            "alias": aliases[provider.key],
            "case_id": case["id"],
            "scenario": case["scenario"],
            "language": case["language"],
            "turn": turn_index,
            "repetition": repetition,
            "status": status,
            "wall_duration_ms": round(wall_ms, 2),
            "provider_duration_ms": round(call_record.get("duration_ms", wall_ms), 2),
            "usage": usage,
            "estimated_cost_usd": _cost_usd(usage, rates.get(provider.model)),
            "finish_reason": call_record.get("finish_reason"),
            "error": error,
            "raw_output": call_record.get("raw_text"),
            "accepted_decision": decision,
            "gold": turn["expected"],
            "score": score,
        })

    def sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction))))
        return round(ordered[index], 2)

    summary_by_alias: dict[str, Any] = {}
    for alias in sorted(set(aliases.values())):
        rows = [row for row in results if row["alias"] == alias]
        accepted = [row for row in rows if row["status"] == "accepted"]
        durations = [float(row["provider_duration_ms"]) for row in rows if row.get("provider_duration_ms") is not None]
        known_costs = [float(row["estimated_cost_usd"]) for row in rows if row.get("estimated_cost_usd") is not None]
        summary_by_alias[alias] = {
            "requests": len(rows),
            "accepted_by_application_validator": len(accepted),
            "acceptance_rate": round(len(accepted) / len(rows), 4) if rows else None,
            "exact_decision_rate": round(sum(bool(row["score"]["all_exact"]) for row in rows) / len(rows), 4) if rows else None,
            "action_exact_rate": round(sum(bool(row["score"]["action_exact"]) for row in rows) / len(rows), 4) if rows else None,
            "latency_p50_ms": percentile(durations, 0.50),
            "latency_p95_ms": percentile(durations, 0.95),
            "input_tokens_total_known": sum(int(row["usage"].get("input_tokens") or 0) for row in rows),
            "output_tokens_total_known": sum(int(row["usage"].get("output_tokens") or 0) for row in rows),
            "cost_usd_total_known": round(sum(known_costs), 8) if known_costs else None,
            "cost_requests_covered": len(known_costs),
        }

    def grouped_summary(group_key: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in results:
            key = f"{row['alias']}:{row[group_key]}"
            groups.setdefault(key, []).append(row)
        summaries: dict[str, Any] = {}
        for key, rows in groups.items():
            accepted = [row for row in rows if row["status"] == "accepted"]
            durations = [float(row["provider_duration_ms"]) for row in rows if row.get("provider_duration_ms") is not None]
            summaries[key] = {
                "requests": len(rows),
                "accepted": len(accepted),
                "acceptance_rate": round(len(accepted) / len(rows), 4),
                "exact_decision_rate": round(sum(bool(row["score"]["all_exact"]) for row in rows) / len(rows), 4),
                "action_exact_rate": round(sum(bool(row["score"]["action_exact"]) for row in rows) / len(rows), 4),
                "latency_p50_ms": percentile(durations, 0.50),
                "latency_p95_ms": percentile(durations, 0.95),
                "input_tokens_known": sum(int(row["usage"].get("input_tokens") or 0) for row in rows),
                "output_tokens_known": sum(int(row["usage"].get("output_tokens") or 0) for row in rows),
                "cost_usd_known": round(sum(float(row["estimated_cost_usd"]) for row in rows if row.get("estimated_cost_usd") is not None), 8) if any(row.get("estimated_cost_usd") is not None for row in rows) else None,
            }
        return summaries

    manifest = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": DATASET_VERSION,
        "seed": seed,
        "repeat": repeat,
        "primary_attempt_timeout_seconds": timeout_seconds,
        "retries": 0,
        "fallbacks": 0,
        "prompt_sha256": sha256(CONVERSATION_TURN_PROMPT),
        "dataset_sha256": sha256(json.dumps(DIALOGUE_CASES, ensure_ascii=False, sort_keys=True)),
        "provider_aliases": sorted(set(aliases.values())),
        "note": "Model names are in the separate blind-map file. This is interpreter-only; no selector candidate pools are included.",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({
            "manifest": manifest,
            "summary_by_alias": summary_by_alias,
            "summary_by_alias_scenario": grouped_summary("scenario"),
            "summary_by_alias_language": grouped_summary("language"),
            "results": results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    map_path.write_text(json.dumps(alias_map, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="eval-consultant-blind-results.json")
    parser.add_argument("--map-output", default="eval-consultant-blind-map.json")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=20261106)
    parser.add_argument("--rates-file", default=None, help="Optional dated JSON rate table keyed by exact model ID.")
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 10:
        parser.error("--repeat must be between 1 and 10")
    if args.timeout_seconds <= 0 or args.timeout_seconds > 30:
        parser.error("--timeout-seconds must be >0 and <=30")

    providers = configured_providers()
    rates = _load_rates(args.rates_file)
    asyncio.run(run_benchmark(
        providers,
        output_path=Path(args.output),
        map_path=Path(args.map_output),
        repeat=args.repeat,
        timeout_seconds=args.timeout_seconds,
        seed=args.seed,
        rates=rates,
    ))


if __name__ == "__main__":
    main()
