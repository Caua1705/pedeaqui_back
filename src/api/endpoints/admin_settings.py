from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    PESSOAS,
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_settings_schema import (
    AdminBranchOperationResponse,
    AdminBranchOrderTypesRequest,
    AdminBranchResponse,
    AdminBranchSettingsUpdate,
    AdminBranchUpdate,
    AdminPaymentMethodCreate,
    AdminPaymentMethodResponse,
    AdminPaymentMethodUpdate,
    AdminRestaurantSettingsResponse,
    AdminRestaurantSettingsUpdate,
    BranchPrepTimeAdjustRequest,
    BusinessHourResponse,
    BusinessHoursReplaceRequest,
    StoreStatusRequest,
)
from src.services.admin_settings_service import AdminSettingsService


# O restaurante sai do token. A FILIAL aparece no path porque um restaurante
# tem varias e o lojista precisa dizer qual esta configurando — mas nao
# autoriza nada: `AdminSettingsService._get_branch` confronta o id com o
# escopo antes de qualquer leitura ou escrita, e responde 404 para filial de
# outro restaurante e para filial fora do escopo deste lojista.
#
# Papeis, neste arquivo: LER e do PESSOAS (o balcao precisa saber se a loja
# esta aberta e qual o horario de hoje), ESCREVER e do GERENCIA, e as
# excecoes tem motivo proprio, comentado em cada uma:
#
# - `PATCH /settings` e `PATCH /branches/{id}/settings` sao SOMENTE_DONO
#   (pedido minimo, taxa de servico, raio de entrega: e o contrato comercial
#   da loja);
# - `store-status` e `prep-time` sao PESSOAS, porque sao as alavancas que a
#   operacao de balcao puxa sozinha as 20h.
router = APIRouter(prefix="/admin", tags=["admin settings"])


