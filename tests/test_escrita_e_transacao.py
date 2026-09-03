"""A anti-vacuidade do varredor de escrita, commit e camadas.

`scripts/escrita_e_transacao.py` responde **zero** contra `src/` hoje, e zero
so vale alguma coisa se estiver provado que ele SABE acusar. Foi essa a licao
mais cara das rodadas anteriores: a varredura de escopo de tenant devolveu
"nenhum" tres vezes seguidas, e as tres vezes o motivo era defeito dela — nome
de funcao colidindo entre modulos, argumento posicional que ela so via como
palavra-chave, sufixo de repositorio que faltava na lista. Um zero de varredor
cego e indistinguivel de um zero de codigo correto.

Cada classe daqui tem os dois lados:

- **a isca**, um pedaco de codigo plantado que ela TEM que acusar;
- **o padrao legitimo**, escrito do jeito que o repositorio escreve de verdade,
  que ela NAO pode acusar.

O segundo lado e o que impede o conserto preguicoso. Um varredor que acusa
tudo passa em toda isca e e inutil na mesma medida.

A arvore e plantada em disco porque o varredor le ARQUIVO — e ler arquivo e
parte do que se esta testando (o indice, os diretorios, o casamento por
prefixo). Um teste que injetasse `ast.Module` pronto pularia justamente a
metade que ja quebrou tres vezes.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.escrita_e_transacao import auditar


SERVICO_CERTO = """
class ServicoCerto:
    def __init__(self, db):
        self.db = db
        self.pedido_repository = PedidoRepository(db)

    def criar(self, dados):
        pedido = self.pedido_repository.create(dados)
        self.pedido_repository.add_history(pedido.id, "created")
        self.db.commit()
        return pedido
