"""Why a device is (or is not) producing buffered pairs — in one word, plus the reason.

A healthy proxy can still capture nothing for a device, and every cause is a *different*
switch: the IP is in per-IP bypass, TLS passthrough is on (configured, or set automatically
because the device refused our certificate), or the device mode is passthrough. Reading the
switch that is off out of four separate fields — three of which live in different places —
was exactly what made "no data in the buffer" look like a broken pipeline.

The mapping is a pure function of the device's configuration so it can be unit-tested and so
the API, the dashboard and the docs all tell the same story.
"""

from __future__ import annotations

from typing import Final

#: States, in the order the runtime would hit them.
CAPTURING: Final = "capturing"
BYPASSED: Final = "bypassed"
RELAYED: Final = "relayed"
PASSTHROUGH: Final = "passthrough"

#: Why a device's TLS is relayed, from ``extra_attributes.tls_untrusted_reason``.
UNTRUSTED_REASONS: Final[dict[str, str]] = {
    "client_refused_certificate": (
        "the device aborted the handshake because it does not trust the Ride CA "
        "(certificate pinning on the device side): its TLS is relayed encrypted"
    ),
}

#: Reasons for a relay that the operator asked for themselves.
CONFIGURED_RELAY: Final = "configured"

_PASSTHROUGH_MODES: Final[frozenset[str]] = frozenset({"passthrough"})


def describe_capture(  # noqa: PLR0913 — keyword-only, one parameter per documented fact
    *,
    bypass: bool = False,
    mode: str | None = None,
    tls_passthrough: bool = False,
    untrusted_reason: str | None = None,
    refusals: int | None = None,
    last_refusal_at: str | None = None,
) -> dict[str, str | int]:
    """Describe a device's capture outlook as ``{"state", "reason", "hint"}``.

    The order mimics the runtime: a bypassed IP never reaches the pipeline, a relayed
    connection is settled during the handshake (before any mode is consulted), and only
    then does the device mode decide whether pairs are correlated.

    ``state == CAPTURING`` (with an empty ``reason``) means nothing is in the way: the
    device *can* produce pairs, and a device that still shows none simply is not talking
    to the proxy.

    ``refusals``/``last_refusal_at`` are context, not a cause: for a relayed device they
    say how many times it refused our certificate and when it last did, which separates a
    device that refused once and is relayed from then on from one still trying to
    negotiate. They are reported only when known and non-zero, so the payload stays
    ``{"state", "reason", "hint"}`` for the ordinary cases.
    """
    if bypass:
        return {
            "state": BYPASSED,
            "reason": "ip_bypass",
            "hint": (
                "this IP is in per-IP bypass: its traffic is forwarded straight to the "
                "cloud with no analysis and nothing is buffered"
            ),
        }

    if tls_passthrough:
        reason = (untrusted_reason or CONFIGURED_RELAY).strip()
        hint = UNTRUSTED_REASONS.get(
            reason,
            "TLS passthrough is on for this device: its connections are forwarded "
            "encrypted and no HTTP pairs are extracted",
        )
        described: dict[str, str | int] = {"state": RELAYED, "reason": reason, "hint": hint}
        if refusals:
            described["refusals"] = refusals
        if last_refusal_at:
            described["last_refusal_at"] = last_refusal_at
        return described

    if (mode or "").strip().lower() in _PASSTHROUGH_MODES:
        return {
            "state": PASSTHROUGH,
            "reason": "mode_passthrough",
            "hint": (
                "the device mode is passthrough: requests are forwarded to the cloud "
                "without analysis or buffering"
            ),
        }

    hint = (
        "requests are analysed and buffered. In production and hybrid mode only requests "
        "without a local match are captured; a device that produces nothing is not "
        "reaching the proxy at all (DNS, topology, or no traffic)"
    )
    return {"state": CAPTURING, "reason": "", "hint": hint}
