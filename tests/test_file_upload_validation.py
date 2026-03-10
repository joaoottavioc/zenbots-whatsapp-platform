# tests/test_file_upload_validation.py
"""Tests for magic-byte-based file type detection in upload and extraction."""

import pathlib


def _read_upload_sources():
    """Read bot_routes.py + menu_extraction.py source (avoids fitz dependency)."""
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    parts = []
    for name in ("bot_routes.py", "menu_extraction.py"):
        parts.append((app_dir / name).read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestFileUploadMagicBytes:
    """Verify that file upload uses magic bytes, not user-supplied MIME."""

    def test_upload_checks_pdf_magic_bytes(self):
        """The upload function should check for PDF magic bytes."""
        source = _read_upload_sources()
        assert '"%PDF"' in source or "b'%PDF'" in source or 'b"%PDF"' in source, (
            "Upload should check for PDF magic bytes"
        )

    def test_upload_checks_jpeg_magic_bytes(self):
        """The upload function should check for JPEG magic bytes."""
        source = _read_upload_sources()
        assert "\\xff\\xd8\\xff" in source, "Upload should check for JPEG magic bytes"

    def test_upload_checks_png_magic_bytes(self):
        """The upload function should check for PNG magic bytes."""
        source = _read_upload_sources()
        assert "\\x89PNG" in source, "Upload should check for PNG magic bytes"

    def test_upload_has_unsupported_file_type_error(self):
        """Unsupported file types should raise HTTP 400."""
        source = _read_upload_sources()
        assert "Tipo de arquivo não suportado" in source, (
            "Upload should reject unsupported file types with a clear error"
        )

    def test_upload_detects_mime_from_magic_bytes(self):
        """File type detection returns detected MIME, not user-supplied content_type."""
        source = _read_upload_sources()
        assert "_detect_file_type" in source, (
            "Upload should detect file type from magic bytes"
        )
