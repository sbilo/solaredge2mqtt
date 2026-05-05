from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class PriceSourceType(str, Enum):
    STATIC = "static"
    ENTSOE = "entsoe"


class EntsoePriceSourceSettings(BaseModel):
    """ENTSO-E Transparency Platform day-ahead prices.

    Day-ahead prices for the next day are published around 12:45 CET.
    The provider fetches once per day at ``fetch_hour`` (local time) and on
    startup if the cache is empty.

    Final prices are derived from the raw spot (EUR/MWh → EUR/kWh) as:

        price_in  = (spot + energy_tax_in  + markup_in)  * (1 + vat_in)
        price_out = (spot + energy_tax_out + markup_out) * (1 + vat_out)
    """

    api_token: str = Field(min_length=1)
    area: str = Field(
        default="10YNL----------L",
        description="ENTSO-E EIC bidding-zone code (e.g. 10YNL----------L for NL).",
    )

    fetch_hour: int = Field(default=14, ge=0, le=23)

    energy_tax_in: float = Field(default=0.0)
    energy_tax_out: float = Field(default=0.0)
    markup_in: float = Field(default=0.0)
    markup_out: float = Field(default=0.0)
    vat_in: float = Field(default=0.0, ge=0.0)
    vat_out: float = Field(default=0.0, ge=0.0)


class PriceSourceSettings(BaseModel):
    type: PriceSourceType = Field(default=PriceSourceType.STATIC)
    entsoe: EntsoePriceSourceSettings | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_source(self) -> "PriceSourceSettings":
        if self.type == PriceSourceType.ENTSOE and self.entsoe is None:
            raise ValueError(
                "prices.source.type is 'entsoe' but "
                "prices.source.entsoe is not configured"
            )
        return self


class PriceSettings(BaseModel):
    consumption: float | None = Field(default=None)
    delivery: float | None = Field(default=None)
    currency: str | None = Field(default=None)
    source: PriceSourceSettings = Field(default_factory=PriceSourceSettings)

    @property
    def is_configured(self) -> bool:
        if self.is_dynamic:
            return self.currency is not None
        return self.is_consumption_configured or self.is_delivery_configured

    @property
    def is_consumption_configured(self) -> bool:
        return self.consumption is not None and self.currency is not None

    @property
    def is_delivery_configured(self) -> bool:
        return self.delivery is not None and self.currency is not None

    @property
    def is_dynamic(self) -> bool:
        return self.source.type != PriceSourceType.STATIC

    @property
    def price_in(self) -> float:
        return self.consumption or 0.0

    @property
    def price_out(self) -> float:
        return self.delivery or 0.0


class EnergySettings(BaseModel):
    retain: bool = Field(default=False)
