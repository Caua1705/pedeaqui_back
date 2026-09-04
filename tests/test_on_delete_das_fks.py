"""O varredor de `ON DELETE` enxerga — sem Postgres, com FKs plantadas.

O zero de `test_on_delete_das_fks_db.py` só vale se este arquivo passar. Um
varredor rodado apenas contra o schema que já está certo não tem como provar
que ele acusaria o schema errado: `auditar` devolveria `[]` tanto por acerto
quanto por regex quebrado, por `contype` trocado, ou por um `if` invertido.

As quatro provas, e a terceira é a que dá o nome ao portão:

1. **`CASCADE` fora da lista é acusado** — a tabela nova que ninguém conferiu;
2. **`SET NULL` fora da lista é acusado** — a metade silenciosa que não some
   com linha nenhuma, só esvazia uma coluna;
3. **barreira derrubada cai aqui sozinha** — uma FK que era `NO ACTION` e
   virou `CASCADE` não está declarada (ela nunca precisou estar), então ela
   aparece pelo mesmo caminho da 1. É por isso que não existe uma segunda
   lista de barreiras para alguém esquecer de atualizar;
4. **`NO ACTION` e `RESTRICT` passam** — se eles fossem acusados, o portão
   pediria declaração de 41 FKs que não fazem nada sozinhas, e ninguém leria
   a lista.

E as duas do outro sentido, para a lista não virar cemitério: entrada que não
existe mais no banco, e entrada que deixou de ser destrutiva.
"""

import pytest

from scripts.on_delete_das_fks import Achado, Fk, auditar_fks, fechamento


def fk(nome, filho, pai, regra, colunas="pai_id"):
    return Fk(nome=nome, filho=filho, pai=pai, regra=regra, colunas=colunas)


def tipos(achados: list[Achado]) -> list[str]:
    return sorted(a.tipo for a in achados)


class TestOQueEleAcusa:
    def test_cascade_fora_da_lista_e_acusado(self):
        achados = auditar_fks([fk("fk_nova", "tabela_nova", "restaurants", "CASCADE")], {})

        assert tipos(achados) == ["destrutiva nao declarada"]
        assert "fk_nova" == achados[0].constraint

    def test_set_null_fora_da_lista_e_acusado(self):
        """A metade que não some com linha nenhuma. `cashback_transactions.
        restaurant_id` é dessa família: a linha fica, some do saldo gastável
        e continua somando no acumulado."""
        achados = auditar_fks([fk("fk_anula", "cashback_transactions", "restaurants", "SET NULL")], {})

        assert tipos(achados) == ["destrutiva nao declarada"]

    def test_barreira_derrubada_aparece_pelo_mesmo_caminho(self):
        """`order_items.product_id` é `NO ACTION` de propósito — é o histórico
        que o cliente ainda consulta (armadilha 13). Trocá-la por `CASCADE`
        parece arrumação: o produto está sendo apagado de qualquer jeito.

        Ela não está em `DESTRUTIVAS` e não tem por que estar; a troca a
        empurra para dentro do conjunto que a lista cobra, e ela cai como
        'não declarada'. **Um mecanismo, os dois estragos.**"""
        antes = auditar_fks([fk("order_items_product_id_fkey", "order_items", "products", "NO ACTION")], {})
        depois = auditar_fks([fk("order_items_product_id_fkey", "order_items", "products", "CASCADE")], {})

        assert antes == []
        assert tipos(depois) == ["destrutiva nao declarada"]

    def test_a_mensagem_avisa_que_pode_ser_uma_barreira_caindo(self):
        """O achado é o mesmo nos dois casos, e quem lê precisa saber que a
        pergunta não é só 'declare esta linha'."""
        achados = auditar_fks([fk("fk_x", "order_items", "products", "CASCADE")], {})

        assert "a barreira caiu" in achados[0].detalhe


class TestOQueEleDeixaPassar:
    @pytest.mark.parametrize("regra", ["NO ACTION", "RESTRICT"])
    def test_o_que_barra_nao_precisa_ser_declarado(self, regra):
        """São 41 FKs, e nenhuma delas age sozinha: o `DELETE` do pai falha,
        alto, na cara de quem apagou. Cobrar declaração delas encheria a lista
        com o caso que não custa nada."""
        achados = auditar_fks([fk("fk_barra", "orders", "customers", regra)], {})

        assert achados == []

    def test_destrutiva_declarada_passa(self):
        achados = auditar_fks(
            [fk("fk_declarada", "order_items", "orders", "CASCADE")],
            {"fk_declarada": "o item E o pedido"},
        )

        assert achados == []