"""


class _Arvore:
    """Uma arvore de `src/` de mentira, com o formato que o varredor espera."""

    def __init__(self, diretorio: str) -> None:
        self.raiz = Path(diretorio)
        for parte in ("src/api/endpoints", "src/services", "src/ai", "src/repositories", "scripts"):
            (self.raiz / parte).mkdir(parents=True, exist_ok=True)

    def escrever(self, caminho: str, codigo: str) -> None:
        (self.raiz / caminho).write_text(textwrap.dedent(codigo), encoding="utf-8")

    def auditar(self) -> dict[str, list]:
        return auditar(self.raiz)


class _CasoComArvore(unittest.TestCase):
    def setUp(self) -> None:
        self._temporario = TemporaryDirectory()
        self.addCleanup(self._temporario.cleanup)
        self.arvore = _Arvore(self._temporario.name)

    def assertNaoAcusa(self, achados: dict[str, list], chave: str) -> None:
        self.assertEqual(achados[chave], [], f"acusou o que e legitimo: {achados[chave]}")


class EscritaSemCommitTests(_CasoComArvore):
    """A que custa dinheiro em silencio: a linha nunca chega ao banco."""

    def test_a_isca_e_acusada(self):
        self.arvore.escrever(
            "src/services/pagamento_service.py",
            """
            class PagamentoService:
                def __init__(self, db):
                    self.db = db
                    self.pagamento_repository = PagamentoRepository(db)

                def registrar(self, dados):
                    self.pagamento_repository.create(dados)
            """,
        )

        achados = self.arvore.auditar()

        self.assertEqual(len(achados["sem_commit"]), 1)
        classe, metodo, chamada, _ = achados["sem_commit"][0]
        self.assertEqual((classe, metodo, chamada), ("PagamentoService", "registrar", "create"))

    def test_o_service_que_commita_no_fim_nao_e_acusado(self):
        self.arvore.escrever("src/services/certo_service.py", SERVICO_CERTO)

        self.assertNaoAcusa(self.arvore.auditar(), "sem_commit")

    def test_o_commit_de_um_metodo_CHAMADO_conta(self):
        """A cadeia, e nao so o corpo.

        `criar` nao commita — quem commita e o `_fechar` que ele chama. Cobrar
        commit no proprio metodo transformaria toda decomposicao em achado.
        """
        self.arvore.escrever(
            "src/services/pedido_service.py",
            """
            class PedidoService:
                def __init__(self, db):
                    self.db = db
                    self.pedido_repository = PedidoRepository(db)

                def criar(self, dados):
                    self.pedido_repository.create(dados)
                    return self._fechar()

                def _fechar(self):
                    self.db.commit()
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "sem_commit")

    def test_o_helper_privado_que_escreve_nao_e_cobrado_sozinho(self):
        """O falso positivo que este varredor ja teve, e o motivo dele.

        `_apagar_enderecos` escreve e nao commita — e esta CERTO: quem commita e
        o `anonimizar` que o chama, uma vez, depois de todos os passos. Cobrar
        commit do helper seria pedir exatamente o contrario da regra, que manda
        as varias escritas cairem na MESMA transacao.
        """
        self.arvore.escrever(
            "src/services/anonimizacao_service.py",
            """
            class AnonimizacaoService:
                def __init__(self, db):
                    self.db = db
                    self.cliente_repository = ClienteRepository(db)

                def anonimizar(self, cliente_id):
                    self._apagar_enderecos(cliente_id)
                    self._limpar_comentarios(cliente_id)
                    self.db.commit()

                def _apagar_enderecos(self, cliente_id):
                    self.cliente_repository.delete_addresses_of(cliente_id)

                def _limpar_comentarios(self, cliente_id):
                    self.cliente_repository.clear_comments_of(cliente_id)
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "sem_commit")

    def test_o_commit_pode_morar_num_SCRIPT_de_manutencao(self):
        """O segundo falso positivo, e o motivo de `scripts/` estar no indice.

        `expire_balance` e chamado por `scripts/expire_cashback.py`, que commita
        no laco — uma vez por pessoa, de proposito, para nao segurar o `FOR
        UPDATE` de quem nao venceu. Um indice que so olhasse `src/` acusaria um
        service correto.
        """
        self.arvore.escrever(
            "src/services/cashback_service.py",
            """
            class CashbackService:
                def __init__(self, db):
                    self.db = db
                    self.cashback_repository = CashbackRepository(db)

                def expirar(self, cliente_id):
                    self.cashback_repository.mark_available_as_expired(cliente_id)
            """,
        )
        self.arvore.escrever(
            "scripts/expirar.py",
            """
            def rodar(db, pessoas):
                for cliente_id in pessoas:
                    CashbackService(db).expirar(cliente_id)
                    db.commit()
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "sem_commit")

    def test_service_que_so_LE_nao_e_cobrado(self):
        self.arvore.escrever(
            "src/services/cardapio_service.py",
            """
            class CardapioService:
                def __init__(self, db):
                    self.db = db
                    self.produto_repository = ProdutoRepository(db)

                def listar(self, filial_id):
                    return self.produto_repository.list_active(filial_id)
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "sem_commit")


class CommitEntreDuasEscritasTests(_CasoComArvore):
    """A atomicidade: uma falha no meio grava a primeira e perde a segunda."""

    def test_a_isca_e_acusada(self):
        self.arvore.escrever(
            "src/services/checkout_service.py",
            """
            class CheckoutService:
                def __init__(self, db):
                    self.db = db
                    self.pedido_repository = PedidoRepository(db)
                    self.cupom_repository = CupomRepository(db)

                def finalizar(self, dados):
                    self.cupom_repository.mark_as_used(dados.cupom_id)
                    self.db.commit()
                    self.pedido_repository.create(dados)
                    self.db.commit()
            """,
        )

        achados = self.arvore.auditar()

        self.assertEqual(len(achados["commit_no_meio"]), 1)
        classe, metodo, _ = achados["commit_no_meio"][0]
        self.assertEqual((classe, metodo), ("CheckoutService", "finalizar"))

    def test_o_commit_no_FIM_das_duas_escritas_e_legitimo(self):
        """E o padrao correto, e o mais comum do repositorio."""
        self.arvore.escrever("src/services/certo_service.py", SERVICO_CERTO)

        self.assertNaoAcusa(self.arvore.auditar(), "commit_no_meio")

    def test_a_ORDEM_e_a_do_codigo_e_nao_a_do_walk(self):
        """`ast.walk` visita em LARGURA, e a largura mente sobre a ordem.

        Este metodo esta certo: as duas escritas acontecem e o commit vem
        depois das duas — atomico. Mas `ast.walk` entrega os nos de
        profundidade 1 primeiro (`create`, o `if`, `commit`) e so depois desce
        para o `mark_as_used` de dentro do `if`. Lido nessa ordem, o metodo
        vira escrita/commit/escrita e seria acusado.

        Por isso `_eventos` ordena por `lineno`. Sem o `sorted`, este teste fica
        vermelho — foi assim que ele foi conferido.
        """
        self.arvore.escrever(
            "src/services/salvamento_service.py",
            """
            class SalvamentoService:
                def __init__(self, db):
                    self.db = db
                    self.pedido_repository = PedidoRepository(db)
                    self.cupom_repository = CupomRepository(db)

                def salvar(self, dados):
                    self.pedido_repository.create(dados)
                    if dados.cupom:
                        self.cupom_repository.mark_as_used(dados.cupom)
                    self.db.commit()
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "commit_no_meio")


