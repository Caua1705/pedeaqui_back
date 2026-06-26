from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


ResponseType = Literal["text", "options", "products", "error"]


class ChatProduct(BaseModel):
    """Product selected by the AI from the retrieved context."""

    id: UUID
    name: str
    description: str | None = None
    price: float | None = None
    slug: str | None = None
    image_url: str | None = None


class ChatResponse(BaseModel):
    """Structured response returned by the Rapi chat."""

    message: str
    response_type: ResponseType
    options: list[str] = Field(default_factory=list)
    products: list[ChatProduct] = Field(default_factory=list)
    finish: bool
