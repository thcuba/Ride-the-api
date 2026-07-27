# Vendor Name Abbreviation Plan

## Mapping
- **Tuya** → **TY** 
- **TP-Link** → **TL**
- **Zehnder** → **ZH**
- **Haier** → **HR**

## Files to Update
1. `core/config.py` - Config model for vendor names
2. `config/config.yaml` - Configuration vendor references
3. `core/database.py` - Database references
4. `adapters/base/__init__.py` - Adapter registry
4. `adapters/tuya/__init__.py` → `adapters/ty/__init__.py`
5. `adapters/tplink/__init__.py` → `adapters/tl/__init__.py`
6. `adapters/zehnder/__init__.py` → `adapters/zh/__init__.py`
7. `adapters/haier/__init__.py` → `adapters/hr/__init__.py`
8. `adapters/__init__.py` - Registry imports
9. `core/server.py` - Vendor routing
10. `core/traffic_selector.py` - Traffic rules
11. `core/correlation.py` - Correlation engine
12. `core/llm_decipher.py` - LLM prompts
13. `core/modification.py` - Modification rules
14. `core/traffic_analysis.py` - Traffic analysis
15. Tests and docs