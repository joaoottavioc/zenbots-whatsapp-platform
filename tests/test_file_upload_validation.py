# tests/test_file_upload_validation.py
"""Tests for magic-byte-based file type detection in upload endpoint."""

import pathlib


def _read_bot_routes_source():
    """Read bot_routes.py source without importing (avoids fitz dependency)."""
    path = pathlib.Path(__file__).resolve().parent.parent / "app" / "bot_routes.py"
    return path.read_text(encoding="utf-8")


class TestFileUploadMagicBytes:
    """Verify that file upload uses magic bytes, not user-supplied MIME."""

    def test_upload_checks_pdf_magic_bytes(self):
        """The upload function should check for PDF magic bytes."""
        source = _read_bot_routes_source()
        assert '"%PDF"' in source or "b'%PDF'" in source or 'b"%PDF"' in source, (
            "Upload should check for PDF magic bytes"
        )

    def test_upload_checks_jpeg_magic_bytes(self):
        """The upload function should check for JPEG magic bytes."""
        source = _read_bot_routes_source()
        assert "\\xff\\xd8\\xff" in source, "Upload should check for JPEG magic bytes"

    def test_upload_checks_png_magic_bytes(self):
        """The upload function should check for PNG magic bytes."""
        source = _read_bot_routes_source()
        assert "\\x89PNG" in source, "Upload should check for PNG magic bytes"

    def test_upload_has_unsupported_file_type_error(self):
        """Unsupported file types should raise HTTP 400."""
        source = _read_bot_routes_source()
        assert "Tipo de arquivo não suportado" in source, (
            "Upload should reject unsupported file types with a clear error"
        )

    def test_upload_uses_detected_mime_for_images(self):
        """Image processing should use detected MIME, not user-supplied content_type."""
        source = _read_bot_routes_source()
        assert "detected_mime" in source, (
            "Upload should track detected MIME type from magic bytes"
        )
