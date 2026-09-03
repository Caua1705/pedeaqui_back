import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import (
    CANCEL_ORDER_RATE_LIMIT,
    DELETE_ACCOUNT_RATE_LIMIT,
    REQUEST_DELETE_CODE_RATE_LIMIT,
    SOCIAL_ACCOUNT_RATE_LIMIT,
    limiter,
)
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
    LinkedSocialAccountResponse,
    LinkGoogleAccountRequest,
    UnlinkSocialAccountRequest,
    ImportCustomerAddressesRequest,
    ImportCustomerAddressesResponse,
    OrdersInFlightResponse,
    UpdateCurrentCustomerRequest,
    UpdateCustomerAddressRequest,
)
from src.schemas.order_schema import CustomerCancelOrderRequest, OrderDetailResponse
from src.schemas.saved_card_schema import SaveCardRequest, SavedCardResponse
from src.services.customer_anonymization_service import CustomerAnonymizationService
from src.services.customer_order_cancel_service import CustomerOrderCancelService
from src.services.customer_social_service import CustomerSocialAccountService
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

    ## A PROVA E UMA DAS DUAS, E QUEM ESCOLHE E A CONTA

    `GET /customers/me` responde `password_set`, e e ele que decide qual campo
    mandar:

        password_set: true   ->  { "password": "..." }
        password_set: false  ->  { "email_code": "123456" }

    `password_set: false` e a conta que entrou so pelo Google: ela nao tem
    senha, e o codigo de seis digitos prova a mesma coisa que a senha provaria
    — que quem esta pedindo tem acesso ao e-mail da conta. Peca o codigo em
    `POST /customers/me/delete-code`.

    **Mandar o campo errado e 400**, com a frase dizendo qual mandar. Nao e
    escolha do app: aceitar o codigo numa conta que TEM senha rebaixaria a
    exigencia de toda conta com senha para "quem le o e-mail".

    O codigo errado e **401** e conta uma tentativa; cinco erros na mesma linha
    respondem **429** e ela nao serve mais. Peca outro.
    """
    CustomerAnonymizationService(db).anonymize(current_customer, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/delete-code",
    response_model=MessageResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Esta conta tem senha: use `password` ao excluir"
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
    },
)
@limiter.limit(REQUEST_DELETE_CODE_RATE_LIMIT)
def request_delete_code(
    request: Request,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Manda o codigo que confirma a exclusao da conta. NADA e excluido aqui.

    E o passo anterior a `DELETE /customers/me` para quem tem
    `password_set: false` — a conta que entrou so pelo Google e nunca definiu
    senha. O codigo vai para o e-mail da conta e vale **10 minutos**.

    **So para conta sem senha.** A que tem senha recebe **400**: ela ja tem
    prova, e um segundo caminho de exclusao onde um bastava deixaria quem tem
    o token e a caixa de entrada apagar a conta sem saber a senha.

    **A resposta e a mesma mesmo quando o codigo nao sai** (cooldown de 60 s,
    ou tres codigos na janela de 15 minutos). Nao ha nada que o app faca de
    diferente, e variar a resposta so contaria quantos codigos ja sairam.

    O e-mail diz EXCLUIR com todas as letras: o mesmo formato de codigo serve
    a tres pedidos neste sistema, e a caixa de entrada e o unico lugar onde a
    pessoa ve o que esta confirmando. E o codigo daqui **nao serve para mais
    nada** — ele mora em `account_deletion_codes`, que nem a verificacao de
    e-mail nem a ligacao com o Google consultam.
    """
    return CustomerAnonymizationService(db).request_deletion_code(current_customer)


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
    """Troca a senha, conferindo a atual.

    **Conta que entrou pelo Google e nunca definiu senha nao passa aqui**: nao
    ha "senha atual" para conferir, e a resposta e sempre "Senha atual
    incorreta". Quem tem `password_set: false` em `GET /customers/me` deve ser
    mandado para `POST /auth/forgot-password`, que e o caminho de DEFINIR a
    primeira senha — o codigo vai para o e-mail que o Google ja verificou.
    """
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


