from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

@runtime_checkable
class Clock(Protocol):
    def now_utc(self) -> datetime: ...

class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

clock = SystemClock()
