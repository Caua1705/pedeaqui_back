from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.customer_model import Customer, CustomerAddress
from src.repositories.customer_repository import CustomerRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.auth_schema import MessageResponse
from src.schemas.customer_schema import (
    ChangeCustomerPasswordRequest,
    CreateCustomerAddressRequest,
    UpdateCurrentCustomerRequest,
    UpdateCustomerAddressRequest,
)
from src.schemas.customer_schema import CurrentCustomerResponse, CustomerOrderHistoryItem
from src.schemas.order_schema import OrderItemResponse
from src.utils.money import money_to_float
from src.utils.security import PasswordTooLongError, hash_password, verify_password


class CustomerService:
    def __init__(self, db: Session):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)

    def get_me(self, customer: Customer) -> CurrentCustomerResponse:
        return CurrentCustomerResponse(
            id=customer.id,
            name=customer.name,
            email=customer.email,
            phone=customer.phone,
            cpf=customer.cpf,
            birth_date=customer.birth_date,
            email_verified=customer.email_verified_at is not None,
            marketing_opt_in=customer.marketing_opt_in,
        )

    def update_me(
        self,
        customer: Customer,
        payload: UpdateCurrentCustomerRequest,
    ) -> CurrentCustomerResponse:
        email_owner = self.customer_repository.get_by_email(payload.email)
        if email_owner and email_owner.id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="E-mail ja esta em uso",
            )

        phone_owner = self.customer_repository.get_by_phone(payload.phone)
        if phone_owner and phone_owner.id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Telefone ja esta em uso",
            )

        try:
            self.customer_repository.update(
                customer,
                name=payload.name,
                email=payload.email,
                phone=payload.phone,
                birth_date=payload.birth_date,
            )
            self.db.commit()
            self.db.refresh(customer)
        except IntegrityError as exc:
            self.db.rollback()
            email_owner = self.customer_repository.get_by_email(payload.email)
            if email_owner and email_owner.id != customer.id:
                detail = "E-mail ja esta em uso"
            else:
                detail = "Telefone ja esta em uso"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from exc
        except Exception:
            self.db.rollback()
            raise

        return self.get_me(customer)

    def change_password(
        self,
        customer: Customer,
        payload: ChangeCustomerPasswordRequest,
    ) -> MessageResponse:
        if not verify_password(payload.current_password, customer.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Senha atual incorreta",
            )
        if payload.new_password != payload.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="As senhas não conferem",
            )
        if len(payload.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A nova senha deve ter pelo menos 8 caracteres",
            )
        try:
            password_hash = hash_password(payload.new_password)
        except PasswordTooLongError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A nova senha é muito longa",
            ) from exc

        try:
            customer.password_hash = password_hash
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return MessageResponse(message="Senha alterada com sucesso")

    def list_orders(self, customer: Customer) -> list[CustomerOrderHistoryItem]:
        rows = self.order_repository.list_orders_by_customer(customer.id)
        return [
            CustomerOrderHistoryItem(
                id=order.id,
                order_number=order.order_number,
                restaurant_name=restaurant_name,
                branch_name=branch_name,
                status=order.status,
                order_type=order.order_type,
                subtotal=money_to_float(order.subtotal),
                delivery_fee=money_to_float(order.delivery_fee),
                service_fee=money_to_float(order.service_fee),
                total=money_to_float(order.total),
                created_at=order.created_at,
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
                    for item in order.items
                ],
            )
            for order, restaurant_name, branch_name in rows
        ]

    def list_addresses(self, customer: Customer) -> list[CustomerAddress]:
        return self.customer_repository.list_addresses(customer.id)

    def create_address(self, customer: Customer, payload: CreateCustomerAddressRequest) -> CustomerAddress:
        try:
            if payload.is_default:
                self.customer_repository.unset_default_addresses(customer.id)
            address = self.customer_repository.create_address(
                customer_id=customer.id,
                **payload.model_dump(),
            )
            self.db.commit()
            self.db.refresh(address)
            return address
        except Exception:
            self.db.rollback()
            raise

    def update_address(self, customer: Customer, address_id: UUID, payload: UpdateCustomerAddressRequest) -> CustomerAddress:
        address = self._get_owned_address(customer, address_id)
        values = payload.model_dump(exclude_unset=True)
        for required_field in ("street", "number", "neighborhood"):
            if required_field in values and not values[required_field]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endereco incompleto")

        try:
            if values.pop("is_default", None):
                self.customer_repository.unset_default_addresses(customer.id)
                address.is_default = True
            for key, value in values.items():
                setattr(address, key, value)
            self.db.add(address)
            self.db.commit()
            self.db.refresh(address)
            return address
        except Exception:
            self.db.rollback()
            raise

    def delete_address(self, customer: Customer, address_id: UUID) -> None:
        address = self._get_owned_address(customer, address_id)
        try:
            self.db.delete(address)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def set_default_address(self, customer: Customer, address_id: UUID) -> CustomerAddress:
        address = self._get_owned_address(customer, address_id)
        try:
            self.customer_repository.unset_default_addresses(customer.id)
            address.is_default = True
            self.db.add(address)
            self.db.commit()
            self.db.refresh(address)
            return address
        except Exception:
            self.db.rollback()
            raise

    def _get_owned_address(self, customer: Customer, address_id: UUID) -> CustomerAddress:
        address = self.customer_repository.get_address(customer.id, address_id)
        if not address:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endereco nao encontrado")
        return address