@router.get(
    "/settings",
    response_model=AdminRestaurantSettingsResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_restaurant_settings(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminRestaurantSettingsResponse:
    """Os PADROES do restaurante (BLOCO C5).

    Nenhum pedido le estes valores direto: a filial os herda nos campos que
    deixou nulos. O que uma loja vai de fato cobrar esta em
    `GET /admin/branches/operation`.

    Sem `platform_commission_percent`: e o percentual que a plataforma
    cobra, negociado por contrato, e nao e assunto do painel do lojista.
    """
    return AdminSettingsService(db).get_restaurant_settings(scope)


@router.patch(
    "/settings",
    response_model=AdminRestaurantSettingsResponse,
    # Pedido minimo, taxa de servico, tempo de preparo padrao e taxa de
    # entrega por km. Sao os numeros com que a loja negocia, e um deles
    # (`min_order_value = 999`) para a operacao inteira sem parecer avaria.
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_restaurant_settings(
    payload: AdminRestaurantSettingsUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminRestaurantSettingsResponse:
    return AdminSettingsService(db).update_restaurant_settings(scope, payload)


@router.patch(
    "/branches/{branch_id}/store-status",
    response_model=AdminBranchOperationResponse,
    # PESSOAS de proposito: "vamos parar de aceitar pedido" as 21h e decisao
    # de quem esta no balcao. Tirar isso do atendente obriga a ligar para o
    # dono, e a saida pratica vira o atendente usando a conta dele — que
    # devolve TODAS as permissoes de uma vez.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def set_store_status(
    branch_id: UUID,
    payload: StoreStatusRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchOperationResponse:
    """Abre ou fecha ESTA filial agora (BLOCO C1).

    Era `PATCH /admin/settings/store-status` e fechava o restaurante
    inteiro. Fechar a filial do Centro fechava a da Aldeota junto, e nao
    havia como fechar so uma — que e a unica coisa que a operacao de fato
    quer fazer as 21h.

    Rota separada do PATCH de configuracoes porque e botao de acao rapida:
    o corpo de um campo so nao arrasta junto valores antigos que estavam
    abertos na tela de configuracao.

    O atendente preso a uma filial descobre o `branch_id` dele em
    `GET /admin/branches`, que ja devolve so a filial do escopo.
    """
    return AdminSettingsService(db).set_store_status(scope, branch_id, payload)


@router.get(
    "/branches",
    response_model=list[AdminBranchResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_branches(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminBranchResponse]:
    """Filiais que este lojista enxerga.

    Quem esta preso a uma filial recebe so ela — o seletor de filial do
    painel ja vem resolvido sem a tela conhecer a regra de escopo.
    """
    return AdminSettingsService(db).list_branches(scope)


# ANTES de `/branches/{branch_id}`, e nao por estilo: o FastAPI casa as rotas
# na ordem em que sao declaradas, e depois daquela o caminho "operation"
# entraria como valor de `branch_id` e sairia 422 de UUID invalido.
@router.get(
    "/branches/operation",
    response_model=list[AdminBranchOperationResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_branch_operation(
    branch_id: UUID | None = Query(default=None),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminBranchOperationResponse]:
    """Como cada filial esta operando agora — a tela de operacao inteira.

    Uma chamada por tela: o dono com cinco lojas ve as cinco chaves de
    abrir/fechar lado a lado, que e a conferencia que nao existia enquanto o
    `is_open` era um so para a rede.

    Cada linha traz `is_open` (a chave que o lojista controla) e
    `is_open_now` (essa chave combinada com a agenda da semana). As duas
    juntas dizem "voce deixou aberta, mas o horario de hoje ja fechou" sem
    uma segunda chamada.

    `overrides` e o que esta gravado na filial — nulo quer dizer que aquele
    campo herda o padrao do restaurante. `effective` e o que o proximo
    pedido vai usar.

    O `branch_id` da querystring so RESTRINGE: quem esta preso a uma filial
    e pedir outra recebe 404.
    """
    return AdminSettingsService(db).list_branch_operation(scope, branch_id)


@router.get(
    "/branches/{branch_id}",
    response_model=AdminBranchResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_branch(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchResponse:
    return AdminSettingsService(db).get_branch(scope, branch_id)


@router.patch(
    "/branches/{branch_id}",
    response_model=AdminBranchResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_branch(
    branch_id: UUID,
    payload: AdminBranchUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchResponse:
    """Endereco, contato e regras de entrega da filial (BLOCO C3)."""
    return AdminSettingsService(db).update_branch(scope, branch_id, payload)


@router.patch(
    "/branches/{branch_id}/order-types",
    response_model=AdminBranchOperationResponse,
    # GERENCIA, e nao PESSOAS como `store-status`: desligar a entrega de uma
    # filial muda o que o cliente ve no app por tempo indeterminado, enquanto
    # fechar a loja e um gesto do dia que alguem reabre na manha seguinte.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def set_branch_order_types(
    branch_id: UUID,
    payload: AdminBranchOrderTypesRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchOperationResponse:
    """Liga e desliga entrega e retirada NESTA filial.

    Eram `accepts_delivery` e `accepts_pickup` do restaurante, e valiam para
    a rede: o quiosque de shopping que so faz retirada desligava a entrega
    de todas as lojas.

    Edicao parcial — mandar so `accepts_delivery` nao mexe na retirada.
    Desligar as duas e permitido e equivale a fechar a loja.
    """
    return AdminSettingsService(db).set_branch_order_types(scope, branch_id, payload)


@router.patch(
    "/branches/{branch_id}/settings",
    response_model=AdminBranchOperationResponse,
    # SOMENTE_DONO, igual ao PATCH dos padroes: sao os mesmos numeros com que
    # a loja negocia, so que valendo para uma filial. Deixar isso na gerencia
    # daria por filial a permissao que se recusou dar no restaurante.
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_branch_settings(
    branch_id: UUID,
    payload: AdminBranchSettingsUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchOperationResponse:
    """As sobrescritas comerciais DESTA filial.

    Tres estados por campo, e a diferenca entre os dois ultimos e o motivo
    de esta rota existir:

    - campo **ausente** do corpo: nao mexe;
    - campo **com valor**: esta filial passa a usar esse valor;
    - campo com **`null` explicito**: esta filial volta a herdar o padrao do
      restaurante (`PATCH /admin/settings`).

    Sem o terceiro estado nao haveria como desfazer uma divergencia — a
    filial ficaria com a copia congelada para sempre, e mudar o padrao nao
    chegaria nela.
    """
    return AdminSettingsService(db).update_branch_settings(scope, branch_id, payload)


@router.get(
    "/branches/{branch_id}/business-hours",
    response_model=list[BusinessHourResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_business_hours(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[BusinessHourResponse]:
    return AdminSettingsService(db).list_business_hours(scope, branch_id)


@router.put(
    "/branches/{branch_id}/business-hours",
    response_model=list[BusinessHourResponse],
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def replace_business_hours(
    branch_id: UUID,
    payload: BusinessHoursReplaceRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[BusinessHourResponse]:
    """Grava a semana inteira da filial (BLOCO C2).

    PUT e nao PATCH porque substitui: o que nao vier no corpo deixa de
    existir, e dia ausente e dia fechado. E como a tela de horario funciona
    — uma grade que o lojista salva de uma vez.
    """
    return AdminSettingsService(db).replace_business_hours(scope, branch_id, payload)


@router.patch(
    "/branches/{branch_id}/prep-time",
    response_model=BusinessHourResponse,
    # PESSOAS, e nao GERENCIA como o resto de horarios. A rota foi desenhada
    # para uso DENTRO do expediente: escreve so na faixa que contem o agora e
    # responde 409 com a loja fechada. E o "estamos atolados, sobe cinco
    # minutos" das 20h — a mesma classe de decisao que `store-status`, e pelo
    # mesmo motivo (senao o atendente usa a conta do dono).
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def adjust_prep_time(
    branch_id: UUID,
    payload: BranchPrepTimeAdjustRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> BusinessHourResponse:
    """Ajusta o tempo de preparo que esta valendo agora.

    E o atalho do dia cheio: `{"delta_minutes": 5}` empurra a janela
    inteira em cinco minutos, `{"delta_minutes": -10}` puxa de volta. Para
    a faixa que ainda nao tem prazo cadastrado, mande o par
    `prep_time_min`/`prep_time_max` uma vez e depois use o delta.

    Escreve SO na faixa de horario que contem o momento atual — a mesma que
    o proximo pedido vai ler. Filial fechada agora responde 409: o cadastro
    da semana inteira e `PUT /admin/branches/{branch_id}/business-hours`.

    Devolve a faixa ajustada, para o painel mostrar o prazo que passou a
    valer sem uma segunda chamada.
    """
    return AdminSettingsService(db).adjust_prep_time(scope, branch_id, payload)


@router.get(
    "/branches/{branch_id}/payment-methods",
    response_model=list[AdminPaymentMethodResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_payment_methods(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminPaymentMethodResponse]:
    """Formas de pagamento da filial, habilitadas e desabilitadas (BLOCO C4)."""
    return AdminSettingsService(db).list_payment_methods(scope, branch_id)


@router.post(
    "/branches/{branch_id}/payment-methods",
    response_model=AdminPaymentMethodResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def create_payment_method(
    branch_id: UUID,
    payload: AdminPaymentMethodCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminPaymentMethodResponse:
    return AdminSettingsService(db).create_payment_method(scope, branch_id, payload)


@router.patch(
    "/payment-methods/{method_id}",
    response_model=AdminPaymentMethodResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_payment_method(
    method_id: UUID,
    payload: AdminPaymentMethodUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminPaymentMethodResponse:
    return AdminSettingsService(db).update_payment_method(scope, method_id, payload)


@router.delete(
    "/payment-methods/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def delete_payment_method(
    method_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a forma de pagamento da filial.

    Apagar e seguro aqui: `orders.payment_method` guarda texto, nao FK,
    entao pedido ja fechado nao muda.
    """
    AdminSettingsService(db).delete_payment_method(scope, method_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
