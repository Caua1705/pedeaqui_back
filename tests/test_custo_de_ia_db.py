"""O custo de IA contra o Postgres: a idempotencia da voz e a agregacao.

As duas metades daqui SAO banco, e dublar qualquer uma seria testar o dublê:

- a idempotencia da voz e sustentada por um UNIQUE parcial em
  `voice_session_id`. Um dublê de repositorio "lembraria" da sessao porque o
  teste mandou lembrar; o que se quer provar e que o BANCO nao deixa nascer a
  segunda linha;
- a agregacao por periodo e um `GROUP BY` com `FILTER`, `SUM` de `NUMERIC` e
  janela meio-aberta. Sao exatamente os pontos em que uma reescrita da
  consulta quebra sem ninguem notar.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.ai.services.chat_llm_service import UsoDoModelo
from src.models.ai_usage_event_model import AIUsageEvent
from src.models.ai_voice_session_model import AIVoiceSession
from src.services.ai_usage_service import AIUsageService
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


ONTEM = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)


def _sessao_de_voz(db, restaurante, **tokens) -> AIVoiceSession:
    sessao = AIVoiceSession(
        restaurant_id=restaurante.id,
        expires_at=ONTEM + timedelta(minutes=10),
        **tokens,
    )
    db.add(sessao)
    db.flush()
    return sessao


class TestGravacaoDoTexto:
    def test_um_turno_vira_uma_linha_com_custo(self, db):
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)

        AIUsageService(db).registrar_texto(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            uso=UsoDoModelo(
                modelo="gpt-5-mini", entrada=1000, entrada_em_cache=0, saida=500
            ),
        )

        evento = db.query(AIUsageEvent).one()
        assert evento.surface == "text"
        assert evento.model == "gpt-5-mini"
        assert evento.branch_id == filial.id
        assert evento.voice_session_id is None
        assert evento.cost_usd == Decimal("0.001250")

    def test_modelo_sem_preco_grava_os_tokens_e_deixa_o_custo_NULO(self, db):
        """NULO e nao zero, e os tokens ficam — a linha continua reprocessavel.

        E o que permite atualizar `src/ai/custo.py` depois e recalcular o mes
        que passou, em vez de descobrir que ele foi gravado como gratuito.
        """
        restaurante = criar_restaurante(db)
        filial = criar_filial(db, restaurante)

        AIUsageService(db).registrar_texto(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            uso=UsoDoModelo(
                modelo="modelo-que-nao-existe", entrada=1000, entrada_em_cache=0, saida=500
            ),
        )

        evento = db.query(AIUsageEvent).one()
        assert evento.cost_usd is None
        assert evento.input_tokens == 1000
        assert evento.output_tokens == 500


class TestGravacaoDaVoz:
    def test_o_aviso_de_fim_repetido_nao_dobra_o_custo(self, db):
        """O caso concreto: o aviso vai com `keepalive` e o navegador o reenvia.

        Sem o UNIQUE de `voice_session_id`, a segunda chegada viraria uma
        segunda linha e a conversa apareceria custando o dobro no relatorio.
        """
        restaurante = criar_restaurante(db)
        sessao = _sessao_de_voz(
            db,
            restaurante,
            input_audio_tokens=1000,
            input_text_tokens=200,
            output_audio_tokens=500,
            output_text_tokens=50,
            cached_tokens=0,
        )
        servico = AIUsageService(db)

        servico.registrar_voz(sessao)
        db.flush()
        servico.registrar_voz(sessao)
        db.flush()

        eventos = db.query(AIUsageEvent).all()
        assert len(eventos) == 1
        assert eventos[0].surface == "voice"
        assert eventos[0].voice_session_id == sessao.id
        assert eventos[0].input_tokens == 1200
        assert eventos[0].output_tokens == 550

    def test_a_segunda_chegada_CORRIGE_a_primeira(self, db):
        """Ela costuma trazer o numero mais completo, e nao o contrario."""
        restaurante = criar_restaurante(db)
        sessao = _sessao_de_voz(db, restaurante, input_audio_tokens=100)
        servico = AIUsageService(db)

        servico.registrar_voz(sessao)
        db.flush()

        sessao.input_audio_tokens = 900
        sessao.output_audio_tokens = 300
        servico.registrar_voz(sessao)
        db.flush()

        evento = db.query(AIUsageEvent).one()
        assert evento.input_tokens == 900
        assert evento.output_tokens == 300

    def test_sessao_que_nao_reportou_numero_nao_vira_linha(self, db):
        """NULO nao e zero (revisao 0023).

        Gravar custo zero aqui inventaria uma conversa de graca que ninguem
        mediu — e ela contaria como chamada no relatorio.
        """
        restaurante = criar_restaurante(db)
        sessao = _sessao_de_voz(db, restaurante)

        AIUsageService(db).registrar_voz(sessao)
        db.flush()

        assert db.query(AIUsageEvent).count() == 0


class TestOsCheckDoBanco:
    def test_linha_de_voz_sem_sessao_e_recusada(self, db):
        """A duplicata que o CHECK impede de nascer.

        Voz sem `voice_session_id` nao teria chave de idempotencia, e o
        proximo aviso de fim criaria outra linha para a mesma conversa.
        """
        restaurante = criar_restaurante(db)
        db.add(
            AIUsageEvent(
                restaurant_id=restaurante.id, surface="voice", model="gpt-realtime-mini"
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_superficie_inventada_e_recusada(self, db):
        restaurante = criar_restaurante(db)
        db.add(
            AIUsageEvent(restaurant_id=restaurante.id, surface="telepatia", model="x")
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


class TestRelatorioPorPeriodo:
    def _semear(self, db):
        restaurante = criar_restaurante(db, nome="Junior da Picanha")
        filial = criar_filial(db, restaurante)
        servico = AIUsageService(db)

        servico.registrar_texto(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            uso=UsoDoModelo("gpt-5-mini", entrada=1000, entrada_em_cache=0, saida=500),
        )
        servico.registrar_texto(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            uso=UsoDoModelo("sem-preco-nenhum", entrada=1000, entrada_em_cache=0, saida=500),
        )
        sessao = _sessao_de_voz(
            db, restaurante, input_audio_tokens=1000, output_audio_tokens=1000
        )
        servico.registrar_voz(sessao)
        db.flush()
        return restaurante

    def test_voz_e_texto_saem_separados_e_o_total_soma_os_dois(self, db):
        restaurante = self._semear(db)

        relatorio = AIUsageService(db).custo_por_restaurante(
            desde=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ate=datetime(2030, 1, 1, tzinfo=timezone.utc),
            restaurant_id=restaurante.id,
        )

        linha = relatorio.restaurants[0]
        assert linha.text_calls == 2
        assert linha.voice_calls == 1
        assert linha.text_cost_usd == Decimal("0.001250")
        # 1000 tokens de audio de entrada (US$ 10/1M) + 1000 de saida
        # (US$ 20/1M) = 0,01 + 0,02
        assert linha.voice_cost_usd == Decimal("0.030000")
        assert relatorio.total_cost_usd == Decimal("0.031250")

    def test_a_chamada_sem_preco_e_contada_e_nao_soma_dinheiro(self, db):
        """A coluna que impede a leitura ingenua do total.

        A segunda chamada de texto usou um modelo fora da tabela de precos:
        ela conta como chamada, soma token e nao soma dolar.
        """
        restaurante = self._semear(db)

        linha = AIUsageService(db).custo_por_restaurante(
            desde=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ate=datetime(2030, 1, 1, tzinfo=timezone.utc),
            restaurant_id=restaurante.id,
        ).restaurants[0]

        assert linha.calls == 3
        assert linha.calls_without_price == 1
        assert linha.input_tokens == 3000

    def test_a_janela_e_meio_aberta_e_o_periodo_vazio_sai_vazio(self, db):
        """`[desde, ate)`. O instante de `ate` ja e do periodo seguinte."""
        restaurante = self._semear(db)
        gravado_em = db.execute(
            text("SELECT min(created_at) FROM ai_usage_events")
        ).scalar_one()

        servico = AIUsageService(db)
        dentro = servico.custo_por_restaurante(
            desde=gravado_em, ate=gravado_em + timedelta(seconds=1), restaurant_id=restaurante.id
        )
        fora = servico.custo_por_restaurante(
            desde=gravado_em + timedelta(seconds=1),
            ate=gravado_em + timedelta(days=1),
            restaurant_id=restaurante.id,
        )

        assert dentro.restaurants != []
        assert fora.restaurants == []
        assert fora.total_cost_usd == Decimal("0.000000")

    def test_um_restaurante_nao_ve_o_outro(self, db):
        restaurante = self._semear(db)
        vizinho = criar_restaurante(db, nome="Vizinho")
        filial_do_vizinho = criar_filial(db, vizinho)
        AIUsageService(db).registrar_texto(
            restaurant_id=vizinho.id,
            branch_id=filial_do_vizinho.id,
            uso=UsoDoModelo("gpt-5-mini", entrada=10_000, entrada_em_cache=0, saida=10_000),
        )

        relatorio = AIUsageService(db).custo_por_restaurante(
            desde=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ate=datetime(2030, 1, 1, tzinfo=timezone.utc),
            restaurant_id=restaurante.id,
        )

        assert [linha.restaurant_id for linha in relatorio.restaurants] == [restaurante.id]
        assert relatorio.total_cost_usd == Decimal("0.031250")

    def test_sem_recorte_sai_um_bloco_por_restaurante(self, db):
        self._semear(db)
        vizinho = criar_restaurante(db, nome="Vizinho")
        filial_do_vizinho = criar_filial(db, vizinho)
        AIUsageService(db).registrar_texto(
            restaurant_id=vizinho.id,
            branch_id=filial_do_vizinho.id,
            uso=UsoDoModelo("gpt-5-mini", entrada=1000, entrada_em_cache=0, saida=0),
        )

        relatorio = AIUsageService(db).custo_por_restaurante(
            desde=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ate=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

        nomes = [linha.restaurant_name for linha in relatorio.restaurants]
        assert "Junior da Picanha" in nomes
        assert "Vizinho" in nomes


class TestAGravacaoNaoDerrubaOQueEstaSendoMedido:
    def test_falha_na_gravacao_do_texto_nao_levanta(self, db):
        """Medir nao pode derrubar o `/chat`, que ja respondeu ao cliente.

        `restaurant_id` inexistente estoura a FK no commit. A funcao tem que
        engolir, dar rollback e seguir.
        """
        AIUsageService(db).registrar_texto(
            restaurant_id=uuid.uuid4(),
            branch_id=None,
            uso=UsoDoModelo("gpt-5-mini", entrada=1, entrada_em_cache=0, saida=1),
        )

        assert db.query(AIUsageEvent).count() == 0
