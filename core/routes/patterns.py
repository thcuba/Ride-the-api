"""Device patterns management, editing, import, and export routes."""

from __future__ import annotations


import logging


import uuid


from fastapi import APIRouter, Request


from fastapi.responses import JSONResponse


from sqlalchemy import delete, select


from core.database import DeviceRegistry, FieldMapping, RequestPattern, ResponseTemplate


from core.pattern_db import decipher_ingest


from pydantic import ValidationError


from core.pattern_db import decipher_ingest


from core.pattern_db.schemas import DeviceModel, PatternDB


from core.pattern_db.validator import validate_pattern


from pydantic import ValidationError

from core.pattern_db import decipher_ingest

from core.pattern_db.schemas import DeviceModel, PatternDB

from core.pattern_db.validator import validate_pattern

from core.routes.common import _client_error


logger = logging.getLogger(__name__)


router = APIRouter(tags=["patterns"])


def _server():

    from core import server

    return server


@router.get("/api/devices/{device_id}/patterns")
async def get_device_patterns(device_id: str):
    """Get learned patterns for a device."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    async with _server().db_manager.device_session(device_id) as session:
        patterns = await session.execute(
            select(RequestPattern).order_by(RequestPattern.confidence.desc())
        )

        pattern_rows = patterns.scalars().all()

        templates = await session.execute(
            select(ResponseTemplate).where(
                ResponseTemplate.pattern_id.in_([rp.pattern_id for rp in pattern_rows])
            )
        )

        template_by_pattern = {t.pattern_id: t for t in templates.scalars().all()}

        patterns_list = []

        for pattern_row in pattern_rows:
            tpl = template_by_pattern.get(pattern_row.pattern_id)

            patterns_list.append(
                {
                    "pattern_id": pattern_row.pattern_id,
                    "method": pattern_row.method,
                    "path": pattern_row.path_pattern,
                    "path_pattern": pattern_row.path_pattern,
                    "protocol": pattern_row.protocol,
                    "intent": pattern_row.intent,
                    "confidence": pattern_row.confidence,
                    "hit_count": pattern_row.hit_count,
                    "required_headers": pattern_row.required_headers,
                    "body_schema": pattern_row.body_schema,
                    "query_param_keys": pattern_row.query_param_keys,
                    "response_template": {
                        "status_code": tpl.status_code,
                        "body_template": tpl.body_template,
                        "headers_template": tpl.headers_template,
                        "field_mappings": tpl.field_mappings,
                        "expected_variables": tpl.expected_variables,
                    }
                    if tpl
                    else None,
                }
            )

        return {"device_id": device_id, "patterns": patterns_list}


@router.get("/api/devices/{device_id}/patterns/export")
async def export_patterns(device_id: str):
    """Export deciphered patterns to portable .ride-pattern.json format."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    ingester = decipher_ingest.DecipherIngest(_server().db_manager)

    try:
        async with _server().db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )

            device = result.scalar_one_or_none()

            vendor = device.vendor if device else "unknown"

            device_type = device.device_type if device else "unknown"

        pattern_db = await ingester.export_patterns(device_id, vendor, device_type)

        header = await _server().db_manager.read_device_meta(device_id)

        if header and (header.get("protocols") or header.get("transport")):
            device_model = await ingester.export_device_model(device_id, vendor, device_type)

            return device_model.model_dump(by_alias=True, exclude_none=True)

        return pattern_db.model_dump(by_alias=True, exclude_none=True)

    except Exception:
        logger.exception("Handler error (500)")

        return JSONResponse(status_code=500, content={"error": "An internal error occurred"})


