"""`OrderRepository` contra o Postgres.

Testes de caracterização: descrevem o que a consulta faz **hoje**. Uma falha
aqui é bug encontrado, não teste errado.

Três coisas concentram os testes, porque são as três que fake em memória não
alcança e que custam caro quando saem erradas:

- o WHERE compartilhado pela página, pelo total e pelos contadores de badge —
  divergindo, o painel mostra 12 pedidos e diz que são 40;
- o `end_at` exclusivo, que é o que não perde o pedido feito às 23:59:59.7;
- `billable` e `excluded` serem complemento exato um do outro, que é o que
  permite ler o relatório de comissão ao lado do de cancelamentos.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from tests.fabricas_db import (
    criar_cliente,
    criar_filial,
    criar_pedido,
    criar_restaurante,
    instante,
)


pytestmark = pytest.mark.db


class TestABuscaDoPainel:
    """O lojista digita número de pedido e nome de cliente no mesmo campo. Só
    dá para saber qual é olhando o que foi digitado."""

    def test_so_digitos_casa_o_numero_do_pedido_exatamente(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        alvo = criar_pedido(db, restaurante, filial, order_number=987654)
        criar_pedido(db, restaurante, filial, order_number=987655)

        encontrados = OrderRepository(db).list_orders_by_restaurant(
            restaurante.id,
            search="987654",
        )

        assert [pedido.id for pedido in encontrados] == [alvo.id]

    def test_numero_parecido_nao_casa_por_prefixo(self, db):
        """É comparação de igualdade, não ILIKE: `order_number` é BigInteger e
        um ILIKE ali forçaria cast da coluna inteira, perdendo o índice."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        criar_pedido(db, restaurante, filial, order_number=987654)

        encontrados = OrderRepository(db).list_orders_by_restaurant(restaurante.id, search="98765")

        assert encontrados == []

    def test_texto_casa_o_nome_do_cliente_sem_diferenciar_maiuscula(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        joana = criar_pedido(db, restaurante, filial, customer_name_snapshot="Dona Joana")
        criar_pedido(db, restaurante, filial, customer_name_snapshot="Seu Pedro")

        encontrados = OrderRepository(db).list_orders_by_restaurant(restaurante.id, search="joana")

        assert [pedido.id for pedido in encontrados] == [joana.id]

    def test_o_porcento_digitado_nao_lista_a_base_inteira(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        criar_pedido(db, restaurante, filial, customer_name_snapshot="Joana")
        criar_pedido(db, restaurante, filial, customer_name_snapshot="Pedro")

        assert OrderRepository(db).list_orders_by_restaurant(restaurante.id, search="%") == []

    def test_o_underline_digitado_nao_casa_com_qualquer_letra(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        com_underline = criar_pedido(db, restaurante, filial, customer_name_snapshot="Ana_")
        criar_pedido(db, restaurante, filial, customer_name_snapshot="Anab")

        encontrados = OrderRepository(db).list_orders_by_restaurant(restaurante.id, search="Ana_")

        assert [pedido.id for pedido in encontrados] == [com_underline.id]


class TestOsFiltrosDaListagem:
    def test_a_pagina_o_total_e_os_badges_concordam(self, db):
        """As três consultas dividem `_admin_list_conditions` justamente para
        isto. O teste passa o MESMO filtro nas três e confere que os números
        fecham."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        for _ in range(3):
            criar_pedido(db, restaurante, filial, status="pending")
        for _ in range(2):
            criar_pedido(db, restaurante, filial, status="accepted")

        repositorio = OrderRepository(db)
        pagina = repositorio.list_orders_by_restaurant(restaurante.id, limit=2)
        total = repositorio.count_orders_by_restaurant(restaurante.id)
        badges = repositorio.count_orders_grouped_by_status(restaurante.id)

        assert len(pagina) == 2
        assert total == 5
        assert badges == {"pending": 3, "accepted": 2}
        assert sum(badges.values()) == total

    def test_os_badges_ignoram_o_filtro_de_status(self, db):
        """`status=None` é passado de propósito lá dentro: filtrar por status
        aqui zeraria todos os outros contadores, e a tela mostraria badge de
        um status só."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        criar_pedido(db, restaurante, filial, status="pending")
        criar_pedido(db, restaurante, filial, status="accepted")

        badges = OrderRepository(db).count_orders_grouped_by_status(restaurante.id)

        assert badges == {"pending": 1, "accepted": 1}

    def test_o_fim_do_periodo_e_exclusivo_e_o_inicio_nao(self, db):
        """O par que define a janela. O service passa o instante em que o dia
        SEGUINTE começa, e é isso que não perde o pedido das 23:59:59.7."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        comeco = instante(2026, 8, 1, 0, 0)
        fim = instante(2026, 8, 2, 0, 0)

        criar_pedido(db, restaurante, filial, created_at=comeco)
        criar_pedido(db, restaurante, filial, created_at=fim - timedelta(milliseconds=300))
        criar_pedido(db, restaurante, filial, created_at=fim)
        criar_pedido(db, restaurante, filial, created_at=comeco - timedelta(seconds=1))

        total = OrderRepository(db).count_orders_by_restaurant(
            restaurante.id,
            start_at=comeco,
            end_at=fim,
        )

        assert total == 2

    def test_o_filtro_de_status_vale_na_pagina_e_no_total(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pendente = criar_pedido(db, restaurante, filial, status="pending")
        criar_pedido(db, restaurante, filial, status="accepted")

        repositorio = OrderRepository(db)
        encontrados = repositorio.list_orders_by_restaurant(restaurante.id, status="pending")

        assert [pedido.id for pedido in encontrados] == [pendente.id]
        assert repositorio.count_orders_by_restaurant(restaurante.id, status="pending") == 1

    def test_o_filtro_de_filial_separa_as_lojas(self, db):
        restaurante = criar_restaurante(db)
        centro = criar_filial(db, restaurante, nome="Centro")
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        do_centro = criar_pedido(db, restaurante, centro)
        criar_pedido(db, restaurante, aldeota)

        repositorio = OrderRepository(db)
        encontrados = repositorio.list_orders_by_restaurant(restaurante.id, branch_id=centro.id)

        assert [pedido.id for pedido in encontrados] == [do_centro.id]
        assert repositorio.count_orders_by_restaurant(restaurante.id, branch_id=centro.id) == 1
        assert repositorio.count_orders_by_restaurant(restaurante.id) == 2

    def test_pedido_de_outro_restaurante_nao_aparece(self, db):
        meu = criar_restaurante(db, nome="O Meu")
        minha_filial = criar_filial(db, meu)
        meu_pedido = criar_pedido(db, meu, minha_filial)

        vizinho = criar_restaurante(db, nome="O Vizinho")
        filial_do_vizinho = criar_filial(db, vizinho)
        criar_pedido(db, vizinho, filial_do_vizinho)

        repositorio = OrderRepository(db)

        assert [p.id for p in repositorio.list_orders_by_restaurant(meu.id)] == [meu_pedido.id]
        assert repositorio.count_orders_by_restaurant(meu.id) == 1

    def test_a_listagem_vem_do_mais_novo_para_o_mais_velho(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        velho = criar_pedido(db, restaurante, filial, created_at=instante(2026, 8, 1, 8))
        novo = criar_pedido(db, restaurante, filial, created_at=instante(2026, 8, 1, 10))
        meio = criar_pedido(db, restaurante, filial, created_at=instante(2026, 8, 1, 9))

        encontrados = OrderRepository(db).list_orders_by_restaurant(restaurante.id)

        assert [pedido.id for pedido in encontrados] == [novo.id, meio.id, velho.id]


class TestOStreamDoPainel:
    def test_o_cursor_e_estrito_e_a_ordem_e_crescente(self, db):
        """`created_at > since`, não `>=`: o cursor avança para o `created_at`
        do último evento emitido, e com `>=` esse mesmo pedido sairia de novo a
        cada reconexão. A ordem é crescente ao contrário da listagem, porque o
        stream entrega na ordem em que aconteceu."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        cursor = instante(2026, 8, 1, 9)

        criar_pedido(db, restaurante, filial, created_at=cursor)
        primeiro = criar_pedido(db, restaurante, filial, created_at=cursor + timedelta(minutes=1))
        segundo = criar_pedido(db, restaurante, filial, created_at=cursor + timedelta(minutes=2))

        encontrados = OrderRepository(db).list_orders_created_since(
            restaurante.id,
            branch_id=None,
            since=cursor,
            limit=10,
        )

        assert [pedido.id for pedido in encontrados] == [primeiro.id, segundo.id]

    def test_o_stream_de_uma_filial_nao_ve_a_outra(self, db):
        restaurante = criar_restaurante(db)
        centro = criar_filial(db, restaurante, nome="Centro")
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        cursor = instante(2026, 8, 1, 9)
        do_centro = criar_pedido(db, restaurante, centro, created_at=cursor + timedelta(minutes=1))
        criar_pedido(db, restaurante, aldeota, created_at=cursor + timedelta(minutes=2))

        encontrados = OrderRepository(db).list_orders_created_since(
            restaurante.id,
            branch_id=centro.id,
            since=cursor,
            limit=10,
        )

        assert [pedido.id for pedido in encontrados] == [do_centro.id]


class TestOStreamDeMudancaDeStatus:
    """Lê `order_status_history`, e não `orders.updated_at`: updated_at muda
    por qualquer escrita (confirmação de pagamento, por exemplo) e não diz para
    qual status o pedido foi."""

    def test_a_juncao_com_orders_e_o_que_separa_os_restaurantes(self, db):
        """`order_status_history` não carrega `restaurant_id` próprio. Sem a
        junção, o stream de um lojista veria a mudança de status de outro."""
        cursor = instante(2026, 8, 1, 9)

        meu = criar_restaurante(db, nome="O Meu")
        minha_filial = criar_filial(db, meu)
        meu_pedido = criar_pedido(db, meu, minha_filial)
        minha_mudanca = self._registrar_mudanca(
            db, meu_pedido, "accepted", cursor + timedelta(minutes=1)
        )

        vizinho = criar_restaurante(db, nome="O Vizinho")
        filial_do_vizinho = criar_filial(db, vizinho)
        pedido_do_vizinho = criar_pedido(db, vizinho, filial_do_vizinho)
        self._registrar_mudanca(db, pedido_do_vizinho, "accepted", cursor + timedelta(minutes=2))

        encontrados = OrderRepository(db).list_status_changes_since(
            meu.id,
            branch_id=None,
            since=cursor,
            limit=10,
        )

        assert [(historico.id, pedido.id) for historico, pedido in encontrados] == [
            (minha_mudanca.id, meu_pedido.id)
        ]

    def test_o_cursor_e_estrito_e_a_ordem_e_crescente(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial)
        cursor = instante(2026, 8, 1, 9)

        self._registrar_mudanca(db, pedido, "no-cursor", cursor)
        primeira = self._registrar_mudanca(db, pedido, "accepted", cursor + timedelta(minutes=1))
        segunda = self._registrar_mudanca(db, pedido, "preparing", cursor + timedelta(minutes=2))

        encontrados = OrderRepository(db).list_status_changes_since(
            restaurante.id,
            branch_id=None,
            since=cursor,
            limit=10,
        )

        assert [historico.id for historico, _ in encontrados] == [primeira.id, segunda.id]

    def test_o_filtro_de_filial_vale_pela_filial_do_pedido(self, db):
        restaurante = criar_restaurante(db)
        centro = criar_filial(db, restaurante, nome="Centro")
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        cursor = instante(2026, 8, 1, 9)

        pedido_do_centro = criar_pedido(db, restaurante, centro)
        do_centro = self._registrar_mudanca(
            db, pedido_do_centro, "accepted", cursor + timedelta(minutes=1)
        )
        pedido_da_aldeota = criar_pedido(db, restaurante, aldeota)
        self._registrar_mudanca(
            db, pedido_da_aldeota, "accepted", cursor + timedelta(minutes=2)
        )

        encontrados = OrderRepository(db).list_status_changes_since(
            restaurante.id,
            branch_id=centro.id,
            since=cursor,
            limit=10,
        )

        assert [historico.id for historico, _ in encontrados] == [do_centro.id]

    @staticmethod
    def _registrar_mudanca(db, pedido, status, criado_em):
        historico = OrderStatusHistory(order_id=pedido.id, status=status)
        historico.created_at = criado_em
        db.add(historico)
        db.flush()
        return historico


class TestAsEscritasDeStatus:
    def test_update_status_grava_o_novo_status(self, db):
        """O repositório só escreve o campo. Quem decide se a transição é
        permitida é a máquina de estados, no service — e é por isso que este
        teste consegue ir de `completed` para `pending` sem erro nenhum."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial, status="pending")

        OrderRepository(db).update_status(pedido, "accepted")

        assert pedido.status == "accepted"

    def test_update_payment_status_so_grava_paid_at_quando_ele_vem(self, db):
        """`paid_at=None` é "não mexa", e não "apague". Sem isso, uma
        atualização posterior de status apagaria a hora em que o dinheiro
        entrou."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial, payment_status="pending")
        pago_em = instante(2026, 8, 1, 12)

        repositorio = OrderRepository(db)
        repositorio.update_payment_status(pedido, "paid", paid_at=pago_em)
        assert pedido.paid_at == pago_em

        repositorio.update_payment_status(pedido, "refunded")
        assert pedido.payment_status == "refunded"
        assert pedido.paid_at == pago_em


class TestOEscopoDosDetalhes:
    def test_o_detalhe_do_painel_exige_o_restaurante_certo(self, db):
        """Enquanto o filtro era só por `Order.id`, qualquer lojista com um
        UUID de pedido em mãos lia nome, telefone e endereço do cliente de
        outro restaurante."""
        vizinho = criar_restaurante(db, nome="O Vizinho")
        filial = criar_filial(db, vizinho)
        pedido = criar_pedido(db, vizinho, filial)
        meu = criar_restaurante(db, nome="O Meu")

        repositorio = OrderRepository(db)

        assert repositorio.get_order_detail(pedido.id, meu.id) is None
        assert repositorio.get_order_detail(pedido.id, vizinho.id).id == pedido.id

    def test_o_detalhe_do_cliente_exige_o_cliente_certo(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        dono = criar_cliente(db)
        intruso = criar_cliente(db)
        pedido = criar_pedido(db, restaurante, filial, cliente=dono)

        repositorio = OrderRepository(db)

        assert repositorio.get_order_detail_for_customer(pedido.id, intruso.id) is None
        assert repositorio.get_order_detail_for_customer(pedido.id, dono.id).id == pedido.id

    def test_o_token_de_acompanhamento_vale_so_no_restaurante_dele(self, db):
        # O token em claro é passado à fábrica porque o banco não o guarda
        # mais: `orders` só tem o hash desde a revisão 0016. É o próprio
        # ponto — ler a linha não devolve a credencial.
        vizinho = criar_restaurante(db, nome="O Vizinho")
        filial = criar_filial(db, vizinho)
        pedido = criar_pedido(db, vizinho, filial, tracking_token="o-token-em-claro")
        meu = criar_restaurante(db, nome="O Meu")

        repositorio = OrderRepository(db)

        assert repositorio.get_order_by_tracking_token(meu.id, "o-token-em-claro") is None
        assert (
            repositorio.get_order_by_tracking_token(vizinho.id, "o-token-em-claro").id
            == pedido.id
        )

    def test_o_historico_do_cliente_traz_o_nome_do_restaurante_e_da_filial(self, db):
        """São duas junções INNER: pedido sem filial ou sem restaurante
        simplesmente não apareceria no histórico. Hoje as duas colunas são NOT
        NULL, então isso não acontece — e é por isso que o INNER é seguro."""
        restaurante = criar_restaurante(db, nome="Junior da Picanha")
        filial = criar_filial(db, restaurante, nome="Centro")
        cliente = criar_cliente(db)
        pedido = criar_pedido(db, restaurante, filial, cliente=cliente)

        historico = OrderRepository(db).list_orders_by_customer(cliente.id)

        assert historico == [(pedido, "Junior da Picanha", "Centro")]

    def test_o_historico_nao_traz_pedido_de_convidado(self, db):
        """Pedido sem `customer_id` (convidado) não pertence a ninguém no
        histórico — quem o acompanha é o `tracking_token`."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        cliente = criar_cliente(db)
        criar_pedido(db, restaurante, filial, cliente=None)

        assert OrderRepository(db).list_orders_by_customer(cliente.id) == []


class TestAComissao:
    """`billable` e `excluded` precisam ser complemento exato um do outro no
    mesmo período. Se um pedido cair nos dois — ou em nenhum — o lojista
    compara os dois relatórios e a conta não fecha."""

    def test_cancelado_recusado_e_estornado_ficam_de_fora_do_extrato(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        comeco = instante(2026, 8, 1, 0)
        fim = instante(2026, 8, 2, 0)
        momento = instante(2026, 8, 1, 12)

        vendido = criar_pedido(db, restaurante, filial, status="completed", created_at=momento)
        criar_pedido(db, restaurante, filial, status="cancelled", created_at=momento)
        criar_pedido(db, restaurante, filial, status="rejected", created_at=momento)
        criar_pedido(
            db,
            restaurante,
            filial,
            status="completed",
            payment_status="refunded",
            created_at=momento,
        )

        repositorio = OrderRepository(db)
        faturados = repositorio.list_orders_for_commission(restaurante.id, comeco, fim)

        assert [pedido.id for pedido in faturados] == [vendido.id]
        assert repositorio.count_excluded_from_commission(restaurante.id, comeco, fim) == 3

    def test_os_dois_conjuntos_cobrem_o_periodo_inteiro_sem_sobrepor(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        comeco = instante(2026, 8, 1, 0)
        fim = instante(2026, 8, 2, 0)
        momento = instante(2026, 8, 1, 12)

        for status in ("pending", "accepted", "completed", "cancelled", "rejected"):
            criar_pedido(db, restaurante, filial, status=status, created_at=momento)
        criar_pedido(
            db,
            restaurante,
            filial,
            status="completed",
            payment_status="refunded",
            created_at=momento,
        )

        repositorio = OrderRepository(db)
        faturados = repositorio.list_orders_for_commission(restaurante.id, comeco, fim)
        excluidos = repositorio.count_excluded_from_commission(restaurante.id, comeco, fim)

        assert len(faturados) + excluidos == 6

    def test_o_extrato_vem_do_mais_velho_para_o_mais_novo(self, db):
        """Ordem crescente, ao contrário da listagem do painel: extrato se lê
        de cima para baixo na ordem em que o dinheiro entrou."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        comeco = instante(2026, 8, 1, 0)
        fim = instante(2026, 8, 2, 0)

        tarde = criar_pedido(
            db, restaurante, filial, status="completed", created_at=instante(2026, 8, 1, 18)
        )
        manha = criar_pedido(
            db, restaurante, filial, status="completed", created_at=instante(2026, 8, 1, 9)
        )

        faturados = OrderRepository(db).list_orders_for_commission(restaurante.id, comeco, fim)

        assert [pedido.id for pedido in faturados] == [manha.id, tarde.id]

    def test_o_extrato_de_um_restaurante_nao_alcanca_o_outro(self, db):
        comeco = instante(2026, 8, 1, 0)
        fim = instante(2026, 8, 2, 0)
        momento = instante(2026, 8, 1, 12)

        meu = criar_restaurante(db, nome="O Meu")
        minha_filial = criar_filial(db, meu)
        meu_pedido = criar_pedido(db, meu, minha_filial, status="completed", created_at=momento)

        vizinho = criar_restaurante(db, nome="O Vizinho")
        filial_do_vizinho = criar_filial(db, vizinho)
        criar_pedido(db, vizinho, filial_do_vizinho, status="completed", created_at=momento)

        faturados = OrderRepository(db).list_orders_for_commission(meu.id, comeco, fim)

        assert [pedido.id for pedido in faturados] == [meu_pedido.id]


class TestOPagamento:
    def test_o_webhook_encontra_o_pedido_pelo_par_provider_e_id(self, db):
        """Sem filtro por restaurante de propósito: o webhook chega do
        gateway, não de um tenant."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial)

        repositorio = OrderRepository(db)
        repositorio.attach_payment_intent(
            pedido,
            provider="sandbox",
            provider_payment_id="pag-123",
        )

        encontrado = repositorio.get_order_by_provider_payment("sandbox", "pag-123")

        assert encontrado.id == pedido.id

    def test_o_mesmo_id_em_outro_provider_nao_casa(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial)

        repositorio = OrderRepository(db)
        repositorio.attach_payment_intent(
            pedido,
            provider="sandbox",
            provider_payment_id="pag-123",
        )

        assert repositorio.get_order_by_provider_payment("mercadopago", "pag-123") is None

    def test_anexar_a_cobranca_volta_o_pagamento_para_pending(self, db):
        """Uma nova tentativa depois de uma recusa reabre o pagamento. É o que
        permite ao pedido recusado ser pago numa segunda tentativa."""
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)
        pedido = criar_pedido(db, restaurante, filial, payment_status="failed")

        OrderRepository(db).attach_payment_intent(
            pedido,
            provider="sandbox",
            provider_payment_id="pag-nova-tentativa",
        )

        assert pedido.payment_status == "pending"


class TestOOrderNumber:
    def test_a_sequence_preenche_o_numero_sozinha(self, db):
        """O default vem da sequence do banco, não do ORM — é exatamente o que
        `Base.metadata.create_all()` teria perdido (armadilha 24). Sem ela o
        INSERT falharia com `order_number` nulo.

        O teste não olha o valor: a sequence é global e imune a rollback, então
        o número do próximo pedido depende de tudo o que já rodou antes.
        """
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)

        pedido = criar_pedido(db, restaurante, filial, total=Decimal("42.00"))
        db.refresh(pedido)

        assert pedido.order_number > 0
