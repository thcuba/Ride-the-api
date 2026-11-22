"""
Sniffer package for Ride-the-API.

Provides universal ingestion for external network traffic captures
(HAR, PCAP, PCAPNG) to populate the LLM learning buffer without requiring
custom root CA installation on pinned IoT devices.
"""

from core.sniffer.har_parser import parse_har_data
from core.sniffer.pcap_parser import parse_pcap_data
from core.sniffer.service import SnifferService, detect_capture_format

__all__ = [
    "SnifferService",
    "detect_capture_format",
    "parse_har_data",
    "parse_pcap_data",
]
