from datetime import datetime

from solaredge2mqtt.core.events.events import BaseEvent


class PricesUpdatedEvent(BaseEvent):
    """Emitted when the hourly price cache is refreshed."""

    def __init__(self, fetched_at: datetime, hours: int) -> None:
        self.fetched_at = fetched_at
        self.hours = hours
