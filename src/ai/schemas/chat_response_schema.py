from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from src.schemas.product_schema import ProductResponse


ResponseType = Literal["text", "options", "products", "error"]


class ChatLLMResponse(BaseModel):
    """Structured decision returned by the LLM."""

    message: str
    response_type: ResponseType
    selected_product_ids: list[UUID] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """Final response returned to the frontend."""

    message: str
    response_type: ResponseType
    products: list[ProductResponse] = Field(default_factory=list)
