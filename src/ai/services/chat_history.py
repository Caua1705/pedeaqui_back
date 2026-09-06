"""O historico da conversa do Rapi — os ultimos turnos daquela sessao.

Ate 04/09/2026 isto era um dicionario de modulo dentro de `chat_service.py`, e
funcionava porque o `Dockerfile` sobe **um** worker. O que ele ja custava:
**todo deploy zera a conversa de quem esta no meio de um pedido.** O que ele
custaria no dia de escalar: `--workers 2` ou uma segunda replica, e a conversa
passa a existir num worker e nao no outro — o cliente alternando entre lembrar
e esquecer conforme o balanceamento, **sem erro, sem log, sem nada**.

Este arquivo separa a POLITICA (quantos turnos, por quanto tempo) do LUGAR onde
a conversa mora. O `ChatService` deixou de saber onde ela esta.

## A interface e de dois metodos, e isso foi escolha

    ler(session_id, agora)                      -> os ultimos turnos
    gravar(session_id, pergunta, resposta, agora)

A limpeza das sessoes vencidas **nao** e um terceiro metodo: ela acontece
dentro do `ler`, com o MESMO `agora` que o turno inteiro usa. Como terceiro
metodo ela obrigaria o backend de Redis a ter uma funcao vazia — e funcao vazia
numa interface e o convite para alguem "consertar" chamando-a de outro lugar.

`agora` entra de fora e nao sai de um relogio interno pelo motivo da armadilha
51: duas leituras do relogio no mesmo turno podem cair em lados diferentes da
virada, e ai a sessao seria limpa e regravada no mesmo pedido.

## O que ele NAO resolve, e e melhor dizer

- **Nao deduplica conversa entre abas.** O `session_id` continua vindo do
  cliente; duas abas com ids diferentes sao duas conversas. E o desenho.
- **Nao sobrevive a reinicio do Redis**, e nao deve: historico de uma hora nao
  e dado duravel, e persisti-lo aumentaria a superficie de LGPD sem nenhum
  ganho para o cliente.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import TypedDict

from src.ai.services.chat_cache import cliente_redis


logger = logging.getLogger("uvicorn.error")


# Dez turnos (pergunta + resposta). O limite existe para o prompt nao crescer
# sem teto: cada turno vai inteiro para o modelo, e conversa longa custa token
# em TODA mensagem seguinte, nao so na ultima.
MAXIMO_DE_MENSAGENS = 20

# Depois disto a sessao e outra conversa. Uma hora e o tempo em que faz sentido
# lembrar do que a pessoa disse — passou disso, lembrar seria mais estranho que
# esquecer.
TTL = timedelta(hours=1)


class Mensagem(TypedDict):
    role: str
    content: str


class _Sessao(TypedDict):
    messages: list[Mensagem]
    last_interaction: datetime


class HistoricoEmMemoria:
    """Um dicionario do processo. O que existia antes, com nome e limites.

    **Vive e morre com o processo**, e as duas consequencias sao as mesmas de
    sempre: zera a cada deploy, e nao e compartilhado entre workers.

    Continua sendo o backend certo em dois casos — sem `REDIS_URL` (a bancada
    local, `bancada.bat`) e na suite de teste, que nao tem Redis e nao deve
    ter.
    """

    def __init__(
        self,
        maximo_de_mensagens: int = MAXIMO_DE_MENSAGENS,
        ttl: timedelta = TTL,
    ) -> None:
        self.maximo_de_mensagens = maximo_de_mensagens
        self.ttl = ttl
        self._sessoes: dict[str, _Sessao] = {}

    def ler(self, session_id: str, agora: datetime) -> list[Mensagem]:
        self._descartar_vencidas(agora)
        sessao = self._sessoes.get(session_id)
        return sessao["messages"][-self.maximo_de_mensagens :] if sessao else []

    def gravar(
        self,
        session_id: str,
        pergunta: str,
        resposta: str,
        agora: datetime,
    ) -> None:
        sessao = self._sessoes.setdefault(
            session_id,
            {"messages": [], "last_interaction": agora},
        )
        sessao["messages"].extend(
            [
                {"role": "user", "content": pergunta},
                {"role": "assistant", "content": resposta},
            ]
        )
        sessao["messages"] = sessao["messages"][-self.maximo_de_mensagens :]
        sessao["last_interaction"] = agora

    def _descartar_vencidas(self, agora: datetime) -> None:
        """A varredura que o Redis nao vai precisar.

        Ela so roda quando ALGUEM CONVERSA, e essa e a limitacao que o TTL do
        Redis corrige de graca: sessao parada de madrugada sobrevive aqui ate a
        primeira mensagem da manha seguinte. Em memoria isso e aceitavel —
        o processo reinicia e leva tudo junto.
        """
        vencidas = [
            session_id
            for session_id, sessao in self._sessoes.items()
            if agora - sessao["last_interaction"] > self.ttl
        ]
        for session_id in vencidas:
            del self._sessoes[session_id]

    def esquecer_tudo(self) -> None:
        """So para teste. A suite precisa comecar cada caso sem conversa alheia."""
        self._sessoes.clear()


# A VERSAO DO FORMATO da chave. Sobe quando o que esta guardado mudar de forma
# — hoje uma lista JSON de `{role, content}`. Sem ela, um deploy que mudasse o
# formato leria o velho como se fosse o novo, e o erro apareceria no meio de uma
# conversa de cliente.
_VERSAO_DA_CHAVE = "v1"


class HistoricoNoRedis:
    """A conversa compartilhada entre workers, deploys e replicas.

    ## A chave vai HASHEADA, e nao e paranoia

    `session_id` e escolhido pelo CLIENTE (`ChatRequest.session_id`,
    `min_length=1`) e `POST /chat` nao tem autenticacao nenhuma. Nada impede um
    front de usar o telefone ou o e-mail da pessoa como identificador de sessao
    — e chave de Redis aparece em `KEYS`, em `SCAN`, em `MONITOR` e em qualquer
    dump.

    E a mesma regra de `ChatCache.embedding_key` e de `_cache_key` da estimativa
    de entrega, e a armadilha 56 registra as duas vezes em que ela foi quebrada
    por quem nao estava lendo o docstring do vizinho.

    **O digest e do `session_id` CRU, sem normalizar.**
    `ChatCache.message_digest` normaliza antes de hashear, e ali isso e certo
    (duas perguntas iguais com caixa diferente devem colidir). Aqui seria
    errado: `abc` e `ABC` sao duas sessoes, e faze-las colidir misturaria a
    conversa de duas pessoas.

    ## O VALOR e texto em claro, e nao ha como nao ser

    Este e o unico item do Redis para o qual nao existe digest: o modelo precisa
    LER o que a pessoa escreveu. Por isso as tres defesas em volta dele:

    1. **TTL no proprio Redis** — `SETEX`, renovado a cada turno. Nao e a
       varredura do backend de memoria, que so roda quando alguem conversa:
       sessao parada de madrugada expira sozinha, sem depender de trafego.
    2. **Nenhuma persistencia.** Conferido em producao em 04/09/2026:
       `aof_enabled=0` e RDB desligado. Se isso mudar, o texto do cliente passa
       a existir em disco alem do TTL — e este backend precisa de outra conversa
       ANTES, nao depois.
    3. **A chave nao liga a pessoa.** O digest do `session_id` nao identifica
       ninguem, entao nao ha o que a exclusao de conta precise alcancar aqui.

    ## Falha do Redis NAO derruba o turno

    Nenhum caminho daqui levanta. Leitura que falha devolve historico VAZIO — o
    Rapi responde sem contexto, que e uma resposta pior e nao um 500. Escrita
    que falha e um turno que nao entrou no historico, e o proximo segue.

    **Nao ha nivel de memoria por baixo**, e e escolha: ele traria de volta o
    "depende de qual worker atendeu" que este backend existe para eliminar, num
    caminho que so roda quando o Redis cai — ou seja, raramente exercitado e
    facil de estar errado sem ninguem saber.
    """

    def __init__(
        self,
        cliente,
        maximo_de_mensagens: int = MAXIMO_DE_MENSAGENS,
        ttl: timedelta = TTL,
    ) -> None:
        self._cliente = cliente
        self.maximo_de_mensagens = maximo_de_mensagens
        self.ttl = ttl

    def ler(self, session_id: str, agora: datetime) -> list[Mensagem]:
        """`agora` nao e usado, e a assinatura o mantem de proposito.

        Quem expira aqui e o Redis. Tirar o parametro faria os dois backends
        terem interfaces diferentes, e o `ChatService` teria que saber qual
        deles esta atendendo — que e exatamente o que este arquivo desfaz.
        """
        try:
            bruto = self._cliente.get(self._chave(session_id))
        except Exception:
            logger.warning("[AI historico] redis_read_failed=true")
            return []
        if bruto is None:
            return []
        try:
            mensagens = json.loads(bruto)
        except (TypeError, ValueError):
            # Valor num formato que nao entendemos. NAO e cache frio: alguem
            # gravou outra coisa naquela chave, ou o formato mudou sem a versao
            # subir. Sai com log proprio para nao se esconder entre as sessoes
            # novas.
            logger.warning("[AI historico] redis_formato_invalido=true")
            return []
        if not isinstance(mensagens, list):
            logger.warning("[AI historico] redis_formato_invalido=true")
            return []
        return mensagens[-self.maximo_de_mensagens :]

    def gravar(
        self,
        session_id: str,
        pergunta: str,
        resposta: str,
        agora: datetime,
    ) -> None:
        mensagens = self.ler(session_id, agora)
        mensagens.extend(
            [
                {"role": "user", "content": pergunta},
                {"role": "assistant", "content": resposta},
            ]
        )
        mensagens = mensagens[-self.maximo_de_mensagens :]
        try:
            # `setex` e nao `set` + `expire`: o TTL entra na MESMA operacao.
            # Separados, um erro entre as duas deixaria a conversa do cliente no
            # Redis para sempre — e o `maxmemory-policy volatile-lru` do compose
            # so descarta chave COM expiracao, entao ela seria imune ao despejo
            # tambem.
            self._cliente.setex(
                self._chave(session_id),
                int(self.ttl.total_seconds()),
                json.dumps(mensagens, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            logger.warning("[AI historico] redis_write_failed=true")

    @staticmethod
    def _chave(session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return f"chat:hist:{_VERSAO_DA_CHAVE}:{digest}"

    def esquecer_tudo(self) -> None:
        """So para teste, e ele NAO varre o Redis.

        Um `SCAN` + `DEL` por prefixo num Redis compartilhado apagaria a
        conversa de quem estiver falando com o Rapi agora. A suite roda sem
        `REDIS_URL` e cai no backend de memoria; se um dia rodar com, e a
        fixture que precisa mudar, nao este metodo.
        """
        logger.warning("[AI historico] esquecer_tudo ignorado: backend e o Redis")


class Historico:
    """A fachada. Escolhe o backend na PRIMEIRA CHAMADA, nao no import.

    Pelo mesmo motivo de `cliente_redis` e de `get_engine`: construido no
    import, ele congelaria `settings` no instante em que qualquer modulo de
    `src` fosse importado — e a suite precisa poder apontar para outra
    configuracao depois disso.
    """

    def __init__(self) -> None:
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            cliente = cliente_redis()
            self._backend = (
                HistoricoNoRedis(cliente) if cliente is not None else HistoricoEmMemoria()
            )
            logger.info(
                "[AI historico] backend=%s",
                "redis" if cliente is not None else "memoria",
            )
        return self._backend

    def ler(self, session_id: str, agora: datetime) -> list[Mensagem]:
        return self.backend.ler(session_id, agora)

    def gravar(self, session_id: str, pergunta: str, resposta: str, agora: datetime) -> None:
        self.backend.gravar(session_id, pergunta, resposta, agora)

    def esquecer_tudo(self) -> None:
        self.backend.esquecer_tudo()


historico = Historico()
