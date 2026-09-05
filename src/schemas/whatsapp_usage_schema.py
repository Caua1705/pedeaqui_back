"""Quantos avisos de WhatsApp sairam, por restaurante e por periodo.

**Este schema NAO aparece no `/openapi.json`**, pelo mesmo motivo de
`ai_usage_schema`: a rota que o usa (`GET /internal/whatsapp-usage`) e
publicada com `include_in_schema=False`. O cartao da Meta e da PLATAFORMA e e
cobrado por conta, nao por loja — quanto cada restaurante consome dele e
numero de quem opera a plataforma, e nao do lojista (armadilhas 16 e 17).

## Sem dinheiro aqui, e a ausencia e deliberada

`ai_usage_schema` traz `cost_usd` porque o preco de um token e publicado e
igual para todo mundo. O do WhatsApp nao e:

- desde 11/2024 a Meta cobra **por mensagem**, por CATEGORIA de template, e o
  preco varia por pais;
- **template de utilidade entregue dentro da janela de atendimento de 24h e
  gratuito**, e os quatro avisos daqui sao todos de utilidade.

Ou seja: para saber quanto cada linha custou seria preciso saber se a janela
daquele cliente estava aberta no INSTANTE do envio — e isso nao esta gravado.
`whatsapp_contact_windows` guarda a janela de AGORA, nao a de entao. Inventar
um preco por mensagem aqui produziria um numero que soma e esta errado, que e
pior que numero nenhum (o criterio de `custo_usd` NULO em `src/ai/custo.py`).

O que fica: **a contagem, que e exata**, e o teto do que pode ter sido cobrado.
Se um dia o numero exato importar, o que falta e uma coluna dizendo se a janela
estava aberta no envio — decisao de schema, com migracao, e nao de leitura.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class WhatsAppUsageByKind(BaseModel):
    """Um tipo de aviso, num restaurante, no periodo."""

    kind: str
    templates: int
    sent: int


class WhatsAppUsageByRestaurant(BaseModel):
    """Quantos avisos UM restaurante mandou no periodo.

    `templates` e o que a plataforma decidiu mandar; `sent` e o que chegou a
    existir na Meta (`wamid` preenchido). A diferenca entre os dois nao e uma
    coisa so, e por isso ela vem quebrada em duas:

    - `refused_here` — recusa NOSSA, antes de qualquer chamada: telefone que
      nao vira E.164, texto livre fora da janela de 24h. Nao custou nada e nao
      indica problema com a Meta;
    - `refused_by_meta` — a chamada saiu e ela respondeu com erro. Numero
      grande aqui e chamado a abrir, e o `error_code` da linha e o que se cita
      nele.
    """

    restaurant_id: uuid.UUID
    restaurant_name: str

    templates: int
    sent: int
    refused_here: int
    refused_by_meta: int

    by_kind: list[WhatsAppUsageByKind]


class WhatsAppUsageReportResponse(BaseModel):
    """O periodo inteiro, um bloco por restaurante.

    `start` e `end` sao a janela `[start, end)` de fato consultada, e nao o que
    o chamador digitou: quem pede "ate 31/08" quer o dia 31 inteiro, e o que vai
    para o `WHERE` e 01/09 as 00h. Mesma escolha de `AIUsageReportResponse`, e
    pelo mesmo motivo — evitar a conversa de "o relatorio comeu o ultimo dia".
    """

    start: datetime
    end: datetime
    restaurants: list[WhatsAppUsageByRestaurant]
    total_templates: int
    total_sent: int
