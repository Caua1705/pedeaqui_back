from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.schemas.auth_schema import MessageResponse
from src.schemas.customer_schema import (
    ChangeCustomerPasswordRequest,
    CreateCustomerAddressRequest,
    CurrentCustomerResponse,
    CustomerAddressResponse,
    CustomerOrderHistoryItem,
    UpdateCustomerAddressRequest,
)
from src.services.customer_service import CustomerService


router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/me", response_model=CurrentCustomerResponse)
def get_me(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CurrentCustomerResponse:
    return CustomerService(db).get_me(current_customer)


@router.patch("/me/password", response_model=MessageResponse)
def change_password(
    payload: ChangeCustomerPasswordRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> MessageResponse:
    return CustomerService(db).change_password(current_customer, payload)


@router.get("/me/orders", response_model=list[CustomerOrderHistoryItem])
def list_orders(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[CustomerOrderHistoryItem]:
    return CustomerService(db).list_orders(current_customer)


@router.get("/me/addresses", response_model=list[CustomerAddressResponse])
def list_addresses(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[CustomerAddressResponse]:
    return CustomerService(db).list_addresses(current_customer)


@router.post("/me/addresses", response_model=CustomerAddressResponse, status_code=status.HTTP_201_CREATED)
def create_address(
    payload: CreateCustomerAddressRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CustomerAddressResponse:
    return CustomerService(db).create_address(current_customer, payload)


@router.patch("/me/addresses/{address_id}", response_model=CustomerAddressResponse)
def update_address(
    address_id: UUID,
    payload: UpdateCustomerAddressRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CustomerAddressResponse:
    return CustomerService(db).update_address(current_customer, address_id, payload)


@router.delete("/me/addresses/{address_id}", response_model=MessageResponse)
def delete_address(
    address_id: UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> MessageResponse:
    CustomerService(db).delete_address(current_customer, address_id)
    return MessageResponse(message="Endereco removido com sucesso")


@router.patch("/me/addresses/{address_id}/default", response_model=CustomerAddressResponse)
def set_default_address(
    address_id: UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CustomerAddressResponse:
    return CustomerService(db).set_default_address(current_customer, address_id)
