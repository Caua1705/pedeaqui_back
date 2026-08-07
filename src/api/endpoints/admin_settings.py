from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope, get_admin_scope
from src.api.dependencies.database import get_db
from src.schemas.admin_settings_schema import (
    AdminBranchResponse,
    AdminBranchUpdate,
    AdminPaymentMethodCreate,
    AdminPaymentMethodResponse,
    AdminPaymentMethodUpdate,
    AdminRestaurantSettingsResponse,
    AdminRestaurantSettingsUpdate,
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
router = APIRouter(prefix="/admin", tags=["admin settings"])


@router.get("/settings", response_model=AdminRestaurantSettingsResponse)
def get_restaurant_settings(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminRestaurantSettingsResponse:
    """Configuracoes do restaurante (BLOCO C5).

    Sem `platform_commission_percent`: e o percentual que a plataforma
    cobra, negociado por contrato, e nao e assunto do painel do lojista.
    """
    return AdminSettingsService(db).get_restaurant_settings(scope)


@router.patch("/settings", response_model=AdminRestaurantSettingsResponse)
def update_restaurant_settings(
    payload: AdminRestaurantSettingsUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminRestaurantSettingsResponse:
    return AdminSettingsService(db).update_restaurant_settings(scope, payload)


@router.patch("/settings/store-status", response_model=AdminRestaurantSettingsResponse)
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


@router.get("/branches", response_model=list[AdminBranchResponse])
def list_branches(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminBranchResponse]:
    """Filiais que este lojista enxerga.

    Quem esta preso a uma filial recebe so ela — o seletor de filial do
    painel ja vem resolvido sem a tela conhecer a regra de escopo.
    """
    return AdminSettingsService(db).list_branches(scope)


@router.get("/branches/{branch_id}", response_model=AdminBranchResponse)
def get_branch(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchResponse:
    return AdminSettingsService(db).get_branch(scope, branch_id)


@router.patch("/branches/{branch_id}", response_model=AdminBranchResponse)
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


@router.get(
    "/branches/{branch_id}/payment-methods",
    response_model=list[AdminPaymentMethodResponse],
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
)
def create_payment_method(
    branch_id: UUID,
    payload: AdminPaymentMethodCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminPaymentMethodResponse:
    return AdminSettingsService(db).create_payment_method(scope, branch_id, payload)


@router.patch("/payment-methods/{method_id}", response_model=AdminPaymentMethodResponse)
def update_payment_method(
    method_id: UUID,
    payload: AdminPaymentMethodUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminPaymentMethodResponse:
    return AdminSettingsService(db).update_payment_method(scope, method_id, payload)


@router.delete("/payment-methods/{method_id}", status_code=status.HTTP_204_NO_CONTENT)
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
