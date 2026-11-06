"""TLS and Certificate management routes."""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from core.database import DeviceRegistry, RelayedConnection
from core.routes.common import _client_error

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tls"])


def _server():
    from core import server

    return server


@router.get("/api/tls/ca-cert")
async def tls_download_ca():
    """Download the CA certificate (PEM) for installation on devices."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        ca_pem = _server().cert_manager.ca_cert_pem()
        return Response(
            content=ca_pem,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": "attachment; filename=ride-the-api-ca.pem"},
        )
    except Exception:
        logger.exception("Handler error (500)")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.get("/api/tls/stats")
async def tls_stats():
    """Return TLS/MITM statistics."""
    stats = {"cert_manager": {}, "mitm_server": False, "device_ports": [], "open_relays": 0}
    if _server().cert_manager:
        stats["cert_manager"] = _server().cert_manager.stats()
    if _server().tls_mitm_server:
        stats["mitm_server"] = True
        stats["listen_ports"] = _server().tls_mitm_server.listen_ports.copy()
        stats["device_ports"] = [
            {
                "ip": dp.ip,
                "port": dp.port,
                "device_id": dp.device_id,
                "first_seen": dp.first_seen.isoformat(),
                "last_seen": dp.last_seen.isoformat(),
            }
            for dp in _server().tls_mitm_server.device_ports.values()
        ]
        stats["open_relays"] = len(_server().tls_mitm_server.get_live_relays())
    return stats


@router.get("/api/tls/relayed")
async def tls_relayed(limit: int = 50):
    """List connections forwarded without decryption, newest first.

    A device that pins its vendor's certificate never completes a handshake with us, so nothing of
    its payload can be read. Its contacts can still be listed -- who, which host, how many bytes in
    each direction, how long, and whether the attempt succeeded. This is the visible trace of a
    device the proxy carries but cannot see inside.

    A contact is recorded when the connection **ends**, and such a device can hold one session open
    for hours: ``in_progress`` lists the ones being forwarded at this instant (with their byte
    counts so far), because otherwise a device that is working looks like a device that is silent.
    """
    live = _server().tls_mitm_server.get_live_relays() if _server().tls_mitm_server else []
    if not _server().db_manager:
        return {"relayed": [], "total": 0, "in_progress": live, "open": len(live)}
    limit = max(1, min(int(limit), 500))
    async with _server().db_manager.core_session() as session:
        total = (await session.execute(select(func.count(RelayedConnection.id)))).scalar_one()
        result = await session.execute(
            select(RelayedConnection).order_by(RelayedConnection.started_at.desc()).limit(limit)
        )
        rows = [
            {
                "device_id": row.device_id,
                "client_ip": row.client_ip,
                "server_name": row.server_name,
                "destination_port": row.destination_port,
                "upstream_ip": row.upstream_ip,
                "bytes_to_cloud": row.bytes_to_cloud,
                "bytes_to_device": row.bytes_to_device,
                "duration_ms": row.duration_ms,
                "outcome": row.outcome,
                "started_at": row.started_at.isoformat() if row.started_at else None,
            }
            for row in result.scalars().all()
        ]
    return {"relayed": rows, "total": total, "in_progress": live, "open": len(live)}


@router.get("/api/tls/device-ports")
async def tls_device_ports():
    """Return IP-to-device mapping for all connected devices."""
    if not _server().tls_mitm_server:
        return {"devices": []}
    devices = []
    for ip, info in _server().tls_mitm_server.device_ports.items():
        devices.append(
            {
                "ip": info.ip,
                "port": info.port,
                "device_id": info.device_id,
                "first_seen": info.first_seen.isoformat(),
                "last_seen": info.last_seen.isoformat(),
            }
        )
    return {"devices": devices}


@router.get("/api/tls/unidentified")
async def tls_unidentified():
    """List device IDs starting with 'ip-' (auto-created by TLS handler)."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    devices = await _server().db_manager.list_devices()
    unidentified = [d for d in devices if d.get("device_id", "").startswith("ip-")]
    return {"unidentified": unidentified}


async def _move_device_db_to_name(device_id: str, new_name: str) -> None:
    """Move a device's database file so it follows the device's name.

    A failure to move the file must not fail the rename that already happened in
    the registry: the device keeps working on its current file, and the reason is
    logged. The common failure is a name already taken by another device's file.
    """
    try:
        if _server().db_manager is None:
            return
        await _server().db_manager.rename_device_database(device_id, new_name)
    except ValueError as e:
        logger.warning(f"Device {device_id} renamed, database not moved: {e}")
    except OSError as e:
        logger.warning(f"Device {device_id} renamed, database file not moved: {e}")


