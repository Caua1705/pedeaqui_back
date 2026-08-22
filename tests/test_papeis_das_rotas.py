"""O papel exigido por CADA rota /admin, travado numa tabela.

## Por que existe uma tabela aqui, se a regra diz que nao deve haver uma

A regra (`src/api/dependencies/admin_scope.py`) e que a autorizacao mora no
`dependencies=[...]` de cada rota, e nao numa tabela central. O motivo: uma
tabela envelhece separada das rotas, e a rota nova nasce sem linha nela —
falhando ABERTA.

Esta tabela e o contrario disso, e por uma diferenca que muda tudo: **ela nao
decide nada.** Ela e a lista do que ja foi decidido, conferida contra o que o
`app` de fato aplica. Rota nova sem papel nao passa despercebida: ela nao
esta aqui, e o teste fica vermelho pedindo uma decisao.

Ou seja, a tabela que falha aberta e a que AUTORIZA; a que falha fechada e a
que AUDITA. Sao a mesma estrutura em papeis opostos.

## O que este arquivo NAO prova

Que o 403 sai de verdade. Isso e `tests/test_auditoria_papeis.py`, que fala
com o Postgres. Aqui se prova a decisao declarada, sem banco — o que faz este
teste rodar no laco rapido (`pytest -m "not db"`), que e onde uma rota nova
aparece pela primeira vez.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from main import app
from src.api.dependencies.admin_scope import (
    AGENTE_DE_IMPRESSAO,
    GERENCIA,
    PESSOAS,
    PESSOAS_E_AGENTE,
    SOMENTE_DONO,
    AdminScope,
    ensure_pode_ler_dinheiro,
)


# metodo + caminho -> papeis que a rota aceita.
PAPEL_ESPERADO = {
    # --- cardapio: ler e do balcao, escrever e da gerencia, preco e do dono
    ("GET", "/admin/categories"): PESSOAS,
    ("POST", "/admin/categories"): GERENCIA,
    ("PATCH", "/admin/categories/reorder"): GERENCIA,
    ("PATCH", "/admin/categories/{category_id}"): GERENCIA,
    ("GET", "/admin/products"): PESSOAS,
    # `price` e obrigatorio na criacao: deixar o gerente criar produto e
    # deixa-lo definir preco.
    ("POST", "/admin/products"): SOMENTE_DONO,
    ("PATCH", "/admin/products/reorder"): GERENCIA,
    ("GET", "/admin/products/{product_id}"): PESSOAS,
    ("PATCH", "/admin/products/{product_id}"): GERENCIA,
    ("PATCH", "/admin/products/{product_id}/availability"): PESSOAS,
    ("POST", "/admin/products/{product_id}/image"): GERENCIA,
    ("GET", "/admin/products/{product_id}/option-groups"): PESSOAS,
    ("POST", "/admin/products/{product_id}/option-groups"): GERENCIA,
    ("PATCH", "/admin/option-groups/{group_id}"): GERENCIA,
    ("POST", "/admin/option-groups/{group_id}/options"): GERENCIA,
    ("PATCH", "/admin/options/{option_id}"): GERENCIA,
    # --- pedidos
    ("GET", "/admin/orders"): PESSOAS,
    ("GET", "/admin/orders/status-counts"): PESSOAS,
    ("POST", "/admin/orders/stream-ticket"): PESSOAS_E_AGENTE,
    ("GET", "/admin/orders/{order_id}"): PESSOAS,
    ("GET", "/admin/orders/{order_id}/print-jobs"): PESSOAS_E_AGENTE,
    ("PATCH", "/admin/orders/{order_id}/status"): PESSOAS,
    ("PATCH", "/admin/orders/{order_id}/cancel"): GERENCIA,
    # --- configuracao
    ("GET", "/admin/settings"): PESSOAS,
    ("PATCH", "/admin/settings"): SOMENTE_DONO,
    ("GET", "/admin/branches"): PESSOAS,
    ("GET", "/admin/branches/operation"): PESSOAS,
    ("GET", "/admin/branches/{branch_id}"): PESSOAS,
    ("PATCH", "/admin/branches/{branch_id}"): GERENCIA,
    ("PATCH", "/admin/branches/{branch_id}/store-status"): PESSOAS,
    ("PATCH", "/admin/branches/{branch_id}/order-types"): GERENCIA,
    # A pausa da entrega e PESSOAS, e `order-types` e GERENCIA, e a diferenca
    # entre as duas e o PRAZO: a pausa vence sozinha em ate 24h, o
    # `accepts_delivery` espera alguem religar. Papel acompanha consequencia.
    ("PATCH", "/admin/branches/{branch_id}/delivery-pause"): PESSOAS,
    ("GET", "/admin/branches/{branch_id}/delivery-time-bands"): PESSOAS,
    ("PUT", "/admin/branches/{branch_id}/delivery-time-bands"): GERENCIA,
    ("PATCH", "/admin/branches/{branch_id}/settings"): SOMENTE_DONO,
    ("GET", "/admin/branches/{branch_id}/business-hours"): PESSOAS,
    ("PUT", "/admin/branches/{branch_id}/business-hours"): GERENCIA,
    ("PATCH", "/admin/branches/{branch_id}/prep-time"): PESSOAS,
    ("GET", "/admin/branches/{branch_id}/payment-methods"): PESSOAS,
    ("POST", "/admin/branches/{branch_id}/payment-methods"): GERENCIA,
    ("PATCH", "/admin/payment-methods/{method_id}"): GERENCIA,
    ("DELETE", "/admin/payment-methods/{method_id}"): GERENCIA,
    # --- impressao
    ("GET", "/admin/branches/{branch_id}/printing-sectors"): PESSOAS,
    ("POST", "/admin/branches/{branch_id}/printing-sectors"): GERENCIA,
    ("PATCH", "/admin/printing-sectors/{sector_id}"): GERENCIA,
    # Rodape e contagem de vias: leitura de balcao, escrita de gerencia. A
    # mensagem PADRAO da marca fica em PATCH /admin/settings, que e do dono.
    ("GET", "/admin/branches/{branch_id}/print-settings"): PESSOAS,
    ("PATCH", "/admin/branches/{branch_id}/print-settings"): GERENCIA,
    ("PATCH", "/admin/products/{product_id}/printing-sector"): GERENCIA,
    ("PATCH", "/admin/categories/{category_id}/printing-sector"): GERENCIA,
    ("POST", "/admin/print-agent/heartbeat"): AGENTE_DE_IMPRESSAO,
    ("POST", "/admin/print-agent/printers"): AGENTE_DE_IMPRESSAO,
    ("GET", "/admin/branches/{branch_id}/print-agent"): PESSOAS,
    ("GET", "/admin/branches/{branch_id}/printers"): GERENCIA,
    ("POST", "/admin/branches/{branch_id}/print-test"): PESSOAS,
    # --- clientes e relatorios: o que uma senha vazada nao pode alcancar
    ("GET", "/admin/customers"): GERENCIA,
    ("GET", "/admin/reports/commission"): SOMENTE_DONO,
    # Os tres de dinheiro passaram de SOMENTE_DONO para GERENCIA na revisao
    # 20260820_0026, e a mudanca NAO afrouxa: a rota agora exige recorte de
    # UMA filial para quem nao e dono (`ensure_pode_ler_dinheiro`), e gerente
    # sem `branch_id` continua recebendo 403. O que se abriu foi o gerente
    # lendo a PROPRIA loja — antes "ler faturamento" so existia na versao "do
    # restaurante inteiro", e era essa que se recusava.
    #
    # `commission` ficou de fora: e o percentual negociado com a plataforma
    # (armadilha 17), nao desempenho de loja, e nao ha recorte que a torne
    # assunto de quem toca o balcao.
    ("GET", "/admin/reports/summary"): GERENCIA,
    ("GET", "/admin/reports/sales-by-day"): GERENCIA,
    ("GET", "/admin/reports/payment-methods"): GERENCIA,
    ("GET", "/admin/reports/products"): GERENCIA,
    ("GET", "/admin/reports/cancellations"): GERENCIA,
    # --- avaliacao de pedido
    #
    # GERENCIA, e nao SOMENTE_DONO. A divisao dos relatorios acima e "dinheiro
    # do restaurante inteiro e do dono"; avaliacao nao diz quanto entrou.
    # Quem conserta atraso e pedido errado e quem toca a loja, e nota que so o
    # dono ve nao vira conserto no balcao.
    ("GET", "/admin/reviews"): GERENCIA,
    # --- cupom: desconto e preco por outra porta
    ("GET", "/admin/coupons"): GERENCIA,
    ("POST", "/admin/coupons"): SOMENTE_DONO,
    ("PATCH", "/admin/coupons/{coupon_id}"): SOMENTE_DONO,
}

# Rotas /admin SEM `exigir_papel`, cada uma com o motivo. Toda rota que nao
# esteja aqui nem em `PAPEL_ESPERADO` derruba o teste — que e o ponto.
SEM_EXIGENCIA_DE_PAPEL = {
    # Login e a porta de entrada de TODOS os papeis, inclusive o do agente de
    # impressao, que se autentica por e-mail e senha do config.ini. Recusar
    # `print_agent` aqui pararia a impressao de todas as lojas. Quem recusa a
    # conta de maquina e a TELA do painel, depois do login.
    ("POST", "/admin/auth/login"),
    # Identidade e troca da propria senha: todo papel tem as duas, e e por
    # `me` que o painel descobre o `role` para desenhar a tela.
    ("GET", "/admin/auth/me"),
    ("PATCH", "/admin/auth/password"),
    # O stream confere o papel DENTRO da funcao (`ensure_role`), e nao pode
    # usar `Depends(exigir_papel(...))`: ele autentica por ticket na
    # querystring, porque o EventSource do navegador nao manda cabecalho.
    # Uma dependencia de papel exigiria o Bearer que aqui nao existe e
    # recusaria o painel inteiro com 401.
    ("GET", "/admin/orders/stream"),
}


def rotas_admin():
    """Cada (metodo, caminho) de /admin, com os papeis que a rota declara."""
    encontradas = []
    for route in app.routes:
        caminho = getattr(route, "path", "")
        if not caminho.startswith("/admin"):
            continue
        for metodo in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            encontradas.append((metodo, caminho, papeis_declarados(route)))
    return encontradas


def papeis_declarados(route) -> tuple[str, ...] | None:
    """Os papeis do `exigir_papel` da rota, ou None se ela nao tem nenhum."""
    for dependencia in route.dependencies:
        papeis = getattr(dependencia.dependency, "papeis_exigidos", None)
        if papeis is not None:
            return papeis
    return None


@pytest.mark.parametrize(
    "metodo,caminho,papeis",
    [pytest.param(*rota, id=f"{rota[0]} {rota[1]}") for rota in rotas_admin()],
)
def test_cada_rota_admin_declara_o_papel_esperado(metodo, caminho, papeis):
    chave = (metodo, caminho)

    if chave in SEM_EXIGENCIA_DE_PAPEL:
        assert papeis is None, (
            f"{metodo} {caminho} esta na lista das que NAO exigem papel, mas "
            f"passou a exigir {papeis}. Se a mudanca e intencional, mova-a "
            f"para PAPEL_ESPERADO."
        )
        return

    assert chave in PAPEL_ESPERADO, (
        f"{metodo} {caminho} e uma rota /admin sem decisao de papel registrada."
        f"\n\nToda rota do painel precisa responder 'quem pode isto?'. Sem a "
        f"linha em PAPEL_ESPERADO, a rota nasce aberta a qualquer papel — "
        f"inclusive ao `print_agent`, cuja senha esta em texto puro no "
        f"config.ini da maquina do balcao.\n\nAcrescente a rota a "
        f"PAPEL_ESPERADO (com o `dependencies=[Depends(exigir_papel(...))]` "
        f"correspondente) ou a SEM_EXIGENCIA_DE_PAPEL, com o motivo escrito."
    )
    assert papeis == PAPEL_ESPERADO[chave], (
        f"{metodo} {caminho} exige {papeis}, mas o contrato registrado e "
        f"{PAPEL_ESPERADO[chave]}. Mudar isto e mudanca de contrato: rotas "
        f"que respondiam 200 passam a responder 403 e o painel precisa "
        f"esconder o botao junto."
    )


def test_o_agente_de_impressao_alcanca_exatamente_as_rotas_de_que_precisa():
    """A lista fechada do que uma senha vazada do balcao compra.

    `print-agent/print_agent/api_client.py` faz seis chamadas: login, ticket,
    stream, print-jobs, heartbeat e a lista de impressoras. Nenhuma outra rota
    do painel pode aceitar este papel — e a frente inteira existe para essa
    lista ser curta.

    Se este teste ficar vermelho, a pergunta nao e "atualizo a lista?", e sim
    "o que o agente passou a precisar, e por que?".
    """
    alcancadas = {
        (metodo, caminho)
        for metodo, caminho, papeis in rotas_admin()
        if papeis is not None and "print_agent" in papeis
    }

    assert alcancadas == {
        ("POST", "/admin/orders/stream-ticket"),
        ("GET", "/admin/orders/{order_id}/print-jobs"),
        ("POST", "/admin/print-agent/heartbeat"),
        ("POST", "/admin/print-agent/printers"),
    }


def test_a_tabela_nao_tem_linha_para_rota_que_nao_existe_mais():
    """Linha orfa aqui e pior que linha faltando.

    Ela da a impressao de que uma rota apagada continua protegida, e a
    proxima pessoa a ler a tabela conta com uma protecao que nao existe.
    """
    reais = {(metodo, caminho) for metodo, caminho, _ in rotas_admin()}
    orfas = set(PAPEL_ESPERADO) - reais | (SEM_EXIGENCIA_DE_PAPEL - reais)

    assert not orfas, f"a tabela cita rotas que o app nao tem: {sorted(orfas)}"


# ---------------------------------------------------------------------------
# A regra que a tabela acima nao consegue expressar
# ---------------------------------------------------------------------------


def _escopo(papel: str, branch_id=None) -> AdminScope:
    return AdminScope(
        admin_user=SimpleNamespace(role=papel),
        restaurant_id=uuid.uuid4(),
        branch_id=branch_id,
    )


class TestQuemLeDinheiro:
    """`ensure_pode_ler_dinheiro`, a segunda excecao a `exigir_papel`.

    A rota declara GERENCIA; quem decide de verdade e o RECORTE que veio
    junto com a chamada — do mesmo jeito que `ensure_pode_definir_preco` e
    decidido pelo corpo.

    A troca de SOMENTE_DONO por "GERENCIA com recorte" nao afrouxa nada. O
    que se recusava era ler o faturamento do RESTAURANTE INTEIRO, que era a
    unica leitura que existia antes de os relatorios terem filial. Essa
    continua recusada; o que passou a existir e o gerente lendo a propria
    loja.
    """

    def test_o_dono_le_sem_recorte(self):
        ensure_pode_ler_dinheiro(_escopo("owner"), None)

    def test_o_dono_le_com_recorte(self):
        ensure_pode_ler_dinheiro(_escopo("owner"), uuid.uuid4())

    def test_o_gerente_le_a_propria_filial(self):
        filial = uuid.uuid4()
        ensure_pode_ler_dinheiro(_escopo("manager", filial), filial)

    def test_o_gerente_sem_recorte_e_403(self):
        """O caso que o SOMENTE_DONO antigo protegia, e continua protegido.

        Sem `branch_id`, a consulta soma as lojas todas — e ai e exatamente a
        leitura de antes.
        """
        with pytest.raises(HTTPException) as erro:
            ensure_pode_ler_dinheiro(_escopo("manager"), None)

        assert erro.value.status_code == 403

    def test_o_atendente_nao_le_nem_com_recorte(self):
        """403 com filial escolhida tambem: `PESSOAS` nao inclui dinheiro."""
        with pytest.raises(HTTPException) as erro:
            ensure_pode_ler_dinheiro(_escopo("attendant"), uuid.uuid4())

        assert erro.value.status_code == 403

    def test_o_agente_de_impressao_nao_le(self):
        """A senha dele fica em texto puro no config.ini da maquina do balcao."""
        with pytest.raises(HTTPException):
            ensure_pode_ler_dinheiro(_escopo("print_agent"), uuid.uuid4())

