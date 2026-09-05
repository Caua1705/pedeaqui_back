"""O tamanho do pool é uma decisão, e ela fica congelada aqui.

`create_engine` sem `pool_size` não é "o padrão do SQLAlchemy" na prática: é
`5 + 10 = 15` conexões por processo, com 30 s de espera antes de a requisição
morrer. Isso valia enquanto ninguém tinha olhado — e quando alguém olhou, em
05/09/2026, o número medido em produção foi `max_connections = 60`.

## Por que um teste, e não só o comentário

O comentário em `src/db/session.py` explica a conta. O que ele não faz é
avisar quando uma das premissas dela mudar, e as duas que mudam sozinhas são:

- **o número de workers.** A conta é `2 x 20 = 40`, e ela existe para
  sobreviver ao `--workers 2` sem ninguém refazê-la. Se alguém subir o
  `pool_size` "porque tem folga", a folga era do segundo worker;
- **os quatro contêineres de manutenção.** Eles constroem o engine com este
  mesmo módulo. Cada um segura uma conexão de cada vez porque o laço é
  sequencial — não porque o pool os impeça.

O teste não sabe qual é o número certo. Ele sabe qual foi o número DECIDIDO, e
cobra que mudá-lo seja um ato, não um efeito colateral.
"""

import unittest

from sqlalchemy import create_engine

from src.db.session import (
    MAX_OVERFLOW,
    POOL_RECYCLE_SECONDS,
    POOL_SIZE,
    POOL_TIMEOUT,
    get_engine,
)


#: O teto medido em produção em 05/09/2026. Refaça a conta se ele mudar.
MAX_CONNECTIONS_DO_BANCO = 60

#: Workers para os quais a conta foi feita — um a mais do que roda hoje.
WORKERS_PREVISTOS = 2

#: O que não é da API: Supabase, os quatro contêineres de manutenção, a
#: janela de deploy e o psql de emergência.
RESERVADO_FORA_DA_API = 20


class TestOsNumerosDecididos(unittest.TestCase):
    def test_o_teto_por_worker_e_vinte(self):
        self.assertEqual(POOL_SIZE + MAX_OVERFLOW, 20)

    def test_a_conta_cabe_no_banco_com_o_segundo_worker(self):
        """A premissa inteira, escrita como aritmética.

        Este é o teste que falha quando alguém sobe o pool "porque tem folga":
        a folga é do worker que ainda não existe.
        """
        teto_da_api = (POOL_SIZE + MAX_OVERFLOW) * WORKERS_PREVISTOS
        self.assertLessEqual(
            teto_da_api + RESERVADO_FORA_DA_API,
            MAX_CONNECTIONS_DO_BANCO,
            "o pool não cabe mais no banco com dois workers",
        )

    def test_a_espera_nao_e_a_de_ninguem_ter_olhado(self):
        """30 s é o padrão do SQLAlchemy, e é tempo que o app já desistiu de
        esperar. Um valor de volta em 30 é o sinal de que o `pool_timeout`
        deixou de ser passado."""
        self.assertNotEqual(POOL_TIMEOUT, 30)
        self.assertEqual(POOL_TIMEOUT, 10)

    def test_a_reciclagem_esta_ligada(self):
        """`-1` é o padrão e significa "nunca recicla". Contra o pooler do
        Supabase isso deixa o `pool_pre_ping` como única defesa."""
        self.assertGreater(POOL_RECYCLE_SECONDS, 0)


class TestOEngineRecebeOsNumeros(unittest.TestCase):
    """As constantes só valem se chegarem ao engine.

    Sem isto, alguém poderia mudar `get_engine` de volta para
    `create_engine(url, pool_pre_ping=True)` e os testes acima continuariam
    verdes descrevendo quatro constantes que ninguém lê.
    """

    def test_o_pool_do_engine_e_o_configurado(self):
        engine = get_engine()
        pool = engine.pool

        self.assertEqual(pool.size(), POOL_SIZE)
        self.assertEqual(pool._max_overflow, MAX_OVERFLOW)
        self.assertEqual(pool._timeout, POOL_TIMEOUT)
        self.assertEqual(pool._recycle, POOL_RECYCLE_SECONDS)

    def test_o_padrao_do_sqlalchemy_e_mesmo_5_mais_10(self):
        """A premissa do arquivo inteiro, medida e não lembrada.

        Se um dia o SQLAlchemy mudar o default, este teste cai e o comentário
        de `session.py` passa a descrever um `antes` que não existiu.
        """
        padrao = create_engine("postgresql+psycopg://ninguem@localhost/nada").pool

        self.assertEqual(padrao.size(), 5)
        self.assertEqual(padrao._max_overflow, 10)
        self.assertEqual(padrao._timeout, 30)


if __name__ == "__main__":
    unittest.main()