class TestAListaNaoViraCemiterio:
    def test_entrada_que_nao_existe_mais_no_banco_e_acusada(self):
        achados = auditar_fks([], {"fk_que_sumiu": "motivo velho"})

        assert tipos(achados) == ["lista velha"]

    def test_entrada_que_deixou_de_ser_destrutiva_e_acusada(self):
        """Alguém trocou um `CASCADE` por `NO ACTION` — o sentido certo, e
        mesmo assim a lista precisa saber, senão ela descreve um banco que não
        existe mais."""
        achados = auditar_fks(
            [fk("fk_virou_barreira", "t", "restaurants", "NO ACTION")],
            {"fk_virou_barreira": "motivo de quando ela apagava"},
        )

        assert tipos(achados) == ["lista velha"]
        assert "NO ACTION" in achados[0].detalhe


class TestOFechamento:
    """O fecho transitivo é o que a tabela de contagens não responde."""

    CADEIA = [
        fk("a", "categories", "restaurants", "CASCADE"),
        fk("b", "products", "categories", "CASCADE"),
        fk("c", "product_option_groups", "products", "CASCADE"),
        fk("d", "order_items", "products", "NO ACTION"),
        fk("e", "orders", "restaurants", "NO ACTION"),
        fk("f", "cashback_transactions", "restaurants", "SET NULL"),
    ]

    def test_a_cascata_vai_ate_o_terceiro_degrau(self):
        """Apagar o restaurante apaga a categoria, que apaga o produto, que
        apaga o grupo de adicionais. É o terceiro degrau que surpreende — e é
        o que some numa leitura FK a FK."""
        resultado = fechamento(self.CADEIA, "restaurants")

        assert resultado["apagadas"] == ["categories", "product_option_groups", "products"]

    def test_o_que_anula_coluna_sai_separado_do_que_apaga(self):
        resultado = fechamento(self.CADEIA, "restaurants")

        assert [str(item) for item in resultado["anuladas"]] == [
            "cashback_transactions.pai_id -> restaurants"
        ]

    def test_barreira_alcancada_pela_cascata_e_listada(self):
        """`order_items.product_id` aponta para `products`, que está dentro do
        fecho. É ela que faz o `DELETE` do restaurante falhar, e ela não
        aparece olhando só as FKs que saem de `restaurants`."""
        resultado = fechamento(self.CADEIA, "restaurants")

        assert "order_items.pai_id -> products" in [str(item) for item in resultado["barreiras"]]

    def test_barreira_cujo_filho_ja_esta_no_fecho_nao_barra(self):
        """Falso positivo caro: o achado pede uma mudança de regra, e mudar a
        regra errada não dá erro no dia em que roda.

        `products.branch_id -> branches` é `NO ACTION`, e `products` está
        dentro do fecho de `restaurants` (por `categories`). A linha que
        dispararia a recusa está sendo apagada na mesma instrução — não há
        bloqueio, e listá-la mandaria alguém 'consertar' uma FK que já
        funciona."""
        cadeia = self.CADEIA + [
            fk("g", "branches", "restaurants", "CASCADE"),
            fk("h", "products", "branches", "NO ACTION", colunas="branch_id"),
        ]

        resultado = fechamento(cadeia, "restaurants")

        assert "products" in resultado["apagadas"]
        assert "products.branch_id -> branches" not in [str(i) for i in resultado["barreiras"]]

    def test_ciclo_nao_trava_a_varredura(self):
        """`products -> categories` e `categories -> products` não existem
        juntas hoje, mas um fecho que não marque visitado roda para sempre, e
        o portão não pode depender de o schema continuar sendo uma árvore."""
        ciclo = [
            fk("i", "b", "a", "CASCADE"),
            fk("j", "a", "b", "CASCADE"),
        ]

        assert fechamento(ciclo, "a")["apagadas"] == ["b"]
