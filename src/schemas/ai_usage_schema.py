"""O rateio do custo de IA por restaurante.

**Este schema NAO aparece no `/openapi.json`**, e isso e escolha: a rota que o
usa (`GET /internal/ai-usage`) e publicada com `include_in_schema=False`. O
painel consome o documento (armadilha 16), e quanto o assistente custa a
plataforma nao e assunto do lojista — e a mesma razao de
`platform_commission_percent` nao aparecer em schema nenhum do painel
(armadilha 17). A diferenca e que la o risco e o lojista EDITAR quanto paga;
aqui e ele saber a margem antes de negociar a comissao.

DINHEIRO EM `Decimal`, e nao em `float`. A armadilha 34 registra que
`CreateOrderResponse` e `OrderDetailResponse` misturam os dois e que a
correcao depende de uma decisao junto com o app do cliente — e manda nao
converter schema isolado enquanto isso. Nada aqui contradiz aquilo: esta
resposta e nova, nao tem consumidor, nao entra no documento e o valor tipico
e US$ 0,0005. Em `float` de duas casas, todo turno do `/chat` sairia como
zero.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class AIUsageByRestaurant(BaseModel):
    """Quanto UM restaurante consumiu no periodo.

    `calls_without_price` e a coluna que impede a leitura ingenua do total:
    ela conta as chamadas cujo modelo nao estava em `src/ai/custo.py`. Elas
    somam token e nao somam dinheiro, entao numero grande aqui significa
    "falta preco na tabela", e nao "custou pouco".
    """

    restaurant_id: uuid.UUID
    restaurant_name: str

    calls: int
    text_calls: int
    voice_calls: int

    cost_usd: Decimal
    text_cost_usd: Decimal
    voice_cost_usd: Decimal

    # Somados das duas superficies. `cached_input_tokens` NAO aparece aqui de
    # proposito: ele e subconjunto da entrada, e uma terceira coluna ao lado
    # das outras duas convidaria a soma que conta o cache duas vezes.
    input_tokens: int
    output_tokens: int

    calls_without_price: int


class AIUsageReportResponse(BaseModel):
    """O periodo inteiro, um bloco por restaurante.

    `start` e `end` sao a janela `[start, end)` de fato consultada, e nao o
    que o chamador digitou: quem pede "ate 31/08" quer o dia 31 inteiro, e o
    que vai para o `WHERE` e 01/09 as 00h. Devolver os dois evita a conversa
    de "o relatorio comeu o ultimo dia".
    """

    start: datetime
    end: datetime
    restaurants: list[AIUsageByRestaurant]
    total_cost_usd: Decimal
