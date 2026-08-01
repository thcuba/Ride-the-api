"""
Protocol Server Manager — unified lifecycle & routing for all protocol listeners.

Each protocol server is registered as a *server plugin* — the manager starts/stops
them, exposes their status via API, and routes intercepted requests to the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from core.config import ProtocolServersConfig

logger = logging.getLogger(__name__)


class ProtocolServerPlugin:
    """Base interface for a protocol server plugin."""

    name: str = ""

    def __init__(self, config: Any):
        self.config = config
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the server. Override per plugin."""
        self._running = True

    async def stop(self) -> None:
        """Stop the server. Override per plugin."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def get_status(self) -> dict:
        """Return status dict for API / Web UI."""
        return {"name": self.name, "running": self._running}

    async def update_config(self, **kwargs) -> bool:
        """Update config at runtime. Override to support hot config."""
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        return True


class ProtocolServerManager:
    """Manages all protocol server plugins — start, stop, status, config."""

    def __init__(self, config: ProtocolServersConfig | None = None):
        self._plugins: dict[str, ProtocolServerPlugin] = {}
        self._config = config or ProtocolServersConfig()
        self._callbacks: dict[str, Callable] = {}  # protocol -> request handler

    def register_plugin(self, plugin: ProtocolServerPlugin, handler: Callable | None = None) -> None:
        """Register a protocol plugin."""
        self._plugins[plugin.name] = plugin
        if handler:
            self._callbacks[plugin.name] = handler
        logger.info("Protocol plugin registered: %s", plugin.name)

    def set_request_handler(self, protocol: str, handler: Callable) -> None:
        """Set a request handler for a protocol."""
        self._callbacks[protocol] = handler

    def get_handler(self, protocol: str) -> Callable | None:
        """Get handler for protocol."""
        return self._callbacks.get(protocol)

    def get_plugin(self, name: str) -> ProtocolServerPlugin | None:
        return self._plugins.get(name)

    def list_plugins(self) -> dict[str, ProtocolServerPlugin]:
        return dict(self._plugins)

    async def start_all(self) -> dict[str, str]:
        """Start all enabled plugins."""
        results = {}
        for name, plugin in self._plugins.items():
            if getattr(plugin.config, "enabled", False):
                try:
                    await plugin.start()
                    results[name] = "started"
                    logger.info("Protocol server %s started", name)
                except Exception as e:
                    results[name] = f"error: {e}"
                    logger.error("Failed to start %s: %s", name, e)
            else:
                results[name] = "disabled"
        return results

    async def stop_all(self) -> None:
        """Stop all running plugins."""
        for name, plugin in self._plugins.items():
            if plugin.running:
                try:
                    await plugin.stop()
                    logger.info("Protocol server %s stopped", name)
                except Exception as e:
                    logger.error("Error stopping %s: %s", name, e)

    async def start_plugin(self, name: str) -> bool:
        """Start a specific plugin by name."""
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        try:
            await plugin.start()
            return True
        except Exception as e:
            logger.error("Failed to start %s: %s", name, e)
            return False

    async def stop_plugin(self, name: str) -> bool:
        """Stop a specific plugin by name."""
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        try:
            await plugin.stop()
            return True
        except Exception as e:
            logger.error("Failed to stop %s: %s", name, e)
            return False

    async def get_all_status(self) -> list[dict]:
        """Get status of all plugins — for API / Web UI."""
        results = []
        for name, plugin in self._plugins.items():
            try:
                status = await plugin.get_status()
                results.append(status)
            except Exception as e:
                results.append({"name": name, "running": False, "error": str(e)})
        return results

    async def update_plugin_config(self, name: str, **kwargs) -> bool:
        """Update config for a specific plugin."""
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        return await plugin.update_config(**kwargs)


# Global manager instance
_manager: ProtocolServerManager | None = None


def get_protocol_server_manager(config: ProtocolServersConfig | None = None) -> ProtocolServerManager:
    """Get global protocol server manager instance."""
    global _manager
    if _manager is None:
        _manager = ProtocolServerManager(config)
    return _manager