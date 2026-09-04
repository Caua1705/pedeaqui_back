"""O schema real: toda FK que apaga ou anula sozinha tem dono escrito.

O levantamento da seção 7 de `docs/modelo-de-dados.md` contou 84 FKs em
05/09/2026 e o número ficou num `.md`. Este arquivo é quem o cobra.

**O zero daqui só vale com `test_on_delete_das_fks.py` verde ao lado**: lá é
que se prova que o varredor acusa o que precisa acusar. Aqui ele poderia ficar
verde por não estar vendo FK nenhuma — uma query com o `contype` errado devolve
zero linhas, e zero linhas dão zero achados. Por isso o segundo teste desta
classe conta.

A releitura das contagens não é redundância com o `.md`: é o contrário. O `.md`
descreve o schema de 05/09; este teste falha no dia em que ele deixar de
descrever, que é justamente o dia em que ninguém releria o `.md`.
"""

import pytest

from scripts.on_delete_das_fks import (
    DESTRUTIVAS,
    RAIZES,
    _fks,
    auditar,
    fechamento,
)
from tests.conftest import url_do_banco_de_teste


@pytest.mark.db
class TestOBancoDeVerdade:
    def test_toda_fk_destrutiva_esta_declarada(self, db):
        # A fixture `db` não é usada para consultar: ela existe para o schema
        # já estar em `head` quando o varredor abrir a própria conexão.
        achados = auditar(url_do_banco_de_teste())

        assert [f"[{a.tipo}] {a}" for a in achados] == []

    def test_o_varredor_esta_de_fato_olhando_para_FKs(self, db):
        """O verde por ausência é indistinguível do verde por acerto.

        As quatro regras precisam aparecer: se uma sumir da contagem, ou o
        `confdeltype` foi lido errado, ou o schema mudou de um jeito que
        merece leitura humana."""
        fks = _fks(url_do_banco_de_teste())
        regras = {fk.regra for fk in fks}

        assert len(fks) >= 80
        assert regras == {"NO ACTION", "CASCADE", "SET NULL", "RESTRICT"}

    def test_as_contagens_do_levantamento_continuam_valendo(self, db):
        """39/35/8/2, medidos em 05/09/2026 e escritos em
        `docs/modelo-de-dados.md`. Vermelho aqui não é defeito — é uma FK nova,
        e o pedido para reler aquela seção antes de fechar o commit."""
        fks = _fks(url_do_banco_de_teste())
        contagem = {regra: sum(1 for fk in fks if fk.regra == regra) for regra in
                    ("NO ACTION", "CASCADE", "SET NULL", "RESTRICT")}

        assert contagem == {"NO ACTION": 39, "CASCADE": 35, "SET NULL": 8, "RESTRICT": 2}
        assert len(DESTRUTIVAS) == 43

    def test_as_barreiras_do_historico_continuam_barrando(self, db):
        """As cinco que a seção 7 nomeia como deliberadas.

        Elas não estão em `DESTRUTIVAS` e não devem estar — o portão as cobre
        pelo complemento. Este teste é o outro lado: ele nomeia as cinco, para
        que derrubar uma delas falhe **dizendo qual**, e não só 'apareceu uma
        destrutiva nova'.

        `whatsapp_messages.channel_id` é a mais fácil de derrubar por
        arrumação: o canal está sendo apagado de qualquer jeito, e o registro
        de que o cliente foi avisado é o único lugar onde isso é visível
        depois."""
        por_nome = {fk.nome: fk for fk in _fks(url_do_banco_de_teste())}

        barreiras = (
            "whatsapp_messages_channel_id_fkey",
            "order_items_product_id_fkey",
            "order_item_options_option_id_fkey",
            "order_item_options_option_group_id_fkey",
            "orders_customer_id_fkey",
        )
        assert {nome: por_nome[nome].regra for nome in barreiras} == {
            nome: "NO ACTION" for nome in barreiras
        }

    def test_o_delete_do_cliente_esbarra_no_pedido(self, db):
        """Exclusão de conta neste sistema é ANONIMIZAÇÃO, não `DELETE`
        (`customer_anonymization_service`). Esta FK é o que impede alguém de
        trocar um mecanismo pelo outro sem perceber — e a razão de ela
        importar está no fecho: `cashback_transactions` está entre as sete
        tabelas que sairiam junto, e ela guarda dinheiro do cliente."""
        fks = _fks(url_do_banco_de_teste())
        resultado = fechamento(fks, "customers")

        assert "cashback_transactions" in resultado["apagadas"]
        assert "orders.customer_id -> customers" in [str(i) for i in resultado["barreiras"]]

    def test_o_fecho_de_toda_raiz_e_calculavel(self, db):
        """Um ciclo de `CASCADE` no schema travaria o relatório. Nenhuma das
        quatro raízes pode deixar de responder."""
        fks = _fks(url_do_banco_de_teste())

        for raiz in RAIZES:
            resultado = fechamento(fks, raiz)
            assert raiz not in resultado["apagadas"]
            assert resultado["apagadas"] or resultado["anuladas"]
