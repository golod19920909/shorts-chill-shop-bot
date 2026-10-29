# -*- coding: utf-8 -*-
"""Offline probe: intent + variants for all B5 dataset cases (no network)."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.ai_service import AIService  # noqa: E402
from services.query_intent import apply_intent, fallback_intent, parse_query_intent  # noqa: E402
from services.query_variants import product_query_variants  # noqa: E402
from eval_b5_dataset import B5_DATASET  # noqa: E402


async def main() -> None:
    ai = AIService(None)
    out: list[str] = []
    for case in B5_DATASET:
        q = case["query"]
        out.append("=" * 78)
        out.append(f"{case['id']} [{case['expected_outcome_hint']}] {q}")
        fb = fallback_intent(q)
        out.append(f"  fallback.product_type : {fb.product_type!r}")
        out.append(f"  fallback.required     : {fb.required_attributes!r}")
        out.append(f"  fallback.excluded     : {fb.excluded_product_types!r}")
        out.append(f"  fallback.compat      : {fb.compatibility!r}")
        out.append(f"  fallback.budget       : {fb.budget.model_dump()}")
        req = await ai.parse_query(q)
        out.append(f"  parsed.normalized     : {req.normalized_query!r}")
        out.append(f"  parsed.max_price      : {req.max_price} {req.currency}")
        try:
            intent, meta = await parse_query_intent(q, ai)
            req = apply_intent(req, intent, intent_source=meta.source)
            out.append(f"  intent.source         : {meta.source}")
            out.append(f"  intent.product        : {intent.product_type!r}")
            out.append(f"  intent.required       : {intent.required_attributes!r}")
            out.append(f"  intent.excluded       : {intent.excluded_product_types!r}")
            out.append(f"  intent.compat        : {intent.compatibility!r}")
            out.append(f"  intent.budget         : {intent.budget.model_dump()}")
        except Exception as exc:  # noqa: BLE001
            out.append(f"  intent ERROR          : {exc!r}")
        out.append(f"  final.normalized      : {req.normalized_query!r}")
        out.append(f"  final.max_price       : {req.max_price} {req.currency}")
        out.append(f"  final.intent_product  : {getattr(req, 'intent_product_type', None)!r}")
        out.append(f"  final.intent_required : {getattr(req, 'intent_required_attributes', None)!r}")
        out.append(f"  final.intent_excluded : {getattr(req, 'intent_excluded_product_types', None)!r}")
        out.append(f"  final.intent_compat   : {getattr(req, 'intent_compatibility', None)!r}")
        out.append(f"  variants              : {product_query_variants(req)!r}")
    Path("tmp_b5_probe_intent_out.txt").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


asyncio.run(main())