class OVocabularioTests(_CasoComArvore):
    """O que ele nao souber classificar sai como achado, nunca como leitura."""

    def test_nome_fora_das_duas_listas_e_ACUSADO(self):
        """A porta que este varredor existe para nao deixar aberta.

        Um metodo de repositorio com nome novo, tratado como leitura por
        exclusao, seria uma escrita sem commit invisivel para sempre. Ele sai
        como "nao classifiquei" e alguem decide — o silencio e que nao pode.
        """
        self.arvore.escrever(
            "src/services/estranho_service.py",
            """
            class EstranhoService:
                def __init__(self, db):
                    self.db = db
                    self.pedido_repository = PedidoRepository(db)

                def fazer(self, pedido_id):
                    self.pedido_repository.esquentar_o_pedido(pedido_id)
            """,
        )

        achados = self.arvore.auditar()

        self.assertEqual(len(achados["nao_classificados"]), 1)
        self.assertEqual(achados["nao_classificados"][0][2], "esquentar_o_pedido")

    def test_nome_do_vocabulario_conhecido_nao_e_acusado(self):
        self.arvore.escrever("src/services/certo_service.py", SERVICO_CERTO)

        self.assertNaoAcusa(self.arvore.auditar(), "nao_classificados")

    def test_metodo_que_nao_e_de_repositorio_e_ignorado(self):
        """`self.calculadora.esquentar(...)` nao e chamada de repositorio.

        Sem o sufixo `_repository` no atributo, o varredor nao tem por que
        opinar — e opinar seria enche-lo de ruido de todo colaborador.
        """
        self.arvore.escrever(
            "src/services/preco_service.py",
            """
            class PrecoService:
                def __init__(self, db):
                    self.db = db
                    self.calculadora = Calculadora()

                def calcular(self, pedido):
                    return self.calculadora.esquentar_o_pedido(pedido)
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "nao_classificados")


class AsCamadasTests(_CasoComArvore):
    """As tres metades que o grep responde. Hoje de pe — e o dia em que
    cairem tem que ser vermelho, e nao descoberta."""

    def test_repositorio_que_commita_e_acusado(self):
        self.arvore.escrever(
            "src/repositories/pedido_repository.py",
            """
            class PedidoRepository:
                def create(self, dados):
                    self.db.add(dados)
                    self.db.commit()
            """,
        )

        self.assertEqual(len(self.arvore.auditar()["repositorio_commita"]), 1)

    def test_repositorio_que_so_da_flush_nao_e_acusado(self):
        """`flush` NAO e commit, e o repositorio usa por um motivo bom: obter o
        id antes do commit do service. Acusar isso mataria o padrao certo."""
        self.arvore.escrever(
            "src/repositories/pedido_repository.py",
            """
            class PedidoRepository:
                def create(self, dados):
                    self.db.add(dados)
                    self.db.flush()
                    return dados
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "repositorio_commita")

    def test_repositorio_que_levanta_HTTPException_e_acusado(self):
        self.arvore.escrever(
            "src/repositories/pedido_repository.py",
            """
            from fastapi import HTTPException

            class PedidoRepository:
                def get(self, pedido_id):
                    raise HTTPException(status_code=404)
            """,
        )

        self.assertTrue(self.arvore.auditar()["repositorio_levanta"])

    def test_comentario_que_MENCIONA_a_regra_nao_e_acusado(self):
        """Todo repositorio deste projeto tem docstring falando do que ele nao
        faz. Casar comentario transformaria a documentacao da regra em violacao
        dela."""
        self.arvore.escrever(
            "src/repositories/pedido_repository.py",
            """
            class PedidoRepository:
                def get(self, pedido_id):
                    # nao levanta HTTPException aqui: a regra e do service
                    # e o commit tambem, self.db.commit() nao mora neste arquivo
                    return None
            """,
        )

        achados = self.arvore.auditar()
        self.assertNaoAcusa(achados, "repositorio_levanta")
        self.assertNaoAcusa(achados, "repositorio_commita")

    def test_endpoint_que_importa_repositorio_e_acusado(self):
        self.arvore.escrever(
            "src/api/endpoints/pedidos.py",
            """
            from src.repositories.pedido_repository import PedidoRepository
            """,
        )

        self.assertEqual(len(self.arvore.auditar()["endpoint_usa_repositorio"]), 1)

    def test_endpoint_que_importa_service_nao_e_acusado(self):
        self.arvore.escrever(
            "src/api/endpoints/pedidos.py",
            """
            from src.services.pedido_service import PedidoService
            """,
        )

        self.assertNaoAcusa(self.arvore.auditar(), "endpoint_usa_repositorio")


class OEstadoDeHojeTests(unittest.TestCase):
    """O numero contra o `src/` de verdade.

    Zero, e ele e FALHA no portao e nao aviso — a distincao da rodada 6: aqui
    nao ha divida herdada para drenar, entao um zero que cresce e regressao.
    """

    def test_o_repositorio_esta_limpo(self):
        achados = auditar()

        self.assertEqual(
            {chave: len(grupo) for chave, grupo in achados.items() if grupo},
            {},
            f"achados novos: {achados}",
        )


if __name__ == "__main__":
    unittest.main()
