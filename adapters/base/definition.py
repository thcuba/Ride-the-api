"""Unified protocol descriptor binding an adapter normalizer and/or network server plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from adapters.base.adapter import ProtocolAdapter


@dataclass
class ProtocolDefinition:
    """Unified descriptor binding an adapter normalizer and/or a network server plugin.

    Enables protocol bundles (e.g. MQTT listener + MQTT payload normalizer) to be
    defined together and discovered dynamically via entry points or direct registration.
    """

    name: str
    adapter_class: type[ProtocolAdapter] | None = None
    server_plugin_class: type | None = None
    config_attr: str | None = None
    adapter_config: dict[str, Any] = field(default_factory=dict)
    adapter_vendor: str | None = None
    description: str = ""

    def create_adapter(self, config: dict[str, Any] | None = None) -> ProtocolAdapter | None:
        """Instantiate the protocol adapter if defined."""
        if not self.adapter_class:
            return None
        cfg = self.adapter_config if config is None else config
        vendor = self.adapter_vendor or self.name
        return self.adapter_class(vendor, cfg)

    def create_server_plugin(
        self,
        servers_config: object,
        handler: Callable | None = None,
    ) -> object | None:
        """Instantiate the network server plugin if defined."""
        if not self.server_plugin_class:
            return None
        attr = self.config_attr or self.name
        sub_cfg = getattr(servers_config, attr, None)
        target_cfg = sub_cfg if sub_cfg is not None else servers_config
        try:
            return cast("object", self.server_plugin_class(target_cfg, handler=handler))
        except TypeError:
            try:
                return cast("object", self.server_plugin_class(target_cfg))
            except TypeError:
                return cast("object", self.server_plugin_class())
