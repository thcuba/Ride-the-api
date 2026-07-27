"""
Adapters package - Auto-register all vendor adapters.
"""

from adapters.base import ProtocolAdapterRegistry, get_adapter_registry


def register_all_adapters() -> ProtocolAdapterRegistry:
    """Register all available vendor adapters."""
    registry = get_adapter_registry()
    
    # Import and register each adapter (lazy imports to avoid circular dependencies)
    from adapters.ty import TYProtocolAdapter
    from adapters.tl import TLProtocolAdapter
    from adapters.zh import ZHProtocolAdapter
    from adapters.hr import HRProtocolAdapter
    
    # TY (Tuya)
    ty_adapter = TYProtocolAdapter("ty", {
        "region": "eu",
        "api_version": "v1.0",
    })
    registry.register(ty_adapter)
    
    # TL (TP-Link)
    tl_adapter = TLProtocolAdapter("tl", {})
    registry.register(tl_adapter)
    
    # ZH (Zehnder)
    zh_adapter = ZHProtocolAdapter("zh", {})
    registry.register(zh_adapter)
    
    # HR (Haier)
    hr_adapter = HRProtocolAdapter("hr", {})
    registry.register(hr_adapter)
    
    return registry


# Auto-register on import
_adapter_registry = None


def get_registered_registry() -> ProtocolAdapterRegistry:
    """Get registry with all adapters registered."""
    global _adapter_registry
    if _adapter_registry is None:
        _adapter_registry = register_all_adapters()
    return _adapter_registry