@router.get("/api/devices/{device_id}/patterns/{pattern_id}")
async def get_pattern_detail(device_id: str, pattern_id: str):
    """Get detailed pattern info including field mappings."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    async with _server().db_manager.device_session(device_id) as session:
        result = await session.execute(
            select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
        )

        pattern = result.scalar_one_or_none()

        if not pattern:
            return JSONResponse(status_code=404, content={"error": "Pattern not found"})

        tpl_result = await session.execute(
            select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id)
        )

        template = tpl_result.scalar_one_or_none()

        mappings_result = await session.execute(
            select(FieldMapping).where(FieldMapping.intent == pattern.intent)
        )

        mappings = [
            {
                "request_field": m.request_field,
                "response_field": m.response_field,
                "transform": m.transform,
                "confidence": m.confidence,
            }
            for m in mappings_result.scalars().all()
        ]

        return {
            "pattern": {
                "pattern_id": pattern.pattern_id,
                "method": pattern.method,
                "path_pattern": pattern.path_pattern,
                "protocol": pattern.protocol,
                "required_headers": pattern.required_headers,
                "body_schema": pattern.body_schema,
                "intent": pattern.intent,
                "confidence": pattern.confidence,
                "hit_count": pattern.hit_count,
            },
            "response_template": {
                "status_code": template.status_code,
                "headers_template": template.headers_template,
                "body_template": template.body_template,
                "field_mappings": template.field_mappings,
                "expected_variables": template.expected_variables,
            }
            if template
            else None,
            "field_mappings": mappings,
        }


async def _resync_device_patterns(device_id: str) -> None:
    """Make the engine serve the device's patterns as now stored.







    The engine serves its in-memory set exclusively when it has one, so an



    API change to the device DB alone left deleted or edited patterns being



    served as before. Best-effort: the DB change already succeeded.



    """

    if _server().orchestrator is None:
        return

    try:
        await _server().orchestrator.sync_engine(device_id)

    except Exception:
        logger.exception("Could not refresh the served patterns for %s", device_id)


@router.put("/api/devices/{device_id}/patterns/{pattern_id}")
async def put_pattern(device_id: str, pattern_id: str, request: Request):
    """Full update of a request pattern, its response template, and field mappings."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        body = await request.json()

        async with _server().db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
            )

            pattern = result.scalar_one_or_none()

            if not pattern:
                pattern = RequestPattern(
                    pattern_id=pattern_id,
                    method=body.get("method", "GET"),
                    path_pattern=body.get("path", body.get("path_pattern", "")),
                    protocol=body.get("protocol", "http"),
                    required_headers=body.get("required_headers", []),
                    body_schema=body.get("body_schema", {}),
                    query_param_keys=body.get("query_param_keys", []),
                    intent=body.get("intent", ""),
                    confidence=body.get("confidence", 0.0),
                )

            for f in (
                "method",
                "path_pattern",
                "protocol",
                "required_headers",
                "body_schema",
                "query_param_keys",
                "intent",
                "confidence",
            ):
                if f in body:
                    setattr(pattern, f, body[f])

            if "path" in body and "path_pattern" not in body:
                pattern.path_pattern = body["path"]

            session.add(pattern)

            tpl_data = body.get("response_template") or {}

            tpl_result = await session.execute(
                select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id)
            )

            tpl = tpl_result.scalar_one_or_none()

            if not tpl:
                tpl = ResponseTemplate(
                    template_id=str(uuid.uuid4())[:16],
                    pattern_id=pattern_id,
                    status_code=tpl_data.get("status_code", 200),
                    headers_template=tpl_data.get("headers_template", {}),
                    body_template=tpl_data.get("body_template", {}),
                    field_mappings=tpl_data.get("field_mappings", {}),
                    expected_variables=tpl_data.get("expected_variables", []),
                )

            else:
                tpl.status_code = tpl_data.get("status_code", tpl.status_code)

                tpl.headers_template = tpl_data.get("headers_template", tpl.headers_template)

                tpl.body_template = tpl_data.get("body_template", tpl.body_template)

                tpl.field_mappings = tpl_data.get("field_mappings", tpl.field_mappings)

                tpl.expected_variables = tpl_data.get("expected_variables", tpl.expected_variables)

            session.add(tpl)

            fm_list = body.get("field_mappings")

            if fm_list is not None:
                await session.execute(
                    delete(FieldMapping).where(FieldMapping.intent == pattern.intent)
                )

                for m in fm_list:
                    mapping_id = m.get("mapping_id") or str(uuid.uuid4())[:16]

                    fm = FieldMapping(
                        mapping_id=mapping_id,
                        request_field=m.get("request_field", ""),
                        request_type=m.get("request_type", ""),
                        response_field=m.get("response_field", ""),
                        response_type=m.get("response_type", ""),
                        transform=m.get("transform"),
                        formula=decipher_ingest.stored_formula(m.get("formula")),
                        enum_values=m.get("enum_values"),
                        intent=pattern.intent,
                        confidence=m.get("confidence", 0.5),
                    )

                    session.add(fm)

            await session.commit()

        await _resync_device_patterns(device_id)

        return {"status": "ok", "pattern_id": pattern_id}

    except Exception as e:
        return _client_error(e, "Invalid pattern update payload")


