import re

def normalize_phone(number: str) -> str:
    return re.sub(r"[^\d]", "", number).lstrip("55")