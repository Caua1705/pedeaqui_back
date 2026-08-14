from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
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
    AdminBranchResponse,
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
# esta aberta e qual o horario de hoje), ESCREVER e do GERENCIA, e as duas
# excecoes tem motivo proprio, comentado em cada uma:
#
# - `PATCH /settings` e SOMENTE_DONO (pedido minimo, taxa de servico, raio de
#   entrega: e o contrato comercial da loja);
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
    """Configuracoes do restaurante (BLOCO C5).

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
    "/settings/store-status",
    response_model=AdminRestaurantSettingsResponse,
    # PESSOAS de proposito: "vamos parar de aceitar pedido" as 21h e decisao
    # de quem esta no balcao. Tirar isso do atendente obriga a ligar para o
    # dono, e a saida pratica vira o atendente usando a conta dele — que
    # devolve TODAS as permissoes de uma vez.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def set_store_status(
    payload: StoreStatusRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminRestaurantSettingsResponse:
    """Abre ou fecha a loja agora (BLOCO C1).

    Rota separada do PATCH de configuracoes porque e botao de acao rapida:
    o corpo de um campo so nao arrasta junto valores antigos que estavam
    abertos na tela de configuracao.
    """
    return AdminSettingsService(db).set_store_status(scope, payload)


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
