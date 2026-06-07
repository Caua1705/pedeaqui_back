from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import ORDER_STATUSES, ORDER_TYPES
from src.models.customer_model import Customer
from src.models.order_item_model import OrderItem
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.branch_repository import BranchRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.delivery_zone_repository import DeliveryZoneRepository
from src.repositories.menu_repository import MenuRepository
from src.repositories.order_repository import OrderRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.order_schema import (
    CreateOrderRequest,
    CreateOrderResponse,
    OrderDetailResponse,
    OrderItemResponse,
)
from src.schemas.common_schema import StatusHistoryResponse
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
        products_by_id = self._get_valid_products(restaurant.id, [item.product_id for item in payload.items])

        subtotal = self._calculate_subtotal(payload, products_by_id)
        service_fee = self._calculate_service_fee(settings)
        delivery_fee = self._calculate_delivery_fee(restaurant.id, payload, settings, address)
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
                notes=payload.notes,
            )
            self.order_repository.create_order(order)

            order_items = [
                self._build_order_item(order.id, products_by_id[item.product_id], item.quantity, item.observation)
                for item in payload.items
            ]
            self.order_repository.create_order_items(order_items)
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
        if current_customer and payload.customer_address_id:
            address = self.customer_repository.get_address(current_customer.id, payload.customer_address_id)
            if not address:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endereco nao encontrado")
            return address
        return payload.address

    def _get_valid_products(self, restaurant_id: UUID, product_ids: list[UUID]) -> dict[UUID, object]:
        unique_ids = list(set(product_ids))
        products = self.product_repository.list_active_by_ids(restaurant_id, unique_ids)
        products_by_id = {product.id: product for product in products}
        if len(products_by_id) != len(unique_ids):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Produto inválido ou indisponível")
        return products_by_id

    def _calculate_subtotal(self, payload: CreateOrderRequest, products_by_id: dict[UUID, object]) -> Decimal:
        subtotal = ZERO
        for item in payload.items:
            product = products_by_id[item.product_id]
            subtotal += to_decimal(product.price) * item.quantity
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

    @staticmethod
    def _build_order_item(order_id: UUID, product, quantity: int, observation: str | None) -> OrderItem:
        unit_price = quantize_money(to_decimal(product.price))
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
