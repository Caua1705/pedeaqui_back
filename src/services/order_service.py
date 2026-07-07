from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import ORDER_STATUSES, ORDER_TYPES
from src.models.customer_model import Customer
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.branch_repository import BranchRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.delivery_zone_repository import DeliveryZoneRepository
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
from src.services.delivery_estimate_service import DeliveryEstimateService
from src.services.restaurant_service import RestaurantService
from src.utils.money import ZERO, money_to_float, quantize_money, to_decimal


class OrderService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.branch_repository = BranchRepository(db)
        self.menu_repository = MenuRepository(db)
        self.product_repository = ProductRepository(db)
        self.delivery_zone_repository = DeliveryZoneRepository(db)
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)

    def create_order(
        self,
        restaurant_slug: str,
        payload: CreateOrderRequest,
        current_customer: Customer | None = None,
    ) -> CreateOrderResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        self._validate_order_type(payload.order_type)

        branch = self.branch_repository.get_active_by_id_and_restaurant(payload.branch_id, restaurant.id)
        if not branch:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filial inválida para este restaurante")

        address = self._resolve_order_address(payload, current_customer)
        self._validate_customer_snapshot(payload, current_customer)
        self._validate_delivery_address(payload, address)
        settings = self.menu_repository.get_settings(restaurant.id)
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
            else self._calculate_delivery_fee(restaurant.id, payload, settings, address)
        )
        total = quantize_money(subtotal + service_fee + delivery_fee)

        try:
            customer_name = current_customer.name if current_customer else payload.customer.name
            customer_phone = current_customer.phone if current_customer else payload.customer.phone
            order = Order(
                restaurant_id=restaurant.id,
                branch_id=branch.id,
                customer_id=current_customer.id if current_customer else None,
                customer_address_id=payload.customer_address_id if current_customer else None,
                customer_name_snapshot=customer_name,
                customer_phone_snapshot=customer_phone,
                order_type=payload.order_type,
                status="pending",
                payment_method=payload.payment_method,
                subtotal=subtotal,
                delivery_fee=delivery_fee,
                service_fee=service_fee,
                total=total,
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
            self.db.commit()
            self.db.refresh(order)
        except Exception:
            self.db.rollback()
            raise

        return CreateOrderResponse(
            id=order.id,
            order_number=order.order_number,
            status=order.status,
            subtotal=money_to_float(order.subtotal),
            delivery_fee=money_to_float(order.delivery_fee),
            service_fee=money_to_float(order.service_fee),
            total=money_to_float(order.total),
            message="Pedido criado com sucesso",
        )

    def get_customer_order(self, restaurant_slug: str, order_number: int, phone: str) -> OrderDetailResponse:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_number_and_phone(restaurant.id, order_number, phone)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return self.to_order_detail_response(order)

    def _validate_order_type(self, order_type: str) -> None:
        if order_type not in ORDER_TYPES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tipo de pedido inválido")

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

    def _calculate_delivery_fee(self, restaurant_id: UUID, payload: CreateOrderRequest, settings, address) -> Decimal:
        if payload.order_type == "pickup":
            return ZERO

        neighborhood = address.neighborhood if address else ""
        delivery_zone = self.delivery_zone_repository.get_active_by_neighborhood(
            restaurant_id=restaurant_id,
            branch_id=payload.branch_id,
            neighborhood=neighborhood,
        )
        if delivery_zone:
            return quantize_money(to_decimal(delivery_zone.delivery_fee))
        return quantize_money(to_decimal(settings.default_delivery_fee if settings else ZERO))

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
            subtotal=money_to_float(order.subtotal),
            delivery_fee=money_to_float(order.delivery_fee),
            service_fee=money_to_float(order.service_fee),
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
