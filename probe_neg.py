from services.query_intent import fallback_intent, _detect_exclusions
qs = ["микроволновка без гриля и без функции разморозки",
      "пылесос без мешка",
      "наушники без проводов и без шумоподавления",
      "микроволновка без гриля",
      "чайник без подсветки и без температурного режима"]
for q in qs:
    print("Q:", q)
    print("  exc:", _detect_exclusions(q))
    it = fallback_intent(q)
    print("  product_type:", it.product_type, "| req:", it.required_attributes, "| exc:", it.excluded_product_types, "| compat:", it.compatibility)
