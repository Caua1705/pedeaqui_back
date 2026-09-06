import logging
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
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.menu_repository import MenuRepository
from src.repositories.order_repository import OrderRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.delivery_schema import DeliveryAddressInput, DeliveryEstimateRequest
from src.schemas.order_schema import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderDetailResponse,
    OrderItemOptionGroupResponse,
    OrderItemOptionResponse,
    OrderItemResponse,
)
from src.schemas.common_schema import StatusHistoryResponse
from src.services.branch_hours_service import BranchHoursService
from src.services.branch_operation import BranchOperation, resolve_branch_operation
from src.services.delivery_estimate_service import (
    DeliveryEstimateResult,
    DeliveryEstimateService,
    build_address_fingerprint,
)
from src.services.cashback_service import CashbackService
from src.services.coupon_service import CouponService
from src.services.idempotency_service import IdempotencyService
from src.services.order_state_machine import (
    INITIAL_ORDER_STATUS,
    INITIAL_PAYMENT_STATUS_BY_FLOW,
)
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, money_to_float, quantize_money, to_decimal
from src.utils.normalization import normalize_digits
from src.utils.security import generate_tracking_token, hash_tracking_token, utcnow


logger = logging.getLogger("uvicorn.error")

CREATE_ORDER_ROUTE = "POST /restaurants/{restaurant_slug}/orders"