@router.put("/api/devices/{device_id}/tls-config")
async def tls_update_device_config(device_id: str, request: Request):
    """Update TLS config for a specific device (name, vendor, passthrough, pinning_bypass)."""
    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})
    try:
        body = await request.json()
        async with _server().db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return JSONResponse(status_code=404, content={"error": "Device not found"})
            renamed_to = None
            if "name" in body and body["name"]:
                if body["name"] != device.name:
                    renamed_to = body["name"]
                device.name = body["name"]
            if "vendor" in body and body["vendor"]:
                device.vendor = body["vendor"]
            extra = dict(device.extra_attributes or {})
            if "passthrough" in body:
                extra["tls_passthrough"] = body["passthrough"]
            if "pinning_bypass" in body:
                extra["tls_pinning_bypass"] = body["pinning_bypass"]
            device.extra_attributes = extra
            session.add(device)
            await session.commit()
            _server().db_manager.invalidate_device_cache(device_id)
            if renamed_to:
                await _move_device_db_to_name(device_id, renamed_to)
            return {"status": "ok", "device_id": device_id}
    except Exception as e:
        return _client_error(e, "Invalid TLS configuration payload")


@router.get("/api/tls/ports")
async def tls_list_ports():
    """List all currently active TLS listen ports (read-only).

    Ports are configured ONLY in config.yaml (``tls_decrypt.listen_ports``)
    and are never added/removed at runtime. Edit config.yaml and restart the
    service to change the set of monitored ports.
    """
    if not _server().tls_mitm_server:
        return {"ports": [], "enabled": False}
    return {"ports": _server().tls_mitm_server.listen_ports.copy(), "enabled": True}


@router.get("/api/tls/frida/script.js")
async def tls_frida_script():
    """Return a Frida script for bypassing certificate pinning."""
    script = "// Ride the API — Certificate Pinning Bypass (Frida)\n// Usage: frida -U -l script.js <app>\n// Requires: Frida installed on device, CA cert installed\n\nJava.perform(function() {\n    // Common SSL pinning bypass targets\n    var X509TrustManager = Java.use('javax.net.ssl.X509TrustManager');\n    var SSLContext = Java.use('javax.net.ssl.SSLContext');\n\n    // Bypass checkClientTrusted / checkServerTrusted\n    X509TrustManager.checkClientTrusted.implementation = function(chain, authType) {\n        console.log('[RideTheAPI] Bypassing checkClientTrusted');\n    };\n    X509TrustManager.checkServerTrusted.implementation = function(chain, authType) {\n        console.log('[RideTheAPI] Bypassing checkServerTrusted');\n    };\n    X509TrustManager.getAcceptedIssuers.implementation = function() {\n        console.log('[RideTheAPI] Returning empty accepted issuers');\n        return [];\n    };\n\n    // Hook OkHttp HostnameVerifier\n    try {\n        var HostnameVerifier = Java.use('javax.net.ssl.HostnameVerifier');\n        HostnameVerifier.verify.implementation = function(hostname, session) {\n            console.log('[RideTheAPI] Bypassing hostname verification for: ' + hostname);\n            return true;\n        };\n    } catch(e) { console.log('[RideTheAPI] HostnameVerifier not found'); }\n\n    // Hook OkHttp CertificatePinner\n    try {\n        var CertificatePinner = Java.use('okhttp3.CertificatePinner');\n        CertificatePinner.check.implementation = function(pin) {\n            console.log('[RideTheAPI] Bypassing certificate pin for: ' + pin);\n        };\n    } catch(e) { console.log(\"[RideTheAPI] CertificatePinner not found\"); }\n\n    console.log(\"[RideTheAPI] Pinning bypass injected successfully\");\n});\n"
    return Response(content=script, media_type="application/javascript")


