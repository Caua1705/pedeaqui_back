import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from src.models.coupon_claim_model import CouponClaim
from src.services.coupon_window import filtro_de_janela
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.order_model import Order
from src.services.customer_segment import segment_expression


class CouponRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id_and_restaurant(
        self,
        coupon_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        *,
        for_update: bool = False,
        agora: datetime | None = None,
    ) -> RestaurantCoupon | None:
        """`agora` recorta pela JANELA de validade; sem ele, traz qualquer cupom.

        Os dois modos existem e nao sao intercambiaveis. O PAINEL precisa
        enxergar a campanha vencida — e la que o lojista a edita ou a
        reativa —, e por isso o default e nao filtrar. Ja as superficies do
        CLIENTE passam `agora`, para o cupom fora da janela nem chegar a ser
        avaliado.
        """
        stmt = select(RestaurantCoupon).options(joinedload(RestaurantCoupon.template)).where(
            RestaurantCoupon.id == coupon_id,
            RestaurantCoupon.restaurant_id == restaurant_id,
            *(filtro_de_janela(agora) if agora is not None else []),
        )
        if for_update:
            stmt = stmt.with_for_update(of=RestaurantCoupon)
        return self.db.scalar(stmt)

    def get_by_code_and_restaurant(
        self,
        code: str,
        restaurant_id: uuid.UUID,
        *,
        for_update: bool = False,
        agora: datetime | None = None,
    ) -> RestaurantCoupon | None:
        """Ver `get_by_id_and_restaurant` sobre o `agora`."""
        stmt = select(RestaurantCoupon).options(joinedload(RestaurantCoupon.template)).where(
            RestaurantCoupon.restaurant_id == restaurant_id,
            func.lower(RestaurantCoupon.code) == code.strip().lower(),
            *(filtro_de_janela(agora) if agora is not None else []),
        )
        if for_update:
            stmt = stmt.with_for_update(of=RestaurantCoupon)
        return self.db.scalar(stmt)

    def lock_coupon(
        self,
        restaurant_id: uuid.UUID,
        *,
        coupon_id: uuid.UUID | None = None,
        coupon_code: str | None = None,
        agora: datetime | None = None,
    ) -> RestaurantCoupon | None:
        if coupon_id is not None:
            return self.get_by_id_and_restaurant(
                coupon_id, restaurant_id, for_update=True, agora=agora
            )
        if coupon_code is not None:
            return self.get_by_code_and_restaurant(
                coupon_code, restaurant_id, for_update=True, agora=agora
            )
        return None

    def list_in_window(
        self,
        restaurant_id: uuid.UUID,
        now: datetime | None = None,
    ) -> list[RestaurantCoupon]:
        """As campanhas ativas e dentro da janela — de TODAS as visibilidades.

        Substituiu `list_public_available`, e a diferenca e o ponto do item 7
        da frente: **a consulta nao filtra mais quem pode ver.** Ela devolve
        os candidatos, e o gate (`public` / `segment` / `private`) roda uma
        vez so, dentro de `CouponService.evaluate`.

        O que se perde ao filtrar aqui, e que ja custou caro em outros
        lugares: com o `WHERE is_public` na consulta, cada superficie precisa
        lembrar de repeti-lo — a vitrine do cardapio, a lista do cliente, o
        preview e o checkout. Quatro copias de uma regra que muda de tres
        valores para tres condicoes diferentes e a divergencia esperando
        acontecer, e o sintoma dela e cupom aparecendo para quem nao devia,
        sem erro e sem log.

        O custo de nao filtrar e ler algumas linhas a mais por restaurante —
        campanhas simultaneas sao unidades, nao milhares — e o indice
        `ix_restaurant_coupons_visibility_window` cobre o resto do WHERE.
        """
        current = now or datetime.now(timezone.utc)
        stmt = (
            select(RestaurantCoupon)
            .options(joinedload(RestaurantCoupon.template))
            .where(
                RestaurantCoupon.restaurant_id == restaurant_id,
                RestaurantCoupon.is_active.is_(True),
                *filtro_de_janela(current),
            )
            .order_by(RestaurantCoupon.sort_order.asc(), RestaurantCoupon.created_at.desc())
        )
        return list(self.db.scalars(stmt).unique().all())

    def segment_of_customer(
        self,
        restaurant_id: uuid.UUID,
        customer_phone: str,
        now: datetime,
    ) -> str:
        """O rotulo RFV deste cliente NESTE restaurante.

        As expressoes sao as de `src/services/customer_segment.py`, as mesmas
        que a listagem de clientes do painel usa. **Nao ha segunda
        implementacao**, e o cabecalho daquele modulo registra o que custou a
        primeira: uma janela de ate 24h por cliente em que a versao Python e
        a SQL discordavam do rotulo, invisivel em leitura de codigo. O cupom
        de segmento tinha que herdar a mesma escada, ou o lojista veria
        "em_risco" na tela de clientes e o cupom de em_risco nao apareceria
        para aquela pessoa.

        **O recorte e por TELEFONE, e nao por `customer_id`** — de novo, o
        mesmo do painel. Quem pediu como convidado antes de criar a conta
        aparece na mesma linha, e por isso um cupom de `novo` nao vai parar
        na mao de quem ja pediu tres vezes sem estar logado.

        O telefone chega ja normalizado (so digitos) porque
        `orders.customer_phone_snapshot` e gravado assim — comparar com um
        `(85) 99999-9999` nao casaria linha nenhuma, e o cliente sairia
        `novo` para sempre (armadilha 27).

        Sem pedido nenhum a agregacao devolve uma linha com contagem zero e
        datas nulas, e a escada cai em `novo` pelo primeiro ramo do CASE. E
        por isso esta funcao devolve `str` e nunca `None`.
        """
        orders_count = func.count(Order.id)
        first_order_at = func.min(Order.created_at)
        last_order_at = func.max(Order.created_at)
        stmt = select(
            segment_expression(orders_count, first_order_at, last_order_at, now)
        ).where(
            Order.restaurant_id == restaurant_id,
            Order.customer_phone_snapshot == customer_phone,
        )
        return self.db.scalar(stmt)

    def claimed_coupon_ids(self, customer_id: uuid.UUID) -> set[uuid.UUID]:
        """Os cupons que este cliente ja resgatou, numa consulta so.

        Conjunto e nao lista porque a pergunta e de pertinencia, e conjunto
        porque a listagem confere um cupom por linha: `has_claim` por cupom
        seria uma ida ao banco por card da tela (N+1).
        """
        stmt = select(CouponClaim.coupon_id).where(CouponClaim.customer_id == customer_id)
        return set(self.db.scalars(stmt).all())

    def get_claim(self, coupon_id: uuid.UUID, customer_id: uuid.UUID) -> CouponClaim | None:
        stmt = select(CouponClaim).where(
            CouponClaim.coupon_id == coupon_id,
            CouponClaim.customer_id == customer_id,
        )
        return self.db.scalar(stmt)

    def create_claim(self, coupon_id: uuid.UUID, customer_id: uuid.UUID) -> CouponClaim:
        claim = CouponClaim(coupon_id=coupon_id, customer_id=customer_id)
        self.db.add(claim)
        self.db.flush()
        return claim

    def list_by_restaurant(self, restaurant_id: uuid.UUID) -> list[RestaurantCoupon]:
        stmt = (
            select(RestaurantCoupon)
            .options(joinedload(RestaurantCoupon.template))
            .where(RestaurantCoupon.restaurant_id == restaurant_id)
            .order_by(RestaurantCoupon.created_at.desc())
        )
        return list(self.db.scalars(stmt).unique().all())

    def count_applied_total(self, coupon_id: uuid.UUID) -> int:
        stmt = select(func.count(CouponRedemption.id)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.status == "applied",
        )
        return int(self.db.scalar(stmt) or 0)

    def count_applied_by_coupon(self, coupon_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Os usos de VARIOS cupons numa consulta so.

        Existe separada de `count_applied_total` por causa da tela de cupons do
        painel: chamar aquela por cupom dentro do `list_admin` seria uma ida ao
        banco por linha da lista (N+1), e o custo cresce junto com o numero de
        campanhas do restaurante — que e exatamente quem abre essa tela.

        Cupom sem nenhuma redencao NAO aparece no resultado: `GROUP BY` so
        devolve grupo que existe. Quem chama resolve com `.get(id, 0)`.
        """
        if not coupon_ids:
            return {}
        stmt = (
            select(CouponRedemption.coupon_id, func.count(CouponRedemption.id))
            .where(
                CouponRedemption.coupon_id.in_(coupon_ids),
                CouponRedemption.status == "applied",
            )
            .group_by(CouponRedemption.coupon_id)
        )
        return {coupon_id: int(total) for coupon_id, total in self.db.execute(stmt).all()}

    def count_applied_redemptions_for_customer(
        self,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> int:
        stmt = select(func.count(CouponRedemption.id)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.customer_id == customer_id,
            CouponRedemption.status == "applied",
        )
        return int(self.db.scalar(stmt) or 0)

    def get_last_applied_redemption_for_customer(
        self,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> datetime | None:
        stmt = select(func.max(CouponRedemption.applied_at)).where(
            CouponRedemption.coupon_id == coupon_id,
            CouponRedemption.customer_id == customer_id,
            CouponRedemption.status == "applied",
        )
        return self.db.scalar(stmt)

    # Compatibility for callers created before the explicit repository name.
    def count_applied_by_customer(self, coupon_id: uuid.UUID, customer_id: uuid.UUID) -> int:
        return self.count_applied_redemptions_for_customer(coupon_id, customer_id)

    def customer_has_valid_order(self, customer_id: uuid.UUID, restaurant_id: uuid.UUID) -> bool:
        stmt = select(Order.id).where(
            Order.customer_id == customer_id,
            Order.restaurant_id == restaurant_id,
            Order.status.notin_(("cancelled", "rejected")),
        ).limit(1)
        return self.db.scalar(stmt) is not None

    def create_redemption(
        self,
        *,
        coupon_id: uuid.UUID,
        customer_id: uuid.UUID,
        order_id: uuid.UUID,
        discount_amount: Decimal,
        idempotency_key: str,
    ) -> CouponRedemption:
        redemption = CouponRedemption(
            coupon_id=coupon_id,
            customer_id=customer_id,
            order_id=order_id,
            discount_amount=discount_amount,
            status="applied",
            idempotency_key=idempotency_key,
        )
        self.db.add(redemption)
        self.db.flush()
        return redemption

    def get_redemption_by_order_id(self, order_id: uuid.UUID) -> CouponRedemption | None:
        stmt = select(CouponRedemption).where(CouponRedemption.order_id == order_id)
        return self.db.scalar(stmt)

    def reverse_redemption(self, redemption: CouponRedemption) -> CouponRedemption:
        if redemption.status != "applied":
            return redemption
        redemption.status = "reversed"
        redemption.reversed_at = datetime.now(timezone.utc)
        self.db.add(redemption)
        self.db.flush()
        return redemption

    def get_template(self, template_id: uuid.UUID) -> CouponTemplate | None:
        return self.db.get(CouponTemplate, template_id)

    def list_active_templates(self) -> list[CouponTemplate]:
        # `name` desempata `sort_order` repetido: sem isso o seletor do painel
        # sai numa ordem diferente a cada requisicao, e quem escolhe a arte
        # pela posicao erra.
        stmt = (
            select(CouponTemplate)
            .where(CouponTemplate.is_active.is_(True))
            .order_by(CouponTemplate.sort_order.asc(), CouponTemplate.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def create_coupon(self, coupon: RestaurantCoupon) -> RestaurantCoupon:
        self.db.add(coupon)
        self.db.flush()
        return coupon

    def save_coupon(self, coupon: RestaurantCoupon) -> RestaurantCoupon:
        self.db.add(coupon)
        self.db.flush()
        return coupon
