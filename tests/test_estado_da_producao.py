"""`scripts/estado_da_producao.py` — a tela que se olha antes de qualquer coisa.

A suite RAPIDA deste script: o que se confere aqui e a imagem, o Redis e o
veredito, que nao precisam de banco. As conferencias que consultam o
Postgres estao em `test_estado_da_producao_db.py`.

Por que um script de diagnostico e testado. Ele erra do jeito mais caro
possivel — dizendo "tudo no lugar" quando nao esta —, e nada nele
denunciaria isso: a saida e texto, ninguem confere texto, e quem o roda esta
justamente sem saber o que ha de errado. Cada teste monta o estado exato do
problema que descreve e confere a SITUACAO daquela conferencia, nunca a
frase inteira.

O dublê do Redis e `SimpleNamespace` de propósito, e isso nao contradiz o
CLAUDE.md: a regra de construir o tipo real vale para schema e model DESTE
repositorio. O cliente do `redis-py` e biblioteca de terceiro, e o que o
codigo usa dele sao dois metodos — nao ha contrato nosso a respeitar.
"""

from types import SimpleNamespace

from scripts.estado_da_producao import (
    ATENCAO,
    ERRO,
    OK,
    Conferencia,
    _veredito,
    conferir_a_imagem,
    conferir_o_redis,
)
from src.core.config import GIT_SHA_NAO_CARIMBADO, settings


def _settings_com(monkeypatch, **valores):
    for nome, valor in valores.items():
        monkeypatch.setattr(settings, nome, valor)
    return settings


def _redis_falso(info: dict):
    """Um cliente que responde ao PING e devolve o `INFO` pedido.

    `info` chega como dicionario, e nao como `**kwargs`, porque um dos
    testes precisa de chave em BYTES — que e como o cliente do projeto
    (`decode_responses=False`) de fato as recebe, e o que `**` recusa.
    """
    return SimpleNamespace(ping=lambda: True, info=lambda: info)


class TestAImagem:
    """"Estou olhando o codigo que eu acho que estou olhando?"

    A pergunta que precede todas as outras. Em 24/08/2026 tres commits
    ficaram sem push e a producao seguiu rodando o codigo anterior; a
    resposta custou uma bateria de medicao.
    """

    def test_sha_carimbado_passa(self, monkeypatch):
        monkeypatch.delenv("ALEMBIC_TARGET", raising=False)
        _settings_com(monkeypatch, GIT_SHA="a267c63", APP_ENV="production")
        conferencia = conferir_a_imagem()
        assert conferencia.situacao == OK
        assert any("a267c63" in linha for linha in conferencia.linhas)

    def test_sem_carimbo_e_ERRO(self, monkeypatch):
        """Nao e ATENCAO: sem o sha nao ha investigacao possivel a seguir.

        E o unico item desta tela que impede TODOS os outros de significarem
        alguma coisa — "o banco esta em head" nao vale nada se ninguem sabe
        que codigo esta consultando esse banco.
        """
        monkeypatch.delenv("ALEMBIC_TARGET", raising=False)
        _settings_com(monkeypatch, GIT_SHA=GIT_SHA_NAO_CARIMBADO)
        assert conferir_a_imagem().situacao == ERRO

    def test_alembic_target_preso_no_env_e_ATENCAO(self, monkeypatch):
        """O estado temporario que vira permanente por esquecimento.

        Com `ALEMBIC_TARGET` no `.env`, toda revisao posterior a ele deixa de
        ser aplicada e o deploy continua passando verde. E ATENCAO e nao
        ERRO porque durante a janela de conferencia da migracao em duas
        etapas ele esta CERTO — quem sabe a diferenca e a pessoa.
        """
        _settings_com(monkeypatch, GIT_SHA="a267c63")
        monkeypatch.setenv("ALEMBIC_TARGET", "20260812_0016")
        conferencia = conferir_a_imagem()
        assert conferencia.situacao == ATENCAO
        assert any("20260812_0016" in linha for linha in conferencia.linhas)

    def test_alembic_target_em_head_nao_e_aviso(self, monkeypatch):
        """`ALEMBIC_TARGET=head` e o padrao escrito a mao: nao ha o que avisar."""
        _settings_com(monkeypatch, GIT_SHA="a267c63")
        monkeypatch.setenv("ALEMBIC_TARGET", "head")
        assert conferir_a_imagem().situacao == OK


