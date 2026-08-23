"""Configuracao de cashback no painel: a regra da rede e a de cada filial.

## Os papeis, e por que ESCREVER aqui e SOMENTE_DONO

Escrever cashback e escrever preco por outra porta, e o precedente do
repositorio ja esta tomado: `POST` e `PATCH` de cupom sao `SOMENTE_DONO`
("desconto e preco por outra porta"), e `PATCH /admin/settings` tambem, por
ser o contrato comercial da loja. A regra de cashback e as duas coisas ao
mesmo tempo.

E ela tem uma propriedade que o cupom nao tem: **ligar a chave mexe em
faturamento no mesmo minuto, sem passar por tela de cliente nenhuma.**
`cashback_rules.enabled` liga o credito E o resgate juntos, e o resgate entra
como subtracao na base da comissao — quer dizer que o primeiro pedido depois
do clique ja fecha com outro numero. Nao ha rascunho, nao ha data de inicio, e
nao ha o "cupom que ninguem usou": a campanha vale para todo pedido a partir
do commit.

LER e `GERENCIA`, o mesmo do `GET /admin/coupons`, e nao `PESSOAS`. O
percentual e termo comercial, nao alavanca de balcao: o atendente nao aplica
cashback a mao — quem resolve e `resolve_cashback_terms` no checkout — e a
senha do balcao e a que mais circula. O gerente precisa porque e ele quem
responde ao cliente que pergunta quanto voltou.

## Por que nao ha DELETE da regra do restaurante

Apagar a linha da rede NAO desliga a campanha: a filial com sobrescrita
propria continua creditando, porque a resolucao nem olha a linha do
restaurante quando a da filial existe. O motivo completo esta em
`AdminCashbackService`.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_cashback_schema import (
    AdminCashbackRuleResponse,
    AdminCashbackRuleView,
    AdminCashbackRuleWrite,
)
from src.services.admin_cashback_service import AdminCashbackService


# O restaurante sai do token; a FILIAL aparece no path e nao autoriza nada —
# `AdminCashbackService._get_branch` confronta o id com o escopo antes de
# qualquer leitura ou escrita, e responde 404 tanto para filial de outro
# restaurante quanto para filial fora do escopo deste lojista.
router = APIRouter(prefix="/admin", tags=["admin cashback"])


@router.get(
    "/cashback-rules",
    response_model=AdminCashbackRuleView,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def get_restaurant_cashback_rule(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCashbackRuleView:
    """A regra padrao da rede — a que toda filial sem sobrescrita herda.

    `source: "none"` significa que ninguem configurou cashback neste
    restaurante, e nao e o mesmo que `enabled: false`. Os dois nao geram
    cashback; so o segundo tem numeros para a tela mostrar.
    """
    return AdminCashbackService(db).get_restaurant_rule(scope)


@router.put(
    "/cashback-rules",
    response_model=AdminCashbackRuleResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def replace_restaurant_cashback_rule(
    payload: AdminCashbackRuleWrite,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCashbackRuleResponse:
    """Cria ou substitui a regra da rede. Nao alcanca filial com sobrescrita.

    **`weekdays` substitui a lista inteira.** Dia que sair do corpo deixa de
    ter percentual proprio e volta a valer `default_percent` — nunca zero.
    """
    return AdminCashbackService(db).replace_restaurant_rule(scope, payload)


@router.get(
    "/branches/{branch_id}/cashback-rules",
    response_model=AdminCashbackRuleView,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def get_branch_cashback_rule(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCashbackRuleView:
    """A regra que VALE nesta filial, com `source` dizendo de onde ela veio.

    `source: "branch"` e regra propria da loja; `"restaurant"` e a da rede,
    herdada inteira. O painel precisa da diferenca para saber se salvar ali
    edita a rede ou **cria uma sobrescrita**.
    """
    return AdminCashbackService(db).get_branch_rule(scope, branch_id)


@router.put(
    "/branches/{branch_id}/cashback-rules",
    response_model=AdminCashbackRuleResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def replace_branch_cashback_rule(
    branch_id: UUID,
    payload: AdminCashbackRuleWrite,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCashbackRuleResponse:
    """Cria ou substitui a sobrescrita desta filial.

    A partir daqui a loja **para de herdar**: mudanca na regra da rede nao a
    alcanca mais, ate alguem apagar a sobrescrita. E tambem como uma loja sai
    da campanha sozinha — sobrescrita propria com `enabled: false`.
    """
    return AdminCashbackService(db).replace_branch_rule(scope, branch_id, payload)


@router.delete(
    "/branches/{branch_id}/cashback-rules",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def delete_branch_cashback_rule(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> Response:
    """Apaga a sobrescrita: a filial volta a herdar a regra da rede.

    404 quando a filial nao tem regra propria — "voltou a herdar agora" e "ja
    herdava" sao estados diferentes na tela.
    """
    AdminCashbackService(db).delete_branch_rule(scope, branch_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
