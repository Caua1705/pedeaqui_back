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
from src.schemas.saved_card_schema import SaveCardRequest, SavedCardResponse
from src.services.customer_anonymization_service import CustomerAnonymizationService
from src.services.customer_service import CustomerService
from src.services.cashback_service import CashbackService
from src.services.order_service import OrderService
from src.services.saved_card_service import SavedCardService


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
    `balance` — que e o acumulado em TODOS os restaurantes, e e exatamente o
    que se perde aqui. A quebra de `by_restaurant[]` serve para nomear as
    lojas no aviso ("R$ 40 no Junior da Picanha"). Nao ha como esta rota
    avisar: quando ela responde, a conta ja foi anonimizada, e nao ha
    desfazer.

    Desde que o credito e o resgate existem, o aviso deixou de ser
    hipotetico: o saldo e dinheiro que a pessoa gastaria no proximo pedido.

    O saldo NAO vem no corpo da resposta, e nao e esquecimento: um numero
    entregue depois do fato nao evita a perda, e publicar um corpo aqui
    trocaria o `204` por `200` — mudanca de contrato para um app que ja
    consome esta rota.

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
    """O saldo de cashback: o acumulado, e o que da para gastar em cada loja.

    ## `balance` e o ACUMULADO. O gastavel e `by_restaurant[]`

    Cashback e dinheiro de quem o concedeu: o que acumulou no Junior da
    Picanha so se gasta la, e nao ha compensacao entre restaurantes. **A tela
    mostra a lista.** Um "R$ 40" com R$ 5 gastaveis na loja aberta e a
    reclamacao pronta.

    O total continua na resposta porque nao quebra o app que ja o consome, e
    porque ele e a resposta certa para uma pergunta que existe: quanto a
    pessoa acumulou no total — que e exatamente o que ela perde ao excluir a
    conta.

    ## `expires_at` anda para frente a cada pedido

    A validade conta a partir do ULTIMO PEDIDO naquele restaurante, e nao da
    data do credito: pedir de novo renova o saldo inteiro. **Se a tela nao
    mostrar a data, o mecanismo perde metade do valor** — o cliente nao tem
    como saber que um pedido novo devolve o prazo.

    Nulo significa "nao vence": restaurante sem campanha configurada, ou
    saldo de quem nunca pediu ali.

    ## O checkout nao manda numero

    O corpo do pedido leva `use_cashback: true` e o servidor resolve quanto
    entra, com a linha do cliente travada. O saldo desta rota e para MOSTRAR;
    ele pode estar velho no minuto do checkout, e quem decide e o servidor.
    """
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


@router.get(
    "/me/cards",
    response_model=list[SavedCardResponse],
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
        status.HTTP_404_NOT_FOUND: {"description": "Restaurante nao encontrado"},
    },
)
def list_saved_cards(
    restaurant_slug: str = Query(
        description=(
            "Restaurante cujos cartoes salvos se quer listar. Obrigatorio: o "
            "cartao vive na conta do gateway DAQUELE lojista e nao e visivel "
            "nem cobravel pela conta de outro."
        ),
    ),
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[SavedCardResponse]:
    """Os cartoes que esta pessoa salvou NESTE restaurante.

    ## Lista vazia nao quer dizer "nunca salvou"

    Ela tambem e a resposta para quem salvou cartao em outra loja da
    plataforma, e para quem salvou antes de o ambiente do gateway virar de
    teste para producao. Nos tres casos a tela e a mesma: oferecer o cadastro
    de um cartao novo.

    ## O que NAO sai daqui

    O id do cartao no Mercado Pago. O `id` da resposta e o nosso UUID, e e
    ele que volta em `card.saved_card_id` na hora de pagar.
    """
    return SavedCardService(db).list_cards(current_customer, restaurant_slug)


@router.post(
    "/me/cards",
    response_model=SavedCardResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
        status.HTTP_409_CONFLICT: {"description": "Conta sem e-mail"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Gateway recusou ou nao respondeu"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Restaurante sem cartao habilitado no ambiente ativo"
        },
    },
)
def save_card(
    payload: SaveCardRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> SavedCardResponse:
    """Salva um cartao tokenizado na conta do restaurante.

    ## O numero do cartao nao passa por aqui

    O corpo leva o `token` que o SDK do Mercado Pago gerou NO NAVEGADOR.
    Nao ha campo de PAN, de CVV nem de validade — e nao deve haver.

    ## Isto nao cobra nada, e o cartao nao e validado agora

    Nao ha cobranca de R$ 1,00 seguida de estorno. O cartao e testado na
    primeira cobranca de verdade, e um cartao sem limite entra na lista e so
    falha no checkout. **Decisao tomada**, com o motivo em
    `docs/cartao-salvo.md`.

    ## 201 repetido para o mesmo cartao devolve a MESMA linha

    Salvar duas vezes o mesmo cartao nao cria duas linhas: o gateway devolve
    o mesmo id e o cadastro existente e reaproveitado.
    """
    return SavedCardService(db).save_card(current_customer, payload)


@router.delete(
    "/me/cards/{card_id}",
    response_model=MessageResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
        status.HTTP_404_NOT_FOUND: {"description": "Cartao nao encontrado nesta conta"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Gateway nao respondeu"},
    },
)
def delete_saved_card(
    card_id: UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Remove o cartao NOS DOIS LADOS: no Mercado Pago e no nosso banco.

    O gateway responde primeiro. Se ele estiver fora do ar a remocao falha
    inteira (502) e o cartao continua na lista — o cliente tenta de novo. A
    ordem inversa deixaria o cartao pendurado na conta do lojista **sem
    referencia nenhuma nossa**, e ninguem mais conseguiria remove-lo.

    ## Pedido em analise que usava este cartao continua em analise

    A cobranca ja existe no gateway e nao depende do cadastro do cartao: ela
    foi criada com um token, nao com o `card_id`. Remover o cartao nao
    cancela, nao estorna e nao muda o resultado do antifraude — o `in_review`
    segue e o webhook decide o pedido do mesmo jeito.
    """
    SavedCardService(db).delete_card(current_customer, card_id)
    return MessageResponse(message="Cartão removido.")
