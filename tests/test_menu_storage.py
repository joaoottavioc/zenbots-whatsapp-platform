# tests/test_menu_storage.py
"""
Tests for S3 key path sanitization in menu_storage.upload_bytes_to_s3.
"""

from unittest.mock import patch, MagicMock
from app.menu_storage import ALLOWED_EXTENSIONS


class TestS3KeySanitization:
    """Verify filename sanitization prevents path traversal and disallowed extensions."""

    def _upload(self, filename, content_type="application/octet-stream"):
        with (
            patch("app.menu_storage.s3_client") as mock_s3,
            patch("app.menu_storage.BUCKET_NAME", "test-bucket"),
            patch("app.menu_storage.REGION", "us-east-1"),
        ):
            mock_s3.upload_fileobj = MagicMock()

            from app.menu_storage import upload_bytes_to_s3

            url = upload_bytes_to_s3(b"fakecontent", filename, content_type)
            # Extract the S3 key from the URL
            key = url.split(".amazonaws.com/")[1]
            return key

    def test_normal_pdf(self):
        key = self._upload("menu.pdf")
        assert key.endswith(".pdf")
        assert key.startswith("uploads/")

    def test_path_traversal_stripped(self):
        key = self._upload("../../etc/passwd.pdf")
        assert ".." not in key
        assert key.endswith(".pdf")

    def test_disallowed_extension_falls_back_to_bin(self):
        key = self._upload("script.exe")
        assert key.endswith(".bin")

    def test_double_extension_attack(self):
        key = self._upload("malware.exe.pdf")
        # rsplit takes last extension which is pdf — allowed
        assert key.endswith(".pdf")

    def test_no_extension(self):
        key = self._upload("noextension")
        assert key.endswith(".bin")

    def test_none_filename(self):
        key = self._upload(None)
        assert key.endswith(".bin")

    def test_jpeg_accepted(self):
        key = self._upload("photo.jpeg")
        assert key.endswith(".jpeg")

    def test_uppercase_normalized(self):
        key = self._upload("image.PNG")
        assert key.endswith(".png")


def test_allowed_extensions_constant():
    """ALLOWED_EXTENSIONS contains exactly the expected set."""
    assert ALLOWED_EXTENSIONS == {"pdf", "jpg", "jpeg", "png", "webp"}
