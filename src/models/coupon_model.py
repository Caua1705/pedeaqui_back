import uuid
from datetime import datetime, time
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, Time, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


# Os tres valores de `restaurant_coupons.visibility`, espelhando o CHECK
# `ck_restaurant_coupons_visibility`. Constante e nao Enum do SQLAlchemy: o
# banco guarda `text` com CHECK, e um `sa.Enum` aqui faria o autogenerate
# propor criar um tipo que nao existe la (armadilha 24).
COUPON_VISIBILITY_PUBLIC = "public"
COUPON_VISIBILITY_SEGMENT = "segment"
COUPON_VISIBILITY_PRIVATE = "private"
COUPON_VISIBILITIES = (
    COUPON_VISIBILITY_PUBLIC,
    COUPON_VISIBILITY_SEGMENT,
    COUPON_VISIBILITY_PRIVATE,
)


class CouponTemplate(Base):
    __tablename__ = "coupon_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    image_path: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(Text, nullable=False)
    discount_value: Mapped[Decimal | None] = mapped_column(Numeric, default=0)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant_coupons = relationship("RestaurantCoupon", back_populates="template")


class RestaurantCoupon(Base):
    __tablename__ = "restaurant_coupons"
    # Os nomes aqui sao os do banco de PRODUCAO, conferidos com
    # `\d restaurant_coupons` contra o `schema_baseline.sql`. O nome importa:
    # e por ele que `_raise_conflict` descobre QUAL regra o lojista esbarrou —
    # e o que estava escrito antes (`uq_restaurant_coupons_restaurant_code`)
    # nao existe em banco nenhum.
    #
    # `(restaurant_id, coupon_template_id)` nao estava declarado, e a ausencia
    # custava caro: UMA arte por restaurante e uma regra de produto inteira que
    # so aparecia como IntegrityError vindo do Postgres. Duas campanhas
    # simultaneas exigem dois templates diferentes.
    #
    # Falta de proposito o terceiro indice unico do banco,
    # `uq_restaurant_coupons_restaurant_code_ci`, sobre
    # `lower(trim(code))`: indice por EXPRESSAO nao ida e volta fiel pelo
    # autogenerate, e declara-lo aqui convidaria a armadilha 24 (proposta de
    # DROP do que o ORM nao reconhece). `_raise_conflict` conhece o nome dele
    # mesmo assim.
    __table_args__ = (
        UniqueConstraint("restaurant_id", "code", name="restaurant_coupons_restaurant_code_unique"),
        UniqueConstraint(
            "restaurant_id",
            "coupon_template_id",
            name="restaurant_coupons_restaurant_template_unique",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    coupon_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("coupon_templates.id"),
        nullable=False,
    )
    # NULLABLE desde a revisao 20260828_0043, e o nulo tem significado:
    # **cupom sem codigo aplica sozinho no checkout**, cupom com codigo exige
    # que a pessoa digite. Nao e "codigo ainda nao preenchido".
    #
    # Os tres UNIQUE do banco continuam valendo sem mudanca — o Postgres
    # trata NULL como distinto de qualquer outro NULL, entao varios cupons
    # sem codigo convivem no mesmo restaurante.
    code: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(Text, nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_order_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    valid_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    # NULLABLE, e o nulo tem significado: **a campanha nao expira**.
    #
    # Esta e a EXCECAO da lista de 16 colunas em que o ORM dizia NOT NULL e o
    # banco aceitava NULL (armadilha 50). Nas outras 15 o banco estava frouxo e
    # o model certo; aqui era o contrario — o banco ja permitia a campanha
    # permanente e o model e que mentia, e a mentira virava `AttributeError`
    # em `_aware(None)`, no checkout.
    #
    # `nullable=False` continua FORA daqui de proposito: alinhar esta coluna
    # apagaria uma possibilidade de produto. Ver `src/services/coupon_window.py`.
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    total_usage_limit: Mapped[int | None] = mapped_column(Integer)
    usage_limit_per_customer: Mapped[int | None] = mapped_column(Integer)
    cooldown_days: Mapped[int | None] = mapped_column(Integer)
    first_order_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Quem enxerga este cupom. Substituiu `is_public` na revisao
    # 20260828_0043 — o booleano nao tinha o terceiro valor, que e o que
    # existe campanha de reativacao para usar.
    #
    #     'public'   aparece na lista para todos
    #     'segment'  aparece so para quem esta em `target_segment`
    #     'private'  nao aparece; so existe para quem digita o codigo
    #
    # Quem le isto NAO e este atributo direto, e sim `CouponService.evaluate`:
    # `private` depende de o cliente ter resgatado (`coupon_claims`) e
    # `segment` depende do RFV dele. Um `if coupon.visibility == "public"`
    # espalhado pelas rotas e a divergencia que a funcao unica evita.
    visibility: Mapped[str] = mapped_column(Text, nullable=False, default=COUPON_VISIBILITY_PUBLIC)
    # O rotulo RFV alvo, e so preenchido quando `visibility == 'segment'` —
    # o CHECK `ck_restaurant_coupons_segment_needs_target` amarra os dois
    # sentidos. Os valores sao os de `CustomerSegment`, nao um vocabulario
    # proprio do cupom: ver o cabecalho de `src/services/customer_segment.py`.
    target_segment: Mapped[str | None] = mapped_column(Text)
    # "So no pix." Nulo = qualquer forma; lista = so nestas (revisao 0046).
    # Os valores sao os de `PAYMENT_METHODS`, e o CHECK do banco cobra a
    # mesma lista. Quem le e `CouponService.evaluate`, com a forma que o
    # chamador passou — sem forma (o Clube, a sacola antes da escolha) o
    # cupom cabe, e o card diz em que forma vale.
    allowed_payment_methods: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # "Das 15h as 18h", em hora LOCAL da operacao, sem fuso — o regime de
    # `branch_business_hours.opens_at`. Os dois nulos = o dia inteiro; os
    # dois preenchidos = a faixa, que pode virar a noite (22h-2h). O CHECK
    # recusa um sem o outro e inicio igual ao fim. Quem le e
    # `coupon_window.dentro_do_horario`, nunca as colunas direto.
    valid_hours_from: Mapped[time | None] = mapped_column(Time)
    valid_hours_until: Mapped[time | None] = mapped_column(Time)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant")
    template = relationship("CouponTemplate", back_populates="restaurant_coupons")
    redemptions = relationship("CouponRedemption", back_populates="coupon")


# A ORDEM DA VITRINE, definida uma vez so — mesmo motivo de
# `product_option_model.ordem_dos_grupos`.
#
# As duas consultas publicas ja ordenavam por `sort_order` e ordenavam
# DIFERENTE: `coupon_repository.list_public_coupons` desempatava por
# `created_at desc` e a do cardapio (`menu_repository`) nao desempatava nada.
# Como o painel nao tinha como gravar `sort_order`, TODO cupom estava no
# `DEFAULT 0` da coluna e o desempate decidia a lista inteira — ou seja, as
# duas superficies mostravam as mesmas campanhas em ordens diferentes, e uma
# delas mudava de ordem entre requisicoes.
def ordem_dos_cupons() -> list:
    """`sort_order`, depois a campanha mais nova, depois o `id`.

    O `id` no fim torna a ordem TOTAL. Sem ele, dois cupons com o mesmo
    `sort_order` criados na mesma transacao (`created_at` e um `now()` so)
    podem sair trocados entre duas requisicoes — a vitrine piscando sem nada
    ter mudado.

    As tres colunas sao `NOT NULL` no schema, entao nao ha `NULLS FIRST`
    escondido aqui: `created_at DESC` com nulo poria a linha sem data no TOPO
    da vitrine, que e o pior lugar para uma surpresa.
    """
    return [
        RestaurantCoupon.sort_order.asc(),
        RestaurantCoupon.created_at.desc(),
        RestaurantCoupon.id.asc(),
    ]
