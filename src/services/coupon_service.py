import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.coupon_model import (
    COUPON_VISIBILITY_PRIVATE,
    COUPON_VISIBILITY_PUBLIC,
    COUPON_VISIBILITY_SEGMENT,
    CouponTemplate,
    RestaurantCoupon,
)
from src.models.customer_model import Customer
from src.core.constants import ORDER_TYPES
from src.repositories.coupon_repository import CouponRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.coupon_schema import (
    CouponAdminResponse,
    CouponClaimRequest,
    CouponClaimResponse,
    CouponCreate,
    CouponTemplateResponse,
    CouponPreviewRequest,
    CouponPreviewResponse,
    CouponUpdate,
    CouponCampaignFields,
    CustomerCouponLabel,
    CustomerCouponResponse,
    CustomerCouponState,
    CustomerCouponsResponse,
)
from src.services.coupon_window import ja_acabou, ja_comecou
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, quantize_money, to_decimal
from src.utils.normalization import normalize_digits
from src.utils.storage import build_storage_url


# `uvicorn.error` e o logger do resto do repositorio: e ele que aparece no
# `docker logs`.
logger = logging.getLogger("uvicorn.error")


# Nome do indice UNIQUE no Postgres -> o que dizer ao lojista.
#
# `restaurant_coupons` tem TRES indices unicos, e ate 23/08/2026 os tres saiam
# como "Codigo de cupom ja existe neste restaurante". Quem esbarrava na ARTE
# trocava o codigo, tomava 409 de novo, e nao tinha como sair do lugar: o campo
# que a mensagem mandava mexer nao era o campo que estava colidindo.
#
# A chave e `exc.orig.diag.constraint_name`. O Postgres preenche esse campo
# tambem para UNIQUE INDEX — nenhum dos tres e CONSTRAINT de tabela —, e isso
# foi conferido contra o banco antes de o codigo passar a depender disso.
UNIQUE_INDEX_MESSAGES = {
    "restaurant_coupons_restaurant_code_unique": "Código de cupom já existe neste restaurante",
    # O mesmo codigo em outra caixa. A mensagem e a mesma de proposito: para o
    # lojista, PROMO10 e promo10 sao o mesmo cupom, e e assim que a busca trata.
    "uq_restaurant_coupons_restaurant_code_ci": "Código de cupom já existe neste restaurante",
    "restaurant_coupons_restaurant_template_unique": (
        "Esta arte já está em uso por outra campanha deste restaurante"
    ),
}


@dataclass(frozen=True)
class CouponEvaluation:
    valid: bool
    discount: Decimal = ZERO
    missing_amount: Decimal = ZERO
    reason: str | None = None
    requires_login: bool = False
    next_available_at: datetime | None = None


@dataclass(frozen=True)
class CustomerAudience:
    """O que o GATE de visibilidade precisa saber sobre quem esta olhando.

    Existe para a lista do cliente nao ir ao banco por card: o segmento sai
    de uma agregacao e os resgates de um `IN`, os dois uma vez por
    requisicao, e `evaluate` consulta este objeto em memoria.

    `segment is None` significa **convidado** (sem token), e nao "cliente sem
    segmento" — todo cliente tem um, `novo` inclusive. A distincao decide
    o gate inteiro: convidado nao enxerga cupom de segmento nem cupom
    privado, porque nao ha de quem eles sejam.
    """

    segment: str | None
    claimed_coupon_ids: frozenset[UUID]

    @property
    def is_guest(self) -> bool:
        return self.segment is None


# Motivo da recusa -> o que o card do cliente vira.
#
# **Esta tabela E a regra do item 4 da frente**, e o que nao esta nela e a
# outra metade: motivo que nao aparece aqui NAO VIRA CARD. Vencido, de outro
# segmento, primeira-compra para quem ja comprou, teto estourado, cooldown
# correndo, privado sem resgate — nada disso e conserto que o cliente possa
# fazer nesta sacola, e um card cinza com uma negativa que ele nao consegue
# resolver so ocupa a tela.
#
# Os dois que sobram sao exatamente os que ele consegue mexer agora: por
# mais coisa na sacola, ou entrar na conta.
REASON_TO_STATE = {
    "minimum_order_not_reached": CustomerCouponState.MISSING_AMOUNT,
    "login_required": CustomerCouponState.LOGIN_REQUIRED,
}


