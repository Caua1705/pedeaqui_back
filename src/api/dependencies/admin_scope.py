"""Quem e o lojista, ONDE ele mexe e O QUE ele pode fazer.

Duas dimensoes, independentes, no mesmo arquivo de proposito — quem for
mudar uma precisa ver a outra.

## ONDE: o escopo (`AdminScope`)

Existe para que a regra de filial seja resolvida em UM lugar. Antes cada
rota teria que lembrar de olhar `admin_user.branch_id` e cada esquecimento
seria um vazamento entre filiais — o mesmo tipo de erro que o
`restaurant_id` no path causava entre restaurantes.

A regra:

- role "owner": ve e edita todas as filiais do restaurante, mesmo que
  `admin_users.branch_id` esteja preenchido. Dono nao se prende a filial.
- role "manager" / "attendant" / "print_agent": se `branch_id` estiver
  preenchido, so aquela filial; se for nulo, todas.

`branch_id = None` neste objeto significa SEMPRE "todas as filiais do
restaurante". O restaurante nunca e opcional: sai do token, ponto.

## O QUE: o papel (`exigir_papel`)

Ate a frente de papeis, `role` nao entrava em NENHUMA decisao de
autorizacao: servia so para o "owner" ignorar `branch_id`, ali em cima. Os
tres papeis eram, na pratica, o mesmo usuario com nomes diferentes — e
`tests/test_auditoria_papeis.py` mediu isso contra o Postgres.

O que fez isso virar urgente: a senha do usuario do agente de impressao fica
em texto puro no `config.ini` da maquina do balcao, e esse usuario era
`attendant`. Quem lesse o arquivo levava junto o preco do cardapio, a lista
de clientes com telefone e o faturamento do restaurante inteiro.

**403 e nao 404 aqui**, ao contrario da regra de filial logo abaixo. Os dois
casos parecem o mesmo e nao sao: 404 protege o que o usuario NAO PODE SABER
que existe (a filial do lado, o restaurante do concorrente). O papel e sobre
um recurso que ele sabe que existe — ele ve o botao na propria tela. Esconder
com 404 nao esconderia nada e so tornaria o erro impossivel de depurar.

**Nao existe tabela central de rota -> papel, e isso e decisao.** A regra
mora no `dependencies=[...]` de cada rota. Uma tabela envelhece separada das
rotas, e a rota nova nasce sem linha nela — falhando ABERTA, que e o modo de
falhar que nao se percebe.
"""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from src.api.dependencies.admin_auth import get_current_admin
from src.models.admin_user_model import AdminUser


# Papel que enxerga o restaurante inteiro. Deixado como constante para que a
# comparacao nao apareca solta como string no meio das regras.
UNRESTRICTED_ROLE = "owner"

# Os conjuntos de papeis, com nome. Cada rota aponta para um destes, e a
# leitura da rota vira uma frase — `dependencies=[Depends(exigir_papel(GERENCIA))]`
# se le sozinho, `("owner", "manager")` no meio do decorador nao.
SOMENTE_DONO = ("owner",)
GERENCIA = ("owner", "manager")

# Tudo menos a maquina. E o default de quase toda rota do painel: o agente de
# impressao nao tem tela, nao tem pessoa e nao deve alcancar nada que nao
# esteja na lista curta abaixo.
PESSOAS = ("owner", "manager", "attendant")

# As oito rotas que o agente de impressao usa, e mais nenhuma. Ver
# `print-agent/print_agent/api_client.py`: login, ticket, stream, print-jobs,
# heartbeat e a lista de impressoras.
AGENTE_DE_IMPRESSAO = ("print_agent",)
PESSOAS_E_AGENTE = ("owner", "manager", "attendant", "print_agent")


