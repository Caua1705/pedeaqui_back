"""Entregadores, pelo painel: a taxa da filial, o cadastro e a atribuicao.

O entregador em si NAO entra aqui — ele nao tem Bearer de lojista. As rotas
dele estao em `courier.py`, com outra dependencia de autenticacao.

Nenhuma destas rotas aceita `print_agent`: a conta de maquina do agente de
impressao continua alcancando as quatro rotas de sempre e mais nenhuma.
"""

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
from src.schemas.courier_schema import (
    AdminAssignmentBatchResponse,
    AdminAssignmentResponse,
    AdminAssignOrdersRequest,
    AdminBranchCourierFeeResponse,
    AdminBranchCourierFeeUpdate,
    AdminCourierAccessResponse,
    AdminCourierCreate,
    AdminCourierResponse,
    AdminCourierUpdate,
    AdminOrderCourierResponse,
)
from src.services.admin_courier_service import AdminCourierService


router = APIRouter(prefix="/admin", tags=["admin couriers"])


# --- A taxa da filial ---------------------------------------------------------


@router.get(
    "/branches/{branch_id}/courier-fee",
    response_model=AdminBranchCourierFeeResponse,
    # GERENCIA e nao PESSOAS: e termo comercial da loja (o que ela paga por
    # corrida), como a leitura de cupom — nao alavanca de balcao.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def get_courier_fee(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchCourierFeeResponse:
    """Quanto esta filial paga ao entregador por corrida.

    `null` nos dois campos e "sem taxa configurada": a atribuicao congela
    snapshot nulo e o historico do entregador mostra a corrida sem valor.
    Nao e zero, de proposito.
    """
    return AdminCourierService(db).get_courier_fee(scope, branch_id)


@router.patch(
    "/branches/{branch_id}/courier-fee",
    response_model=AdminBranchCourierFeeResponse,
    # SOMENTE_DONO: e dinheiro que a loja paga. A regra do repositorio e que
    # quem escreve dinheiro e o dono, e aqui ela vale sem excecao por corpo.
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_courier_fee(
    branch_id: UUID,
    payload: AdminBranchCourierFeeUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchCourierFeeResponse:
    """`base + km x por_km`, congelada em cada atribuicao a partir daqui.

    Motoboy pago por corrida: `courier_fee_base` preenchida e
    `courier_fee_per_km` em `0` (ou ausente). Campo ausente do corpo nao e
    tocado; `null` explicito apaga. **Nao mexe em nenhuma corrida ja
    atribuida** — a taxa e congelada no momento da atribuicao.

    Nenhum numero que o CLIENTE paga muda com isto.
    """
    return AdminCourierService(db).update_courier_fee(scope, branch_id, payload)


# --- O cadastro ---------------------------------------------------------------


@router.get(
    "/couriers",
    response_model=list[AdminCourierResponse],
    # PESSOAS: e o atendente quem atribui pedido a motoboy, e para isso ele
    # precisa da lista. O que sai aqui e nome e telefone de funcionario, nao
    # de cliente.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_couriers(
    branch_id: UUID | None = Query(
        default=None,
        description="Filtra por filial. Quem so tem acesso a uma filial ja vem filtrado.",
    ),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminCourierResponse]:
    """Os entregadores do restaurante do token, inativos inclusive.

    Excluidos nao aparecem. `has_access` diz se ja existe um link+codigo
    valendo — o par em si nunca sai desta rota.
    """
    return AdminCourierService(db).list_couriers(scope, branch_id=branch_id)


@router.post(
    "/couriers",
    response_model=AdminCourierResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def create_courier(
    payload: AdminCourierCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCourierResponse:
    """Nome e telefone, numa filial. Nasce ativo e SEM acesso: o link e o
    codigo saem de `POST /admin/couriers/{id}/access`.

    O telefone e unico por filial (409). Quem serve duas lojas tem dois
    cadastros, um por filial.
    """
    return AdminCourierService(db).create_courier(scope, payload)


@router.get(
    "/couriers/{courier_id}",
    response_model=AdminCourierResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_courier(
    courier_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCourierResponse:
    return AdminCourierService(db).get_courier(scope, courier_id)


@router.patch(
    "/couriers/{courier_id}",
    response_model=AdminCourierResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def update_courier(
    courier_id: UUID,
    payload: AdminCourierUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCourierResponse:
    """PATCH parcial: nome, telefone, ativo/inativo.

    `is_active: false` tira o acesso na hora e **devolve os pedidos abertos
    dele para a fila** (as atribuicoes sao fechadas). `true` de volta nao
    reabre nada e nao recria o acesso: o par gerado antes continua valendo.
    """
    return AdminCourierService(db).update_courier(scope, courier_id, payload)


@router.delete(
    "/couriers/{courier_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def delete_courier(
    courier_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> Response:
    """Exclui o entregador: some das listas, perde o acesso na hora, os
    pedidos abertos voltam para a fila. O historico de corridas dele
    continua existindo — e o que o dono usa para pagar.

    O telefone fica livre para um cadastro novo.
    """
    AdminCourierService(db).delete_courier(scope, courier_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/couriers/{courier_id}/access",
    response_model=AdminCourierAccessResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def generate_courier_access(
    courier_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCourierAccessResponse:
    """Gera (ou REGENERA) o link e o codigo do entregador.

    A resposta e a unica vez que os dois existem em claro. O painel monta a
    URL do app do entregador com `link_token` e entrega o par ao motoboy;
    ele digita o codigo uma vez e o app guarda.

    Chamar de novo gera outro par, e **o anterior morre na hora** — nao ha
    sessao nem token derivado que sobreviva. E o botao de "o motoboy saiu".
    Entregador inativo responde 409.
    """
    return AdminCourierService(db).generate_access(scope, courier_id)


# --- A atribuicao ---------------------------------------------------------------


@router.post(
    "/couriers/{courier_id}/assignments",
    response_model=AdminAssignmentBatchResponse,
    # PESSOAS: por pedido nas maos de um motoboy e trabalho do balcao, como
    # aceitar e mover o pedido.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def assign_orders(
    courier_id: UUID,
    payload: AdminAssignOrdersRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminAssignmentBatchResponse:
    """Atribui um ou mais pedidos a este entregador.

    **A resposta e por item, na ordem do corpo.** Cada pedido diz `ok` ou o
    `error`: `not_found` (nao alcancado por este lojista), `not_delivery`
    (retirada), `order_closed` (ja terminou) ou `other_branch` (o motoboy e
    de outra filial). Os `ok` sao gravados juntos; os outros nao sao
    gravados. A rota so responde erro HTTP para o entregador em si (404 fora
    do escopo, 409 inativo) ou para o corpo (422).

    **Reatribuir e chamar esta rota com outro entregador**: a atribuicao
    anterior e fechada e a nova aberta, com a taxa de agora. Atribuir ao
    mesmo de novo nao muda nada.

    A taxa do entregador e congelada aqui (`courier_fee_snapshot`), da
    configuracao da filial sobre a distancia que o pedido ja tinha. Nulo
    quando a filial nao tem taxa configurada.
    """
    return AdminCourierService(db).assign_orders(scope, courier_id, payload)


@router.get(
    "/couriers/{courier_id}/assignments",
    response_model=list[AdminAssignmentResponse],
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def list_open_assignments(
    courier_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[AdminAssignmentResponse]:
    """Os pedidos que estao nas maos deste entregador agora (atribuicoes
    abertas), do mais antigo ao mais novo. Historico e o do proprio
    entregador, pelo link dele."""
    return AdminCourierService(db).list_open_assignments(scope, courier_id)


@router.get(
    "/orders/{order_id}/courier",
    response_model=AdminOrderCourierResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_order_courier(
    order_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOrderCourierResponse:
    """Quem esta com este pedido. Os dois campos nulos = ninguem ainda —
    estado normal do pedido, e nao 404. 404 e o pedido que este lojista nao
    alcanca."""
    return AdminCourierService(db).get_order_courier(scope, order_id)


@router.delete(
    "/orders/{order_id}/courier",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def unassign_order(
    order_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> Response:
    """Tira o pedido das maos de quem estiver com ele. 409 se ninguem
    estiver. A linha da atribuicao nao e apagada: fica fechada, com quem e
    quando, no historico."""
    AdminCourierService(db).unassign_order(scope, order_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
