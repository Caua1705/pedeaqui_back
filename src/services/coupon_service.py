from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.customer_model import Customer
from src.core.constants import ORDER_TYPES
from src.repositories.coupon_repository import CouponRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.coupon_schema import (
    AvailableCouponResponse,
    AvailableCouponsResponse,
    CouponAdminResponse,
    CouponCreate,
    CouponTemplateResponse,
    CouponPreviewRequest,
    CouponPreviewResponse,
    CouponUpdate,
    CouponCampaignFields,
)
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, quantize_money, to_decimal
from src.utils.storage import build_storage_url


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
    "restaurant_coupons_restaurant_code_unique": "Codigo de cupom ja existe neste restaurante",
    # O mesmo codigo em outra caixa. A mensagem e a mesma de proposito: para o
    # lojista, PROMO10 e promo10 sao o mesmo cupom, e e assim que a busca trata.
    "uq_restaurant_coupons_restaurant_code_ci": "Codigo de cupom ja existe neste restaurante",
    "restaurant_coupons_restaurant_template_unique": (
        "Esta arte ja esta em uso por outra campanha deste restaurante"
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
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de desconto invalido")
        return quantize_money(max(discount, ZERO))

    def evaluate(
        self,
        coupon: RestaurantCoupon,
        *,
        restaurant_id: UUID,
        subtotal: Decimal,
        delivery_fee: Decimal,
        customer: Customer | None,
        require_public: bool,
        now: datetime | None = None,
    ) -> CouponEvaluation:
        current = self._aware(now or self.clock())
        valid_from = self._aware(coupon.valid_from)
        valid_until = self._aware(coupon.valid_until)
        subtotal = quantize_money(to_decimal(subtotal))
        minimum = quantize_money(to_decimal(coupon.min_order_value))

        if coupon.restaurant_id != restaurant_id:
            return CouponEvaluation(False, reason="coupon_from_another_restaurant")
        if not coupon.is_active:
            return CouponEvaluation(False, reason="inactive")
        if require_public and not coupon.is_public:
            return CouponEvaluation(False, reason="not_public")
        if current < valid_from:
            return CouponEvaluation(False, reason="not_started")
        if current > valid_until:
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

    def get_available(
        self,
        restaurant_slug: str,
        *,
        subtotal: Decimal | None,
        delivery_fee: Decimal | None,
        order_type: str | None,
        customer: Customer | None,
    ) -> AvailableCouponsResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if order_type is not None and order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido invalido")
        products_subtotal = quantize_money(to_decimal(subtotal))
        fee = quantize_money(to_decimal(delivery_fee))
        if order_type == "pickup":
            fee = ZERO
        responses: list[AvailableCouponResponse] = []

        current = self._aware(self.clock())
        for coupon in self.repository.list_public_available(restaurant.id, now=current):
            evaluation = self.evaluate(
                coupon,
                restaurant_id=restaurant.id,
                subtotal=products_subtotal,
                delivery_fee=fee,
                customer=customer,
                require_public=True,
                now=current,
            )
            if customer is not None and evaluation.reason in {"customer_limit_reached", "first_order_only"}:
                continue
            if evaluation.reason == "total_limit_reached":
                continue
            responses.append(
                AvailableCouponResponse(
                    id=coupon.id,
                    code=coupon.code,
                    title=coupon.title,
                    description=coupon.description,
                    discount_type=coupon.discount_type,
                    discount_value=quantize_money(to_decimal(coupon.discount_value)),
                    max_discount_amount=(
                        quantize_money(to_decimal(coupon.max_discount_amount))
                        if coupon.max_discount_amount is not None
                        else None
                    ),
                    min_order_value=quantize_money(to_decimal(coupon.min_order_value)),
                    valid_until=coupon.valid_until,
                    cooldown_days=coupon.cooldown_days,
                    eligible=evaluation.valid,
                    requires_login=evaluation.requires_login,
                    estimated_discount=evaluation.discount,
                    missing_amount=evaluation.missing_amount,
                    ineligibility_reason=evaluation.reason,
                    next_available_at=evaluation.next_available_at,
                )
            )
        return AvailableCouponsResponse(coupons=responses)

    def preview(
        self,
        restaurant_slug: str,
        payload: CouponPreviewRequest,
        customer: Customer,
    ) -> CouponPreviewResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        if payload.order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido invalido")
        effective_delivery_fee = ZERO if payload.order_type == "pickup" else payload.delivery_fee
        coupon = self._find_coupon(
            restaurant.id,
            coupon_id=payload.coupon_id,
            coupon_code=payload.coupon_code,
            for_update=False,
        )
        evaluation = self.evaluate(
            coupon,
            restaurant_id=restaurant.id,
            subtotal=payload.subtotal,
            delivery_fee=effective_delivery_fee,
            customer=customer,
            require_public=True,
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
        if customer is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatorio para usar cupom")
        coupon = self._find_coupon(
            restaurant_id,
            coupon_id=coupon_id,
            coupon_code=coupon_code,
            for_update=True,
        )
        evaluation = self.evaluate(
            coupon,
            restaurant_id=restaurant_id,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            customer=customer,
            require_public=True,
        )
        if not evaluation.valid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=evaluation.reason or "Cupom invalido")
        return coupon, evaluation.discount

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
        self._ensure_template_agrees(template, payload.discount_type)
        if self.repository.get_by_code_and_restaurant(payload.code, restaurant_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codigo de cupom ja existe neste restaurante")
        coupon = RestaurantCoupon(restaurant_id=restaurant_id, **payload.model_dump())
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
        tipo de desconto.

        Nao e cupom preso: a saida e o proprio PATCH que conserta a mentira —
        `{"discount_type": <o do template>}` ou `{"coupon_template_id": <a arte
        certa>}` passam, e podem vir junto do `is_active` na mesma chamada.
        Conferir so quando o corpo TOCA no tipo seria mais permissivo e pior:
        deixaria a divergencia sobreviver a todas as outras edicoes.
        """
        self._get_restaurant(restaurant_id)
        coupon = self.repository.get_by_id_and_restaurant(coupon_id, restaurant_id)
        if coupon is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cupom nao encontrado")
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
        template = self._load_active_template(validated.coupon_template_id)
        self._ensure_template_agrees(template, validated.discount_type)
        code_owner = self.repository.get_by_code_and_restaurant(validated.code, restaurant_id)
        if code_owner is not None and code_owner.id != coupon.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codigo de cupom ja existe neste restaurante")
        for field, value in validated.model_dump().items():
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
    ) -> RestaurantCoupon:
        coupon = (
            self.repository.lock_coupon(
                restaurant_id,
                coupon_id=coupon_id,
                coupon_code=coupon_code,
            )
            if for_update
            else (
                self.repository.get_by_id_and_restaurant(coupon_id, restaurant_id)
                if coupon_id is not None
                else self.repository.get_by_code_and_restaurant(coupon_code or "", restaurant_id)
            )
        )
        if coupon is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cupom nao encontrado para este restaurante")
        return coupon

    def _get_restaurant(self, restaurant_id: UUID):
        restaurant = self.restaurant_repository.get_by_id(restaurant_id)
        if restaurant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurante nao encontrado")
        return restaurant

    def _load_active_template(self, template_id: UUID) -> CouponTemplate:
        template = self.repository.get_template(template_id)
        if template is None or not template.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Template de cupom invalido")
        return template

    @staticmethod
    def _ensure_template_agrees(template: CouponTemplate, discount_type: str) -> None:
        """O template e a ARTE que o cliente ve; o cupom e a conta que ele paga.

        `template.discount_type` nao e lido por linha nenhuma do calculo — quem
        desconta e sempre `coupon.discount_type`. Entao um cupom `percent`
        pendurado numa arte de frete gratis anuncia "Frete gratis" na vitrine e
        tira 10% no checkout: nada falha, nada e logado, e quem descobre e o
        cliente na tela de pagamento.

        A recusa fica do lado do CUPOM, e nao do template, de proposito. Fazer o
        template IMPOR o tipo pareceria mais esperto e seria pior: o POST do
        painel passaria a gravar calado um valor diferente do que mandou, e o
        lojista veria o campo que ele preencheu voltar trocado.
        """
        if template.discount_type == discount_type:
            return
        CouponService._raise_unprocessable([
            {
                "loc": ["body", "discount_type"],
                "msg": (
                    f"Tipo de desconto do cupom ({discount_type}) nao confere com o do "
                    f"template ({template.discount_type})"
                ),
                "type": "coupon_template_discount_type_mismatch",
            }
        ])

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
