## 2024-10-10 - Device Settings Caching
**Learning:** Performing database queries repeatedly within high-frequency network interception loops (like `handle_response`) causes severe performance degradation due to SQLite database latency and locking.
**Action:** Use an in-memory TTL Cache (like `cachetools.TTLCache`) mapped to the `device_id` to store frequently accessed properties. Always invalidate this cache whenever an associated entity updates in the database.
