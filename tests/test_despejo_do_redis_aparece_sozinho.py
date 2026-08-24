"""Redis despejando chave vira aviso no log da limpeza — sem ninguem olhar painel.

O QUE ISTO COBRE. O cache de embedding do Rapi foi para o Redis, que sobe com
`--maxmemory 256mb --maxmemory-policy volatile-lru`. Essa politica escolhe a
vitima entre as chaves **com TTL**, e todas as nossas tem — inclusive o contador
de rate limit, cuja janela E o TTL.

O modo de falha e assimetrico e silencioso: um embedding ocupa ~6 KB, um
contador de limite algumas dezenas de bytes. Sob pressao, o contador sai, o
`slowapi` le a chave ausente como zero, o cliente ganha orcamento novo e o
limite deixa de valer. Nao ha erro, nao ha log, e o
`in_memory_fallback_enabled` nao cobre isso — ele existe para o Redis fora do
ar, nao para o Redis respondendo com uma chave que foi apagada.

`evicted_keys` do `INFO` e o sinal, e o numero esperado e ZERO para sempre. Mas
metrica que exige alguem lembrar de olhar nao e deteccao: e um numero
esperando. Por isso ela sai no stdout do container `limpeza`, que ja roda
sozinho todo dia.

AS TRES COISAS QUE ESTE ARQUIVO TRAVA, em ordem de quanto custaria perder:

1. **A checagem NUNCA falha o script.** `cleanup_idempotency_keys.py` esta
   encadeado com `&&` ao `expire_cashback.py`, que vence SALDO DE CLIENTE.
   Falhar aqui trocaria um aviso de diagnostico por um bug de dinheiro.
2. **Despejo diferente de zero sai com o suspeito nomeado.** Um aviso que so
   diz "evicted_keys=42" manda a proxima pessoa investigar do zero.
3. **Zero tambem sai.** Silencio absoluto e indistinguivel de checagem que
   parou de rodar.
"""

import pytest

from scripts.cleanup_idempotency_keys import conferir_despejo_do_redis


def info_de(despejadas: int, usada: int = 200 * 1024 * 1024) -> dict:
    return {
        "evicted_keys": despejadas,
        "used_memory": usada,
        "maxmemory": 256 * 1024 * 1024,
    }


class RedisFalso:
    def __init__(self, info: dict):
        self._info = info

    def info(self):
        return self._info


class RedisQuebrado:
    def info(self):
        raise ConnectionError("redis fora do ar")


class TestNuncaFalhaOScript:
    @pytest.mark.parametrize(
        "cliente",
        [RedisFalso(info_de(0)), RedisFalso(info_de(9999)), RedisQuebrado()],
        ids=["sem_despejo", "com_despejo", "redis_fora_do_ar"],
    )
    def test_devolve_zero_em_todos_os_casos(self, cliente):
        """`&&` com o `expire_cashback.py`: retorno nao-zero para o dinheiro."""
        assert conferir_despejo_do_redis(cliente) == 0

    def test_redis_fora_do_ar_nao_levanta_e_diz_que_seguiu(self, capsys):
        conferir_despejo_do_redis(RedisQuebrado())

        saida = capsys.readouterr().out
        assert "seguindo" in saida

    def test_info_com_campo_faltando_nao_levanta(self, capsys):
        """Formato do INFO que mude do lado do Redis vira zero, nao excecao."""
        assert conferir_despejo_do_redis(RedisFalso({})) == 0
        assert "evicted_keys=0" in capsys.readouterr().out

    def test_info_em_bytes_e_lido(self, capsys):
        """O cliente compartilhado usa `decode_responses=False` por causa do vetor."""
        cru = {b"evicted_keys": b"7", b"used_memory": b"1048576", b"maxmemory": b"0"}

        conferir_despejo_do_redis(RedisFalso(cru))

        assert "evicted_keys=7" in capsys.readouterr().out


class TestOAviso:
    def test_despejo_nomeia_o_suspeito_e_a_vitima(self, capsys):
        conferir_despejo_do_redis(RedisFalso(info_de(42)))

        saida = capsys.readouterr().out
        assert "ATENCAO" in saida
        assert "evicted_keys=42" in saida
        # A vitima: quem perde e o rate limit, e e por isso que importa.
        assert "RATE LIMIT" in saida
        # O suspeito: quem ocupa o espaco.
        assert "emb:v1:*" in saida

    def test_o_aviso_diz_que_banco_separado_nao_resolve(self, capsys):
        """E o conserto intuitivo e errado: `maxmemory` e por INSTANCIA.

        Sem esta frase no proprio aviso, a proxima pessoa muda `/0` para `/1`,
        conclui que resolveu, e o despejo continua.
        """
        conferir_despejo_do_redis(RedisFalso(info_de(1)))

        saida = capsys.readouterr().out
        assert "NAO resolve" in saida
        assert "armadilha 41" in saida

    def test_sem_despejo_a_linha_sai_do_mesmo_jeito(self, capsys):
        """Silencio seria indistinguivel de checagem que parou de rodar."""
        conferir_despejo_do_redis(RedisFalso(info_de(0)))

        saida = capsys.readouterr().out
        assert "evicted_keys=0" in saida
        assert "ATENCAO" not in saida

    def test_sem_redis_diz_que_nao_conferiu(self, capsys, monkeypatch):
        """`REDIS_URL` vazio nao pode virar um "Ok." que ninguem conferiu."""
        import src.ai.services.chat_cache as cache_module

        monkeypatch.setattr(cache_module, "cliente_redis", lambda: None)

        assert conferir_despejo_do_redis() == 0
        assert "nao conferido" in capsys.readouterr().out
