"""O historico da conversa no Redis: a chave, o TTL, e a falha que nao derruba.

Os testes do CONTRATO (teto de mensagens, TTL, o que o `ChatService` consome)
ficam em `tests/test_chat_service.py` — la eles rodam contra o backend que a
suite usa, que e o de memoria. Aqui esta o que so o backend de Redis tem:

- a chave vai HASHEADA, e o `session_id` do cliente nao aparece nela;
- o TTL entra na MESMA operacao da escrita;
- **falha do Redis nao derruba o turno**, em nenhum dos caminhos.

O cliente de Redis e dublado com `SimpleNamespace` e com classes escritas a
mao, e isso e o uso CERTO dele (CLAUDE.md): ele e colaborador, nao dado. Nao ha
contrato nosso a respeitar num cliente de biblioteca externa — quem o define e
a chamada que fazemos.
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from src.ai.services.chat_history import (
    MAXIMO_DE_MENSAGENS,
    TTL,
    HistoricoEmMemoria,
    HistoricoNoRedis,
)


AGORA = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

# Um `session_id` com cara de dado pessoal, que e exatamente o caso que a chave
# hasheada existe para cobrir: nada impede um front de usar o telefone da pessoa
# como identificador de sessao.
SESSAO = "5585999990000"


class RedisDeMentira:
    """Guarda o que mandarem, e conta o que foi chamado."""

    def __init__(self, inicial=None):
        self.valores = dict(inicial or {})
        self.setex_chamado = []
        self.set_chamado = []

    def get(self, chave):
        return self.valores.get(chave)

    def setex(self, chave, ttl, valor):
        self.setex_chamado.append((chave, ttl, valor))
        self.valores[chave] = valor

    def set(self, chave, valor):
        self.set_chamado.append((chave, valor))
        self.valores[chave] = valor


class RedisQueCai:
    def __init__(self, erro=None):
        self.erro = erro or ConnectionError("redis fora do ar")

    def get(self, chave):
        raise self.erro

    def setex(self, chave, ttl, valor):
        raise self.erro


class AChaveTests(unittest.TestCase):
    def test_o_session_id_nao_aparece_na_chave(self):
        """A regra da armadilha 56, aplicada antes de ser quebrada desta vez.

        Chave de Redis aparece em `KEYS`, em `MONITOR` e em qualquer dump. O
        `session_id` vem do cliente e `POST /chat` nao tem autenticacao — nada
        impede um front de usar o telefone como identificador.
        """
        chave = HistoricoNoRedis._chave(SESSAO)

        self.assertNotIn(SESSAO, chave)
        self.assertNotIn("5585", chave)

    def test_a_chave_tem_namespace_e_versao(self):
        """Prefixo pelo motivo do `embedding_key`: o Redis e compartilhado com
        o rate limit, o cache de embedding e o de entrega."""
        chave = HistoricoNoRedis._chave(SESSAO)

        self.assertTrue(chave.startswith("chat:hist:v1:"))

    def test_sessoes_diferentes_dao_chaves_diferentes(self):
        self.assertNotEqual(HistoricoNoRedis._chave("a"), HistoricoNoRedis._chave("b"))

    def test_a_caixa_do_session_id_IMPORTA(self):
        """`abc` e `ABC` sao duas sessoes, e nao uma.

        `ChatCache.message_digest` normaliza antes de hashear, e ali isso e
        certo — duas perguntas iguais com caixa diferente devem colidir. Aqui
        seria errado: colidir misturaria a conversa de duas pessoas.
        """
        self.assertNotEqual(HistoricoNoRedis._chave("abc"), HistoricoNoRedis._chave("ABC"))

    def test_a_mesma_sessao_da_a_mesma_chave(self):
        self.assertEqual(HistoricoNoRedis._chave(SESSAO), HistoricoNoRedis._chave(SESSAO))


class OTtlTests(unittest.TestCase):
    def test_a_escrita_usa_SETEX_e_nao_set(self):
        """O TTL na MESMA operacao.

        Separados (`set` + `expire`), um erro entre as duas deixaria a conversa
        do cliente no Redis para sempre — e o `maxmemory-policy volatile-lru`
        do compose so descarta chave COM expiracao, entao ela seria imune ao
        despejo tambem.
        """
        cliente = RedisDeMentira()
        HistoricoNoRedis(cliente).gravar(SESSAO, "oi", "ola", AGORA)

        self.assertEqual(len(cliente.setex_chamado), 1)
        self.assertEqual(cliente.set_chamado, [])

    def test_o_ttl_e_o_da_politica_em_segundos(self):
        cliente = RedisDeMentira()
        HistoricoNoRedis(cliente).gravar(SESSAO, "oi", "ola", AGORA)

        _, ttl, _ = cliente.setex_chamado[0]
        self.assertEqual(ttl, int(TTL.total_seconds()))

    def test_cada_turno_RENOVA_o_prazo(self):
        """Sessao ativa nao pode vencer no meio da conversa."""
        cliente = RedisDeMentira()
        historico = HistoricoNoRedis(cliente)

        historico.gravar(SESSAO, "oi", "ola", AGORA)
        historico.gravar(SESSAO, "e a picanha?", "temos", AGORA + timedelta(minutes=30))

        self.assertEqual(len(cliente.setex_chamado), 2)


class OConteudoTests(unittest.TestCase):
    def test_um_turno_grava_a_pergunta_e_a_resposta(self):
        cliente = RedisDeMentira()
        historico = HistoricoNoRedis(cliente)

        historico.gravar(SESSAO, "quero pizza", "temos calabresa", AGORA)

        self.assertEqual(
            historico.ler(SESSAO, AGORA),
            [
                {"role": "user", "content": "quero pizza"},
                {"role": "assistant", "content": "temos calabresa"},
            ],
        )

    def test_o_teto_de_mensagens_vale_tambem_aqui(self):
        """A POLITICA e a mesma nos dois backends.

        Se o teto valesse so na memoria, o prompt cresceria sem limite em
        producao — e o sintoma seria custo de token subindo sem nada no codigo
        ter mudado.
        """
        cliente = RedisDeMentira()
        historico = HistoricoNoRedis(cliente)

        for turno in range(30):
            historico.gravar(SESSAO, f"pergunta {turno}", f"resposta {turno}", AGORA)

        conversa = historico.ler(SESSAO, AGORA)

        self.assertEqual(len(conversa), MAXIMO_DE_MENSAGENS)
        self.assertEqual(conversa[-1], {"role": "assistant", "content": "resposta 29"})

    def test_uma_sessao_desconhecida_comeca_vazia(self):
        self.assertEqual(HistoricoNoRedis(RedisDeMentira()).ler("nunca-vista", AGORA), [])

    def test_acentuacao_sobrevive_a_ida_e_volta(self):
        """`ensure_ascii=False` no JSON. Sem isso a conversa volta escapada e o
        prompt do modelo recebe `\\u00e7` em vez de `ç`."""
        cliente = RedisDeMentira()
        historico = HistoricoNoRedis(cliente)

        historico.gravar(SESSAO, "tem porção de calabresa?", "temos, é ótima", AGORA)

        conversa = historico.ler(SESSAO, AGORA)
        self.assertEqual(conversa[0]["content"], "tem porção de calabresa?")
        self.assertEqual(conversa[1]["content"], "temos, é ótima")


class AFalhaQueNaoDerrubaTests(unittest.TestCase):
    """Nenhum caminho daqui pode levantar. O cache e desempenho; a pergunta nao."""

    def test_leitura_com_o_redis_fora_do_ar_devolve_vazio(self):
        conversa = HistoricoNoRedis(RedisQueCai()).ler(SESSAO, AGORA)

        self.assertEqual(conversa, [])

    def test_escrita_com_o_redis_fora_do_ar_nao_levanta(self):
        HistoricoNoRedis(RedisQueCai()).gravar(SESSAO, "oi", "ola", AGORA)

    def test_valor_em_formato_invalido_devolve_vazio(self):
        """Alguem gravou outra coisa naquela chave, ou o formato mudou sem a
        versao subir. Cache frio e a resposta certa; 500 nao."""
        chave = HistoricoNoRedis._chave(SESSAO)
        cliente = RedisDeMentira({chave: "isto nao e json"})

        self.assertEqual(HistoricoNoRedis(cliente).ler(SESSAO, AGORA), [])

    def test_json_valido_que_nao_e_lista_devolve_vazio(self):
        """`json.loads` aceitaria `{"a": 1}` sem reclamar, e o `[-20:]` logo
        adiante estouraria com `TypeError` — dentro do turno do cliente."""
        chave = HistoricoNoRedis._chave(SESSAO)
        cliente = RedisDeMentira({chave: json.dumps({"nao": "e lista"})})

        self.assertEqual(HistoricoNoRedis(cliente).ler(SESSAO, AGORA), [])

    def test_gravar_sobre_um_valor_invalido_nao_levanta(self):
        """O caminho composto: `gravar` LE antes de escrever.

        Sem a leitura defensiva, um valor estragado derrubaria a escrita
        tambem — e ai a sessao ficaria travada no lixo ate o TTL.
        """
        chave = HistoricoNoRedis._chave(SESSAO)
        cliente = RedisDeMentira({chave: "lixo"})
        historico = HistoricoNoRedis(cliente)

        historico.gravar(SESSAO, "oi", "ola", AGORA)

        self.assertEqual(
            historico.ler(SESSAO, AGORA),
            [
                {"role": "user", "content": "oi"},
                {"role": "assistant", "content": "ola"},
            ],
        )


class OsDoisBackendsConcordamTests(unittest.TestCase):
    """A mesma sequencia de chamadas produz a mesma conversa nos dois.

    E o teste que o plano da rodada 2 pedia, e o que impede os dois de
    divergirem em silencio: o de memoria e o que a suite exercita em todo lugar,
    e o de Redis e o que roda em producao.
    """

    def _sequencia(self, historico):
        historico.gravar("s1", "quero pizza", "temos calabresa", AGORA)
        historico.gravar("s1", "e a picanha?", "temos tambem", AGORA + timedelta(minutes=1))
        return historico.ler("s1", AGORA + timedelta(minutes=2))

    def test_a_conversa_e_identica(self):
        self.assertEqual(
            self._sequencia(HistoricoEmMemoria()),
            self._sequencia(HistoricoNoRedis(RedisDeMentira())),
        )

    def test_o_teto_corta_no_mesmo_lugar(self):
        def encher(historico):
            for turno in range(30):
                historico.gravar("s1", f"p{turno}", f"r{turno}", AGORA)
            return historico.ler("s1", AGORA)

        self.assertEqual(encher(HistoricoEmMemoria()), encher(HistoricoNoRedis(RedisDeMentira())))


if __name__ == "__main__":
    unittest.main()
