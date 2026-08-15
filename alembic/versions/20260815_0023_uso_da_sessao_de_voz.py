"""o uso de tokens de cada sessao de voz

Revision ID: 20260815_0023
Revises: 20260815_0022
Create Date: 2026-08-15

## Por que estas colunas

A 0021 registra que uma sessao EXISTIU — quem pediu, para qual restaurante,
quando, e ate quando podia durar. Isso responde "quantas sessoes", e nao
responde "quanto custou": duas conversas de dois minutos podem diferir dez
vezes em consumo, porque audio de entrada, audio de saida e texto tem precos
diferentes e nenhum deles e proporcional ao relogio.

O numero existe e hoje se perde. A OpenAI manda o consumo no evento
`response.done` da Realtime, mas esse evento chega ao NAVEGADOR: o backend nao
participa da conversa e nunca o ve. Estas colunas sao onde o navegador
deposita o total ao encerrar, por `POST /voice/session/{id}/ended`.

## Sao NUMEROS, e so numeros

Nada de texto da conversa, nada de transcricao. O que se quer aqui e
conciliacao de fatura, e para isso o conteudo nao serve para nada — enquanto
guardar audio ou transcricao de cliente criaria uma base de dado pessoal com
retencao que ninguem definiu.

## O que cada coluna guarda

Os quatro contadores de token espelham o `usage` do `response.done`, com o
mesmo nome que a OpenAI usa, para o front nao ter que traduzir:

    input_audio_tokens    usage.input_token_details.audio_tokens
    input_text_tokens     usage.input_token_details.text_tokens
    output_audio_tokens   usage.output_token_details.audio_tokens
    output_text_tokens    usage.output_token_details.text_tokens
    cached_tokens         usage.input_token_details.cached_tokens

`cached_tokens` e SUBCONJUNTO da entrada, nao uma quinta parcela: e a fatia dos
tokens de entrada que veio do cache e por isso saiu mais barata. Somar os
cinco conta o cache duas vezes e infla o total. Quem soma, soma os quatro
primeiros — e por isso nao existe coluna de "total": ela viraria a primeira
soma errada gravada no banco.

`duration_seconds` e a duracao de AUDIO medida pelo navegador, do inicio da
conversa ate o encerramento. O servidor tem uma versao propria de graca
(`ended_at - issued_at`), e ela mede outra coisa: a credencial e emitida antes
de o microfone abrir, e o encerramento so e reportado depois. Serve de
grandeza, nao de medida.

## Nulo nao e zero

Todas nascem NULL e continuam NULL quando ninguem reporta — sessao que encerra
sem mandar numero nenhum continua encerrando normalmente, e isso e requisito,
nao tolerancia: o navegador pode ser fechado no meio, e o aviso de fim ja e
best-effort hoje.

NULL entao significa "nao reportado" e 0 significa "reportou zero" (sessao em
que o cliente nao chegou a falar). A media de duracao do relatorio ignora as
NULL, o que e a leitura certa: media sobre as que sabemos.

## INTEGER, e por que isso importa no schema da rota

INTEGER do Postgres vai ate 2.147.483.647. A rota nao tem autenticacao — quem
tem o `sessao_id` reporta o que quiser — entao o schema do corpo limita cada
campo a 2.000.000.000. Sem esse teto, um numero maior que o INTEGER nao vira
validacao: vira erro do driver no INSERT, ou seja, 500.

Pelo mesmo motivo o numero daqui e ESTIMATIVA de conciliacao, e nao verdade
contabil. Quem cobra e a fatura da OpenAI; isto e o que permite dividir a
fatura por restaurante e ver quem consome o que.

## Sem indice novo

O relatorio (`scripts/voice_usage_report.py`) roda sob demanda, agrega uma
tabela que cresce por sessao emitida e nao esta no caminho de nenhuma
requisicao. Indice para ele hoje seria custo de escrita em toda emissao para
servir uma consulta que ninguem faz com pressa.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260815_0023"
down_revision: Union[str, None] = "20260815_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUNAS = (
    "input_audio_tokens",
    "input_text_tokens",
    "output_audio_tokens",
    "output_text_tokens",
    "cached_tokens",
    "duration_seconds",
)


def upgrade() -> None:
    for coluna in COLUNAS:
        op.add_column(
            "ai_voice_sessions",
            sa.Column(coluna, sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for coluna in reversed(COLUNAS):
        op.drop_column("ai_voice_sessions", coluna)
