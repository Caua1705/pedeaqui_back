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

from src.api.dependencies.admin_scope import (
    AGENTE_DE_IMPRESSAO,
    GERENCIA,
    PESSOAS,
    PESSOAS_E_AGENTE,
    SOMENTE_DONO,
    AdminScope,
    ensure_pode_definir_cashback,
    ensure_pode_ler_dinheiro,
)
from tests.rotas_do_app import rotas_com_caminho


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
    # O PERFIL e outra tabela (`restaurants`) e outra decisao: a vitrine da
    # marca inteira, e `assistant_notes`, que entra no PROMPT do assistente
    # que fala com o cliente. Escrever no prompt nao pode ser a senha do
    # balcao, que e a que mais circula.
    ("GET", "/admin/restaurant"): PESSOAS,
    ("PATCH", "/admin/restaurant"): SOMENTE_DONO,
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
    # --- entregadores
    #
    # A taxa e o que a LOJA PAGA por corrida: ler e termo comercial (GERENCIA,
    # como o cupom), escrever e dinheiro (SOMENTE_DONO). Nenhuma rota daqui
    # aceita o agente de impressao.
    ("GET", "/admin/branches/{branch_id}/courier-fee"): GERENCIA,
    ("PATCH", "/admin/branches/{branch_id}/courier-fee"): SOMENTE_DONO,
    # Ler a lista e do balcao: e o atendente quem atribui pedido a motoboy.
    # Cadastrar, editar, excluir e gerar o codigo sao da gerencia, como o
    # setor de impressao — o codigo e credencial, e credencial nao sai da
    # senha que mais circula.
    ("GET", "/admin/couriers"): PESSOAS,
    ("POST", "/admin/couriers"): GERENCIA,
    ("GET", "/admin/couriers/{courier_id}"): PESSOAS,
    ("PATCH", "/admin/couriers/{courier_id}"): GERENCIA,
    ("DELETE", "/admin/couriers/{courier_id}"): GERENCIA,
    ("POST", "/admin/couriers/{courier_id}/access"): GERENCIA,
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
    # --- cashback: a mesma porta do cupom, com uma diferenca
    #
    # ESCREVER e SOMENTE_DONO pelo motivo do cupom (desconto e preco por
    # outra porta) mais um que o cupom nao tem: `enabled` liga o credito E o
    # resgate juntos, o resgate entra como subtracao na base da comissao, e o
    # primeiro pedido depois do commit ja fecha com outro numero. Nao existe o
    # "cupom que ninguem usou".
    #
    # LER e GERENCIA, igual a `GET /admin/coupons`: e termo comercial, nao
    # alavanca de balcao — o atendente nao aplica cashback a mao, quem resolve
    # e `resolve_cashback_terms` no checkout.
    ("GET", "/admin/cashback-rules"): GERENCIA,
    ("PUT", "/admin/cashback-rules"): SOMENTE_DONO,
    ("GET", "/admin/branches/{branch_id}/cashback-rules"): GERENCIA,
    ("PUT", "/admin/branches/{branch_id}/cashback-rules"): SOMENTE_DONO,
    ("DELETE", "/admin/branches/{branch_id}/cashback-rules"): SOMENTE_DONO,
    # --- cupom: desconto e preco por outra porta
    ("GET", "/admin/coupons"): GERENCIA,
    ("POST", "/admin/coupons"): SOMENTE_DONO,
    ("PATCH", "/admin/coupons/{coupon_id}"): SOMENTE_DONO,
    # O catalogo de arte da plataforma. GERENCIA e nao SOMENTE_DONO porque a
    # lista nao tem valor, prazo nem restaurante — e o que o painel precisa
    # para desenhar tanto o seletor do POST quanto a tela de leitura.
    ("GET", "/admin/coupon-templates"): GERENCIA,
    # --- usuarios do painel: as quatro sao do dono, inclusive a de LER
    #
    # A lista diz quem tem acesso ao faturamento do restaurante — e o mapa de
    # quem atacar, e nao o que o balcao precisa para atender. A pergunta "o
    # gerente precisa ver a equipe da filial dele?" foi feita e respondida
    # antes de escrever a rota: nao. Se um dia virar necessidade, e rota
    # diferente, com recorte de filial e sem e-mail na resposta.
    ("GET", "/admin/users"): SOMENTE_DONO,
    ("POST", "/admin/users"): SOMENTE_DONO,
    ("PATCH", "/admin/users/{admin_user_id}"): SOMENTE_DONO,
    ("POST", "/admin/users/{admin_user_id}/reset-password"): SOMENTE_DONO,
    # --- relato de erro: PESSOAS, e de proposito o papel mais largo
    #
    # Quem mais esbarra em erro e quem passa o dia na tela, e esse e o
    # atendente. Fechar na gerencia faria o erro chegar de segunda mao,
    # contado por quem nao viu. A rota so escreve, so no proprio restaurante
    # (que sai do token) e nao le nada.
    ("POST", "/admin/error-reports"): PESSOAS,
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
    """Cada (metodo, caminho) de /admin, com os papeis que a rota declara.

    `rotas_com_caminho()` e nao `app.routes`: desde o starlette 1.6 o
    `include_router` nao copia mais as rotas para la, e ler `app.routes`
    direto devolvia ZERO rotas /admin — com a lista vazia, o teste
    parametrizado abaixo gera zero casos e a auditoria some sem falhar.
    Ver `tests/rotas_do_app.py`.
    """
    encontradas = []
    for route in rotas_com_caminho():
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


