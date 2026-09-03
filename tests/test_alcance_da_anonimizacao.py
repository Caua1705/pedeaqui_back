"""Nenhuma tabela com dado de pessoa fica fora da exclusao de conta.

A armadilha 38 cobre a tabela que NAO pende de `customers` — ali a retencao e
o mecanismo de exclusao, e por isso a tabela nasce com prazo. Este arquivo
cobra a outra metade: a tabela que PENDE e que
`CustomerAnonymizationService` precisa alcancar.

O caso que criou o varredor esta no docstring dele
(`scripts/alcance_da_anonimizacao.py`): `customer_social_identities` guarda o
`sub` do Google, que e o identificador estavel da pessoa naquele sistema. Sem
o passo novo, excluir a conta deixaria o ponteiro de pe — e, pior, quem
excluisse e voltasse pelo Google seria logado numa conta `is_active=False`,
403 para sempre e sem como se recadastrar.

Os tres testes de baixo nao sao decoracao. O modo de falha real deste tipo de
portao nao e o teste de cima ficar vermelho: e o VARREDOR parar de enxergar, e
o teste de cima ficar verde por vacuidade — que e indistinguivel de verde por
acerto (a mesma familia do dublê de dado e do teste mudo).
"""

import unittest

from scripts.alcance_da_anonimizacao import (
    ALCANCE,
    Alcancada,
    Fora,
    Indireta,
    passos_do_service,
    tabelas_que_pendem_da_pessoa,
    varrer,
)


class TestOAlcanceDaAnonimizacao(unittest.TestCase):
    def test_nenhuma_tabela_com_customer_id_fica_fora(self) -> None:
        achados = varrer()
        self.assertEqual(
            achados,
            [],
            "\n".join(f"{a.tabela}: {a.problema}" for a in achados),
        )


class TestOVarredorEnxerga(unittest.TestCase):
    """Ele acha as tabelas e os passos de verdade, e nao conjuntos vazios."""

    def test_acha_as_tabelas_com_customer_id(self) -> None:
        tabelas = tabelas_que_pendem_da_pessoa()
        # `customers` e o alvo; `orders` e a que guarda os snapshots da
        # pessoa. Se estas duas nao aparecerem, o `Base.metadata` nao foi
        # populado e o portao inteiro esta vazio.
        self.assertIn("customers", tabelas)
        self.assertIn("orders", tabelas)

    def test_acha_os_passos_do_service(self) -> None:
        passos = passos_do_service()
        self.assertIn("anonymize", passos)
        self.assertIn("_anonymize_customer", passos)

    def test_toda_entrada_declarada_e_de_um_dos_tres_tipos(self) -> None:
        for tabela, entrada in ALCANCE.items():
            with self.subTest(tabela=tabela):
                self.assertIsInstance(entrada, (Alcancada, Fora, Indireta))


class TestOVarredorAcusa(unittest.TestCase):
    """Planta cada um dos quatro defeitos e exige que ele seja acusado."""

    def test_acusa_tabela_com_customer_id_nao_declarada(self) -> None:
        achados = varrer(
            diretas={"tabela_nova"},
            passos={"_qualquer"},
            alcance={},
        )
        self.assertEqual([a.tabela for a in achados], ["tabela_nova"])

    def test_acusa_passo_declarado_que_nao_existe_mais(self) -> None:
        achados = varrer(
            diretas={"tabela_nova"},
            passos=set(),
            alcance={"tabela_nova": Alcancada("_passo_que_alguem_renomeou")},
        )
        self.assertEqual(len(achados), 1)
        self.assertIn("_passo_que_alguem_renomeou", achados[0].problema)

    def test_acusa_entrada_de_cemiterio(self) -> None:
        achados = varrer(
            diretas=set(),
            passos={"_passo"},
            alcance={"tabela_que_sumiu": Alcancada("_passo")},
        )
        self.assertEqual(len(achados), 1)
        self.assertIn("cemiterio", achados[0].problema)

    def test_acusa_indireta_cujo_caminho_deixou_de_pender_da_pessoa(self) -> None:
        achados = varrer(
            diretas=set(),
            passos={"_passo"},
            alcance={"tabela_indireta": Indireta("via_que_sumiu", "_passo")},
        )
        self.assertEqual(len(achados), 1)
        self.assertIn("via_que_sumiu", achados[0].problema)

    def test_deixa_passar_o_que_esta_certo(self) -> None:
        """O par do `pytest.raises`: a MESMA chamada com o dado certo nao acusa.

        Sem ele, os quatro testes acima ficariam verdes com um varredor que
        acusa tudo — inclusive o que esta correto.
        """
        achados = varrer(
            diretas={"tabela_direta", "via_boa"},
            passos={"_passo"},
            alcance={
                "tabela_direta": Alcancada("_passo"),
                "via_boa": Fora("nao guarda nada da pessoa"),
                "tabela_indireta": Indireta("via_boa", "_passo"),
            },
        )
        self.assertEqual(achados, [])


if __name__ == "__main__":
    unittest.main()