class CouponService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = CouponRepository(db)
        self.restaurant_repository = RestaurantRepository(db)
        self.restaurant_service = RestaurantService(db)
        self.clock = lambda: datetime.now(timezone.utc)

    @staticmethod
    def calculate_discount(coupon: RestaurantCoupon, subtotal: Decimal, delivery_fee: Decimal) -> Decimal:
        subtotal = quantize_money(to_decimal(subtotal))
        delivery_fee = quantize_money(to_decimal(delivery_fee))
        discount_value = to_decimal(coupon.discount_value)

        if coupon.discount_type == "fixed":
            discount = min(discount_value, subtotal)
        elif coupon.discount_type == "percent":
            discount = subtotal * discount_value / Decimal("100")
            if coupon.max_discount_amount is not None:
                discount = min(discount, to_decimal(coupon.max_discount_amount))
            discount = min(discount, subtotal)
        elif coupon.discount_type == "free_delivery":
            discount = delivery_fee
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de desconto inválido")
        return quantize_money(max(discount, ZERO))

    def evaluate(
        self,
        coupon: RestaurantCoupon,
        *,
        restaurant_id: UUID,
        subtotal: Decimal,
        delivery_fee: Decimal,
        customer: Customer | None,
        audience: CustomerAudience | None = None,
        now: datetime | None = None,
    ) -> CouponEvaluation:
        """CABE OU NAO CABE. A unica resposta para essa pergunta no sistema.

        Chamam esta funcao a listagem do app, o preview, a auto-aplicacao e a
        criacao do pedido. As tres primeiras sao PREVIEW; a validacao que
        vale e a da criacao do pedido, que roda esta mesma funcao com o cupom
        travado (`SELECT ... FOR UPDATE`) dentro da transacao.

        **O parametro `require_public` saiu, e a saida e o ponto.** Ele fazia
        "de qual superficie eu vim" ser decisao do chamador, e uma superficie
        nova que esquecesse de passar `True` publicava cupom privado sem erro
        nenhum. Hoje quem responde e a coluna `visibility`, sempre, aqui
        dentro.

        A ORDEM DOS RAMOS e legivel de cima para baixo e nao e arbitraria:
        primeiro o que o cliente nao enxerga, depois o que ja passou, depois
        o que estourou, e so no fim o que falta na sacola. O ultimo e o unico
        que o cliente resolve, e por isso e o unico que sai com numero.
        """
        current = self._aware(now or self.clock())
        audience = audience or self.audience_of(customer, restaurant_id, now=current)
        subtotal = quantize_money(to_decimal(subtotal))
        minimum = quantize_money(to_decimal(coupon.min_order_value))

        if coupon.restaurant_id != restaurant_id:
            return CouponEvaluation(False, reason="coupon_from_another_restaurant")
        if not coupon.is_active:
            return CouponEvaluation(False, reason="inactive")
        if not self._can_see(coupon, audience):
            return CouponEvaluation(False, reason="not_visible")
        # A janela sai de `coupon_window`, que e o MESMO lugar de onde os dois
        # repositorios tiram o `where()`. Enquanto a regra estava escrita aqui
        # em Python e la em SQL, `valid_until` nulo — que significa "nao
        # expira" — era `AttributeError` neste ponto: `_aware(None)`, no
        # caminho do dinheiro.
        if not ja_comecou(coupon.valid_from, current):
            return CouponEvaluation(False, reason="not_started")
        if ja_acabou(coupon.valid_until, current):
            return CouponEvaluation(False, reason="expired")
        if coupon.total_usage_limit is not None:
            if self.repository.count_applied_total(coupon.id) >= coupon.total_usage_limit:
                return CouponEvaluation(False, reason="total_limit_reached")

        requires_customer_check = (
            coupon.usage_limit_per_customer is not None
            or coupon.cooldown_days is not None
            or bool(coupon.first_order_only)
        )
        if customer is None and requires_customer_check:
            missing = quantize_money(max(minimum - subtotal, ZERO))
            return CouponEvaluation(
                False,
                missing_amount=missing,
                reason="login_required",
                requires_login=True,
            )
        if customer is not None:
            limite = self._customer_limit_reason(coupon, customer, restaurant_id, current)
            if limite is not None:
                return limite

        if subtotal < minimum:
            return CouponEvaluation(
                False,
                missing_amount=quantize_money(minimum - subtotal),
                reason="minimum_order_not_reached",
            )
        return CouponEvaluation(
            True,
            discount=self.calculate_discount(coupon, subtotal, delivery_fee),
        )

    @staticmethod
    def _can_see(coupon: RestaurantCoupon, audience: CustomerAudience) -> bool:
        """O gate de visibilidade, e o unico lugar que le `coupon.visibility`.

        **Convidado enxerga so o que e publico, e isso vale inclusive para o
        motivo da recusa.** Devolver "entre na conta para usar" num cupom
        privado seria anunciar a existencia dele — junto com o titulo e o
        codigo, que e exatamente o que `private` existe para nao publicar. O
        cupom simplesmente nao esta la.

        O mesmo para `segment`: um convidado poderia se encaixar depois de
        logar, mas nao ha como saber antes, e mostrar a campanha "para quem
        sumiu" a quem nunca pediu e pior do que nao mostrar nada.
        """
        if coupon.visibility == COUPON_VISIBILITY_PUBLIC:
            return True
        if audience.is_guest:
            return False
        if coupon.visibility == COUPON_VISIBILITY_PRIVATE:
            return coupon.id in audience.claimed_coupon_ids
        if coupon.visibility == COUPON_VISIBILITY_SEGMENT:
            return coupon.target_segment == audience.segment
        # Valor que o CHECK do banco nao deixa entrar. Se um dia chegar aqui,
        # o cupom fica INVISIVEL — errar para o lado de nao publicar.
        return False

    def _customer_limit_reason(
        self,
        coupon: RestaurantCoupon,
        customer: Customer,
        restaurant_id: UUID,
        current: datetime,
    ) -> CouponEvaluation | None:
        """Os tres tetos que dependem de QUEM esta pedindo. `None` = passou.

        Separado de `evaluate` porque sao tres idas ao banco condicionais
        empilhadas, e dentro dela custavam dois niveis de indentacao a mais
        sem acrescentar nada a leitura da escada principal.
        """
        if coupon.usage_limit_per_customer is not None:
            usages = self.repository.count_applied_redemptions_for_customer(coupon.id, customer.id)
            if usages >= coupon.usage_limit_per_customer:
                return CouponEvaluation(False, reason="customer_limit_reached")
        if coupon.cooldown_days is not None:
            last_applied_at = self.repository.get_last_applied_redemption_for_customer(
                coupon.id,
                customer.id,
            )
            if last_applied_at is not None:
                next_available_at = self._aware(last_applied_at) + timedelta(days=coupon.cooldown_days)
                if current < next_available_at:
                    return CouponEvaluation(
                        False,
                        reason="cooldown_active",
                        next_available_at=next_available_at,
                    )
        if coupon.first_order_only and self.repository.customer_has_valid_order(customer.id, restaurant_id):
            return CouponEvaluation(False, reason="first_order_only")
        return None

    def audience_of(
        self,
        customer: Customer | None,
        restaurant_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CustomerAudience:
        """Segmento e resgates de quem esta olhando, em duas consultas.

        Montado UMA vez por requisicao e repassado a `evaluate` de cada
        cupom da lista — sem isso a tela do Clube faria duas idas ao banco
        por card.

        O telefone e normalizado aqui de novo, e nao so no cadastro: contas
        antigas podem ter `(85) 99999-9999` gravado, e
        `orders.customer_phone_snapshot` e sempre digitos. A comparacao com o
        telefone cru nao casa linha nenhuma e devolveria `novo` para um
        cliente fiel — sem erro, sem log (armadilha 27).
        """
        if customer is None:
            return CustomerAudience(segment=None, claimed_coupon_ids=frozenset())
        return CustomerAudience(
            segment=self.repository.segment_of_customer(
                restaurant_id,
                normalize_digits(customer.phone),
                self._aware(now or self.clock()),
            ),
            claimed_coupon_ids=frozenset(self.repository.claimed_coupon_ids(customer.id)),
        )

    def list_for_customer(
        self,
        restaurant_slug: str,
        *,
        subtotal: Decimal | None,
        delivery_fee: Decimal | None,
        order_type: str | None,
        customer: Customer | None,
    ) -> CustomerCouponsResponse:
        """A lista de cupons do app, com o estado JA DECIDIDO.

        Substituiu `get_available` (`GET .../coupons/available`), e nao e so
        troca de nome: aquela devolvia todo cupom publico com um
        `eligible: false` e um `ineligibility_reason` cru, e o app tinha que
        traduzir sete motivos em ingles para decidir o que pintar. Hoje o
        backend decide, e o que chega ao app sao tres estados e uma etiqueta.

        O que NAO entra na lista esta em `REASON_TO_STATE`, junto do porque.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if order_type is not None and order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido inválido")
        products_subtotal = quantize_money(to_decimal(subtotal))
        fee = quantize_money(to_decimal(delivery_fee))
        # Retirada nao tem taxa a descontar, entao um cupom de frete gratis
        # anunciaria um desconto que o checkout nao vai dar.
        if order_type == "pickup":
            fee = ZERO

        current = self._aware(self.clock())
        audience = self.audience_of(customer, restaurant.id, now=current)
        avaliados = []
        for coupon in self.repository.list_in_window(restaurant.id, now=current):
            evaluation = self.evaluate(
                coupon,
                restaurant_id=restaurant.id,
                subtotal=products_subtotal,
                delivery_fee=fee,
                customer=customer,
                audience=audience,
                now=current,
            )
            avaliados.append((coupon, evaluation))

        # O automatico que o checkout aplicaria a ESTA sacola, pela mesma
        # escolha de `auto_apply_for_order`. Sem sacola nao ha o que comparar
        # (a tela do Clube), e convidado nao recebe automatico no checkout —
        # nos dois casos nenhum card e marcado, porque marcar seria prometer
        # o que o checkout nao vai fazer.
        escolhido = None
        if subtotal is not None and customer is not None:
            escolhido = self._pick_automatic(
                [(evaluation.discount, coupon) for coupon, evaluation in avaliados if evaluation.valid]
            )

        cards = []
        for coupon, evaluation in avaliados:
            card = self._customer_card(
                coupon, evaluation, auto_apply=escolhido is not None and coupon.id == escolhido.id
            )
            if card is not None:
                cards.append(card)
        return CustomerCouponsResponse(coupons=cards)

    @staticmethod
    def _customer_card(
        coupon: RestaurantCoupon,
        evaluation: CouponEvaluation,
        auto_apply: bool = False,
    ) -> CustomerCouponResponse | None:
        """Um card, ou `None` quando o cupom nao deve aparecer.

        `None` e o caso comum e nao um erro: a maioria das campanhas de um
        restaurante nao e para quem esta olhando naquele momento.
        """
        if evaluation.valid:
            state = CustomerCouponState.APPLICABLE
        else:
            state = REASON_TO_STATE.get(evaluation.reason)
        if state is None:
            return None

        template = coupon.template
        return CustomerCouponResponse(
            id=coupon.id,
            code=coupon.code,
            title=coupon.title,
            description=coupon.description,
            image_url=build_storage_url(template.image_path) if template is not None else None,
            discount_type=coupon.discount_type,
            min_order_value=quantize_money(to_decimal(coupon.min_order_value)),
            valid_until=coupon.valid_until,
            label=CouponService._label(coupon),
            visibility=coupon.visibility,
            auto_apply=auto_apply,
            state=state,
            discount_amount=evaluation.discount,
            missing_amount=evaluation.missing_amount,
        )

    @staticmethod
    def _label(coupon: RestaurantCoupon) -> CustomerCouponLabel | None:
        """Cupom de segmento tem etiqueta; cupom publico nao tem nenhuma.

        A regra inteira cabe em duas linhas e a segunda e a que costuma ser
        esquecida: se todo mundo ve, "para todos" nao informa nada e so gasta
        o espaco do card.

        Cupom PRIVADO tambem sai sem etiqueta. Ele chegou ali porque a pessoa
        digitou o codigo — ela sabe de onde ele veio melhor que o card.
        """
        if coupon.visibility == COUPON_VISIBILITY_SEGMENT:
            return CustomerCouponLabel.SELECTED_FOR_YOU
        return None

    def claim(
        self,
        restaurant_slug: str,
        payload: CouponClaimRequest,
        customer: Customer,
    ) -> CouponClaimResponse:
        """Resgatar um codigo SEM SACOLA — o cupom passa a ser do cliente.

        E a porta do Clube: a pessoa digita `VOLTA15`, o cupom entra na lista
        dela e aplica depois, no checkout. Nada de valor acontece aqui.

        **O resgate grava VISIBILIDADE, e nada mais.** Janela, minimo, teto
        total, teto por cliente, cooldown e primeira-compra continuam sendo
        conferidos na criacao do pedido, sobre a sacola daquele momento. Por
        isso a linha em `coupon_claims` nao tem valor nem status: nao ha
        estado a percorrer, e antecipar qualquer uma dessas regras aqui
        criaria a segunda resposta para "este cupom vale?".

        **A recusa e SEMPRE a mesma frase, e o 404 tambem.** Codigo que nao
        existe, codigo de outro restaurante, campanha desativada e cupom de
        segmento que nao e o desta pessoa saem identicos. Distinguir os casos
        transformaria a rota num oraculo de quais codigos existem — que e o
        que o limite por IP (`COUPON_CLAIM_RATE_LIMIT`) encarece e esta
        resposta torna inutil (armadilha 18).

        Idempotente: resgatar de novo devolve o mesmo cupom, sem erro. O
        UNIQUE `(coupon_id, customer_id)` e a rede embaixo disso — duas
        requisicoes simultaneas nao criam duas linhas.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        coupon = self.repository.get_by_code_and_restaurant(payload.code, restaurant.id)
        if coupon is None or not coupon.is_active:
            self._raise_unknown_code()

        current = self._aware(self.clock())
        audience = self.audience_of(customer, restaurant.id, now=current)
        # Cupom de SEGMENTO nao se resgata digitando: quem se encaixa ja o
        # ve na lista, e quem nao se encaixa nao passa a ver por ter o
        # codigo. Um cupom publico resgatado nao muda nada e nao e erro —
        # a pessoa digitou um codigo que existe, e ele ja era dela.
        if coupon.visibility == COUPON_VISIBILITY_SEGMENT and not self._can_see(coupon, audience):
            self._raise_unknown_code()

        if self.repository.get_claim(coupon.id, customer.id) is None:
            try:
                self.repository.create_claim(coupon.id, customer.id)
                self.db.commit()
            except IntegrityError:
                # Duas requisicoes ao mesmo tempo: a outra ganhou, e o
                # resultado que a pessoa queria ja aconteceu.
                self.db.rollback()
            except Exception:
                self.db.rollback()
                raise
            audience = self.audience_of(customer, restaurant.id, now=current)

        # A sacola do Clube e VAZIA: o card volta com o minimo inteiro
        # faltando quando a campanha tem minimo, e e essa a verdade — o
        # cupom e dele e ainda nao cabe.
        evaluation = self.evaluate(
            coupon,
            restaurant_id=restaurant.id,
            subtotal=ZERO,
            delivery_fee=ZERO,
            customer=customer,
            audience=audience,
            now=current,
        )
        card = self._customer_card(coupon, evaluation)
        if card is None:
            self._raise_unknown_code()
        return CouponClaimResponse(coupon=card)

    @staticmethod
    def _raise_unknown_code() -> NoReturn:
        """UMA resposta para todos os jeitos de o codigo nao servir.

        Ver o docstring de `claim`. Mudar esta mensagem em um dos chamadores
        e so em um reabre a enumeracao de codigos.
        """
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Código de cupom inválido ou indisponível",
        )

    def preview(
        self,
        restaurant_slug: str,
        payload: CouponPreviewRequest,
        customer: Customer,
    ) -> CouponPreviewResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if payload.order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido inválido")
        effective_delivery_fee = ZERO if payload.order_type == "pickup" else payload.delivery_fee
        # UM instante para a busca e para a avaliacao. Lendo o relogio duas
        # vezes, o cupom podia passar pelo filtro e ser recusado por
        # `expired` na linha seguinte — a resposta contaria uma historia que
        # nunca foi verdade num unico momento.
        agora = self._aware(self.clock())
        # O `dentro_da_janela` nao e usado aqui, e isso e deliberado: o preview
        # existe para EXPLICAR, e quem explica e `evaluate` — com
        # `not_started` ou `expired`, que dizem coisas diferentes ao cliente.
        # Quem cobra que as duas formas da regra concordem e o checkout.
        coupon, _ = self._find_coupon(
            restaurant.id,
            coupon_id=payload.coupon_id,
            coupon_code=payload.coupon_code,
            for_update=False,
            agora=agora,
        )
        evaluation = self.evaluate(
            coupon,
            restaurant_id=restaurant.id,
            subtotal=payload.subtotal,
            delivery_fee=effective_delivery_fee,
            customer=customer,
            now=agora,
        )
        subtotal = quantize_money(payload.subtotal)
        delivery_fee = quantize_money(effective_delivery_fee)
        return CouponPreviewResponse(
            valid=evaluation.valid,
            coupon_id=coupon.id,
            coupon_code=coupon.code,
            discount_type=coupon.discount_type,
            discount_amount=evaluation.discount,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total_after_coupon=quantize_money(subtotal + delivery_fee - evaluation.discount),
            ineligibility_reason=evaluation.reason,
            next_available_at=evaluation.next_available_at,
        )

    def lock_and_validate_for_order(
        self,
        *,
        restaurant_id: UUID,
        coupon_id: UUID | None,
        coupon_code: str | None,
        subtotal: Decimal,
        delivery_fee: Decimal,
        customer: Customer | None,
    ) -> tuple[RestaurantCoupon, Decimal]:
        """A VALIDACAO QUE VALE. Tudo antes disto e preview.

        Roda com o cupom travado (`SELECT ... FOR UPDATE`) e dentro da
        transacao do pedido — e o que impede dois pedidos simultaneos de
        furarem o mesmo `total_usage_limit`.
        """
        if customer is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatório para usar cupom")
        # Ver o comentario do mesmo par em `preview`: um instante so.
        agora = self._aware(self.clock())
        coupon, dentro_da_janela_pelo_sql = self._find_coupon(
            restaurant_id,
            coupon_id=coupon_id,
            coupon_code=coupon_code,
            for_update=True,
            agora=agora,
        )
        evaluation = self.evaluate(
            coupon,
            restaurant_id=restaurant_id,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            customer=customer,
            now=agora,
        )
        if not evaluation.valid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=evaluation.reason or "Cupom inválido")

        # A SEGUNDA GUARDA, e ela so pode falhar se as duas formas da regra de
        # janela divergirem: o SQL de `filtro_de_janela` disse que este cupom
        # esta fora, e o Python de `evaluate` disse que ele vale.
        #
        # Isso nao deveria acontecer — as duas saem de `coupon_window.py`, lado
        # a lado, justamente para nao poderem discordar. Mas "nao deveria" e o
        # que a rodada anterior encontrou TRES vezes escrito de tres jeitos, e
        # aqui o preco de estar errado e desconto aplicado num pedido pago.
        #
        # 409 e nao 400: nao ha nada que o cliente possa mudar no corpo dele
        # para consertar isso. E defeito nosso, e o log tem que dizer isso alto.
        if not dentro_da_janela_pelo_sql:
            logger.error(
                "[Cupom] as duas formas da janela divergiram | coupon_id=%s | "
                "valid_from=%s | valid_until=%s | agora=%s",
                coupon.id,
                coupon.valid_from,
                coupon.valid_until,
                agora,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cupom indisponível neste momento",
            )
        return coupon, evaluation.discount

    def auto_apply_for_order(
        self,
        *,
        restaurant_id: UUID,
        subtotal: Decimal,
        delivery_fee: Decimal,
        customer: Customer | None,
    ) -> tuple[RestaurantCoupon, Decimal] | None:
        """O cupom SEM CODIGO que esta sacola ganha sozinha. `None` se nenhum.

        Chamado no `create_order` **somente quando o corpo nao trouxe cupom
        nenhum**. Seletor explicito vence sempre: e ele que faz "trocar de
        cupom" funcionar sem erro, porque o cliente escolhendo outro nao
        precisa desfazer o primeiro — o pedido carrega um cupom so, e o
        ultimo escolhido e o que vai.

        **So vale para cliente logado**, e nao e politica: `coupon_redemptions`
        tem `customer_id NOT NULL`, entao um desconto automatico para
        convidado nao teria onde registrar o uso — e o teto por cliente da
        campanha deixaria de existir para quem nao entrasse na conta.

        ## Por que escolher pelo MAIOR desconto, e o que desempata

        Entre dois cupons automaticos que cabem, o cliente nao tem tela para
        escolher — ele nem sabe que ha dois. Dar o menor seria a casa
        anunciando um desconto e entregando outro.

        O empate e resolvido por `sort_order` e depois por `id`, e o segundo
        criterio existe para a escolha ser **deterministica**: sem ele, dois
        cupons de R$ 10 se revezariam entre requisicoes e o total do mesmo
        pedido mudaria de nome de cupom entre o preview e o checkout.

        A confirmacao final passa por `lock_and_validate_for_order`, e nao
        pelo resultado desta busca: entre escolher e gravar, o teto total da
        campanha pode ter sido atingido por outro pedido.
        """
        if customer is None:
            return None
        current = self._aware(self.clock())
        # As campanhas automaticas sao lidas ANTES do publico, e a ordem e
        # economia no caminho mais quente do sistema: restaurante sem cupom
        # automatico — que e a maioria — paga UMA consulta a mais no
        # checkout, e nao tres. Montar a audiencia primeiro custaria a
        # agregacao de RFV e a lista de resgates em todo pedido, para nada.
        automaticos = [
            coupon
            for coupon in self.repository.list_in_window(restaurant_id, now=current)
            if coupon.code is None
        ]
        if not automaticos:
            return None

        audience = self.audience_of(customer, restaurant_id, now=current)
        candidatos = []
        for coupon in automaticos:
            evaluation = self.evaluate(
                coupon,
                restaurant_id=restaurant_id,
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                customer=customer,
                audience=audience,
                now=current,
            )
            if evaluation.valid:
                candidatos.append((evaluation.discount, coupon))

        escolhido = self._pick_automatic(candidatos)
        if escolhido is None:
            return None
        return self.lock_and_validate_for_order(
            restaurant_id=restaurant_id,
            coupon_id=escolhido.id,
            coupon_code=None,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            customer=customer,
        )

    @staticmethod
    def _pick_automatic(
        candidatos: list[tuple[Decimal, RestaurantCoupon]],
    ) -> RestaurantCoupon | None:
        """O cupom SEM CODIGO que entra sozinho, entre os que cabem.

        UMA funcao para os dois lados — o checkout (`auto_apply_for_order`) e
        o card do cliente (`auto_apply` em `list_for_customer`) —, porque a
        escolha e decisao de dinheiro: o card que diz "este entra sozinho"
        tem que ser o que o checkout de fato aplica, e duas copias da regra
        divergiriam na primeira mudanca de desempate.

        Maior desconto; empate por `sort_order` e depois por `id`, para a
        escolha ser deterministica entre duas requisicoes. Cupom com codigo
        e cupom cujo desconto nesta sacola e zero nao entram: o primeiro a
        pessoa precisa digitar, o segundo nao desconta nada.
        """
        elegiveis = [
            (desconto, coupon)
            for desconto, coupon in candidatos
            if coupon.code is None and desconto > ZERO
        ]
        if not elegiveis:
            return None
        return max(
            elegiveis,
            key=lambda par: (par[0], -(par[1].sort_order or 0), str(par[1].id)),
        )[1]

    def create_redemption(self, coupon: RestaurantCoupon, customer: Customer, order_id: UUID, discount: Decimal):
        existing = self.repository.get_redemption_by_order_id(order_id)
        if existing is not None:
            return existing
        return self.repository.create_redemption(
            coupon_id=coupon.id,
            customer_id=customer.id,
            order_id=order_id,
            discount_amount=quantize_money(discount),
            idempotency_key=f"order:{order_id}",
        )

    def reverse_for_order(self, order_id: UUID) -> None:
        redemption = self.repository.get_redemption_by_order_id(order_id)
        if redemption is not None and redemption.status == "applied":
            self.repository.reverse_redemption(redemption)

    def list_admin(self, restaurant_id: UUID) -> list[CouponAdminResponse]:
        self._get_restaurant(restaurant_id)
        coupons = self.repository.list_by_restaurant(restaurant_id)
        usage = self.repository.count_applied_by_coupon([coupon.id for coupon in coupons])
        return [self._admin_response(coupon, usage.get(coupon.id, 0)) for coupon in coupons]

    @staticmethod
    def _admin_response(coupon: RestaurantCoupon, total_usage_count: int) -> CouponAdminResponse:
        """O contador entra por fora porque nao e coluna do cupom.

        Ele sai de `coupon_redemptions`, e quem o conta muda conforme a rota: a
        LISTA usa uma agregacao unica para as campanhas todas, o POST sabe que e
        zero sem perguntar nada ao banco.
        """
        response = CouponAdminResponse.model_validate(coupon)
        response.total_usage_count = total_usage_count
        return response

    def list_templates(self) -> list[CouponTemplateResponse]:
        return [self._template_response(template) for template in self.repository.list_active_templates()]

    @staticmethod
    def _template_response(template: CouponTemplate) -> CouponTemplateResponse:
        return CouponTemplateResponse(
            id=template.id,
            name=template.name,
            image_path=template.image_path,
            image_url=build_storage_url(template.image_path),
            discount_type=template.discount_type,
            discount_value=template.discount_value,
            sort_order=template.sort_order or 0,
        )

    def create_admin(self, restaurant_id: UUID, payload: CouponCreate) -> CouponAdminResponse:
        self._get_restaurant(restaurant_id)
        template = self._load_active_template(payload.coupon_template_id)
        self._ensure_template_agrees(template, payload.discount_type, payload.discount_value)
        # Cupom sem codigo nao colide com nada: o UNIQUE do Postgres trata
        # NULL como distinto, e varias campanhas automaticas convivem no
        # mesmo restaurante. Sem esta guarda, `get_by_code_and_restaurant`
        # recebia `None` e estourava em `code.strip()`.
        if payload.code is not None and self.repository.get_by_code_and_restaurant(payload.code, restaurant_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Código de cupom já existe neste restaurante")
        coupon = RestaurantCoupon(restaurant_id=restaurant_id, **self._campaign_columns(payload))
        try:
            self.repository.create_coupon(coupon)
            self.db.commit()
            self.db.refresh(coupon)
        except IntegrityError as exc:
            self.db.rollback()
            self._raise_conflict(exc)
        except Exception:
            self.db.rollback()
            raise
        # Zero sem ir ao banco: cupom que acabou de nascer nao tem redencao, e
        # `coupon_redemptions` referencia um pedido que ainda nao existe.
        return self._admin_response(coupon, 0)

    def update_admin(self, restaurant_id: UUID, coupon_id: UUID, payload: CouponUpdate) -> CouponAdminResponse:
        """PATCH parcial, validado sobre o cupom INTEIRO depois do merge.

        Consequencia que vale saber antes de procurar bug: a concordancia com o
        template e conferida no resultado do merge, nao no que veio no corpo.
        Um cupom que ja esteja gravado divergente (so existe se alguem escrever
        por SQL — nenhuma rota grava assim desde este commit) recusa QUALQUER
        PATCH com 422, ate um `{"is_active": false}` que nao chega perto do
        tipo nem do valor do desconto.

        Nao e cupom preso: a saida e o proprio PATCH que conserta a mentira —
        `{"discount_type": ..., "discount_value": ...}` iguais aos do template,
        ou `{"coupon_template_id": <a arte certa>}`, passam e podem vir junto do
        `is_active` na mesma chamada. Conferir so quando o corpo TOCA nesses
        campos seria mais permissivo e pior: deixaria a divergencia sobreviver a
        todas as outras edicoes.

        **A arte segue a regra oposta, e a diferenca esta em `_template_do_patch`:**
        o tipo divergente e mentira do proprio cupom, e o lojista consegue
        conserta-la; a arte desativada e decisao da PLATAFORMA sobre o
        catalogo, e nao ha PATCH que a desfaca. Por isso a mescla so exige
        arte ativa quando o corpo TROCA de arte.
        """
        self._get_restaurant(restaurant_id)
        coupon = self.repository.get_by_id_and_restaurant(coupon_id, restaurant_id)
        if coupon is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cupom não encontrado")
        changes = payload.model_dump(exclude_unset=True)
        merged = {
            field: getattr(coupon, field)
            for field in CouponCampaignFields.model_fields
        }
        merged.update(changes)
        try:
            validated = CouponCampaignFields.model_validate(merged)
        except ValidationError as exc:
            self._raise_merged_validation(exc)
        template = self._template_do_patch(coupon, validated.coupon_template_id)
        self._ensure_template_agrees(template, validated.discount_type, validated.discount_value)
        if validated.code is not None:
            code_owner = self.repository.get_by_code_and_restaurant(validated.code, restaurant_id)
            if code_owner is not None and code_owner.id != coupon.id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Código de cupom já existe neste restaurante")
        for field, value in self._campaign_columns(validated).items():
            setattr(coupon, field, value)
        try:
            self.repository.save_coupon(coupon)
            self.db.commit()
            self.db.refresh(coupon)
        except IntegrityError as exc:
            self.db.rollback()
            self._raise_conflict(exc)
        except Exception:
            self.db.rollback()
            raise
        # Uma consulta a mais numa rota de escrita rara. Devolver `null` aqui
        # sairia mais barato e faria a linha editada perder o contador na tela,
        # bem no momento em que o lojista esta olhando para ela.
        return self._admin_response(coupon, self.repository.count_applied_total(coupon.id))

    @staticmethod
    def _campaign_columns(campaign: CouponCampaignFields) -> dict:
        """Os campos da campanha na forma que a COLUNA aceita.

        `visibility` e `target_segment` sao `str, Enum` no schema e `text` no
        banco, e `model_dump()` devolve o membro do enum, nao a string. Um
        membro chegando na coluna depende do adaptador do driver desembrulhar
        a subclasse de `str` sozinho — funciona hoje, e o dia em que parar de
        funcionar grava `CouponVisibility.PUBLIC` numa coluna com CHECK e o
        erro aparece como violacao de constraint, longe daqui.

        Desembrulhar explicitamente custa tres linhas e tira a duvida.
        """
        return {
            field: value.value if isinstance(value, Enum) else value
            for field, value in campaign.model_dump().items()
        }

    @staticmethod
    def _raise_unprocessable(errors: list[dict]) -> NoReturn:
        """422 na MESMA forma que o FastAPI usa para o corpo da requisicao.

        `{"detail": [{"loc", "msg", "type"}]}`, com `loc` comecando em `body`.
        A rota tem duas fontes de 422 — a validacao do corpo, que e do FastAPI,
        e as regras que so o service consegue conferir (a concordancia com o
        template e a revalidacao da mescla do PATCH). Devolver formas
        diferentes obrigaria o painel a adivinhar qual delas chegou antes de
        conseguir apontar o campo errado na tela.
        """
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    @staticmethod
    def _raise_merged_validation(exc: ValidationError) -> NoReturn:
        """A mescla do PATCH invalida e 422, e ate agora era 500.

        `CouponCampaignFields.model_validate(merged)` levanta `ValidationError`
        do pydantic, e o FastAPI so traduz a que ELE mesmo levanta ao montar o
        corpo — a que sai de dentro do handler sobe como excecao qualquer e vira
        "Internal Server Error". Ou seja: um PATCH com `valid_until` anterior ao
        `valid_from` respondia 500, sem nada no corpo que dissesse o motivo.

        `input` e `ctx` ficam de fora do que sai: eles carregam o valor cru, que
        pode ser `Decimal` ou `datetime` e nao atravessa o JSON.
        """
        CouponService._raise_unprocessable([
            {"loc": ["body", *erro["loc"]], "msg": erro["msg"], "type": erro["type"]}
            for erro in exc.errors()
        ])

    @staticmethod
    def _raise_conflict(exc: IntegrityError) -> NoReturn:
        """Traduz a violacao de UNIQUE para o campo que o lojista tem que mexer.

        Indice desconhecido NAO vira 409: relanca. Um 409 chutado manda o
        lojista mexer num campo que nao tem nada a ver com o que o banco
        recusou, e foi exatamente esse chute que segurou a arte repetida sob a
        mensagem do codigo. O erro relancado ja carrega o nome do indice.
        """
        diag = getattr(exc.orig, "diag", None)
        message = UNIQUE_INDEX_MESSAGES.get(getattr(diag, "constraint_name", None))
        if message is None:
            raise exc
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message) from exc

    def _find_coupon(
        self,
        restaurant_id: UUID,
        *,
        coupon_id: UUID | None,
        coupon_code: str | None,
        for_update: bool,
        agora: datetime,
    ) -> tuple[RestaurantCoupon, bool]:
        """O cupom que o CLIENTE apontou, E se ele esta dentro da janela.

        Devolve os DOIS, e a segunda metade e o ponto: sem ela, "nao existe" e
        "existe e venceu" viram a mesma resposta — e para o cliente essa
        diferenca decide o que ele faz em seguida. "Cupom nao encontrado" para
        um codigo que existe manda a pessoa conferir se digitou errado e tentar
        de novo; "cupom vencido" encerra o assunto.

        ## Duas consultas, e a segunda so no caminho raro

        A primeira usa `filtro_de_janela` — e o cupom aplicavel, e e o caminho
        comum. So quando ela volta vazia e que a segunda pergunta, **sem
        filtro**, se aquele codigo existe: e a diferenca entre as duas
        respostas.

        A segunda nunca pede `FOR UPDATE`. Travar linha que ja se sabe que nao
        vai ser aplicada seguraria o cupom de outro pedido por nada.

        ## A defesa em profundidade continua, e agora ela e CONFERIVEL

        O `dentro_da_janela` que sai daqui vem do **SQL**; o `not_started` /
        `expired` de `evaluate` vem do **Python**. Sao as duas formas da mesma
        regra (`src/services/coupon_window.py`), e quem as usa cobra que elas
        concordem — ver `lock_and_validate_for_order`. Duas formas que se
        conferem valem mais que uma que ninguem contesta: o defeito que este
        modulo inteiro existe para impedir e justamente elas divergirem.

        O painel NAO passa por aqui: ele chama o repositorio direto, sem
        `agora`, porque precisa enxergar a campanha vencida para edita-la.
        """
        dentro = self._buscar_cupom(
            restaurant_id,
            coupon_id=coupon_id,
            coupon_code=coupon_code,
            for_update=for_update,
            agora=agora,
        )
        if dentro is not None:
            return dentro, True

        fora = self._buscar_cupom(
            restaurant_id,
            coupon_id=coupon_id,
            coupon_code=coupon_code,
            for_update=False,
            agora=None,
        )
        if fora is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cupom não encontrado para este restaurante",
            )
        return fora, False

    def _buscar_cupom(
        self,
        restaurant_id: UUID,
        *,
        coupon_id: UUID | None,
        coupon_code: str | None,
        for_update: bool,
        agora: datetime | None,
    ) -> RestaurantCoupon | None:
        """A consulta crua. `agora=None` traz o cupom seja qual for a janela."""
        if for_update:
            return self.repository.lock_coupon(
                restaurant_id,
                coupon_id=coupon_id,
                coupon_code=coupon_code,
                agora=agora,
            )
        if coupon_id is not None:
            return self.repository.get_by_id_and_restaurant(
                coupon_id, restaurant_id, agora=agora
            )
        return self.repository.get_by_code_and_restaurant(
            coupon_code or "", restaurant_id, agora=agora
        )

    def _get_restaurant(self, restaurant_id: UUID):
        restaurant = self.restaurant_repository.get_by_id(restaurant_id)
        if restaurant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurante não encontrado")
        return restaurant

    def _load_active_template(self, template_id: UUID) -> CouponTemplate:
        """A arte que o lojista esta ESCOLHENDO. Aposentada nao serve.

        Quem esta MANTENDO a arte que ja usa passa por `_template_do_patch`, e
        o motivo esta la: as duas perguntas parecem a mesma e nao sao.
        """
        template = self.repository.get_template(template_id)
        if template is None or not template.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Template de cupom inválido")
        return template

    def _template_do_patch(self, coupon: RestaurantCoupon, template_id: UUID) -> CouponTemplate:
        """A arte que o PATCH vai gravar: ativa quando MUDA, qualquer uma quando FICA.

        O cupom preso, que este metodo existe para abrir. `_load_active_template`
        rodava sobre o resultado da mescla, e a mescla repete a arte que ja
        esta gravada — entao bastava a plataforma desativar uma arte para
        TODO PATCH daquele cupom responder 400 "Template de cupom invalido",
        inclusive um `{"is_active": false}` sozinho, que nao chega perto do
        campo. E nao havia saida: trocar de arte tambem e edicao, e desligar a
        campanha era exatamente o que o lojista estava tentando fazer.

        A linha e ESCOLHER contra MANTER. Recusar quem mantem nao protegia
        nada — a arte aposentada continua na vitrine, porque
        `list_public_available` nao olha `template.is_active` — e custava o
        unico caminho de tirar a campanha do ar.

        Consequencia de aceitar isto: `{"is_active": true}` sobre um cupom
        desligado com arte aposentada volta a por a campanha no ar. E
        coerente, e nao um buraco: desativar a arte esconde a arte do seletor
        do painel, nunca foi (nem hoje e) o jeito de derrubar campanha alheia.
        Quando precisar existir esse jeito, ele e uma alavanca propria da
        plataforma sobre o CUPOM, e nao um efeito colateral do catalogo.

        Nao vai ao banco: `get_by_id_and_restaurant` ja traz o template pelo
        `joinedload`, e a coluna e NOT NULL com FK — nao ha caminho que
        devolva nulo aqui.
        """
        if template_id != coupon.coupon_template_id:
            return self._load_active_template(template_id)
        return coupon.template

    @staticmethod
    def _ensure_template_agrees(
        template: CouponTemplate,
        discount_type: str,
        discount_value: Decimal,
    ) -> None:
        """O template e a ARTE que o cliente ve; o cupom e a conta que ele paga.

        Nada do template e lido pelo calculo — quem desconta e sempre o par
        `coupon.discount_type` / `coupon.discount_value`. Entao a arte e a conta
        podem divergir em DOIS eixos, e os dois mentem para o cliente do mesmo
        jeito: nada falha, nada e logado, e quem descobre e ele na tela de
        pagamento.

        - **tipo:** um cupom `percent` pendurado numa arte de frete gratis
          anuncia "Frete gratis" na vitrine e tira 10% no checkout;
        - **valor:** um cupom de 7% pendurado na arte de "10% OFF" anuncia dez
          e tira sete.

        O valor entrou aqui em 23/08/2026, e a lacuna era so essa: o tipo ja era
        conferido, o valor nao era conferido por linha nenhuma. Nao havia par
        errado gravado em producao porque o PAINEL copia o valor da arte ao
        montar o POST — o que e uma protecao de TELA, e some no dia em que
        aquela tela ganhar um campo de desconto editavel.

        Nao ha ramo para `template.discount_value` nulo porque a coluna e
        `numeric(10,2) DEFAULT 0 NOT NULL` no banco (conferido no
        `schema_baseline.sql`). O `Decimal | None` do model e do schema e mais
        frouxo que o banco, e nao o contrario.

        A recusa fica do lado do CUPOM, e nao do template, de proposito. Fazer o
        template IMPOR o tipo e o valor pareceria mais esperto e seria pior: o
        POST do painel passaria a gravar calado um valor diferente do que
        mandou, e o lojista veria o campo que ele preencheu voltar trocado.

        As duas divergencias saem JUNTAS na mesma lista quando acontecem juntas
        (trocar so a arte diverge nos dois eixos de uma vez). Uma por vez faria
        o lojista consertar o tipo, tomar 422 de novo pelo valor, e concluir que
        a tela esta quebrada.
        """
        divergencias = []
        if template.discount_type != discount_type:
            divergencias.append({
                "loc": ["body", "discount_type"],
                "msg": (
                    f"Tipo de desconto do cupom ({discount_type}) não confere com o do "
                    f"template ({template.discount_type})"
                ),
                "type": "coupon_template_discount_type_mismatch",
            })
        # `to_decimal` nos dois lados porque as colunas tem escalas diferentes:
        # `numeric(10,2)` no template e `numeric(12,2)` no cupom. A comparacao
        # de `Decimal` ja e numerica (10 == 10.00), e a conversao so garante que
        # o outro lado nao chegue como int ou float de um teste.
        if to_decimal(template.discount_value) != to_decimal(discount_value):
            divergencias.append({
                "loc": ["body", "discount_value"],
                "msg": (
                    f"Valor do desconto do cupom ({discount_value}) não confere com o do "
                    f"template ({template.discount_value})"
                ),
                "type": "coupon_template_discount_value_mismatch",
            })
        if divergencias:
            CouponService._raise_unprocessable(divergencias)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