@router.patch("/api/devices/{device_id}/patterns/{pattern_id}")
async def patch_pattern(device_id: str, pattern_id: str, request: Request):
    """Partial update for a pattern."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        body = await request.json()

        async with _server().db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
            )

            pattern = result.scalar_one_or_none()

            if not pattern:
                return JSONResponse(status_code=404, content={"error": "Pattern not found"})

            for f in (
                "method",
                "path_pattern",
                "protocol",
                "required_headers",
                "body_schema",
                "query_param_keys",
                "intent",
                "confidence",
            ):
                if f in body:
                    setattr(pattern, f, body[f])

            session.add(pattern)

            tpl_data = body.get("response_template")

            if tpl_data is not None:
                tpl_result = await session.execute(
                    select(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id)
                )

                tpl = tpl_result.scalar_one_or_none()

                if not tpl:
                    tpl = ResponseTemplate(
                        template_id=str(uuid.uuid4())[:16],
                        pattern_id=pattern_id,
                        status_code=tpl_data.get("status_code", 200),
                        headers_template=tpl_data.get("headers_template", {}),
                        body_template=tpl_data.get("body_template", {}),
                        field_mappings=tpl_data.get("field_mappings", {}),
                        expected_variables=tpl_data.get("expected_variables", []),
                    )

                else:
                    if "status_code" in tpl_data:
                        tpl.status_code = tpl_data["status_code"]

                    if "headers_template" in tpl_data:
                        tpl.headers_template = tpl_data["headers_template"]

                    if "body_template" in tpl_data:
                        tpl.body_template = tpl_data["body_template"]

                    if "field_mappings" in tpl_data:
                        tpl.field_mappings = tpl_data["field_mappings"]

                    if "expected_variables" in tpl_data:
                        tpl.expected_variables = tpl_data["expected_variables"]

                session.add(tpl)

            fm_list = body.get("field_mappings")

            if fm_list is not None:
                await session.execute(
                    delete(FieldMapping).where(FieldMapping.intent == pattern.intent)
                )

                for m in fm_list:
                    mapping_id = m.get("mapping_id") or str(uuid.uuid4())[:16]

                    fm = FieldMapping(
                        mapping_id=mapping_id,
                        request_field=m.get("request_field", ""),
                        request_type=m.get("request_type", ""),
                        response_field=m.get("response_field", ""),
                        response_type=m.get("response_type", ""),
                        transform=m.get("transform"),
                        formula=decipher_ingest.stored_formula(m.get("formula")),
                        enum_values=m.get("enum_values"),
                        intent=pattern.intent,
                        confidence=m.get("confidence", 0.5),
                    )

                    session.add(fm)

            await session.commit()

        await _resync_device_patterns(device_id)

        return {"status": "ok", "pattern_id": pattern_id}

    except Exception as e:
        return _client_error(e, "Invalid pattern update payload")


@router.delete("/api/devices/{device_id}/patterns/{pattern_id}")
async def delete_pattern(device_id: str, pattern_id: str):
    """Delete a pattern and its associated response template and field mappings."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        async with _server().db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
            )

            pattern = result.scalar_one_or_none()

            if not pattern:
                return JSONResponse(status_code=404, content={"error": "Pattern not found"})

            await session.execute(
                delete(ResponseTemplate).where(ResponseTemplate.pattern_id == pattern_id)
            )

            await session.execute(
                delete(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
            )

            intent_still_used = (
                await session.execute(
                    select(RequestPattern.id)
                    .where(RequestPattern.intent == pattern.intent)
                    .limit(1)
                )
            ).first()

            if intent_still_used is None:
                await session.execute(
                    delete(FieldMapping).where(FieldMapping.intent == pattern.intent)
                )

            await session.commit()

        await _resync_device_patterns(device_id)

        return {"status": "deleted", "pattern_id": pattern_id}

    except Exception as e:
        return _client_error(e, "Could not delete the pattern")


@router.post("/api/devices/{device_id}/patterns/import")
async def import_patterns(device_id: str, request: Request):
    """Import patterns from portable .ride-pattern.json format."""

    if not _server().db_manager:
        return JSONResponse(status_code=503, content={"error": "Service not ready"})

    try:
        body = await request.json()

        result = validate_pattern(body)

        if not result.valid:
            return JSONResponse(
                status_code=422,
                content={"error": "Pattern validation failed", "details": result.to_dict()},
            )

        ingester = decipher_ingest.DecipherIngest(_server().db_manager)

        if "pattern-schema/v2" in body.get("$schema", ""):
            model = DeviceModel.model_validate(body)

            count = await ingester.import_device_model(device_id, model)

        else:
            pattern_db = PatternDB.model_validate(body)

            count = await ingester.import_patterns(device_id, pattern_db)

        await _resync_device_patterns(device_id)

        return {"imported": count, "device_id": device_id, "warnings": result.warnings}

    except ValidationError as e:
        return JSONResponse(
            status_code=422, content={"error": "Pattern validation failed", "details": e.to_dict()}
        )

    except Exception as e:
        return _client_error(e, "Invalid pattern import payload")
