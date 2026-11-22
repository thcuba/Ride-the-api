"""
Example Protocol Adapter package.

Exports ExampleProtocolAdapter and PROTOCOL_DEFINITION.
"""

from __future__ import annotations

from adapters.example.adapter import (
    ExampleProtocolAdapter,
)
from adapters.example.definition import PROTOCOL_DEFINITION

__all__ = [
    "ExampleProtocolAdapter",
    "PROTOCOL_DEFINITION",
]
