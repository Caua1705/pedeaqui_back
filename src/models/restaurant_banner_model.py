import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


#: Os dois valores de `banner_type`, e o espelho declarado do CHECK do banco
#: (`restaurant_banners_banner_type_check`) em `scripts/espelhos_de_enum.py`.
#:
#: Eram literais soltos nas duas chamadas de `get_banners_by_type`, e o espelho
#: era `SEM_ESPELHO` com essa frase como motivo. As duas chamadas viraram uma
#: (auditoria 5.2) e o motivo caiu junto — sobrou o valor em si, que e o que a
#: armadilha 15 pede declarado: valor so no banco e oferecido na tela e
#: recusado na escrita; valor so no codigo morre no INSERT.
BANNER_HERO = "hero"
BANNER_HIGHLIGHT = "highlight"
BANNER_TYPES = (BANNER_HERO, BANNER_HIGHLIGHT)


class RestaurantBanner(Base):
    __tablename__ = "restaurant_banners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    banner_type: Mapped[str] = mapped_column(Text, nullable=False, default=BANNER_HERO)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
