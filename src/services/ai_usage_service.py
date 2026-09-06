"""Grava o que cada chamada de IA custou, e soma isso por restaurante.

## A pergunta

"A comissao paga a conta do assistente?" Ate esta frente, ninguem sabia — a
fatura da OpenAI chega com um numero so para a plataforma inteira. A tabela e
`ai_usage_events` (revisao 20260902_0044); os precos e a conta estao em
`src/ai/custo.py`.

## MEDIR NAO PODE DERRUBAR O QUE ESTA SENDO MEDIDO

As duas funcoes de gravacao engolem qualquer falha e seguem. O cliente ja tem
a resposta dele quando elas rodam, e um `/chat` que responde 500 porque a
contabilidade nao gravou seria trocar a operacao pela planilha.

O preco disso e conhecido e aceito: chamada perdida e chamada que nao aparece
no relatorio. Por isso a falha sai no log com nome proprio
(`custo_nao_gravado=true`) — o buraco fica visivel em vez de virar um numero
menor sem explicacao.

## Quem commita

`registrar_texto` commita: no `/chat` ela e a UNICA escrita do turno.

Houve uma segunda gravacao aqui, `registrar_voz`, que NAO commitava — ela
pegava carona na transacao do encerramento da sessao falada. Saiu em
06/09/2026 com o resto do assistente de voz. As linhas que ela gravou
continuam na tabela e continuam somando no relatorio; ver
`src/models/ai_usage_event_model.py`.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.ai.custo import CASAS_DO_CUSTO, custo_de_texto
from src.ai.services.chat_llm_service import UsoDoModelo
from src.models.ai_usage_event_model import AIUsageEvent, SURFACE_TEXT
from src.repositories.ai_usage_repository import AIUsageRepository
from src.schemas.ai_usage_schema import AIUsageByRestaurant, AIUsageReportResponse


logger = logging.getLogger("uvicorn.error")


class AIUsageService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AIUsageRepository(db)

    # -- gravacao -----------------------------------------------------------

    def registrar_texto(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        uso: UsoDoModelo | None,
    ) -> None:
        """Uma linha por turno do `/chat`. COMMITA.

        Commita porque no `/chat` esta e a unica escrita do turno: a rota le
        cardapio, chama o modelo e responde, sem tocar em mais nada. Deixar a
        transacao aberta para o `get_db` fechar sem commit descartaria a
        linha.

        `uso` nulo e o caso normal de nao gravar nada: a OpenAI nao mandou
        `usage`, ou a resposta veio cortada e a excecao levou a resposta crua
        junto (ver `_resgatar_resposta_cortada`). Gravar zero ali seria uma
        chamada de graca no relatorio.
        """
        if uso is None:
            return

        try:
            self.repository.add(
                AIUsageEvent(
                    restaurant_id=restaurant_id,
                    branch_id=branch_id,
                    surface=SURFACE_TEXT,
                    model=uso.modelo,
                    input_tokens=uso.entrada,
                    cached_input_tokens=uso.entrada_em_cache,
                    output_tokens=uso.saida,
                    cost_usd=custo_de_texto(
                        modelo=uso.modelo,
                        entrada=uso.entrada,
                        entrada_em_cache=uso.entrada_em_cache,
                        saida=uso.saida,
                    ),
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.warning(
                "[AI custo] custo_nao_gravado=true | superficie=texto "
                "| restaurant_id=%s",
                restaurant_id,
                exc_info=True,
            )

    # -- leitura ------------------------------------------------------------

    def custo_por_restaurante(
        self,
        desde: datetime,
        ate: datetime,
        restaurant_id: uuid.UUID | None = None,
    ) -> AIUsageReportResponse:
        """O rateio da fatura por restaurante, na janela `[desde, ate)`.

        O dono da verdade continua sendo a fatura da OpenAI — isto e o rateio
        dela, e a diferenca aparece em `chamadas_sem_preco`: chamada de modelo
        que nao esta em `src/ai/custo.py` soma token e nao soma dinheiro.
        """
        linhas = self.repository.custo_por_restaurante(
            desde=desde, ate=ate, restaurant_id=restaurant_id
        )
        restaurantes = [
            AIUsageByRestaurant(
                restaurant_id=linha.restaurant_id,
                restaurant_name=linha.restaurante,
                calls=linha.chamadas,
                text_calls=linha.chamadas_texto,
                # HISTORICO desde 06/09/2026: a voz saiu do projeto e estes
                # dois campos param de crescer. Continuam na resposta porque
                # sem eles `calls` deixaria de fechar com `text_calls` em toda
                # janela que alcance agosto ou o comeco de setembro de 2026.
                voice_calls=linha.chamadas_voz,
                cost_usd=linha.custo_usd,
                text_cost_usd=linha.custo_texto_usd,
                voice_cost_usd=linha.custo_voz_usd,
                input_tokens=linha.tokens_entrada or 0,
                output_tokens=linha.tokens_saida or 0,
                calls_without_price=linha.sem_preco,
            )
            for linha in linhas
        ]
        total = sum((item.cost_usd for item in restaurantes), Decimal("0"))
        return AIUsageReportResponse(
            start=desde,
            end=ate,
            restaurants=restaurantes,
            total_cost_usd=total.quantize(CASAS_DO_CUSTO),
        )
