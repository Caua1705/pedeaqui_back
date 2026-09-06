"""A conferência de filial que era escrita seis vezes (auditoria §3.1).

O que se trava aqui:

1. **as duas conferências são necessárias, e são diferentes.** Uma sozinha
   deixa passar um caso real, e os dois casos são opostos — por isso há um
   teste para cada;
2. **a leitura canônica é a ATIVA.** Era a das cinco cópias; a sexta lia a
   versão que devolve a filial ativa ou não, e este arquivo prova qual das
   duas ficou;
3. **os argumentos são só por nome.** É a armadilha que a auditoria achou:
   `admin_menu_service` chamava `_get_branch(branch_id, scope)` — a ordem
   invertida das outras cinco —, e nenhum type checker separa `AdminScope` de
   `UUID` num `f(a, b)` mal copiado. Com `*` na assinatura, a chamada trocada
   deixa de existir.

Não leva marcador `db`: `BranchScope` recebe o repositório por atributo, e o
que se testa aqui é a REGRA, não o SQL. O SQL das duas leituras tem cobertura
própria em `tests/test_branch_default_db.py`.
"""

import unittest
import uuid

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.services.branch_scope import BranchScope
from tests import fabricas


class _RepositorioFalso:
    """Dublê de COLABORADOR (o repositório), e não de dado.

    Guarda com que argumentos foi chamado porque a ORDEM deles é metade do que
    este arquivo prova: `(branch_id, restaurant_id)`, e não o contrário.
    """

    def __init__(self, ativas=(), inativas=()):
        self.ativas = list(ativas)
        self.todas = list(ativas) + list(inativas)
        self.chamadas: list[tuple] = []

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        self.chamadas.append((branch_id, restaurant_id))
        return next(
            (
                filial
                for filial in self.ativas
                if filial.id == branch_id and filial.restaurant_id == restaurant_id
            ),
            None,
        )

    def get_by_id_and_restaurant(self, branch_id, restaurant_id):
        self.chamadas.append((branch_id, restaurant_id))
        return next(
            (
                filial
                for filial in self.todas
                if filial.id == branch_id and filial.restaurant_id == restaurant_id
            ),
            None,
        )


def _escopo(restaurant_id, branch_id=None) -> AdminScope:
    return AdminScope(
        admin_user=None, restaurant_id=restaurant_id, branch_id=branch_id
    )


def _servico(ativas=(), inativas=()) -> BranchScope:
    servico = BranchScope.__new__(BranchScope)
    servico.branch_repository = _RepositorioFalso(ativas, inativas)
    return servico


class DuasConferenciasTests(unittest.TestCase):
    """Uma não substitui a outra, e cada teste mostra o caso que a outra perde."""

    def test_a_filial_do_proprio_escopo_passa(self):
        restaurante = uuid.uuid4()
        filial = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[filial])

        achada = servico.get(
            scope=_escopo(restaurante, branch_id=filial.id), branch_id=filial.id
        )

        self.assertIs(achada, filial)

    def test_o_gerente_preso_a_uma_loja_nao_alcanca_a_vizinha(self):
        """Quem barra aqui é `ensure_branch_allowed`, e SÓ ele.

        As duas filiais são do mesmo restaurante, então o repositório
        devolveria a vizinha sem reclamar: a segunda conferência não vê
        diferença entre elas.
        """
        restaurante = uuid.uuid4()
        minha = fabricas.filial(restaurant_id=restaurante)
        vizinha = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[minha, vizinha])

        with self.assertRaises(HTTPException) as erro:
            servico.get(
                scope=_escopo(restaurante, branch_id=minha.id), branch_id=vizinha.id
            )

        self.assertEqual(erro.exception.status_code, 404)

    def test_o_dono_de_um_restaurante_nao_alcanca_a_filial_de_outro(self):
        """O caso oposto, e o que `ensure_branch_allowed` NÃO pega.

        O dono tem `scope.branch_id` nulo por desenho, então
        `ensure_branch_allowed` retorna na primeira linha sem conferir nada.
        Quem barra é o recorte de restaurante do repositório — e é por isso que
        as duas conferências existem.
        """
        meu = uuid.uuid4()
        alheia = fabricas.filial(restaurant_id=uuid.uuid4())
        servico = _servico(ativas=[alheia])

        with self.assertRaises(HTTPException) as erro:
            servico.get(scope=_escopo(meu), branch_id=alheia.id)

        self.assertEqual(erro.exception.status_code, 404)

    def test_os_dois_404_sao_iguais(self):
        """Um 403 num deles confirmaria que aquela filial existe."""
        restaurante = uuid.uuid4()
        minha = fabricas.filial(restaurant_id=restaurante)
        vizinha = fabricas.filial(restaurant_id=restaurante)
        alheia = fabricas.filial(restaurant_id=uuid.uuid4())
        servico = _servico(ativas=[minha, vizinha, alheia])

        erros = []
        for escopo, pedida in (
            (_escopo(restaurante, branch_id=minha.id), vizinha.id),
            (_escopo(restaurante), alheia.id),
        ):
            with self.assertRaises(HTTPException) as erro:
                servico.get(scope=escopo, branch_id=pedida)
            erros.append((erro.exception.status_code, erro.exception.detail))

        self.assertEqual(erros[0], erros[1])


