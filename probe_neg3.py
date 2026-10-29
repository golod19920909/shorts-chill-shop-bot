import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0,".")
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("t_si", "tests/test_search_intent.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from services.query_variants import product_query_variants
for q in ["микроволновка без гриля и без функции разморозки",
          "микроволновка без гриля, без разморозки",
          "пылесос без мешка и без щетки",
          "наушники без проводов",
          "микроволновка"]:
    r,_,_ = m.resolve_and_apply(q)
    print("Q:", q)
    print("  normalized:", r.normalized_query)
    print("  keywords:", r.keywords)
    print("  pt:", r.intent_product_type)
    print("  excl_types:", r.intent_excluded_product_types)
    print("  excl_feats:", r.excluded_features)
    print("  req_feats:", r.required_features)
    print("  variants:", product_query_variants(r))
