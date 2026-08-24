## 2026-03-31 - Pre-computed Sets for Validation Hot Paths
**Learning:** Dynamic set comprehensions like `{m.upper() for m in valid}` executed repeatedly during batch capture file validation introduce unnecessary allocations and overhead (~2.2x slower) compared to pre-computed module-level lookup dictionaries.
**Action:** Check validation and routing hot paths for repeated set constructions and pre-compute uppercase/lowercased lookups at module or init level.