@dataclass(frozen=True)
class AdminScope:
    """O que este lojista pode ver e escrever.

    Frozen de proposito: escopo e resultado da autenticacao, nao estado que
    um service possa afrouxar no meio de uma requisicao.
    """

    admin_user: AdminUser
    restaurant_id: uuid.UUID
    branch_id: uuid.UUID | None

    @property
    def sees_all_branches(self) -> bool:
        return self.branch_id is None

    def ensure_branch_allowed(self, branch_id: uuid.UUID) -> None:
        """Recusa uma filial fora do escopo.

        404 e nao 403 pelo mesmo motivo de `ensure_restaurant_scope`: um 403
        confirmaria que aquela filial existe. Para quem esta preso a uma
        filial, as outras simplesmente nao existem.
        """
        if self.branch_id is not None and self.branch_id != branch_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )

    def resolve_branch_filter(self, requested_branch_id: uuid.UUID | None) -> uuid.UUID | None:
        """Filial a usar no WHERE de uma listagem.

        O filtro que o painel manda na querystring so restringe, nunca
        amplia: um lojista preso a uma filial que pedir outra recebe 404 em
        vez da lista alheia.
        """
        if requested_branch_id is None:
            return self.branch_id
        self.ensure_branch_allowed(requested_branch_id)
        return requested_branch_id


def build_admin_scope(admin_user: AdminUser) -> AdminScope:
    """Aplica a regra de filial a um lojista ja autenticado.

    Separada da dependencia porque o stream SSE nao autentica por
    `Depends(get_current_admin)` — ele valida um ticket de querystring — e
    precisa chegar exatamente no mesmo escopo. Duas copias da regra seriam
    duas chances de divergir.
    """
    branch_id = None if admin_user.role == UNRESTRICTED_ROLE else admin_user.branch_id
    return AdminScope(
        admin_user=admin_user,
        restaurant_id=admin_user.restaurant_id,
        branch_id=branch_id,
    )


def get_admin_scope(admin_user: AdminUser = Depends(get_current_admin)) -> AdminScope:
    return build_admin_scope(admin_user)


def ensure_role(admin_user: AdminUser, papeis: tuple[str, ...]) -> None:
    """Recusa quem nao tem o papel. 403.

    O papel vem do BANCO (`admin_user.role`), nunca do `role` que viaja no
    token. `get_admin_from_token` recarrega o usuario a cada requisicao
    justamente para isso: rebaixar alguem no banco vale na hora, sem esperar
    as 12h do token expirarem e sem lista de revogacao.
    """
    if admin_user.role in papeis:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Seu perfil de acesso nao permite esta acao",
    )


def ensure_pode_definir_preco(admin_user: AdminUser) -> None:
    """Quem escreve dinheiro no cardapio e o dono. 403 para os outros.

    Esta e a UNICA regra que `exigir_papel` nao consegue expressar, e por um
    motivo concreto: `PATCH /admin/products/{id}` edita nome, descricao,
    categoria e preco pela mesma rota, e o gerente precisa dos tres
    primeiros. Quem decide aqui e o CORPO da requisicao, nao a rota — entao
    a checagem mora dentro dela, na linha de cima do campo.

    O que ela protege: a conta de gerente valendo desconto ilimitado. Sem
    isso, "editar produto" e "dar 99% em tudo" sao a mesma permissao.
    """
    ensure_role(admin_user, SOMENTE_DONO)


def exigir_papel(papeis: tuple[str, ...]):
    """Dependencia de rota que exige um dos papeis.

    Usada em `dependencies=[...]` do decorador, e nao na assinatura da
    funcao: a rota nao precisa do valor, so do efeito, e assim a checagem
    fica visivel na primeira linha de quem le a rota.
    """

    def dependencia(admin_user: AdminUser = Depends(get_current_admin)) -> None:
        ensure_role(admin_user, papeis)

    # O papel fica legivel na propria funcao para
    # `tests/test_papeis_das_rotas.py` conseguir enumerar a decisao de TODA
    # rota /admin sem banco e sem desmontar closure. E o que faz uma rota nova
    # sem papel virar teste vermelho em vez de rota aberta.
    dependencia.papeis_exigidos = papeis
    return dependencia
