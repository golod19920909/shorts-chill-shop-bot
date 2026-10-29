import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
from services.query_intent import fallback_intent, _detect_exclusions

queries = [
    "микроволновка без гриля и без функции разморозки",
    "микроволновка без гриля",
    "чайник без подсветки и без функции отключения",
    "без гриля и без функции разморозки",
]
for q in queries:
    print("QUERY:", q)
    print("  detect:", _detect_exclusions(q))
    i = fallback_intent(q)
    print("  intent excluded:", i.excluded_product_types, "| pt:", i.product_type, "| req:", i.required_attributes, "| compat:", i.compatibility)