@router.post(
    "/me/orders/{order_id}/cancel",
    response_model=OrderDetailResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"},
        status.HTTP_404_NOT_FOUND: {"description": "Pedido nao encontrado, ou nao e deste cliente"},
        status.HTTP_409_CONFLICT: {
            "description": (
                "O pedido ja saiu da janela em que o cliente cancela sozinho "
                "(a partir de `preparing`), ou ja e um estado final"
            )
        },
    },
)
@limiter.limit(CANCEL_ORDER_RATE_LIMIT)
def cancel_order(
    request: Request,
    order_id: UUID,
    payload: CustomerCancelOrderRequest | None = None,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    """O cliente logado desistindo do proprio pedido, de QUALQUER aparelho.

    E a contrapartida autenticada de
    `POST /{slug}/orders/track/{token}/cancel`, do mesmo jeito que a rota
    acima e a de `/orders/track/{token}`: aqui o vinculo sai de
    `orders.customer_id`, entao nao ha token nenhum para guardar nem para
    vazar. O `tracking_token` so vive no `localStorage` do aparelho que fez o
    pedido — quem pediu pelo celular e abriu o app no computador nao alcancava
    o proprio pedido.

    **A rota do token continua existindo**, e nao e redundancia: ela e a unica
    saida do convidado, que nao tem conta para autenticar.

    **Ate onde ela vai: `pending` e `accepted`**, a mesma janela da outra
    porta. A partir de `preparing` o insumo ja saiu do estoque e quem decide
    quem come o prejuizo passa a ser o lojista — a rota responde **409** e o
    app manda o cliente falar com o restaurante.

    **O corpo e opcional**, e o motivo dentro dele tambem, pelo mesmo motivo
    da outra: exigir justificativa de quem desiste de um pedido que nem
    comecou produz um campo preenchido com "a".

    ## O que ela faz junto, e que o app nao precisa pedir

    E a MESMA escrita do cancelamento pelo painel, entao o cupom volta a ficar
    disponivel, o cashback resgatado volta para o saldo e **o pagamento online
    e estornado**. O estorno acontece depois do commit e nao pode derrubar
    esta resposta: se o gateway estiver fora do ar, o cancelamento vale e uma
    varredura devolve o dinheiro depois.

    **Sem `Idempotency-Key`**, e ela nao faz falta: o segundo clique chega com
    o pedido ja em `cancelled` e leva 409 da maquina de estados.
    """
    return CustomerOrderCancelService(db).cancel_for_customer(
        current_customer, order_id, payload
    )


@router.get(
    "/me/social",
    response_model=list[LinkedSocialAccountResponse],
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado"}},
)
def list_social_accounts(
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[LinkedSocialAccountResponse]:
    """As contas de provedor conectadas a esta conta. E a tela de "contas
    conectadas", e o que as outras duas rotas desta secao pressupoem.

    Lista vazia significa que a conta abre so por e-mail e senha.

    **Nao vem o `provider_user_id`** (o `sub` do Google), de proposito: ele e
    identificador da pessoa dentro do provedor e pertence a exportacao da
    LGPD, que e um pedido explicito — nao a uma tela que abre sozinha.

    Junto com `password_set` de `GET /customers/me`, esta lista diz tudo que a
    tela precisa saber: uma conta com `password_set: false` e uma unica conta
    conectada nao pode desconectar essa conta (`DELETE` responde 400), e a tela
    deve mostrar isso desabilitado, com o motivo, antes do clique.
    """
    return CustomerSocialAccountService(db).list_for(current_customer)


@router.post(
    "/me/social/google",
    response_model=list[LinkedSocialAccountResponse],
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Senha nao informada, `nonce_token` vencido, ou conta sem senha "
                "utilizavel (defina uma antes)"
            )
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Nao autenticado, senha incorreta, ou `id_token`/`nonce` invalido"
        },
        status.HTTP_409_CONFLICT: {
            "description": "Esta conta do Google ja esta conectada a outra conta"
        },
    },
)
@limiter.limit(SOCIAL_ACCOUNT_RATE_LIMIT)
def link_google_account(
    request: Request,
    payload: LinkGoogleAccountRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[LinkedSocialAccountResponse]:
    """Conecta o Google a esta conta SEM sair dela.

    E o caminho de quem ja tem conta por e-mail e quer passar a entrar pelo
    Google. Sem esta rota, a unica forma seria sair, entrar com Google, cair no
    caso (b) e digitar um codigo — para alguem que ja esta autenticado.

    Mande `id_token` e `nonce_token` (peca o par em `POST /auth/google/nonce`)
    **e a senha atual**.

    ## Por que a senha

    Conectar acrescenta uma forma de entrar. Sem a senha, um token roubado
    vira acesso permanente: o ladrao conecta o Google dele, voce troca a senha
    — o que mata todos os tokens — e ele volta pelo botao do Google. A troca de
    senha deixaria de ser o que expulsa quem entrou na sua conta.

    Conta com `password_set: false` recebe **400**: ela nao tem senha para
    provar. Defina uma por "Esqueci minha senha" e conecte depois.

    ## O e-mail do Google NAO precisa ser o da conta

    Conectar o Gmail do trabalho a uma conta pessoal e o caso comum, e ele
    funciona: a sessao ja prova de quem e a conta e o `id_token` prova de quem
    e o Google.

    Conectar duas vezes o mesmo Google e a mesma coisa que conectar uma. Se
    aquele Google ja pertence a OUTRA conta, a resposta e 409.
    """
    return CustomerSocialAccountService(db).link_google(current_customer, payload)


@router.delete(
    "/me/social/{provider}",
    response_model=list[LinkedSocialAccountResponse],
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Seria a unica forma de entrar, ou a conta nao tem senha "
                "utilizavel para confirmar"
            )
        },
        status.HTTP_401_UNAUTHORIZED: {"description": "Nao autenticado, ou senha incorreta"},
        status.HTTP_404_NOT_FOUND: {"description": "Esta conta nao esta conectada a esse provedor"},
    },
)
@limiter.limit(SOCIAL_ACCOUNT_RATE_LIMIT)
def unlink_social_account(
    request: Request,
    provider: str,
    payload: UnlinkSocialAccountRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[LinkedSocialAccountResponse]:
    """Desconecta um provedor. Devolve a lista que sobrou.

    Mande a senha atual no corpo — desconectar mexe em forma de entrar, do
    mesmo jeito que conectar.

    ## A TRAVA: nunca deixa a conta sem porta

    Conta com `password_set: false` e **uma unica** conta conectada recebe
    **400**, com a frase dizendo que aquela e a unica forma de entrar. Sem
    senha e sem provedor a pessoa nao entra mais — e ela nao descobriria isso
    no clique, e sim na proxima vez que tentasse entrar, sem nenhuma pista.

    **A tela deve mostrar isso antes.** `GET /customers/me/social` mais o
    `password_set` de `GET /customers/me` dizem exatamente quando o botao de
    desconectar tem que estar desabilitado, e com que explicacao.

    Desconectar **nao apaga nada** da conta: pedidos, endereços, cashback e
    cupons continuam iguais — eles pendem do cliente, nunca do provedor. O que
    sai e o vinculo, e aquele Google fica livre para ser conectado a outra
    conta depois.
    """
    return CustomerSocialAccountService(db).unlink(current_customer, provider, payload)


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

    ## Os DOIS ids, e para que serve cada um

    `id` e o nosso UUID, e e ele que volta em `card.saved_card_id` na hora
    de pagar. `provider_card_id` e o id do cartao na conta do Mercado Pago
    do lojista, e o navegador precisa dele para gerar o token da cobranca
    (`card_id` + CVV) — sem ele a tokenizacao volta `invalid card_id`.
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
