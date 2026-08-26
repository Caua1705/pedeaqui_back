"""O lock que serializa `alembic upgrade` entre replicas.

Marcado `db` porque nao ha como dublar isto: o que se quer testar E o
comportamento do `pg_advisory_xact_lock` do Postgres — quem espera quem, e
quando o lock e solto. Um dublê de `Connection` responderia o que o teste
escrevesse, que e exatamente o que a regra de dublê do CLAUDE.md proibe.

DUAS CONEXOES DE VERDADE, e nao a fixture `db`: aquela sessao vive dentro de
UMA transacao, e o assunto aqui e o que acontece entre DUAS. A fixture
`engine_de_teste` da o banco com schema pronto; as conexoes saem dela.
"""

import threading
import time

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from src.db.advisory_lock import CHAVE_DA_MIGRACAO, travar_para_migrar


pytestmark = pytest.mark.db


def _tentar_pegar(conexao) -> bool:
    """`True` quando o lock estava livre — e ai esta conexao passa a segura-lo."""
    return bool(
        conexao.execute(
            text("SELECT pg_try_advisory_xact_lock(:chave)"),
            {"chave": CHAVE_DA_MIGRACAO},
        ).scalar()
    )


def _quantos_esperam(conexao) -> int:
    """Quantos backends estao na FILA do nosso lock, pelo `pg_locks`.

    `NOT granted` e o pedido que ainda nao foi atendido — quer dizer, alguem
    parado dentro de `pg_advisory_xact_lock`. E o que permite ao teste saber
    que a outra thread ja chegou no bloqueio, em vez de dormir um tanto e
    torcer.

    A chave de 64 bits e guardada partida em duas colunas de 32:
    `classid` em cima, `objid` embaixo. A nossa cabe inteira embaixo.
    """
    return conexao.execute(
        text(
            """
            SELECT count(*) FROM pg_locks
             WHERE locktype = 'advisory'
               AND NOT granted
               AND classid = 0
               AND objid::bigint = :objid
            """
        ),
        {"objid": CHAVE_DA_MIGRACAO},
    ).scalar()


class TestQuemChegaSegundoEsbarra:
    """A propriedade que faz o lock existir: dois migradores nao andam juntos."""

    def test_a_segunda_conexao_nao_consegue_o_lock(self, engine_de_teste: Engine):
        primeira = engine_de_teste.connect()
        segunda = engine_de_teste.connect()
        try:
            primeira.begin()
            travar_para_migrar(primeira)

            segunda.begin()
            assert _tentar_pegar(segunda) is False
        finally:
            primeira.close()
            segunda.close()

    def test_o_lock_e_solto_quando_a_transacao_acaba(self, engine_de_teste: Engine):
        """Sem release explicito, e este teste e o que garante isso.

        E a propriedade que cobre o migrador que MORRE no meio: a transacao
        cai junto com a conexao e o proximo entra. Aqui o fim e um rollback,
        que e o mesmo caminho de codigo do Postgres.
        """
        primeira = engine_de_teste.connect()
        segunda = engine_de_teste.connect()
        try:
            transacao = primeira.begin()
            travar_para_migrar(primeira)
            transacao.rollback()

            segunda.begin()
            assert _tentar_pegar(segunda) is True
        finally:
            primeira.close()
            segunda.close()

    def test_a_mesma_transacao_pode_pedir_duas_vezes(self, engine_de_teste: Engine):
        """Advisory lock e reentrante para o dono, e isso nao e detalhe.

        Uma revisao que chamasse `travar_para_migrar` de novo, ou um retry
        dentro da mesma transacao, nao pode se auto-travar — seria um deadlock
        de uma replica so, que e o modo de falha mais confuso possivel.
        """
        conexao = engine_de_teste.connect()
        try:
            conexao.begin()
            travar_para_migrar(conexao)
            travar_para_migrar(conexao)
            assert _tentar_pegar(conexao) is True
        finally:
            conexao.close()


class TestOAvisoSaiQuandoHouveEspera:
    """A linha que so a espera consegue escrever.

    O guard de `--workers` le `sys.argv` e por isso enxerga um processo so.
    Ter esperado o lock e a unica prova que uma replica tem de que existe
    outra — e se a espera deixar de falar, essa prova se perde em silencio.
    """

    def test_lock_livre_nao_imprime_nada(self, engine_de_teste: Engine, capsys):
        conexao = engine_de_teste.connect()
        try:
            conexao.begin()
            travar_para_migrar(conexao)
            assert capsys.readouterr().out == ""
        finally:
            conexao.close()

    def test_a_pausa_e_explicada_ANTES_de_bloquear(self, engine_de_teste: Engine, capsys):
        """A ordem, que e a razao de a linha existir.

        Ela serve para quem esta olhando o `docker logs` durante a pausa.
        Impressa depois do bloqueio, chegaria junto com o fim da espera —
        quando ninguem mais precisa dela — e nenhum assert sobre o texto
        final pegaria a diferenca.

        O `lock_timeout` e o que torna isto verificavel sem thread: o
        bloqueio passa a ter fim, e o que sobrou impresso ANTES dele fica
        provado por ter sobrevivido a excecao.
        """
        primeira = engine_de_teste.connect()
        segunda = engine_de_teste.connect()
        try:
            primeira.begin()
            travar_para_migrar(primeira)

            segunda.begin()
            segunda.execute(text("SET LOCAL lock_timeout = '250ms'"))
            capsys.readouterr()
            with pytest.raises(DBAPIError):
                travar_para_migrar(segunda)
            saida = capsys.readouterr().out
        finally:
            primeira.close()
            segunda.close()

        assert "Esperando a vez" in saida
        # E so isso: o aviso das duas replicas vem DEPOIS do bloqueio, e o
        # bloqueio nao terminou.
        assert "OUTRA REPLICA" not in saida

    def test_quem_esperou_avisa_que_ha_outra_replica(self, engine_de_teste: Engine, capsys):
        """O caminho inteiro: espera de verdade, destrava, e o aviso sai.

        A segunda replica roda numa thread porque so assim ela pode ficar
        PARADA enquanto esta thread solta o lock — que e o que acontece em
        producao entre dois containers.

        Nada aqui dorme por um tanto e torce: a espera de verdade e esperar
        `pg_locks` mostrar o pedido na fila.
        """
        primeira = engine_de_teste.connect()
        transacao = primeira.begin()
        travar_para_migrar(primeira)
        capsys.readouterr()

        def segunda_replica() -> None:
            conexao = engine_de_teste.connect()
            try:
                conexao.begin()
                travar_para_migrar(conexao)
            finally:
                conexao.close()

        thread = threading.Thread(target=segunda_replica, name="segunda-replica")
        thread.start()
        try:
            limite = time.monotonic() + 10
            while _quantos_esperam(primeira) == 0:
                assert time.monotonic() < limite, "a outra thread nunca entrou na fila do lock"
                time.sleep(0.05)
            transacao.rollback()
            thread.join(timeout=10)
            assert not thread.is_alive(), "a outra thread nao destravou depois do rollback"
        finally:
            primeira.close()

        saida = capsys.readouterr().out
        assert "Esperando a vez" in saida
        assert "OUTRA REPLICA" in saida
        # O perigo tem nome: o historico do /chat, que nao tem caminho de
        # Redis. Sem esta frase o aviso vira "algo esta estranho".
        assert "/chat" in saida
