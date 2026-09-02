from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from src.models.ai_feedback_model import AIFeedback
from src.schemas.ai_feedback_schema import AIFeedbackRequest


class AIFeedbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, feedback: AIFeedbackRequest) -> None:
        """Grava o voto, ou troca o que ja existia para aquela mensagem.

        NAO commita, ao contrario de antes: quem commita e o service, sempre
        (regra de camadas do projeto). Enquanto o commit morava aqui, o
        service devolvia `success=True` sobre uma transacao que ele nao
        controlava — e um erro depois desta linha deixava o voto gravado com
        a resposta dizendo o contrario.
        """
        existing_feedback = self.db.scalar(
            select(AIFeedback).where(
                AIFeedback.session_id == feedback.session_id,
                AIFeedback.assistant_message == feedback.assistant_message,
            )
        )

        if existing_feedback:
            # Mesmo voto de novo: nada muda. Sair aqui evita um UPDATE que
            # so reescreveria o valor que ja esta la.
            if existing_feedback.feedback == feedback.feedback:
                return

            existing_feedback.feedback = feedback.feedback
            self.db.flush()
            return

        self.db.add(
            AIFeedback(
                restaurant_id=feedback.restaurant_id,
                session_id=feedback.session_id,
                user_message=feedback.user_message,
                assistant_message=feedback.assistant_message,
                response_type=feedback.response_type,
                selected_product_ids=feedback.selected_product_ids,
                feedback=feedback.feedback,
            )
        )
        self.db.flush()

    def delete_created_before(self, cutoff: datetime) -> int:
        """Apaga o feedback velho — E o que nao tem data. Devolve quantos sairam.

        Corta por `created_at` e nao por vencimento porque a linha nao tem
        vencimento: ela vale enquanto servir de amostra de qualidade das
        respostas do Rapi. Quem sabe ate quando e
        `chat_service.feedback_retention_cutoff`, e e de la que o `cutoff`
        tem que vir — o repositorio so consulta.

        ## O `IS NULL`, e por que ele nao e paranoia

        `ai_feedback.created_at` **aceita nulo** — e a unica das seis tabelas
        com varredura de retencao em que isso acontece (as outras cinco sao
        `NOT NULL`; conferido no `information_schema`). E `created_at < cutoff`
        com nulo **nao e falso, e NULO**: a linha nunca casava, e o texto em
        claro que a pessoa digitou para o Rapi ficava para sempre.

        Sem 500, sem log, sem tela onde isso aparecesse. Era o pior modo de
        falha do levantamento da armadilha 50 — pior que os que derrubam a
        rota, porque ninguem percebe.

        **Apagar e a escolha certa, e a alternativa deixa isso claro.** A linha
        sem data nao tem como provar que e recente, e o que ela guarda e dado
        pessoal em texto puro. Entre manter dado de idade desconhecida e perder
        uma amostra de qualidade do Rapi, quem decide e a LGPD — e o que se
        perde e uma linha que ja estava fora do inventario.

        O nulo nao vem daqui: a coluna tem `DEFAULT now()` e o model sempre a
        omite. Ele vem de INSERT feito por fora (armadilha 33). Quando o schema
        for alinhado (`alembic/preparadas/`), este `or_` vira redundante — e
        pode ficar, porque custa nada e o dia em que alguem afrouxar a coluna
        de novo nao avisa ninguem.
        """
        resultado = self.db.execute(
            delete(AIFeedback).where(
                or_(
                    AIFeedback.created_at < cutoff,
                    AIFeedback.created_at.is_(None),
                )
            )
        )
        return resultado.rowcount or 0
