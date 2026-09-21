from __future__ import annotations


MIB = 1024 * 1024
GIB = 1024 * MIB

# The browser API currently transports binary inputs as Base64 JSON. These
# limits are deliberately generous for a personal REW workflow while keeping
# accidental or hostile allocations finite.
MAX_REW_TEXT_BYTES = 32 * MIB
MAX_ATTACHMENT_BYTES = 256 * MIB
MAX_BACKUP_ARCHIVE_BYTES = 512 * MIB
MAX_SMALL_JSON_BODY_BYTES = 2 * MIB

# Native measurement ingress is one resource-safety policy with the browser
# transport above, not a separate quota. The native file dialog preflights the
# on-disk REW text size before reading (htdt.ingress.read_file_bounded), and
# already-decoded payloads are re-checked at the parser boundary.
MAX_NATIVE_REW_TEXT_FILE_BYTES = MAX_REW_TEXT_BYTES

# The localhost REW JSON API embeds Base64 float32 arrays. Bounding the raw
# HTTP body and each decoded array keeps JSON/Base64/tuple expansion finite
# even if the endpoint misbehaves. Ordinary REW frequency responses carry a
# few thousand samples per array (384 ppo across ~10 octaves is about 4k), so
# a 1 Mi-sample ceiling per array is deliberately generous rather than a
# workflow quota.
MAX_REW_API_RESPONSE_BYTES = 32 * MIB
MAX_REW_ARRAY_SAMPLES = 1024 * 1024
MAX_REW_ARRAY_BYTES = 4 * MAX_REW_ARRAY_SAMPLES

# Native .htdt-backup files are local filesystem artifacts rather than browser
# request bodies. Keep the limits generous for REW/measurement assets while
# still bounding malicious/corrupt ZIP expansion and member fan-out.
MAX_NATIVE_BACKUP_ARCHIVE_BYTES = 8 * GIB
MAX_NATIVE_BACKUP_EXPANDED_BYTES = 16 * GIB
MAX_NATIVE_BACKUP_MEMBER_BYTES = 4 * GIB
MAX_NATIVE_BACKUP_MANIFEST_BYTES = 2 * MIB
MAX_NATIVE_BACKUP_MEMBERS = 4096
MAX_NATIVE_BACKUP_COMPRESSION_RATIO = 1000.0

# Capture ingestion bundles carry opaque source evidence plus dense ARKit mesh
# geometry. Aggregate per-ingest bounds are enforced on declared plan values
# before any payload is hashed, parsed, or staged, so hostile or corrupt
# manifests cannot force unbounded allocation. Limits stay generous for real
# captures while keeping the deduplicated native database comfortably inside
# the 4 GiB backup member ceiling.
MAX_CAPTURE_INGEST_SOURCE_EVIDENCE_COUNT = 4096
MAX_CAPTURE_INGEST_MESH_COUNT = 2048
MAX_CAPTURE_INGEST_SOURCE_BYTES = 2 * GIB
MAX_CAPTURE_INGEST_VERTEX_COUNT = 16 * 1024 * 1024
MAX_CAPTURE_INGEST_FACE_COUNT = 32 * 1024 * 1024
MAX_CAPTURE_INGEST_WORKING_BYTES = 8 * GIB


def max_base64_chars(decoded_bytes: int) -> int:
    if decoded_bytes < 0:
        raise ValueError('decoded_bytes must be non-negative')
    return 4 * ((decoded_bytes + 2) // 3)


MAX_REW_TEXT_BASE64_CHARS = max_base64_chars(MAX_REW_TEXT_BYTES)
MAX_ATTACHMENT_BASE64_CHARS = max_base64_chars(MAX_ATTACHMENT_BYTES)
MAX_BACKUP_BASE64_CHARS = max_base64_chars(MAX_BACKUP_ARCHIVE_BYTES)
MAX_REW_ARRAY_BASE64_CHARS = max_base64_chars(MAX_REW_ARRAY_BYTES)

# JSON field names/quotes and ordinary metadata need only a small allowance on
# top of the encoded payload itself.
JSON_ENVELOPE_ALLOWANCE_BYTES = 1 * MIB
MAX_REW_REQUEST_BODY_BYTES = MAX_REW_TEXT_BASE64_CHARS + JSON_ENVELOPE_ALLOWANCE_BYTES
MAX_ATTACHMENT_REQUEST_BODY_BYTES = MAX_ATTACHMENT_BASE64_CHARS + JSON_ENVELOPE_ALLOWANCE_BYTES
MAX_RESTORE_REQUEST_BODY_BYTES = MAX_BACKUP_BASE64_CHARS + JSON_ENVELOPE_ALLOWANCE_BYTES