class LeituraCanonicaTests(unittest.TestCase):
    def test_filial_DESATIVADA_responde_404(self):
        """A decisão do §3.1: a canônica é `get_active_by_id_and_restaurant`.

        Cinco cópias já liam assim; a sexta (WhatsApp) não, e passou a ler.
        O motivo está escrito no docstring de `get_by_id_and_restaurant`, a
        outra leitura: ela existe para a reimpressão de comanda e diz que
        "não serve para configurar nem para vender".
        """
        restaurante = uuid.uuid4()
        fechada = fabricas.filial(restaurant_id=restaurante, is_active=False)
        servico = _servico(inativas=[fechada])

        with self.assertRaises(HTTPException) as erro:
            servico.get(scope=_escopo(restaurante), branch_id=fechada.id)

        self.assertEqual(erro.exception.status_code, 404)

    def test_a_contraprova_a_MESMA_filial_ativa_passa(self):
        """Sem isto, o teste acima passaria com um repositório que recusa tudo.

        É o procedimento da armadilha 52: todo `raises` novo confere que a
        mesma chamada com o dado certo NÃO levanta.
        """
        restaurante = uuid.uuid4()
        aberta = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[aberta])

        self.assertIs(
            servico.get(scope=_escopo(restaurante), branch_id=aberta.id), aberta
        )

    def test_a_ordem_dos_argumentos_do_repositorio_e_branch_depois_restaurante(self):
        """As duas leituras recebem `(branch_id, restaurant_id)`, nessa ordem.

        Trocar os dois no repositório não levanta erro nenhum: são dois `UUID`,
        e o `WHERE` sai com os campos invertidos — devolvendo `None` sempre, ou
        pior, a linha errada. O teste fixa a ordem que o repositório espera.
        """
        restaurante = uuid.uuid4()
        filial = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[filial])

        servico.get(scope=_escopo(restaurante), branch_id=filial.id)

        self.assertEqual(servico.branch_repository.chamadas, [(filial.id, restaurante)])


class SomenteporNomeTests(unittest.TestCase):
    """A armadilha que a auditoria §3.1 achou, fechada na assinatura."""

    def test_chamar_posicionalmente_levanta_TypeError(self):
        """Era assim que as seis cópias se distinguiam — e uma estava invertida.

        `admin_menu_service` chamava `_get_branch(branch_id, scope)`; as outras
        cinco, `_get_branch(scope, branch_id)`. Os dois argumentos são
        posicionais e nenhum type checker separa `AdminScope` de `UUID`, então
        a cópia trocada só apareceria como 404 estranho em produção.
        """
        restaurante = uuid.uuid4()
        filial = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[filial])

        with self.assertRaises(TypeError):
            servico.get(_escopo(restaurante), filial.id)

    def test_e_a_ordem_trocada_por_NOME_continua_funcionando(self):
        """O ponto de `*`: com nome, a ordem deixa de significar alguma coisa."""
        restaurante = uuid.uuid4()
        filial = fabricas.filial(restaurant_id=restaurante)
        servico = _servico(ativas=[filial])

        self.assertIs(
            servico.get(branch_id=filial.id, scope=_escopo(restaurante)), filial
        )


if __name__ == "__main__":
    unittest.main()
