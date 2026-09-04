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
from sqlalchemy import select, text

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
        recente = TestExpurgoDoFeedback._criar_feedback(db, restaurante, 1)

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


@pytest.mark.db
class TestALinhaSemData:
    """`created_at` NULO — a linha que a retencao nunca alcancava.

    `ai_feedback.created_at` e a UNICA das seis tabelas com varredura de
    retencao cujo `created_at` aceita nulo (conferido no
    `information_schema`: as outras cinco sao NOT NULL). E `created_at <
    cutoff` com nulo nao e falso — e NULO —, entao a linha nunca casava e o
    texto em claro do cliente ficava para sempre.

    Nao ha 500, nao ha log, nao ha tela onde isso apareca. E o pior modo de
    falha desta classe inteira, e por isso ele veio antes dos que dao 500.

    O nulo nao vem do ORM — a coluna tem `DEFAULT now()` e o model sempre a
    omite, deixando o banco preencher. Ele vem de INSERT feito por fora, que e
    a mesma origem de todas as 15 (armadilha 33).

    **DESDE 05/09/2026 O NULO NOVO NAO ENTRA MAIS**, e este teste teve que
    mudar por causa disso. A revisao `20260905_0055` (etapa 1 do alinhamento)
    criou `ck_ai_feedback_created_at_nao_nula` como `CHECK ... NOT VALID`: ela
    nao reparou as linhas antigas, mas recusa toda linha nova — inclusive o
    INSERT cru deste teste.

    Entao o teste passou a ENCENAR a linha legada, derrubando a restricao,
    inserindo e recriando `NOT VALID` (recriar funciona sobre a linha nula, que
    e a metade menos obvia do que `NOT VALID` significa). O que ele guarda
    continua valendo e continua importando: o `OR created_at IS NULL` do
    expurgo existe para as linhas que JA ESTAO em producao, e elas so somem
    quando a etapa 2 rodar ou quando a retencao as alcancar.
    """

    RESTRICAO = "ck_ai_feedback_created_at_nao_nula"

    def _criar_sem_data(self, db, restaurante):
        """INSERT CRU, e a razao disso e o achado deste teste.

        **O ORM nao consegue gravar este nulo nem quando se pede.**
        `AIFeedback(created_at=None)` nao grava `NULL`: com `server_default`,
        o SQLAlchemy trata o `None` explicito como "deixe o banco preencher" e
        OMITE a coluna do INSERT. Medido — a linha sai com `now()`.

        Isso prova, do lado de dentro, o que a armadilha 33 diz do lado de
        fora: esta linha so pode ter nascido de escrita feita **por fora do
        ORM** — SQL manual no Supabase, script de importacao, correcao a mao.
        E por isso o teste tem que escrever do mesmo jeito que ela nasceu.
        """
        # A etapa 1 do alinhamento (revisao `20260905_0055`) recusa este
        # INSERT. Derrubar e recriar `NOT VALID` deixa o banco no MESMO estado
        # em que producao esta hoje: a restricao existe, e a linha antiga que
        # a contradiz continua la.
        db.execute(text(f'ALTER TABLE ai_feedback DROP CONSTRAINT "{self.RESTRICAO}"'))
        db.execute(
            text(
                """
                INSERT INTO ai_feedback (
                    restaurant_id, session_id, user_message, assistant_message,
                    response_type, selected_product_ids, feedback, created_at
                ) VALUES (
                    :restaurant_id, :session_id, :user_message, :assistant_message,
                    'text', '{}'::uuid[], 'like', NULL
                )
                """
            ),
            {
                "restaurant_id": restaurante.id,
                "session_id": "sessao-sem-data",
                "user_message": "moro na rua das Flores, 200",
                "assistant_message": "Temos entrega para essa regiao!",
            },
        )
        db.execute(
            text(
                f'ALTER TABLE ai_feedback ADD CONSTRAINT "{self.RESTRICAO}" '
                'CHECK ("created_at" IS NOT NULL) NOT VALID'
            )
        )
        db.flush()
        return db.scalar(
            select(AIFeedback).where(AIFeedback.session_id == "sessao-sem-data")
        )

    def test_a_linha_sem_data_e_apagada_pela_retencao(self, db):
        """Apagar e a escolha certa, e vale dizer por que.

        A linha nao tem como provar que e recente, e o que ela guarda e texto
        em claro de pessoa. Entre manter dado pessoal de idade desconhecida e
        perder uma amostra de qualidade do Rapi, a LGPD decide — e o que se
        perde e uma linha que ja estava fora do inventario.
        """
        restaurante = criar_restaurante(db)
        sem_data = self._criar_sem_data(db, restaurante)

        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 1
        assert db.get(AIFeedback, sem_data.id) is None

    def test_a_linha_recente_continua_sobrevivendo(self, db):
        """O outro lado: o `IS NULL` nao pode virar "apaga tudo".

        Sem este teste, um `or_` mal colocado passaria apagando a tabela
        inteira a cada varredura — e o sintoma seria a amostra de qualidade
        sumir sem ninguem entender por que.
        """
        restaurante = criar_restaurante(db)
        recente = TestExpurgoDoFeedback._criar_feedback(db, restaurante, 1)
        sem_data = self._criar_sem_data(db, restaurante)

        removidos = AIFeedbackRepository(db).delete_created_before(
            feedback_retention_cutoff(utcnow())
        )

        assert removidos == 1
        assert db.get(AIFeedback, recente.id) is not None
        assert db.get(AIFeedback, sem_data.id) is None
