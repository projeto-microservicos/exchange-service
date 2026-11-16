from uuid import UUID
from pydantic import BaseModel, Field


class ExchangeQuote(BaseModel):
    sell: float = Field(gt=0, allow_inf_nan=False, description="Provider ask, or 1/bid of the inverse pair")
    buy: float = Field(gt=0, allow_inf_nan=False, description="Provider bid, or 1/ask of the inverse pair")
    date: str
    account: UUID = Field(alias="id-account")
