from __future__ import annotations

from htdt.limits import (
    MAX_ATTACHMENT_BASE64_CHARS,
    MAX_REW_TEXT_BASE64_CHARS,
    max_base64_chars,
)
from htdt.models import AttachmentCreate, ImportPreviewRequest


def field_max_length(model: type, field_name: str) -> int | None:
    field = model.model_fields[field_name]
    for constraint in field.metadata:
        value = getattr(constraint, 'max_length', None)
        if value is not None:
            return int(value)
    return None


def test_base64_length_formula() -> None:
    assert max_base64_chars(0) == 0
    assert max_base64_chars(1) == 4
    assert max_base64_chars(3) == 4
    assert max_base64_chars(4) == 8


def test_models_expose_endpoint_specific_base64_caps() -> None:
    assert field_max_length(ImportPreviewRequest, 'raw_base64') == MAX_REW_TEXT_BASE64_CHARS
    assert field_max_length(AttachmentCreate, 'raw_base64') == MAX_ATTACHMENT_BASE64_CHARS