def test_a_auditoria_enxerga_rotas_admin():
    """A trava que faltava, e que custou a auditoria ficar DESLIGADA em producao.

    `test_cada_rota_admin_declara_o_papel_esperado` e parametrizado sobre
    `rotas_admin()`. Lista vazia nao falha: o pytest gera zero casos, o
    arquivo fica verde e a conferencia de papel de TODA rota /admin
    simplesmente deixa de acontecer.

    Foi o que aconteceu quando o starlette 1.6 entrou em producao por um
    `--build` com o `requirements.txt` sem pino: `app.routes` parou de
    conter as rotas, `rotas_admin()` passou a devolver zero, e nada aqui
    ficou vermelho por causa DISSO — dois testes irmaos cairam por
    compararem com conjuntos nao vazios, e foram eles que denunciaram.

    O numero e um PISO generoso, nao a contagem exata: subir para o total de
    hoje faria este teste falhar a cada rota nova, que e ruido, e o que ele
    precisa provar e "a descoberta funcionou", nao "sao exatamente N".
    """
    encontradas = rotas_admin()

    assert len(encontradas) > 40, (
        f"a auditoria encontrou apenas {len(encontradas)} rotas /admin. Com "
        f"lista vazia (ou quase) o teste parametrizado gera zero casos e a "
        f"conferencia de papel some SEM ficar vermelha. Provavel causa: o "
        f"jeito de listar as rotas do `app` mudou de novo — ver "
        f"`tests/rotas_do_app.py`."
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


class TestQuemDefineCashbackNaFormaDePagamento:
    """`ensure_pode_definir_cashback`, a terceira excecao a `exigir_papel`.

    A rota (`POST`/`PATCH` de forma de pagamento) declara GERENCIA, e assim
    fica: cadastrar bandeira, rotulo, icone e ordem e trabalho de quem toca a
    loja. Quem decide este campo e o CORPO, como em
    `ensure_pode_definir_preco`.

    O que ela fecha: sem esta regra, o dono definiria o percentual do
    cashback (as rotas de `cashback-rules` sao SOMENTE_DONO) e o gerente
    escolheria em quais formas de pagamento ele sai — meia decisao de cada
    lado da mesma campanha.
    """

    def test_o_dono_define(self):
        ensure_pode_definir_cashback(SimpleNamespace(role="owner"))

    def test_o_gerente_nao_define(self):
        with pytest.raises(HTTPException) as erro:
            ensure_pode_definir_cashback(SimpleNamespace(role="manager"))

        assert erro.value.status_code == 403

    def test_o_atendente_nao_define(self):
        with pytest.raises(HTTPException) as erro:
            ensure_pode_definir_cashback(SimpleNamespace(role="attendant"))

        assert erro.value.status_code == 403