class OrderService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.branch_hours_service = BranchHoursService(db)
        self.delivery_estimate_repository = DeliveryEstimateRepository(db)
        self.menu_repository = MenuRepository(db)
        self.product_repository = ProductRepository(db)
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)
        self.coupon_service = CouponService(db)
        self.cashback_service = CashbackService(db)
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
        operation = resolve_branch_operation(branch, settings)
        self._validate_branch_accepts_order(payload.order_type, operation)
        self.branch_hours_service.ensure_branch_is_open(branch.id)
        payment_flow = self._resolve_payment_flow(branch.id, payload.payment_method)

        # Reserva antes de qualquer escrita e antes da chamada paga ao Google
        # Maps: um reenvio devolve a resposta gravada sem gastar rota nem
        # criar pedido. Fica na mesma transacao do INSERT do pedido, entao um
        # erro adiante libera a chave junto com o rollback.
        replayed = self.idempotency_service.begin(
            scope=self._idempotency_scope(restaurant.id, payload, current_customer),
            key=idempotency_key,
            request_fingerprint=self._idempotency_fingerprint(payload),
        )
        if replayed is not None:
            return IdempotencyService.parse_stored_response(CreateOrderResponse, replayed)

        self._validate_delivery_address(payload, address)
        delivery_estimate = self._estimate_delivery(
            restaurant_slug,
            payload,
            address,
            current_customer,
            restaurant_id=restaurant.id,
        )
        products_by_id = self._get_valid_products(branch.id, [item.product_id for item in payload.items])

        selected_options_by_item = [
            self._validate_selected_options(products_by_id[item.product_id], item.selected_options)
            for item in payload.items
        ]
        subtotal = self._calculate_subtotal(payload, products_by_id, selected_options_by_item)
        self._validate_minimum_order_value(subtotal, operation)
        service_fee = self._calculate_service_fee(operation)
        delivery_fee = (
            quantize_money(to_decimal(delivery_estimate.delivery_fee))
            if delivery_estimate is not None
            else ZERO
        )
        # O frete gratis e aplicado AQUI — depois do subtotal (que e o que a
        # regra compara) e ANTES do cupom (que pode ser do tipo
        # `free_delivery` e descontaria a taxa de novo, devolvendo dinheiro
        # sobre uma taxa que ninguem ia pagar).
        delivery_fee, delivery_fee_waived = self._apply_free_delivery(
            delivery_fee, subtotal, operation
        )
        coupon = None
        coupon_discount = ZERO
        discount_total = ZERO

        try:
            # A ORDEM DOS DOIS LOCKS E FIXA: cliente primeiro, cupom depois,
            # em todo caminho. Duas transacoes que peguem os mesmos dois
            # recursos em ordens opostas fecham ciclo, e o Postgres mata uma
            # por deadlock — no checkout, no horario de pico.
            #
            # O lock e tomado ANTES de ler o saldo (que so acontece depois do
            # cupom, porque o teto do resgate depende dele): sem ele, dois
            # pedidos simultaneos do mesmo cliente leem o mesmo saldo e
            # gastam o dinheiro duas vezes.
            self._lock_customer_for_cashback(payload, current_customer)
            coupon, coupon_discount = self._resolve_coupon(
                payload,
                restaurant_id=restaurant.id,
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                current_customer=current_customer,
            )
            cashback_redeemed = self._redeem_cashback(
                payload,
                current_customer,
                restaurant_id=restaurant.id,
                branch_id=branch.id,
                teto=quantize_money(subtotal - coupon_discount),
            )
            discount_total = quantize_money(coupon_discount + cashback_redeemed)
            total = quantize_money(subtotal + service_fee + delivery_fee - discount_total)
            commission_percent, commission_base, commission_amount = self._calculate_commission(
                subtotal=subtotal,
                coupon_discount_on_products=self._coupon_discount_on_products(
                    coupon, coupon_discount
                ),
                cashback_redeemed=cashback_redeemed,
                settings=settings,
            )
            customer_name = current_customer.name if current_customer else payload.customer.name
            # Normaliza de novo na escrita, e nao so no schema: o telefone do
            # snapshot tambem alimenta o escopo da idempotencia, entao ele
            # precisa estar em digitos venha de onde vier.
            # `current_customer.phone` ja e normalizado no cadastro, mas
            # contas antigas podem nao ser.
            customer_phone = normalize_digits(
                current_customer.phone if current_customer else payload.customer.phone
            )
            # O UNICO lugar em que o token existe em claro. Ele vive nesta
            # variavel local ate entrar na resposta; o banco recebe so o
            # hash, e nao ha caminho de volta (armadilha 19: nao existe rota
            # de reemissao, e agora tambem nao existiria como atende-la).
            tracking_token = generate_tracking_token()
            order = Order(
                restaurant_id=restaurant.id,
                tracking_token_hash=hash_tracking_token(tracking_token),
                branch_id=branch.id,
                customer_id=current_customer.id if current_customer else None,
                customer_address_id=payload.customer_address_id if current_customer else None,
                customer_name_snapshot=customer_name,
                customer_phone_snapshot=customer_phone,
                order_type=payload.order_type,
                status=INITIAL_ORDER_STATUS,
                payment_method=payload.payment_method,
                payment_flow=payment_flow,
                # Pedido online nasce devendo: so vira "paid" quando o
                # gateway avisar, e so entao pode ser aceito. Pedido pago na
                # entrega nasce em `on_delivery` e nunca muda.
                payment_status=INITIAL_PAYMENT_STATUS_BY_FLOW[payment_flow],
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                delivery_fee_waived=delivery_fee_waived,
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
            # Depois do INSERT do pedido porque a linha do razao aponta para
            # ele, e na mesma transacao pelo mesmo motivo: ou o pedido e o
            # debito existem os dois, ou nenhum dos dois.
            self.cashback_service.register_redemption(order, cashback_redeemed)

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
                OrderStatusHistory(
                    order_id=order.id,
                    status=INITIAL_ORDER_STATUS,
                    changed_by="system",
                    note="Pedido criado",
                )
            )
            response = CreateOrderResponse(
                id=order.id,
                order_number=order.order_number,
                # Unica vez que o token sai da API, e a unica vez que ele
                # existe em claro fora da memoria deste processo. Se o
                # cliente perder, nao ha como reemitir: o banco so tem o
                # hash.
                tracking_token=tracking_token,
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

    @staticmethod
    def _idempotency_fingerprint(payload: CreateOrderRequest) -> str:
        """sha256 do corpo do pedido, para separar retry de conflito.

        Mesma chave e mesmo corpo e RETRY: devolve a resposta gravada. Mesma
        chave com corpo diferente e CONFLITO: 422, pedindo chave nova.

        **Todo campo do corpo entra hoje, e isso e uma consequencia, nao uma
        escolha permanente.** O `source` do funil ficava de fora daqui porque
        nao mudava item, preco, endereco nem forma de pagamento — a mesma
        chave com outra origem tinha que devolver o pedido original. Com a
        frente de origem removida, nao sobrou nenhum campo dessa natureza, e
        a exclusao virou lista vazia.

        A regra que fica, e que vale para o proximo campo (armadilha 37):

        - campo que **nao muda o que foi pedido** volta a entrar numa lista
          de exclusao aqui;
        - campo que **muda o pedido** fica de fora dela — `use_cashback` e o
          caso, porque ele muda o total, e ai o conflito e a resposta certa.

        E o custo de qualquer campo novo no corpo e o mesmo dos dois lados: o
        fingerprint sai de `model_dump()`, entao um campo a mais muda o hash
        de TODO corpo, e uma chave reservada antes do deploy e retentada
        depois recebe 422 por um pedido identico durante as 24h de vida dela.
        E a armadilha 7 pelo outro lado — la o custo era acrescentar campo
        obrigatorio na RESPOSTA gravada, aqui e acrescentar qualquer campo ao
        CORPO que a assina.
        """
        corpo = payload.model_dump(mode="json")
        return IdempotencyService.fingerprint(corpo)

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

    @staticmethod
    def _apply_free_delivery(
        delivery_fee: Decimal,
        subtotal: Decimal,
        operation: BranchOperation,
    ) -> tuple[Decimal, Decimal]:
        """Zera a taxa quando o pedido alcanca o valor da campanha.

        Devolve a taxa a cobrar e QUANTO foi perdoado — o segundo so existe
        para ser gravado. Ver `orders.delivery_fee_waived`: sem capturar isso
        na escrita, "quanto essa campanha me custou em agosto" fica sem
        resposta para sempre, porque nao da para recalcular a rota de um
        pedido que ja passou.

        A comparacao e com o SUBTOTAL (produtos), e nao com o total: incluir a
        propria taxa faria o frete gratis se autoconceder — um pedido de
        R$ 55 com taxa de R$ 7 alcancaria o teto de R$ 60 por causa da taxa
        que ele esta tentando nao pagar. E o mesmo motivo pelo qual
        `min_order_value` tambem olha o subtotal.

        `>=` e nao `>`: "acima de R$ 60" na tela do lojista significa "60
        fecha o pedido com entrega gratis" para todo cliente que le a frase.

        Nao faz nada quando a filial nao aderiu a campanha, quando ela esta
        ligada sem valor cadastrado (nao ha o que comparar) e na retirada,
        onde a taxa ja e zero e perdoar zero nao e informacao nenhuma.
        """
        if not operation.free_delivery_enabled:
            return delivery_fee, ZERO
        if operation.free_delivery_min_order_value is None:
            return delivery_fee, ZERO
        if delivery_fee <= ZERO:
            return delivery_fee, ZERO
        if subtotal < quantize_money(to_decimal(operation.free_delivery_min_order_value)):
            return delivery_fee, ZERO
        return ZERO, delivery_fee

    def _validate_order_type(self, order_type: str) -> None:
        if order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido inválido")

    def _validate_branch_accepts_order(
        self,
        order_type: str,
        operation: BranchOperation,
    ) -> None:
        """is_open, accepts_delivery e accepts_pickup — da FILIAL escolhida.

        Os tres campos existiam em `restaurant_settings` e valiam para a rede
        inteira: quem fechasse a filial do Centro fechava a da Aldeota junto,
        e nao havia como fechar so uma. Desde a revisao 20260818_0025 eles sao
        da filial, e e a filial DESTE pedido que decide.

        Sao checagem de verdade, e nao enfeite de tela: antes de existirem,
        quem impedia pedido com a loja fechada era o frontend, e bastava
        chamar a API direto para furar.

        Isto NAO substitui `ensure_branch_is_open`: aquele le a agenda da
        semana, este le a pausa manual. Estar dentro do horario nao significa
        que o balcao nao apertou "fechar agora" as 21h.
        """
        if not operation.is_open:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A loja está fechada no momento",
            )
        if order_type == "delivery" and not operation.accepts_delivery:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esta loja não está aceitando entrega",
            )
        if order_type == "delivery" and not operation.accepts_delivery_now:
            # A pausa temporaria precisa ser conferida AQUI, e nao so na
            # estimativa: o pedido que chega com `delivery_estimate_token`
            # valido reaproveita a estimativa guardada e nao passa por
            # `DeliveryEstimateService.estimate()`. Sem esta linha, o cliente
            # que estimou as 18h55 fecharia o pedido as 19h05, com a entrega
            # pausada desde as 19h — e a loja receberia justamente o pedido
            # que pausou para nao receber.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A entrega está pausada no momento",
            )
        if order_type == "pickup" and not operation.accepts_pickup:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Esta loja não está aceitando retirada",
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
                detail="Forma de pagamento obrigatória",
            )
        if payment_method not in PAYMENT_METHODS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Forma de pagamento inválida",
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
                detail="Esta filial não aceita esta forma de pagamento",
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatório")

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
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Cliente autenticado obrigatório")
        if current_customer and payload.customer_address_id:
            address = self.customer_repository.get_address(current_customer.id, payload.customer_address_id)
            if not address:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endereço não encontrado")
            return address
        return payload.address

    def _estimate_delivery(
        self,
        restaurant_slug: str,
        payload: CreateOrderRequest,
        address,
        current_customer: Customer | None,
        restaurant_id: UUID,
    ):
        if payload.order_type != "delivery":
            return None

        estimate_request = self._build_estimate_request(payload, address)
        reused = self._reuse_stored_estimate(
            token=payload.delivery_estimate_token,
            restaurant_id=restaurant_id,
            branch_id=payload.branch_id,
            estimate_request=estimate_request,
            current_customer=current_customer,
        )
        if reused is not None:
            return reused

        estimate = DeliveryEstimateService(self.db).estimate(
            restaurant_slug,
            estimate_request,
            current_customer,
        )
        if not estimate.serviceable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=estimate.message or estimate.reason or "Endereço fora da área de entrega",
            )
        return estimate

    @staticmethod
    def _build_estimate_request(payload: CreateOrderRequest, address) -> DeliveryEstimateRequest:
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
        return DeliveryEstimateRequest(
            branch_id=payload.branch_id,
            address_id=payload.customer_address_id,
            address=inline_address,
        )

    def _reuse_stored_estimate(
        self,
        *,
        token: str | None,
        restaurant_id: UUID,
        branch_id: UUID,
        estimate_request: DeliveryEstimateRequest,
        current_customer: Customer | None,
    ):
        """Aproveita a estimativa que o cliente ja pediu no checkout.

        Sem isto, criar o pedido refazia geocode + rota no Google poucos
        minutos depois de /delivery/estimate ter feito exatamente as mesmas
        duas chamadas: custo dobrado por pedido, e a conexao de banco presa
        durante um I/O externo que pode levar segundos.

        NADA de valor vem do cliente — ele devolve so o token. Taxa,
        distancia e prazo sao lidos da linha em `delivery_estimates`.

        Qualquer divergencia (outra filial, outro endereco, outro cliente,
        vencida) simplesmente cai fora e o Google e chamado de novo. E o
        caminho seguro: no pior caso pagamos a chamada que pagariamos antes.
        """
        if not token:
            return None

        stored = self.delivery_estimate_repository.get_valid_by_token(token, utcnow())
        if stored is None:
            self._log_estimate_not_reused("token_invalido_ou_vencido")
            return None
        if stored.restaurant_id != restaurant_id or stored.branch_id != branch_id:
            self._log_estimate_not_reused("restaurante_ou_filial_diferente")
            return None
        expected_customer_id = current_customer.id if current_customer else None
        if stored.customer_id != expected_customer_id:
            # Estimativa de um cliente logado nao vale para outro nem para
            # visitante: ela pode ter usado um endereco salvo da conta.
            self._log_estimate_not_reused("cliente_diferente")
            return None
        if stored.address_fingerprint != build_address_fingerprint(estimate_request):
            # A defesa que importa: estimar o endereco perto e fechar o
            # pedido para o distante pagando a taxa do primeiro.
            self._log_estimate_not_reused("endereco_diferente")
            return None

        logger.info(
            "[Delivery estimate] reaproveitada estimate_id=%s provider=%s",
            stored.id,
            stored.provider,
        )
        return DeliveryEstimateResult(
            serviceable=True,
            reason=None,
            message=None,
            distance_km=float(stored.distance_km) if stored.distance_km is not None else None,
            travel_time_min=stored.travel_time_min,
            prep_time_min=stored.prep_time_min,
            prep_time_max=stored.prep_time_max,
            eta_min=stored.eta_min,
            eta_max=stored.eta_max,
            delivery_fee=money_to_float(stored.delivery_fee),
            provider=stored.provider,
            fallback=False,
            latitude=float(stored.latitude) if stored.latitude is not None else None,
            longitude=float(stored.longitude) if stored.longitude is not None else None,
        )

    @staticmethod
    def _log_estimate_not_reused(reason: str) -> None:
        # Em info e nao warning: nao e erro, e o custo de uma chamada ao
        # Google. Vira o primeiro grep quando a conta do Maps subir.
        logger.info("[Delivery estimate] estimativa nao reaproveitada motivo=%s", reason)
    def _get_valid_products(self, branch_id: UUID, product_ids: list[UUID]) -> dict[UUID, object]:
        """Os produtos do pedido, conferidos contra a FILIAL que vai produzi-lo.

        `branch_id` e nao `restaurant_id` desde a revisao 20260820_0026, e e a
        barreira que faltava: com o cardapio por filial, um `product_id` da
        loja do Centro num pedido da Aldeota passava por aqui sem nada
        recusa-lo. O pedido entrava, com o preco do Centro, e quem descobria
        era a cozinha da Aldeota procurando um produto que ela nao tem.

        O 400 nao diz QUAL produto, pelo mesmo motivo de sempre (armadilha
        23): a rota e publica e nao pode virar oraculo de quais UUIDs
        existem.
        """
        unique_ids = list(set(product_ids))
        products = self.product_repository.list_active_by_ids(branch_id, unique_ids)
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
                    detail="Grupo de opção inválido para este produto",
                )
            option = next(
                (option for option in group.options if option.id == selected.option_id and option.is_active),
                None,
            )
            if not option:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Opção inválida para este grupo",
                )
            if option.id in {item.id for item in selected_by_group[group.id]}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Opção duplicada no mesmo produto",
                )
            selected_by_group[group.id].append(option)

        for group in active_groups:
            # GRUPO OBRIGATORIO SEM OPCAO ATIVA CONTINUA RECUSANDO O PEDIDO, e
            # isso e proposital: grupo obrigatorio existe porque a cozinha nao
            # produz sem aquela informacao, e vender sem ela manda uma picanha
            # sem ponto para a chapa.
            #
            # O produto ja nao aparece no cardapio nesse estado
            # (`MenuService._blocking_required_group`), entao quem chega aqui
            # e carrinho montado ANTES de o lojista desativar a ultima opcao.
            # Este 400 e a rede desse caso — o mesmo papel que o 400 de
            # `is_available` cumpre para o produto esgotado (armadilha 23).
            selected_count = len(selected_by_group[group.id])
            min_select = group.min_select or 0
            max_select = group.max_select or 0
            required_min = max(min_select, 1) if group.is_required else 0
            if selected_count < required_min:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Opção obrigatória não selecionada: {group.name}",
                )
            if selected_count and selected_count < min_select:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Selecione pelo menos {min_select} opções em {group.name}",
                )
            if max_select and selected_count > max_select:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Selecione no máximo {max_select} opções em {group.name}",
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

    def _resolve_coupon(
        self,
        payload: CreateOrderRequest,
        *,
        restaurant_id,
        subtotal: Decimal,
        delivery_fee: Decimal,
        current_customer,
    ) -> tuple:
        """O cupom deste pedido: o escolhido, ou o automatico, ou nenhum.

        **UM CUPOM POR PEDIDO, e a estrutura garante isso** — nao ha lista
        aqui, `orders.coupon_id` e uma coluna so e `coupon_redemptions` tem
        UNIQUE em `order_id`. Trocar de cupom nao precisa de rota de
        "remover": o corpo do pedido carrega o ultimo escolhido, e o
        anterior simplesmente nao foi mandado.

        **Seletor explicito desliga a auto-aplicacao.** Sem essa ordem, o
        cliente que escolhe um cupom de R$ 10 ganharia tambem o automatico de
        R$ 5 — dois cupons no mesmo pedido pela porta dos fundos, exatamente
        o que a coluna unica existe para impedir.

        A auto-aplicacao roda dentro da MESMA transacao e com o mesmo lock do
        caminho explicito: ela termina em `lock_and_validate_for_order`.

        Cupom e cashback CONTINUAM ACUMULANDO. Sao coisas diferentes — o
        cupom e campanha do lojista, o cashback e dinheiro que o cliente ja
        tinha — e o teto do resgate ja desconta o cupom (`subtotal -
        coupon_discount`), entao os dois juntos nunca passam do subtotal.
        """
        # A forma de pagamento do pedido ja foi validada por
        # `_resolve_payment_flow` antes daqui; e por ela que "so no pix"
        # barra de verdade quem escolheu o cartao.
        if payload.coupon_id is not None or payload.coupon_code is not None:
            return self.coupon_service.lock_and_validate_for_order(
                restaurant_id=restaurant_id,
                coupon_id=payload.coupon_id,
                coupon_code=payload.coupon_code,
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                customer=current_customer,
                payment_method=payload.payment_method,
            )
        automatico = self.coupon_service.auto_apply_for_order(
            restaurant_id=restaurant_id,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            customer=current_customer,
            payment_method=payload.payment_method,
        )
        if automatico is None:
            return None, ZERO
        return automatico

    def _lock_customer_for_cashback(
        self,
        payload: CreateOrderRequest,
        current_customer: Customer | None,
    ) -> None:
        """Trava a linha do cliente antes de qualquer leitura de saldo.

        So quando o pedido vai mesmo mexer no saldo: travar a linha de todo
        cliente em todo pedido serializaria pedidos que nao disputam nada.
        """
        if not payload.use_cashback:
            return
        if current_customer is None:
            return
        self.customer_repository.lock_customer(current_customer.id)

    def _redeem_cashback(
        self,
        payload: CreateOrderRequest,
        current_customer: Customer | None,
        *,
        restaurant_id: UUID,
        branch_id: UUID,
        teto: Decimal,
    ) -> Decimal:
        """Quanto de cashback este pedido resgata.

        Convidado com `use_cashback` recebe 401, e nao um resgate silencioso
        de zero: e a mesma resposta que o cupom da (`lock_and_validate_for_order`),
        e as duas mecanicas de desconto nao podem divergir em nada que o
        lojista ou o app precisem explicar. Sem conta nao ha saldo.

        Saldo abaixo do minimo, loja sem campanha e teto zerado devolvem ZERO
        sem erro nenhum: nada disso e culpa de quem esta pedindo, e o valor
        efetivo volta na resposta em `cashback_redeemed_amount`.
        """
        if not payload.use_cashback:
            return ZERO
        if current_customer is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cliente autenticado obrigatório para usar cashback",
            )
        return self.cashback_service.amount_to_redeem(
            customer=current_customer,
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            momento=utcnow(),
            teto=teto,
        )

    @staticmethod
    def _coupon_discount_on_products(coupon, coupon_discount: Decimal) -> Decimal:
        """Quanto do desconto do cupom saiu da MERCADORIA.

        Para cupom de valor (`fixed`, `percent`) e o desconto inteiro: ele
        derruba o preco dos produtos, e o restaurante recebeu menos por eles.

        Para `free_delivery` e ZERO, e essa e a razao desta funcao existir.
        Aquele desconto vale exatamente a taxa de entrega — que NAO esta na
        base da comissao, porque nao e receita da venda. Subtrai-lo da base
        tirava comissao de mercadoria que o cliente pagou integralmente.

        E ficava incoerente com o frete gratis por REGRA
        (`_apply_free_delivery`), que zera a mesma taxa e nao mexe na base:
        dois caminhos para o mesmo frete gratis, cobrando comissoes
        diferentes pelo mesmo pedido.
        """
        if coupon is None:
            return ZERO
        if coupon.discount_type == "free_delivery":
            return ZERO
        return coupon_discount

    def _calculate_commission(
        self,
        *,
        subtotal: Decimal,
        coupon_discount_on_products: Decimal,
        cashback_redeemed: Decimal,
        settings,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Comissao da plataforma sobre este pedido.

        Base = subtotal dos produtos - desconto de cupom SOBRE PRODUTO -
        cashback usado.

        O que NAO entra na base, e por que:
        - taxa de entrega: e do entregador/da filial, nao e receita da venda;
        - taxa de servico: idem, e repasse;
        - taxa do gateway: custo de quem recebe, nao base de calculo.

        O desconto entra como subtracao porque o restaurante recebeu menos:
        cobrar comissao sobre um valor que ninguem pagou seria cobrar sobre
        o proprio desconto. **Mas so o desconto que saiu do produto** — quem
        separa os dois e `_coupon_discount_on_products`, e o parametro tem
        esse nome para nao aceitar o desconto cru sem alguem reparar.

        Congelado no pedido de proposito. Se fosse calculado no fim do mes,
        mudar o percentual do restaurante hoje reescreveria a comissao de
        pedidos de tres semanas atras.
        """
        percent = self._commission_percent(settings)
        base = quantize_money(subtotal - coupon_discount_on_products - cashback_redeemed)
        # Piso em zero. Hoje nenhum caminho chega a base negativa — o cupom
        # de valor ja e limitado ao subtotal e o frete gratis nao entra mais
        # nesta conta —, mas base negativa seria comissao negativa, isto e, a
        # plataforma pagando o restaurante. O piso fica.
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

    def _calculate_service_fee(self, operation: BranchOperation) -> Decimal:
        if not operation.service_fee_enabled:
            return ZERO
        return operation.service_fee_amount

    def _validate_minimum_order_value(
        self,
        subtotal: Decimal,
        operation: BranchOperation,
    ) -> None:
        if subtotal < operation.min_order_value:
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
            # Congelada aqui pelo mesmo motivo do nome: `product_id` aponta
            # para a linha de UMA filial e essa linha pode ser renomeada
            # depois. E o que faz `/admin/reports/products` somar as duas
            # lojas na mesma linha mesmo que uma delas renomeie o produto.
            catalog_key_snapshot=product.catalog_key,
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
    def _item_option_groups(item: OrderItem) -> list[OrderItemOptionGroupResponse]:
        """Os adicionais do item, reunidos pelo grupo em que foram escolhidos.

        Sem isto o detalhe do pedido nao tinha como mostrar complemento
        nenhum, e a cozinha nao conseguia preparar um item que trocou o
        arroz por espaguete: os snapshots estavam gravados em
        `order_item_options` e nao apareciam no contrato.

        A ordem e imposta aqui porque `order_item_options` nao tem coluna de
        ordem nem `created_at`, e o id e aleatorio (`gen_random_uuid`) — sem
        `sorted`, a mesma comanda sairia com os adicionais embaralhados a
        cada requisicao e quem confere o pedido pela posicao na tela erraria.
        Alfabetica e a unica ordem estavel disponivel: a do cardapio
        (`sort_order` do grupo) nao foi congelada no pedido.
        """
        options_by_group: dict[UUID, list[OrderItemOption]] = {}
        group_names: dict[UUID, str] = {}
        for option in item.options:
            options_by_group.setdefault(option.option_group_id, []).append(option)
            group_names[option.option_group_id] = option.option_group_name_snapshot

        return [
            OrderItemOptionGroupResponse(
                option_group_id=group_id,
                option_group_name_snapshot=group_names[group_id],
                options=[
                    OrderItemOptionResponse(
                        id=option.id,
                        option_id=option.option_id,
                        option_name_snapshot=option.option_name_snapshot,
                        additional_price_snapshot=money_to_float(
                            option.additional_price_snapshot
                        ),
                    )
                    for option in sorted(
                        group_options, key=lambda option: option.option_name_snapshot
                    )
                ],
            )
            for group_id, group_options in sorted(
                options_by_group.items(), key=lambda pair: group_names[pair[0]]
            )
        ]

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
                    option_groups=OrderService._item_option_groups(item),
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
