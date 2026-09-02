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
- **Nao vale para a voz.** O atendente de voz nao usa isto — o historico dele
  e da sessao do Realtime, do lado da OpenAI.
"""

import logging
from datetime import datetime, timedelta
from typing import TypedDict


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


# A instancia do processo. Trocada por `HistoricoNoRedis` quando houver
# `REDIS_URL` — ver o commit seguinte.
historico = HistoricoEmMemoria()
