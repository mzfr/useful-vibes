from __future__ import annotations

import io
from collections.abc import Callable

from django.http import HttpResponse


def decode_csv_upload(upload) -> str | None:
    """Decode a UTF-8 CSV upload, tolerating the common byte-order mark."""
    try:
        return upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def csv_export_response(user, exporter: Callable, filename: str) -> HttpResponse:
    buffer = io.StringIO()
    exporter(user, buffer)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
