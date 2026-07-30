from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import (
    DEFAULT_PLATFORM_COMMISSION_PERCENT,
    ORDER_TYPES,
    PAYMENT_METHODS,
)
from src.models.customer_model import Customer
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.branch_repository import BranchRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.menu_repository import MenuRepository
from src.repositories.order_repository import OrderRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.delivery_schema import DeliveryAddressInput, DeliveryEstimateRequest
from src.schemas.order_schema import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderDetailResponse,
    OrderItemResponse,
)
from src.schemas.common_schema import StatusHistoryResponse
from src.services.branch_hours_service import BranchHoursService
from src.services.delivery_estimate_service import DeliveryEstimateService
from src.services.coupon_service import CouponService
from src.services.idempotency_service import IdempotencyService
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, money_to_float, quantize_money, to_decimal
from src.utils.normalization import normalize_digits
from src.utils.security import generate_tracking_token


CREATE_ORDER_ROUTE = "POST /restaurants/{restaurant_slug}/orders"


class OrderService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.branch_hours_service = BranchHoursService(db)
        self.menu_repository = MenuRepository(db)
        self.product_repository = ProductRepository(db)
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)
        self.coupon_service = CouponService(db)
        self.idempotency_service = IdempotencyService(db)

    def create_order(
        self,
        restaurant_slug: str,
        payload: CreateOrderRequest,
        current_customer: Customer | None = None,
        idempotency_key: str | None = None,
    ) -> CreateOrderResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        self._validate_order_type(payload.order_type)

        branch = self.branch_repository.get_active_by_id_and_restaurant(payload.branch_id, restaurant.id)
        if not branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filial inválida para este restaurante")

        address = self._resolve_order_address(payload, current_customer)
        self._validate_customer_snapshot(payload, current_customer)

        # As tres checagens abaixo ficam ANTES da reserva de idempotencia e da
        # chamada paga ao Google: sao consultas baratas e recusam cedo o
        # pedido que a loja nao poderia receber de jeito nenhum.
        settings = self.menu_repository.get_settings(restaurant.id)
        self._validate_store_accepts_order(payload.order_type, settings)
        self.branch_hours_service.ensure_branch_is_open(branch.id)
        payment_flow = self._resolve_payment_flow(branch.id, payload.payment_method)

        # Reserva antes de qualquer escrita e antes da chamada paga ao Google
        # Maps: um reenvio devolve a resposta gravada sem gastar rota nem
        # criar pedido. Fica na mesma transacao do INSERT do pedido, entao um
        # erro adiante libera a chave junto com o rollback.
        replayed = self.idempotency_service.begin(
            scope=self._idempotency_scope(restaurant.id, payload, current_customer),
            key=idempotency_key,
            request_fingerprint=IdempotencyService.fingerprint(payload.model_dump(mode="json")),
        )
        if replayed is not None:
            return CreateOrderResponse.model_validate(replayed)

        self._validate_delivery_address(payload, address)
        delivery_estimate = self._estimate_delivery(
            restaurant_slug,
            payload,
            address,
            current_customer,
        )
        products_by_id = self._get_valid_products(restaurant.id, [item.product_id for item in payload.items])

        selected_options_by_item = [
            self._validate_selected_options(products_by_id[item.product_id], item.selected_options)
            for item in payload.items
        ]
        subtotal = self._calculate_subtotal(payload, products_by_id, selected_options_by_item)
        self._validate_minimum_order_value(subtotal, settings)
        service_fee = self._calculate_service_fee(settings)
        delivery_fee = (
            quantize_money(to_decimal(delivery_estimate.delivery_fee))
            if delivery_estimate is not None
            else ZERO
        )
        coupon = None
        coupon_discount = ZERO
        discount_total = ZERO

        try:
            if payload.coupon_id is not None or payload.coupon_code is not None:
                coupon, coupon_discount = self.coupon_service.lock_and_validate_for_order(
                    restaurant_id=restaurant.id,
                    coupon_id=payload.coupon_id,
                    coupon_code=payload.coupon_code,
                    subtotal=subtotal,
                    delivery_fee=delivery_fee,
                    customer=current_customer,
                )
            cashback_redeemed = ZERO
            discount_total = quantize_money(coupon_discount + cashback_redeemed)
            total = quantize_money(subtotal + service_fee + delivery_fee - discount_total)
            commission_percent, commission_base, commission_amount = self._calculate_commission(
                subtotal=subtotal,
                coupon_discount=coupon_discount,
                cashback_redeemed=cashback_redeemed,
                settings=settings,
            )
            customer_name = current_customer.name if current_customer else payload.customer.name
            # Normaliza de novo na escrita, e nao so no schema: o snapshot e o
            # que get_customer_order compara, entao ele precisa estar em
            # digitos venha de onde vier. `current_customer.phone` ja e
            # normalizado no cadastro, mas contas antigas podem nao ser.
            customer_phone = normalize_digits(
                current_customer.phone if current_customer else payload.customer.phone
            )
            order = Order(
                restaurant_id=restaurant.id,
                tracking_token=generate_tracking_token(),
                branch_id=branch.id,
                customer_id=current_customer.id if current_customer else None,
                customer_address_id=payload.customer_address_id if current_customer else None,
                customer_name_snapshot=customer_name,
                customer_phone_snapshot=customer_phone,
                order_type=payload.order_type,
                status="pending",
                payment_method=payload.payment_method,
                payment_flow=payment_flow,
                # Pedido online nasce devendo: so vira "paid" quando o
                # gateway avisar, e so entao pode ser aceito. Pedido pago na
                # entrega nasce em `on_delivery` e nunca muda.
                payment_status="pending" if payment_flow == "online" else "on_delivery",
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                service_fee=service_fee,
                coupon_id=coupon.id if coupon else None,
                coupon_code_snapshot=coupon.code if coupon else None,
                coupon_discount_amount=coupon_discount,
                cashback_redeemed_amount=cashback_redeemed,
                discount_total=discount_total,
                total=total,
                commission_percent=commission_percent,
                commission_base_amount=commission_base,
                commission_amount=commission_amount,
                address_street=address.street if address else None,
                address_number=address.number if address else None,
                address_neighborhood=address.neighborhood if address else None,
                address_complement=address.complement if address else None,
                address_reference=address.reference if address else None,
                address_city=getattr(address, "city", None) if address else None,
                address_state=getattr(address, "state", None) if address else None,
                address_zipcode=getattr(address, "zipcode", None) if address else None,
                delivery_latitude=delivery_estimate.latitude if delivery_estimate else None,
                delivery_longitude=delivery_estimate.longitude if delivery_estimate else None,
                delivery_distance_km=delivery_estimate.distance_km if delivery_estimate else None,
                delivery_travel_time_min=delivery_estimate.travel_time_min if delivery_estimate else None,
                delivery_prep_time_min=delivery_estimate.prep_time_min if delivery_estimate else None,
                delivery_prep_time_max=delivery_estimate.prep_time_max if delivery_estimate else None,
                delivery_eta_min=delivery_estimate.eta_min if delivery_estimate else None,
                delivery_eta_max=delivery_estimate.eta_max if delivery_estimate else None,
                delivery_estimate_provider=delivery_estimate.provider if delivery_estimate else None,
                delivery_estimated_at=datetime.now(timezone.utc) if delivery_estimate else None,
                notes=payload.notes,
            )
            self.order_repository.create_order(order)
            if coupon is not None:
                self.coupon_service.create_redemption(coupon, current_customer, order.id, coupon_discount)

            order_items = [
                self._build_order_item(
                    order.id,
                    products_by_id[item.product_id],
                    item.quantity,
                    item.observation,
                    selected_options_by_item[index],
                )
                for index, item in enumerate(payload.items)
            ]
            self.order_repository.create_order_items(order_items)
            order_item_options = [
                self._build_order_item_option(order_item.id, group, option)
                for order_item, selected_options in zip(order_items, selected_options_by_item)
                for group, option in selected_options
            ]
            if order_item_options:
                self.order_repository.create_order_item_options(order_item_options)
            self.order_repository.create_status_history(
                OrderStatusHistory(order_id=order.id, status="pending", changed_by="system", note="Pedido criado")
            )
            response = CreateOrderResponse(
                id=order.id,
                order_number=order.order_number,
                # Unica vez que o token sai da API. Se o cliente perder,
                # nao ha como reemitir sem estar logado — e o preco de o
                # segredo ser a propria chave de acesso.
                tracking_token=order.tracking_token,
                status=order.status,
                payment_flow=order.payment_flow,
                payment_status=order.payment_status,
                subtotal=money_to_float(order.subtotal),
                delivery_fee=money_to_float(order.delivery_fee),
                service_fee=money_to_float(order.service_fee),
                coupon_code=order.coupon_code_snapshot,
                coupon_discount_amount=quantize_money(order.coupon_discount_amount),
                cashback_redeemed_amount=quantize_money(order.cashback_redeemed_amount),
                discount_total=quantize_money(order.discount_total),
                total=money_to_float(order.total),
                message="Pedido criado com sucesso",
            )
            # Gravada antes do commit para que a resposta armazenada e o
            # pedido entrem no banco atomicamente. Se o commit falhar, nao
            # sobra chave apontando para um pedido que nao existe.
            if self.idempotency_service.has_reservation:
                self.idempotency_service.complete(
                    response_body=response.model_dump(mode="json"),
                    order_id=order.id,
                )
            self.db.commit()
            self.db.refresh(order)
        except Exception:
            self.db.rollback()
            raise

        return response

    def _idempotency_scope(
        self,
        restaurant_id: UUID,
        payload: CreateOrderRequest,
        current_customer: Customer | None,
    ) -> str:
        # Identidade do solicitante: a conta quando ha login, senao o
        # telefone do pedido guest. Sem isso a chave de um cliente poderia
        # devolver o pedido de outro.
        if current_customer is not None:
            requester = f"customer:{current_customer.id}"
        else:
            requester = f"phone:{normalize_digits(payload.customer.phone)}"
        return IdempotencyService.build_scope(
            restaurant_id=restaurant_id,
            route=CREATE_ORDER_ROUTE,
            requester=requester,
        )

    def get_order_by_tracking_token(self, restaurant_slug: str, tracking_token: str) -> OrderDetailResponse:
        """Consulta publica de pedido, agora pelo token opaco.

        Substituiu `/orders/{order_number}?phone=...`. Aquela rota casava um
        numero de sequence GLOBAL com um telefone: quem tinha o telefone de
        alguem varria os numeros vizinhos ate achar o pedido e lia endereco
        residencial, itens e historico. O token nao e enumeravel e so quem
        criou o pedido o recebeu.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_tracking_token(restaurant.id, tracking_token)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return self.to_order_detail_response(order)

    def get_customer_order(self, customer: Customer, order_id: UUID) -> OrderDetailResponse:
        """Consulta de pedido do cliente autenticado.

        Sem token nenhum: o vinculo vem de `orders.customer_id`, entao um
        cliente so alcanca o proprio pedido mesmo tendo o UUID de outro.
        """
        order = self.order_repository.get_order_detail_for_customer(order_id, customer.id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return self.to_order_detail_response(order)

    def _validate_order_type(self, order_type: str) -> None:
        if order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido inválido")

    def _validate_store_accepts_order(self, order_type: str, settings) -> None:
        """is_open, accepts_delivery e accepts_pickup, finalmente lidos.

        Os tres campos existiam em restaurant_settings, apareciam no cardapio
        e nao bloqueavam nada: quem impedia pedido com a loja fechada era o
        frontend. Bastava chamar a API direto para furar.

        Sem linha de settings o restaurante segue aceitando pedido — e o
        comportamento que sempre valeu, e mudar isso agora derrubaria
        restaurantes que nunca configuraram nada.
        """
        if settings is None:
            return
        if settings.is_open is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A loja esta fechada no momento",
            )
        if order_type == "delivery" and settings.accepts_delivery is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esta loja nao esta aceitando entrega",
            )
        if order_type == "pickup" and settings.accepts_pickup is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esta loja nao esta aceitando retirada",
            )

    def _resolve_payment_flow(self, branch_id: UUID, payment_method: str | None) -> str:
        """Valida a forma de pagamento e diz por onde o dinheiro entra.

        Duas listas precisam concordar: PAYMENT_METHODS (o que a plataforma
        conhece) e branch_payment_methods (o que ESTA filial habilitou). Antes
        disso o campo era texto livre de 50 caracteres gravado direto no
        pedido — dava para fechar pedido com payment_method="banana", e a
        filial recebia um pedido em uma forma que ela nao aceita.

        Devolve "online" ou "delivery", que e o que decide se o pedido nasce
        esperando o gateway.
        """
        if not payment_method:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Forma de pagamento obrigatoria",
            )
        if payment_method not in PAYMENT_METHODS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Forma de pagamento invalida",
            )

        enabled_methods = self.branch_repository.list_enabled_payment_methods(branch_id)
        flows = {
            method.payment_flow
            for method in enabled_methods
            if method.method_type == payment_method
        }
        if not flows:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esta filial nao aceita esta forma de pagamento",
            )
        # A filial pode oferecer o mesmo metodo nos dois fluxos (pix pelo
        # gateway e pix na entrega, por exemplo). Sem um campo no pedido para
        # o cliente escolher, ficamos com o caminho mais restritivo: online
        # exige pagamento confirmado antes de o pedido ir para a cozinha, e
        # errar para o lado restritivo nao entrega comida de graca.
        return "online" if "online" in flows else "delivery"

    def _validate_customer_snapshot(self, payload: CreateOrderRequest, current_customer: Customer | None) -> None:
        if current_customer or payload.customer:
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatorio")

    def _validate_delivery_address(self, payload: CreateOrderRequest, address) -> None:
        if payload.order_type != "delivery":
            return
        if not address:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endereço é obrigatório para entrega")
        required_values = [address.street, address.number, address.neighborhood]
        if any(not value or not value.strip() for value in required_values):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endereço é obrigatório para entrega")

    def _resolve_order_address(self, payload: CreateOrderRequest, current_customer: Customer | None):
        if payload.customer_address_id and not current_customer:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatorio")
        if current_customer and payload.customer_address_id:
            address = self.customer_repository.get_address(current_customer.id, payload.customer_address_id)
            if not address:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endereco nao encontrado")
            return address
        return payload.address

    def _estimate_delivery(
        self,
        restaurant_slug: str,
        payload: CreateOrderRequest,
        address,
        current_customer: Customer | None,
    ):
        if payload.order_type != "delivery":
            return None
        inline_address = None
        if payload.customer_address_id is None:
            inline_address = DeliveryAddressInput(
                street=address.street,
                number=address.number,
                neighborhood=address.neighborhood,
                city=getattr(address, "city", None),
                state=getattr(address, "state", None),
                zipcode=getattr(address, "zipcode", None),
                latitude=getattr(address, "latitude", None),
                longitude=getattr(address, "longitude", None),
            )
        estimate = DeliveryEstimateService(self.db).estimate(
            restaurant_slug,
            DeliveryEstimateRequest(
                branch_id=payload.branch_id,
                address_id=payload.customer_address_id,
                address=inline_address,
            ),
            current_customer,
        )
        if not estimate.serviceable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=estimate.message or estimate.reason or "Endereco fora da area de entrega",
            )
        return estimate
    def _get_valid_products(self, restaurant_id: UUID, product_ids: list[UUID]) -> dict[UUID, object]:
        unique_ids = list(set(product_ids))
        products = self.product_repository.list_active_by_ids(restaurant_id, unique_ids)
        products_by_id = {product.id: product for product in products}
        if len(products_by_id) != len(unique_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Produto inválido ou indisponível")
        return products_by_id

    def _validate_selected_options(self, product, selected_options: list) -> list[tuple[object, object]]:
        active_groups = [group for group in product.option_groups if group.is_active]
        groups_by_id = {group.id: group for group in active_groups}
        selected_by_group: dict[UUID, list[object]] = {group.id: [] for group in active_groups}

        for selected in selected_options:
            group = groups_by_id.get(selected.option_group_id)
            if not group:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Grupo de opcao invalido para este produto",
                )
            option = next(
                (option for option in group.options if option.id == selected.option_id and option.is_active),
                None,
            )
            if not option:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Opcao invalida para este grupo",
                )
            if option.id in {item.id for item in selected_by_group[group.id]}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Opcao duplicada no mesmo produto",
                )
            selected_by_group[group.id].append(option)

        for group in active_groups:
            selected_count = len(selected_by_group[group.id])
            min_select = group.min_select or 0
            max_select = group.max_select or 0
            required_min = max(min_select, 1) if group.is_required else 0
            if selected_count < required_min:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Opcao obrigatoria nao selecionada: {group.name}",
                )
            if selected_count and selected_count < min_select:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Selecione pelo menos {min_select} opcoes em {group.name}",
                )
            if max_select and selected_count > max_select:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Selecione no maximo {max_select} opcoes em {group.name}",
                )

        return [
            (group, option)
            for group in active_groups
            for option in selected_by_group[group.id]
        ]

    def _calculate_subtotal(
        self,
        payload: CreateOrderRequest,
        products_by_id: dict[UUID, object],
        selected_options_by_item: list[list[tuple[object, object]]],
    ) -> Decimal:
        subtotal = ZERO
        for index, item in enumerate(payload.items):
            product = products_by_id[item.product_id]
            options_total = sum(
                (to_decimal(option.additional_price) for _, option in selected_options_by_item[index]),
                ZERO,
            )
            subtotal += (to_decimal(product.price) + options_total) * item.quantity
        return quantize_money(subtotal)

    def _calculate_commission(
        self,
        *,
        subtotal: Decimal,
        coupon_discount: Decimal,
        cashback_redeemed: Decimal,
        settings,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Comissao da plataforma sobre este pedido.

        Base = subtotal dos produtos - desconto de cupom - cashback usado.

        O que NAO entra na base, e por que:
        - taxa de entrega: e do entregador/da filial, nao e receita da venda;
        - taxa de servico: idem, e repasse;
        - taxa do gateway: custo de quem recebe, nao base de calculo.

        O desconto entra como subtracao porque o restaurante recebeu menos:
        cobrar comissao sobre um valor que ninguem pagou seria cobrar sobre
        o proprio desconto.

        Congelado no pedido de proposito. Se fosse calculado no fim do mes,
        mudar o percentual do restaurante hoje reescreveria a comissao de
        pedidos de tres semanas atras.
        """
        percent = self._commission_percent(settings)
        base = quantize_money(subtotal - coupon_discount - cashback_redeemed)
        # Desconto maior que o subtotal e possivel com cupom de frete
        # gratis somado a cashback. Base negativa viraria comissao negativa,
        # isto e, a plataforma pagando o restaurante.
        base = max(base, ZERO)
        amount = quantize_money(base * percent / Decimal("100"))
        return percent, base, amount

    @staticmethod
    def _commission_percent(settings) -> Decimal:
        # Restaurante sem linha de settings (ou com o campo nulo em banco
        # antigo) cai no padrao da plataforma. Devolver zero aqui seria
        # silenciosamente abrir mao da receita.
        if settings is None or settings.platform_commission_percent is None:
            return to_decimal(DEFAULT_PLATFORM_COMMISSION_PERCENT)
        return to_decimal(settings.platform_commission_percent)

    def _calculate_service_fee(self, settings) -> Decimal:
        if not settings or not settings.service_fee_enabled:
            return ZERO
        return quantize_money(to_decimal(settings.service_fee_amount))

    def _validate_minimum_order_value(self, subtotal: Decimal, settings) -> None:
        minimum_order_value = quantize_money(to_decimal(settings.min_order_value if settings else ZERO))
        if subtotal < minimum_order_value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Pedido abaixo do valor mínimo do restaurante.",
            )

    @staticmethod
    def _build_order_item(
        order_id: UUID,
        product,
        quantity: int,
        observation: str | None,
        selected_options: list[tuple[object, object]],
    ) -> OrderItem:
        options_total = sum((to_decimal(option.additional_price) for _, option in selected_options), ZERO)
        unit_price = quantize_money(to_decimal(product.price) + options_total)
        return OrderItem(
            order_id=order_id,
            product_id=product.id,
            product_code_snapshot=product.code,
            product_name_snapshot=product.name,
            product_description_snapshot=product.description,
            unit_price_snapshot=unit_price,
            quantity=quantity,
            observation=observation,
            total=quantize_money(unit_price * quantity),
        )

    @staticmethod
    def _build_order_item_option(order_item_id: UUID, group, option) -> OrderItemOption:
        return OrderItemOption(
            order_item_id=order_item_id,
            option_group_id=group.id,
            option_id=option.id,
            option_group_name_snapshot=group.name,
            option_name_snapshot=option.name,
            additional_price_snapshot=quantize_money(to_decimal(option.additional_price)),
        )

    @staticmethod
    def to_order_detail_response(order: Order) -> OrderDetailResponse:
        fallback_date = datetime.min.replace(tzinfo=timezone.utc)
        items = sorted(order.items, key=lambda item: item.created_at or fallback_date)
        history = sorted(order.status_history, key=lambda item: item.created_at or fallback_date)
        return OrderDetailResponse(
            id=order.id,
            order_number=order.order_number,
            restaurant_id=order.restaurant_id,
            branch_id=order.branch_id,
            customer_id=order.customer_id,
            customer_address_id=order.customer_address_id,
            customer_name_snapshot=order.customer_name_snapshot,
            customer_phone_snapshot=order.customer_phone_snapshot,
            order_type=order.order_type,
            status=order.status,
            payment_method=order.payment_method,
            payment_flow=order.payment_flow,
            payment_status=order.payment_status,
            paid_at=order.paid_at,
            subtotal=money_to_float(order.subtotal),
            delivery_fee=money_to_float(order.delivery_fee),
            service_fee=money_to_float(order.service_fee),
            coupon_code=order.coupon_code_snapshot,
            coupon_discount_amount=quantize_money(order.coupon_discount_amount),
            cashback_redeemed_amount=quantize_money(order.cashback_redeemed_amount),
            discount_total=quantize_money(order.discount_total),
            total=money_to_float(order.total),
            address_street=order.address_street,
            address_number=order.address_number,
            address_neighborhood=order.address_neighborhood,
            address_complement=order.address_complement,
            address_reference=order.address_reference,
            address_city=order.address_city,
            address_state=order.address_state,
            address_zipcode=order.address_zipcode,
            delivery_latitude=float(order.delivery_latitude) if order.delivery_latitude is not None else None,
            delivery_longitude=float(order.delivery_longitude) if order.delivery_longitude is not None else None,
            delivery_distance_km=money_to_float(order.delivery_distance_km) if order.delivery_distance_km is not None else None,
            delivery_travel_time_min=order.delivery_travel_time_min,
            delivery_prep_time_min=order.delivery_prep_time_min,
            delivery_prep_time_max=order.delivery_prep_time_max,
            delivery_eta_min=order.delivery_eta_min,
            delivery_eta_max=order.delivery_eta_max,
            delivery_estimate_provider=order.delivery_estimate_provider,
            delivery_estimated_at=order.delivery_estimated_at,
            notes=order.notes,
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=[
                OrderItemResponse(
                    id=item.id,
                    product_id=item.product_id,
                    product_code_snapshot=item.product_code_snapshot,
                    product_name_snapshot=item.product_name_snapshot,
                    product_description_snapshot=item.product_description_snapshot,
                    unit_price_snapshot=money_to_float(item.unit_price_snapshot),
                    quantity=item.quantity,
                    observation=item.observation,
                    total=money_to_float(item.total),
                    created_at=item.created_at,
                )
                for item in items
            ],
            status_history=[
                StatusHistoryResponse(
                    id=item.id,
                    status=item.status,
                    changed_by=item.changed_by,
                    note=item.note,
                    created_at=item.created_at,
                )
                for item in history
            ],
        )
