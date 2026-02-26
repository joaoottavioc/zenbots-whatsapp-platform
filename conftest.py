import sys
import os

# Make sure 'app' is importable regardless of where pytest is invoked from
sys.path.insert(0, os.path.dirname(__file__))

# Set a test encryption key so decrypt_value/encrypt_value work in tests.
# This must happen before any app module imports the encryption module.
if "ENCRYPTION_KEY" not in os.environ:
    from cryptography.fernet import Fernet
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
