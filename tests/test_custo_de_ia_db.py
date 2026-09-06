"""O custo de IA contra o Postgres: a agregacao, e o que sobrou da voz.

A agregacao por periodo E banco — um `GROUP BY` com `FILTER`, `SUM` de
`NUMERIC` e janela meio-aberta. Sao exatamente os pontos em que uma reescrita
da consulta quebra sem ninguem notar, e dublar qualquer um seria testar o
dublê.

## A voz saiu do projeto em 06/09/2026, e este arquivo guarda a metade que fica

`registrar_voz` nao existe mais, entao os testes da gravacao falada sairam com
ela. O que NAO saiu, e o que `TestRelatorioPorPeriodo` continua provando, e que
**as linhas ja gravadas continuam saindo no relatorio**: `voice_calls` e
`voice_cost_usd` sao historicos, e a promessa escrita em
`src/schemas/ai_usage_schema.py` — `calls` fecha com `text_calls` + `voice_calls`
em qualquer janela — so vale enquanto isso for verdade.

Por isso a linha de voz do cenario e semeada por **SQL cru**: o ORM nao mapeia
mais `voice_session_id` e `AIVoiceSession` nao existe. O SQL aqui e a unica
forma de escrever a linha que producao ja tem — e e tambem por isso que este
arquivo esta na lista de codigo acoplado da revisao preparada
`20260906_0060`, que derruba a coluna e a tabela.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.ai.services.chat_llm_service import UsoDoModelo
from src.models.ai_usage_event_model import AIUsageEvent
from src.services.ai_usage_service import AIUsageService
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


ONTEM = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)


def _linha_de_voz_ja_gravada(db, restaurante, custo: str) -> None:
    """Uma sessao de voz e o evento de custo dela, como producao ja os tem.

    SQL CRU e nao ORM, e nao ha escolha: `AIVoiceSession` saiu do codigo em
    06/09/2026 e `voice_session_id` deixou de ser mapeada. Escrever isto pelo
    ORM e impossivel — o que e exatamente a propriedade que o resto do
    repositorio quer, e o motivo de esta linha precisar existir aqui: sem ela,
    "o relatorio continua somando a voz" seria uma frase sem teste.

    Os dois INSERT juntos porque a CHECK
    `(surface = 'voice') = (voice_session_id IS NOT NULL)` amarra os dois: nem
    a sessao sozinha vira custo, nem o custo nasce sem a sessao.
    """
    sessao_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO ai_voice_sessions (id, restaurant_id, expires_at) "
            "VALUES (:id, :r, :expira)"
        ),
        {"id": sessao_id, "r": restaurante.id, "expira": ONTEM + timedelta(minutes=10)},
    )
    db.execute(
        text(
            "INSERT INTO ai_usage_events "
            "(restaurant_id, surface, model, input_tokens, output_tokens, "
            " cost_usd, voice_session_id) "
            "VALUES (:r, 'voice', 'gpt-realtime-mini', 1000, 1000, :custo, :s)"
        ),
        {"r": restaurante.id, "custo": Decimal(custo), "s": sessao_id},
    )


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


class TestOsCheckDoBanco:
    def test_linha_de_voz_sem_sessao_e_recusada(self, db):
        """O CHECK que sobreviveu ao codigo que ele protegia.

        Ele nasceu para impedir linha de voz sem chave de idempotencia. Com a
        voz fora do projeto ele passou a valer para outra coisa, e ela importa
        mais: o ORM nao mapeia mais `voice_session_id`, entao TODA linha que
        ele grava deixa a coluna nula — e uma que tentasse nascer com
        `surface = 'voice'` seria recusada pelo banco em vez de virar custo
        historico falso.
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
        # 1000 tokens de audio de entrada (US$ 10/1M) + 1000 de saida
        # (US$ 20/1M) = 0,01 + 0,02, que e o que a conta de voz gravava
        # quando ela existia.
        _linha_de_voz_ja_gravada(db, restaurante, custo="0.030000")
        db.flush()
        return restaurante

    def test_voz_e_texto_saem_separados_e_o_total_soma_os_dois(self, db):
        """A promessa do schema: `calls` fecha com `text_calls` + `voice_calls`.

        E o que impede a saida da voz de virar um buraco no relatorio — o mes
        de agosto de 2026 tem chamada falada dentro, e ela nao pode aparecer no
        total sem aparecer em coluna nenhuma.
        """
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
        assert linha.voice_cost_usd == Decimal("0.030000")
        assert linha.calls == linha.text_calls + linha.voice_calls
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
