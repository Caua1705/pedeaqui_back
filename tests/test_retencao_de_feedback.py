"""Retenção de `ai_feedback` — o resíduo de LGPD que a anonimização não alcança.

`ai_feedback.user_message` guarda em texto puro o que a pessoa digitou para o
Rapi, e a tabela não tem `customer_id`: `POST /chat` não autentica, então não
há cliente na requisição para gravar ali. A exclusão de conta nunca alcançou
essas linhas — está nomeado como o maior resíduo em
`docs/lgpd-fase2-exclusao-de-conta.md`, seção 6.

O conserto é retenção por `created_at`, no container `limpeza` que já existe.
O que estes testes travam são as duas metades dele: a **conta** do corte e o
**DELETE** que ela alimenta.

A segunda metade é `-m db` de propósito. O que ela protege é o comportamento
do `DELETE ... WHERE created_at < %s` contra o Postgres — incluindo a
fronteira `<` versus `<=`, que num dublê seria só a repetição do operador que
o código já escreveu.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from src.models.ai_feedback_model import AIFeedback
from src.repositories.ai_feedback_repository import AIFeedbackRepository
from src.services.chat_service import (
    FEEDBACK_RETENTION_DAYS,
    feedback_retention_cutoff,
)
from src.utils.security import utcnow
from tests.fabricas_db import criar_restaurante


class TestCorteDeRetencao:
    """A conta, isolada. Sem banco: é aritmética de data."""

    def test_corte_fica_no_passado(self):
        agora = utcnow()

        assert feedback_retention_cutoff(agora) < agora

    def test_corte_e_a_janela_configurada(self):
        agora = utcnow()

        esperado = agora - timedelta(days=FEEDBACK_RETENTION_DAYS)

        assert feedback_retention_cutoff(agora) == esperado

    def test_feedback_de_ontem_esta_dentro_da_janela(self):
        agora = utcnow()
        ontem = agora - timedelta(days=1)

        assert ontem > feedback_retention_cutoff(agora)

    def test_feedback_do_ano_passado_esta_fora(self):
        agora = utcnow()
        ano_passado = agora - timedelta(days=365)

        assert ano_passado < feedback_retention_cutoff(agora)


@pytest.mark.db
class TestExpurgoDoFeedback:
    """O DELETE, contra o Postgres."""

    @staticmethod
    def _criar_feedback(db, restaurante, dias_atras: int) -> AIFeedback:
        """Uma linha com idade escolhida.

        `created_at` é atribuído à mão porque o `server_default` grava `now()`
        e todo cenário aqui precisa de linha velha.
        """
        feedback = AIFeedback(
            restaurant_id=restaurante.id,
            session_id=f"sessao-{dias_atras}",
            user_message="moro na rua das Flores, 200, apto 31",
            assistant_message="Temos entrega para essa região!",
            response_type="text",
            selected_product_ids=[],
            feedback="like",
            created_at=utcnow() - timedelta(days=dias_atras),
        )
        db.add(feedback)
        db.flush()
        return feedback

    def test_apaga_o_que_passou_da_janela(self, db):
        restaurante = criar_restaurante(db)
        velho = self._criar_feedback(db, restaurante, FEEDBACK_RETENTION_DAYS + 1)

        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 1
        assert db.get(AIFeedback, velho.id) is None

    def test_mantem_o_que_esta_dentro_da_janela(self, db):
        restaurante = criar_restaurante(db)
        recente = self._criar_feedback(db, restaurante, 1)

        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 0
        assert db.get(AIFeedback, recente.id) is not None

    def test_o_texto_da_pessoa_some_do_banco(self, db):
        """O que este trabalho existe para fazer, dito sem intermediário.

        Não é "a linha sumiu": é que a frase digitada não está mais em lugar
        nenhum da tabela. É o mesmo formato dos testes de anonimização —
        medir o dado da PESSOA, não a contagem de linhas.
        """
        restaurante = criar_restaurante(db)
        self._criar_feedback(db, restaurante, FEEDBACK_RETENTION_DAYS + 30)

        AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        textos = db.scalars(select(AIFeedback.user_message)).all()
        assert not [texto for texto in textos if "rua das Flores" in texto]

    def test_separa_velho_de_novo_na_mesma_passada(self, db):
        restaurante = criar_restaurante(db)
        velho = self._criar_feedback(db, restaurante, FEEDBACK_RETENTION_DAYS + 10)
        recente = self._criar_feedback(db, restaurante, 5)

        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 1
        assert db.get(AIFeedback, velho.id) is None
        assert db.get(AIFeedback, recente.id) is not None

    def test_banco_sem_feedback_velho_nao_e_erro(self, db):
        """O caso de todo dia: o container roda, não acha nada, sai em zero.

        Sem isto, um `rowcount` `None` (que o SQLAlchemy devolve em alguns
        caminhos) só apareceria em produção, no `print` do container.
        """
        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 0