@router.get("/api/tls/certs")
async def tls_list_certs():
    """List all imported and auto-generated certificates."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        imported = _server().cert_manager.list_imported_certs()
        return {"certs": imported}
    except Exception:
        logger.exception("Handler error (500)")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.get("/api/tls/certs/{hostname}")
async def tls_get_cert_info(hostname: str):
    """Get detailed info about a specific certificate."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        info = _server().cert_manager.get_cert_info(hostname)
        if not info:
            return JSONResponse(
                status_code=404, content={"error": f"No certificate found for '{hostname}'"}
            )
        return {"cert": info}
    except ValueError as e:
        return _client_error(e, "Invalid certificate lookup request")
    except Exception:
        logger.exception("Handler error (500)")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.post("/api/tls/certs/upload")
async def tls_upload_cert(
    hostname: str = Form(...), cert: UploadFile = File(...), key: UploadFile = File(...)
):
    """Upload a certificate + private key (PEM format) for a specific hostname."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        cert_pem = await cert.read()
        key_pem = await key.read()
        _server().cert_manager.import_cert(
            hostname, cert_pem.decode("utf-8"), key_pem.decode("utf-8")
        )
        return {
            "status": "ok",
            "hostname": hostname,
            "message": f"Certificate imported for '{hostname}'",
        }
    except (ValueError, UnicodeDecodeError) as e:
        return _client_error(
            e, "Invalid certificate upload. Provide a valid PEM or DER certificate file."
        )
    except Exception:
        logger.exception("Failed to upload cert")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.post("/api/tls/certs/upload-json")
async def tls_upload_cert_json(request: Request):
    """Upload a certificate + private key as JSON (base64-encoded PEM)."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        body = await request.json()
        hostname = body.get("hostname")
        cert_b64 = body.get("cert_base64")
        key_b64 = body.get("key_base64")
        if not all([hostname, cert_b64, key_b64]):
            return JSONResponse(
                status_code=400,
                content={"error": "Missing 'hostname', 'cert_base64', or 'key_base64'"},
            )
        cert_pem = base64.b64decode(cert_b64).decode("utf-8")
        key_pem = base64.b64decode(key_b64).decode("utf-8")
        _server().cert_manager.import_cert(hostname, cert_pem, key_pem)
        return {"status": "ok", "hostname": hostname}
    except (ValueError, UnicodeDecodeError, binascii.Error) as e:
        return _client_error(
            e, "Invalid certificate payload. Provide a valid PEM or DER certificate."
        )
    except Exception:
        logger.exception("Failed to upload cert via JSON")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.delete("/api/tls/certs/{hostname}")
async def tls_delete_cert(hostname: str):
    """Delete an imported certificate, reverting to auto-generated."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        deleted = _server().cert_manager.delete_cert(hostname)
        if not deleted:
            return JSONResponse(
                status_code=404,
                content={"error": f"No imported certificate found for '{hostname}'"},
            )
        return {
            "status": "ok",
            "hostname": hostname,
            "message": f"Certificate deleted for '{hostname}'",
        }
    except ValueError as e:
        return _client_error(e, "Invalid certificate deletion request")
    except Exception:
        logger.exception("Failed to delete cert")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.post("/api/tls/certs/{hostname}/rotate")
async def tls_rotate_cert(hostname: str, request: Request):
    """Replace an existing certificate without downtime."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        body = await request.json()
        cert_b64 = body.get("cert_base64")
        key_b64 = body.get("key_base64")
        if not all([cert_b64, key_b64]):
            return JSONResponse(
                status_code=400, content={"error": "Missing 'cert_base64' or 'key_base64'"}
            )
        cert_pem = base64.b64decode(cert_b64).decode("utf-8")
        key_pem = base64.b64decode(key_b64).decode("utf-8")
        _server().cert_manager.import_cert(hostname, cert_pem, key_pem)
        return {
            "status": "ok",
            "hostname": hostname,
            "message": f"Certificate rotated for '{hostname}'",
        }
    except (ValueError, UnicodeDecodeError, binascii.Error) as e:
        return _client_error(
            e, "Could not rotate the certificate. Check the certificate and CA configuration."
        )
    except Exception:
        logger.exception("Failed to rotate cert")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.post("/api/tls/root-ca/download")
async def tls_download_root_ca():
    """Download the root CA certificate for manual installation on devices."""
    if not _server().cert_manager:
        return JSONResponse(status_code=503, content={"error": "Cert manager not ready"})
    try:
        ca_pem = _server().cert_manager.ca_cert_pem()
        return Response(
            content=ca_pem,
            media_type="application/x-pem-file",
            headers={"Content-Disposition": "attachment; filename=ride-the-api-ca.pem"},
        )
    except Exception:
        logger.exception("Handler error (500)")
        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})
