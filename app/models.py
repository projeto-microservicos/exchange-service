from uuid import UUID
from pydantic import BaseModel, Field


class ExchangeQuote(BaseModel):
    sell: float = Field(gt=0, allow_inf_nan=False, description="PTAX source sell / target buy, using BRL as reference")
    buy: float = Field(gt=0, allow_inf_nan=False, description="PTAX source buy / target sell, using BRL as reference")
    date: str
    account: UUID = Field(alias="id-account")
