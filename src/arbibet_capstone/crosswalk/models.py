"""
Pydantic models for validated data at the API boundary.
"""
from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime


class Outcome(BaseModel):
    id: str
    name: Optional[str] = None
    odds: float
    p: Optional[float] = None
    last_change: Optional[str] = None

    @field_validator("id", mode="before")
    @classmethod
    def coerce_id_to_str(cls, v):
        if v is None:
            return ""
        return str(v).strip()

    @field_validator("odds", mode="before")
    @classmethod
    def coerce_odds(cls, v):
        return float(v)


class Market(BaseModel):
    market_id: str
    market_name: Optional[str] = None
    outcomes: list[Outcome]

    @field_validator("market_id", mode="before")
    @classmethod
    def coerce_market_id(cls, v):
        return str(v)