class TestORedis:
    """`evicted_keys` e o numero, e o esperado dele e zero para sempre."""

    def test_sem_redis_url_e_ATENCAO(self, monkeypatch):
        """Nao e ERRO: a API sobe e serve pedido sem Redis.

        O que se perde e limite valendo com mais de um processo e cache de
        embedding entre clientes. Derrubar a tela inteira por isso apagaria
        a diferenca entre "degradado" e "parado".
        """
        _settings_com(monkeypatch, REDIS_URL=None)
        assert conferir_o_redis().situacao == ATENCAO

    def test_redis_limpo_passa(self, monkeypatch):
        _settings_com(monkeypatch, REDIS_URL="redis://redis:6379/0")
        conferencia = conferir_o_redis(
            _redis_falso({"evicted_keys": 0, "used_memory": 12_582_912, "maxmemory": 268_435_456})
        )
        assert conferencia.situacao == OK

    def test_despejo_e_ERRO(self, monkeypatch):
        """Uma chave despejada pode ser um contador de rate limit.

        E contador despejado nao da erro: o `slowapi` le a chave ausente como
        zero, o cliente ganha orcamento novo, e o limite deixa de valer em
        silencio (armadilha 41). Por isso o numero esperado e zero, e nao
        "poucos".
        """
        _settings_com(monkeypatch, REDIS_URL="redis://redis:6379/0")
        conferencia = conferir_o_redis(
            _redis_falso({"evicted_keys": 1, "used_memory": 268_000_000, "maxmemory": 268_435_456})
        )
        assert conferencia.situacao == ERRO
        assert any("evicted_keys=1" in linha for linha in conferencia.linhas)

    def test_redis_que_nao_responde_e_ERRO(self, monkeypatch):
        """Sem engolir a excecao: um Redis fora do ar tem que aparecer na tela."""
        _settings_com(monkeypatch, REDIS_URL="redis://redis:6379/0")

        def recusar():
            raise ConnectionError("Connection refused")

        cliente = SimpleNamespace(ping=recusar, info=recusar)
        assert conferir_o_redis(cliente).situacao == ERRO

    def test_info_com_chave_em_bytes(self, monkeypatch):
        """O cliente do projeto sobe com `decode_responses=False`.

        E ele sobe assim porque o embedding e gravado como bytes (ver
        `cliente_redis`). Ler `INFO` esperando `str` daria zero em toda
        chave, e o despejo passaria despercebido justamente no processo
        configurado como o de producao.
        """
        _settings_com(monkeypatch, REDIS_URL="redis://redis:6379/0")
        conferencia = conferir_o_redis(
            _redis_falso({b"evicted_keys": b"7", b"used_memory": b"1024", b"maxmemory": b"2048"})
        )
        assert conferencia.situacao == ERRO
        assert any("evicted_keys=7" in linha for linha in conferencia.linhas)


class TestOVeredito:
    """A ultima linha, que e a que a pessoa le primeiro."""

    def test_erro_ganha_de_atencao(self):
        veredito = _veredito([
            Conferencia("Redis", ATENCAO),
            Conferencia("Revisao do banco", ERRO),
        ])
        assert "Revisao do banco" in veredito
        assert "Redis" not in veredito

    def test_so_atencao_nao_diz_que_ha_problema(self):
        veredito = _veredito([Conferencia("Redis", ATENCAO)])
        assert "Nenhum erro" in veredito

    def test_tudo_ok(self):
        assert "Tudo no lugar" in _veredito([Conferencia("Redis", OK)])


def test_toda_conferencia_ruim_diz_o_que_fazer(monkeypatch):
    """A regra de forma que segura o valor desta tela.

    Diagnostico sem proximo passo obriga quem leu a ir procurar o passo em
    outro lugar — as duas da manha, com a operacao parada. As duas
    conferencias que rodam sem banco sao levadas ao pior estado de cada uma,
    e o que se confere e que a linha `->` existe.
    """
    monkeypatch.setenv("ALEMBIC_TARGET", "20260812_0016")
    _settings_com(monkeypatch, GIT_SHA=GIT_SHA_NAO_CARIMBADO, REDIS_URL=None)

    ruins = [
        conferir_a_imagem(),
        conferir_o_redis(),
        conferir_o_redis(_redis_falso({"evicted_keys": 9, "used_memory": 1, "maxmemory": 2})),
    ]
    for conferencia in ruins:
        assert conferencia.situacao != OK
        assert conferencia.o_que_fazer, f"{conferencia.titulo} nao diz o que fazer"
