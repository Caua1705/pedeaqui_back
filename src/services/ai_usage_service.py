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

As duas nao fazem a mesma coisa, e a diferenca esta no docstring de cada uma.
Em resumo: no `/chat` esta e a UNICA escrita do turno, entao ela commita; na
voz ela pega carona na transacao do encerramento, que ja existe e ja commita.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.ai.custo import CASAS_DO_CUSTO, custo_de_texto, custo_de_voz
from src.ai.services.chat_llm_service import UsoDoModelo
from src.core.config import settings
from src.models.ai_usage_event_model import AIUsageEvent, SURFACE_TEXT, SURFACE_VOICE
from src.models.ai_voice_session_model import AIVoiceSession
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

    def registrar_voz(self, sessao: AIVoiceSession) -> None:
        """Uma linha por SESSAO de voz. NAO commita — quem commita e `encerrar`.

        Nao commita porque ela roda dentro de `VoiceSessionService.encerrar`,
        que ja abre e fecha a transacao do encerramento. Um commit aqui
        gravaria o custo de uma sessao que ainda pode nao ter sido encerrada.

        **Por sessao, e nao por turno**, porque nao ha turno visivel daqui: o
        audio vai do navegador direto para a OpenAI e o backend nao ve os
        eventos da conversa. O numero chega uma vez so, no aviso de fim.

        **Idempotente**, e isso e requisito: o aviso de fim vai com
        `keepalive` e e reenviado quando a aba fecha. Sem a releitura por
        `voice_session_id` (que o UNIQUE parcial do banco garante ser unica),
        a segunda chegada dobraria o custo daquela conversa. A segunda chegada
        CORRIGE a primeira, e nao o contrario: ela costuma trazer o numero
        mais completo.

        O modelo gravado e o `VOICE_MODEL` de AGORA, e nao o de quando a
        credencial foi emitida — a sessao nao guarda essa informacao. Trocar
        `VOICE_MODEL` no meio de uma conversa carimbaria a linha com o modelo
        novo; e uma janela de minutos, e o conserto seria uma coluna em
        `ai_voice_sessions` que so serviria para isso.
        """
        entrada_audio = sessao.input_audio_tokens or 0
        entrada_texto = sessao.input_text_tokens or 0
        saida_audio = sessao.output_audio_tokens or 0
        saida_texto = sessao.output_text_tokens or 0
        if not (entrada_audio or entrada_texto or saida_audio or saida_texto):
            # Sessao que encerrou sem reportar numero nenhum. NULL nao e zero
            # (ver a revisao 0023): gravar uma linha de custo zero aqui
            # inventaria uma conversa de graca que ninguem mediu.
            return

        modelo = settings.VOICE_MODEL
        em_cache = sessao.cached_tokens or 0
        custo = custo_de_voz(
            modelo=modelo,
            entrada_audio=entrada_audio,
            entrada_texto=entrada_texto,
            entrada_em_cache=em_cache,
            saida_audio=saida_audio,
            saida_texto=saida_texto,
        )

        try:
            evento = self.repository.get_by_voice_session(sessao.id)
            if evento is None:
                self.repository.add(
                    AIUsageEvent(
                        restaurant_id=sessao.restaurant_id,
                        surface=SURFACE_VOICE,
                        voice_session_id=sessao.id,
                        model=modelo,
                        input_tokens=entrada_audio + entrada_texto,
                        cached_input_tokens=em_cache,
                        output_tokens=saida_audio + saida_texto,
                        cost_usd=custo,
                    )
                )
                return

            evento.model = modelo
            evento.input_tokens = entrada_audio + entrada_texto
            evento.cached_input_tokens = em_cache
            evento.output_tokens = saida_audio + saida_texto
            evento.cost_usd = custo
        except Exception:
            logger.warning(
                "[AI custo] custo_nao_gravado=true | superficie=voz | sessao_id=%s",
                sessao.id,
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
