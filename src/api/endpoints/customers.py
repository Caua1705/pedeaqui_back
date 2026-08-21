import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import DELETE_ACCOUNT_RATE_LIMIT, limiter
from src.models.customer_model import Customer
from src.schemas.auth_schema import MessageResponse
from src.schemas.cashback_schema import CashbackBalanceResponse, CashbackTransactionsResponse
from src.schemas.customer_schema import (
    ChangeCustomerPasswordRequest,
    CreateCustomerAddressRequest,
    CurrentCustomerResponse,
    CustomerDataExportResponse,
    CustomerAddressResponse,
    CustomerOrderHistoryItem,
    DeleteCustomerAccountRequest,
    ImportCustomerAddressesRequest,
    ImportCustomerAddressesResponse,
    OrdersInFlightResponse,
    UpdateCurrentCustomerRequest,
    UpdateCustomerAddressRequest,
)
from src.schemas.order_schema import OrderDetailResponse
from src.services.customer_anonymization_service import CustomerAnonymizationService
from src.services.customer_service import CustomerService
from src.services.cashback_service import CashbackService
from src.services.order_service import OrderService


router = APIRouter(prefix="/customers", tags=["customers"])
logger = logging.getLogger("uvicorn.error")


@router.get("/me", response_model=CurrentCustomerResponse)
def get_me(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CurrentCustomerResponse:
    return CustomerService(db).get_me(current_customer)


@router.get(
    "/me/export",
    response_model=CustomerDataExportResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"}},
)
def export_me(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CustomerDataExportResponse:
    """Tudo que a plataforma guarda sobre quem pediu, num pacote so.

    Direito de acesso e portabilidade (LGPD, Art. 18, II e V). O escopo e
    sempre o dono do token, e nao ha parametro de cliente aqui — nem deve
    haver: uma rota de exportacao que aceitasse id viraria a maneira mais
    conveniente de baixar a base inteira.
    """
    return CustomerService(db).export_me(current_customer)


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado, ou senha incorreta"},
        status.HTTP_409_CONFLICT: {
            "description": "Ha pedido em andamento; a exclusao e recusada por enquanto",
            "model": OrdersInFlightResponse,
        },
    },
)
@limiter.limit(DELETE_ACCOUNT_RATE_LIMIT)
def delete_me(
    request: Request,
    payload: DeleteCustomerAccountRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> Response:
    """Exclusao da conta (LGPD, Art. 18, VI). NAO TEM DESFAZER.

    E o par natural de `GET /me/export`, que fica logo acima: **vejo o que
    voces tem** e **apaguem**.

    A conta e ANONIMIZADA, nao apagada — o pedido continua existindo para o
    restaurante, sem nada da pessoa dentro. O e-mail e o telefone sao
    liberados para recadastro, e o token desta propria chamada morre junto.
    Os campos exatos estao em `CustomerAnonymizationService`.

    ## O SALDO DE CASHBACK E PERDIDO. Avise antes de chamar esta rota.

    O recadastro nasce com **id novo** — e o que libera o e-mail e o telefone
    e justamente a saida deles da tabela. O cashback continua ligado ao id
    velho, e nao ha caminho de volta: a pessoa volta como desconhecida.

    **Chame `GET /customers/me/cashback` na tela de confirmacao** e mostre o
    saldo junto do aviso. Nao ha como esta rota avisar: quando ela responde,
    a conta ja foi anonimizada, e nao ha desfazer.

    O saldo NAO vem no corpo da resposta, e nao e esquecimento: um numero
    entregue depois do fato nao evita a perda, e publicar um corpo aqui
    trocaria o `204` por `200` — mudanca de contrato para um app que ja
    consome esta rota. Quando o resgate de cashback existir (hoje o saldo e
    sempre zero — ver a armadilha 26), o campo entra junto com a mudanca do
    app, de uma vez so.

    O corpo leva a senha atual: `DELETE` com corpo e incomum mas legal, e a
    alternativa a colocaria na querystring, ou seja, no log do proxy.
    """
    CustomerAnonymizationService(db).anonymize(current_customer, payload.password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me/cashback",
    response_model=CashbackBalanceResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"}},
)
def get_cashback_balance(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CashbackBalanceResponse:
    return CashbackService(db).get_balance(current_customer)


@router.get(
    "/me/cashback/transactions",
    response_model=CashbackTransactionsResponse,
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"}},
)
def list_cashback_transactions(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CashbackTransactionsResponse:
    return CashbackService(db).list_transactions(current_customer, limit, offset)


@router.patch(
    "/me",
    response_model=CurrentCustomerResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
        status.HTTP_409_CONFLICT: {"description": "E-mail ou telefone ja esta em uso"},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Dados invalidos"},
    },
)
def update_me(
    payload: UpdateCurrentCustomerRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CurrentCustomerResponse:
    return CustomerService(db).update_me(current_customer, payload)


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


@router.get("/me/orders/{order_id}", response_model=OrderDetailResponse)
def get_order(
    order_id: UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    """Detalhe do pedido para o cliente logado.

    E a contrapartida autenticada de /orders/track/{token}: aqui o vinculo
    sai de `orders.customer_id`, entao nao ha token nenhum para guardar nem
    para vazar.
    """
    return OrderService(db).get_customer_order(current_customer, order_id)


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


@router.post(
    "/me/addresses/import",
    response_model=ImportCustomerAddressesResponse,
    status_code=status.HTTP_200_OK,
)
def import_addresses(
    payload: ImportCustomerAddressesRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> ImportCustomerAddressesResponse:
    logger.info(
        "[Address import contract] route=customer_address_import addresses_count=%d",
        len(payload.addresses),
    )
    return CustomerService(db).import_addresses(current_customer, payload)

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
