"""As duas correções de 15/08/2026: preço no contexto e produto citado vira cartão.

Contexto de cada uma, para quem chegar aqui pelo `git blame`:

1. **PREÇO.** O modelo não recebia preço nenhum. Perguntado "quanto custa?",
   ele só podia desconversar ou inventar. Passar o preço abre a porta para o
   texto dizer um valor diferente do que o cartão mostra — e é essa divergência
   que a maior parte deste arquivo cerca.

2. **SELEÇÃO.** Log de produção: `context_products=5`, `response_type=text`,
   `products_count=0`. O modelo citou "**Pudim**, **Brownie** e **Torta de
   Limão**" em negrito e devolveu `selected_product_ids` vazio, então o cliente
   leu a recomendação sem nenhum cartão para tocar.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from src.ai.services.retrieval_service import RetrievalService
from src.services import chat_service
from src.services.chat_service import ChatService
from src.utils.money import format_money_br


PUDIM = uuid.uuid4()
BROWNIE = uuid.uuid4()
TORTA = uuid.uuid4()


def retrieved(product_id, name, price="R$ 23,90"):
    produto = {"id": product_id, "name": name, "short_description": ""}
    if price is not None:
        produto["price"] = price
    return produto


class FakePriceRepository:
    """Devolve preço só para os ids vendáveis, como `sellable_prices_by_id`."""

    def __init__(self, prices):
        self.prices = dict(prices)
        self.ids_asked = None

    def sellable_prices_by_id(self, restaurant_id, product_ids):
        self.ids_asked = list(product_ids)
        return {
            product_id: price
            for product_id, price in self.prices.items()
            if product_id in set(product_ids)
        }


def make_retrieval_service(prices) -> RetrievalService:
    """`RetrievalService` sem `__init__`: ele constrói `EmbeddingService`, que
    exige credencial da OpenAI, e nada disso importa para o carimbo de preço."""
    service = RetrievalService.__new__(RetrievalService)
    service.product_repository = FakePriceRepository(prices)
    return service


# ---------------------------------------------------------------------------
# 1. O preço no contexto
# ---------------------------------------------------------------------------


class TestOPrecoQueVaiParaOModelo:
    def test_o_produto_recebe_o_preco_vigente_formatado(self):
        service = make_retrieval_service({PUDIM: Decimal("12.50")})

        com_preco = service._with_current_prices(uuid.uuid4(), [retrieved(PUDIM, "Pudim", price=None)])

        assert com_preco[0]["price"] == "R$ 12,50"

    def test_o_preco_e_lido_por_requisicao_e_nao_vem_do_que_estava_no_contexto(self):
        """A garantia contra o cache de 20 minutos.

        O dicionário que chega já traz um preço velho — é o que estaria
        guardado no `chat_cache`. O que vai ao modelo tem de ser o do banco.
        """
        service = make_retrieval_service({PUDIM: Decimal("14.00")})

        com_preco = service._with_current_prices(
            uuid.uuid4(),
            [retrieved(PUDIM, "Pudim", price="R$ 12,50")],
        )

        assert com_preco[0]["price"] == "R$ 14,00"

    def test_produto_que_ficou_indisponivel_some_do_contexto(self):
        """Fecha o caminho que produzia texto com produto e `products` vazio.

        Marcado como indisponível depois de entrar no cache, o produto seria
        recomendado no texto e sumiria na hidratação — que aplica os mesmos
        filtros. Some antes de o modelo o ver.
        """
        service = make_retrieval_service({PUDIM: Decimal("12.50")})

        com_preco = service._with_current_prices(
            uuid.uuid4(),
            [retrieved(PUDIM, "Pudim", price=None), retrieved(BROWNIE, "Brownie", price=None)],
        )

        assert [produto["id"] for produto in com_preco] == [PUDIM]

    def test_busca_vazia_nao_consulta_preco(self):
        service = make_retrieval_service({})

        assert service._with_current_prices(uuid.uuid4(), []) == []

    def test_o_resto_do_contexto_e_preservado(self):
        service = make_retrieval_service({PUDIM: Decimal("12.50")})
        entrada = {
            "id": PUDIM,
            "name": "Pudim",
            "short_description": "de leite condensado",
            "category_name": "Sobremesas",
        }

        com_preco = service._with_current_prices(uuid.uuid4(), [entrada])

        assert com_preco[0]["name"] == "Pudim"
        assert com_preco[0]["short_description"] == "de leite condensado"
        assert com_preco[0]["category_name"] == "Sobremesas"


class TestOFormatoDoPreco:
    """O modelo copia a string. O formato É o contrato."""

    def test_duas_casas_e_virgula(self):
        assert format_money_br(Decimal("23.9")) == "R$ 23,90"

    def test_valor_redondo_mantem_os_centavos(self):
        assert format_money_br(Decimal("50")) == "R$ 50,00"

    def test_nao_ha_separador_de_milhar(self):
        """Um ponto de milhar é um ponto que o modelo pode reproduzir como
        separador decimal."""
        assert format_money_br(Decimal("1234.50")) == "R$ 1234,50"

    def test_arredonda_meio_para_cima_como_o_resto_do_sistema(self):
        assert format_money_br(Decimal("23.905")) == "R$ 23,91"


class TestADivergenciaDePreco:
    """A janela que sobra: o preço muda entre a leitura do contexto e a do cartão."""

    def test_preco_igual_nos_dois_lados_nao_gera_aviso(self, caplog):
        contexto = [retrieved(PUDIM, "Pudim", price="R$ 12,50")]
        cartoes = [SimpleNamespace(id=PUDIM, price=12.50)]

        ChatService._log_price_divergence(contexto, cartoes)

        assert "preco divergente" not in caplog.text

    def test_preco_diferente_gera_aviso_com_os_dois_valores(self, caplog):
        """O cartão está certo — vem do banco. O aviso existe para a
        divergência ter outra testemunha além do cliente."""
        contexto = [retrieved(PUDIM, "Pudim", price="R$ 12,50")]
        cartoes = [SimpleNamespace(id=PUDIM, price=14.00)]

        ChatService._log_price_divergence(contexto, cartoes)

        assert "preco divergente" in caplog.text
        assert "R$ 12,50" in caplog.text
        assert "R$ 14,00" in caplog.text

    def test_produto_sem_preco_no_contexto_nao_gera_aviso(self, caplog):
        """Nada foi mostrado ao modelo, então não há o que divergir."""
        contexto = [retrieved(PUDIM, "Pudim", price=None)]
        cartoes = [SimpleNamespace(id=PUDIM, price=14.00)]

        ChatService._log_price_divergence(contexto, cartoes)

        assert "preco divergente" not in caplog.text


# ---------------------------------------------------------------------------
# 2. Produto citado no texto vira cartão
# ---------------------------------------------------------------------------


class TestResgateDoProdutoCitado:
    def test_o_caso_de_producao_tres_citados_e_nenhum_selecionado(self):
        contexto = [
            retrieved(PUDIM, "Pudim"),
            retrieved(BROWNIE, "Brownie"),
            retrieved(TORTA, "Torta de Limão"),
        ]
        texto = "Temos **Pudim**, **Brownie** e **Torta de Limão**. Qual você prefere?"

        resgatados = ChatService._rescue_products_named_in_text(texto, contexto, [])

        assert resgatados == [PUDIM, BROWNIE, TORTA]

    def test_a_ordem_e_a_do_TEXTO_e_nao_a_da_busca(self):
        """Mesmo invariante de `_validate_selected_product_ids`: é no texto que
        o modelo explicou os produtos, e trocar desalinha o carrossel."""
        contexto = [retrieved(PUDIM, "Pudim"), retrieved(BROWNIE, "Brownie")]
        texto = "Recomendo o **Brownie**. Se preferir mais leve, o **Pudim**."

        assert ChatService._rescue_products_named_in_text(texto, contexto, []) == [BROWNIE, PUDIM]

    def test_acento_e_caixa_nao_impedem_o_resgate(self):
        """O modelo escreve "Torta de Limão", o banco pode guardar "torta de
        limao". Sem achatar os dois lados, nada casa."""
        contexto = [retrieved(TORTA, "torta de limao")]

        resgatados = ChatService._rescue_products_named_in_text("Tem **Torta de LIMÃO** hoje.", contexto, [])

        assert resgatados == [TORTA]

    def test_selecao_nao_vazia_e_deixada_em_paz(self):
        """"não temos **Pudim**, mas temos **Brownie**": o modelo escolheu, e
        acrescentar o Pudim ali seria adivinhar."""
        contexto = [retrieved(PUDIM, "Pudim"), retrieved(BROWNIE, "Brownie")]
        texto = "Não temos **Pudim** hoje, mas o **Brownie** está ótimo."

        resgatados = ChatService._rescue_products_named_in_text(texto, contexto, [BROWNIE])

        assert resgatados == [BROWNIE]

    def test_texto_sem_nenhum_produto_continua_vazio(self):
        contexto = [retrieved(PUDIM, "Pudim")]

        assert ChatService._rescue_products_named_in_text("Olá! Como posso ajudar?", contexto, []) == []

    def test_produto_de_nome_contido_em_outro_nao_vira_cartao_extra(self):
        """"**Coca-Cola Zero**" contém "Coca-Cola". Sem a regra do nome mais
        longo primeiro, um nome citado viraria dois cartões."""
        zero, comum = uuid.uuid4(), uuid.uuid4()
        contexto = [retrieved(comum, "Coca-Cola"), retrieved(zero, "Coca-Cola Zero")]

        resgatados = ChatService._rescue_products_named_in_text("Temos **Coca-Cola Zero**.", contexto, [])

        assert resgatados == [zero]

    def test_os_dois_nomes_citados_viram_os_dois_cartoes(self):
        """O contrário do teste acima: citados de verdade, os dois entram."""
        zero, comum = uuid.uuid4(), uuid.uuid4()
        contexto = [retrieved(comum, "Coca-Cola"), retrieved(zero, "Coca-Cola Zero")]

        resgatados = ChatService._rescue_products_named_in_text(
            "Temos **Coca-Cola Zero** e **Coca-Cola**.", contexto, []
        )

        assert set(resgatados) == {zero, comum}

    def test_produto_que_nao_veio_da_busca_nao_e_resgatado(self):
        """A defesa de `_validate_selected_product_ids` continua valendo: o
        resgate só alcança o que a NOSSA busca devolveu.

        Os dois produtos estão citados no texto; só o Pudim existe na busca. O
        Pavê é alucinação do modelo e não pode virar cartão — é exatamente o
        que o resgate NÃO pode enfraquecer.
        """
        contexto = [retrieved(PUDIM, "Pudim")]

        resgatados = ChatService._rescue_products_named_in_text(
            "Temos **Pudim** e **Pavê de Chocolate** também!", contexto, []
        )

        assert resgatados == [PUDIM]

    def test_texto_so_com_produto_alucinado_nao_resgata_nada(self):
        contexto = [retrieved(PUDIM, "Pudim")]

        resgatados = ChatService._rescue_products_named_in_text(
            "Temos **Pavê de Chocolate** também!", contexto, []
        )

        assert resgatados == []

    def test_id_malformado_na_busca_nao_derruba_o_resgate(self):
        contexto = [{"id": "nao-e-uuid", "name": "Pudim"}, retrieved(BROWNIE, "Brownie")]

        resgatados = ChatService._rescue_products_named_in_text(
            "Temos **Pudim** e **Brownie**.", contexto, []
        )

        assert resgatados == [BROWNIE]

    def test_produto_sem_nome_e_ignorado(self):
        contexto = [{"id": PUDIM, "name": ""}, retrieved(BROWNIE, "Brownie")]

        assert ChatService._rescue_products_named_in_text("Só o **Brownie**.", contexto, []) == [BROWNIE]


class TestOResgateVirandoCartao:
    """O resgate só vale se `_response_type` reagir a ele — é o passo que
    transforma a resposta de "text" em "products"."""

    def test_com_produto_resgatado_a_resposta_deixa_de_ser_texto(self):
        llm = SimpleNamespace(response_type="text", selected_product_ids=[])
        produtos = [SimpleNamespace(id=PUDIM)]

        assert ChatService._response_type(llm, [PUDIM], produtos) == "products"

    def test_sem_nada_resgatado_continua_texto(self):
        llm = SimpleNamespace(response_type="text", selected_product_ids=[])

        assert ChatService._response_type(llm, [], []) == "text"


# ---------------------------------------------------------------------------
# 3. Com que frequência a rede de segurança precisa agir
# ---------------------------------------------------------------------------


class TestATaxaDeResgate:
    """Uma rede que trabalha em 1 de cada 9 turnos não é mais uma rede.

    Foi o que a bateria de 24/08/2026 mostrou em produção: um WARNING de
    resgate em nove turnos. O aviso sozinho não diz isso — ele conta os
    acionamentos e nunca os turnos em que a seleção veio bem, então "vi três
    avisos hoje" não separa três em trinta de três em nove. Sem o
    denominador não há como saber se mexer no `SYSTEM_PROMPT` melhorou ou
    piorou, que é a única decisão que este número serve para tomar.
    """

    def setup_method(self):
        """O contador é estado de MÓDULO: sem zerar, um teste conta o outro.

        Mesma razão da fixture `cache_limpo` em
        `tests/test_chat_cache_de_embedding_no_redis.py` — e o resto deste
        arquivo chama `_rescue_products_named_in_text` doze vezes, então o
        vazamento seria garantido, não hipotético.
        """
        chat_service._resgates_por_nome = 0
        chat_service._turnos_com_llm = 0

    def test_o_denominador_conta_o_turno_que_nao_precisou_de_resgate(self):
        contexto = [retrieved(PUDIM, "Pudim"), retrieved(BROWNIE, "Brownie")]

        ChatService._rescue_products_named_in_text("Recomendo o Brownie.", contexto, [BROWNIE])

        assert chat_service._turnos_com_llm == 1
        assert chat_service._resgates_por_nome == 0

    def test_texto_sem_produto_nenhum_tambem_conta_como_turno(self):
        """O modelo não citar produto é ele acertando, não um turno a ignorar.

        Contar só os turnos que citaram produto inflaria a taxa: o
        denominador viraria "turnos em que havia o que resgatar", e a
        pergunta que se quer responder é sobre TODOS os turnos que chamaram
        o modelo.
        """
        contexto = [retrieved(PUDIM, "Pudim")]

        ChatService._rescue_products_named_in_text("Olá! Como posso ajudar?", contexto, [])

        assert chat_service._turnos_com_llm == 1
        assert chat_service._resgates_por_nome == 0

    def test_o_resgate_soma_nos_dois_contadores(self):
        contexto = [retrieved(PUDIM, "Pudim")]

        ChatService._rescue_products_named_in_text("Temos **Pudim**.", contexto, [])

        assert chat_service._turnos_com_llm == 1
        assert chat_service._resgates_por_nome == 1

    def test_o_aviso_carrega_o_placar_e_a_taxa(self, caplog):
        """O que se lê no radar: numerador, denominador e a divisão pronta.

        A taxa vai calculada porque quem lê o log às duas da manhã com a
        operação de pé não vai dividir 3 por 27 de cabeça — e é a taxa, não
        o numerador, que decide se o `SYSTEM_PROMPT` precisa de atenção.
        """
        contexto = [retrieved(PUDIM, "Pudim"), retrieved(BROWNIE, "Brownie")]

        # Três turnos limpos e um que precisou da rede: 1 em 4.
        for _ in range(3):
            ChatService._rescue_products_named_in_text("Recomendo o Brownie.", contexto, [BROWNIE])

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            ChatService._rescue_products_named_in_text("Temos **Pudim**.", contexto, [])

        assert "Resgatados pelo nome." in caplog.text
        assert "resgates=1" in caplog.text
        assert "turnos_com_llm=4" in caplog.text
        assert "taxa=25.0%" in caplog.text

    def test_turno_limpo_nao_escreve_linha_nenhuma(self, caplog):
        """O denominador viaja no próximo aviso, e não num log por turno.

        Uma linha em INFO a cada turno para dizer "nada aconteceu" é ruído no
        caminho quente, e o `/chat` já escreve dezenas de linhas por
        requisição.
        """
        contexto = [retrieved(BROWNIE, "Brownie")]

        with caplog.at_level("INFO", logger="uvicorn.error"):
            ChatService._rescue_products_named_in_text("Recomendo o Brownie.", contexto, [BROWNIE])

        assert "turnos_com_llm" not in caplog.text


class TestOTurnoQueTerminaSemCartao:
    """O irmão do resgate — e o que a rede de segurança NÃO alcança.

    Produção, 24/08/2026: "vocês têm sushi?" voltou `products_count=0` com um
    texto que se oferecia para mostrar os peixes da casa **sem escrever o nome
    de nenhum**. Sem nome no texto não há o que resgatar, então
    `_rescue_products_named_in_text` saiu calado — e, pior, contabilizou
    aquele turno como limpo no placar do resgate.

    O mesmo turno trouxe 2 ou 3 cartões nas duas baterias anteriores. É
    intermitência, não latência, e sem contador ela só é vista por quem estava
    olhando a tela naquela hora.
    """

    def setup_method(self):
        chat_service._turnos_com_contexto = 0
        chat_service._turnos_sem_cartao = 0

    def test_busca_com_produto_e_zero_cartao_e_o_caso(self, caplog):
        contexto = [retrieved(PUDIM, "Pudim"), retrieved(BROWNIE, "Brownie")]

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            chat_service._contar_cartao_do_turno(contexto, [])

        assert "nenhum chegou ao cliente" in caplog.text
        assert "sem_cartao=1" in caplog.text
        assert "turnos_com_contexto=1" in caplog.text
        assert "taxa=100.0%" in caplog.text

    def test_cartao_entregue_conta_no_denominador_e_nao_avisa(self, caplog):
        contexto = [retrieved(PUDIM, "Pudim")]

        with caplog.at_level("WARNING", logger="uvicorn.error"):
            chat_service._contar_cartao_do_turno(contexto, [SimpleNamespace(id=PUDIM)])

        assert chat_service._turnos_com_contexto == 1
        assert chat_service._turnos_sem_cartao == 0
        assert caplog.text == ""

    def test_busca_vazia_fica_INTEIRA_fora_da_conta(self, caplog):
        """"Vocês abrem domingo?" termina sem cartão porque não havia cartão.

        Contar isso no denominador misturaria acerto com defeito e faria a
        taxa cair sozinha toda vez que alguém perguntasse sobre horário.
        """
        with caplog.at_level("WARNING", logger="uvicorn.error"):
            chat_service._contar_cartao_do_turno([], [])

        assert chat_service._turnos_com_contexto == 0
        assert chat_service._turnos_sem_cartao == 0
        assert caplog.text == ""

    def test_a_taxa_e_sobre_os_turnos_com_contexto(self):
        contexto = [retrieved(PUDIM, "Pudim")]
        cartao = [SimpleNamespace(id=PUDIM)]

        for _ in range(3):
            chat_service._contar_cartao_do_turno(contexto, cartao)
        chat_service._contar_cartao_do_turno(contexto, [])
        chat_service._contar_cartao_do_turno([], [])

        assert chat_service._turnos_com_contexto == 4
        assert chat_service._turnos_sem_cartao == 1

    def test_o_turno_resgatado_NAO_conta_como_sem_cartao(self):
        """A fronteira entre os dois placares, num teste só.

        O resgate roda antes da hidratação: quando ele age, o cartão sai, e
        este contador vê um turno normal. `sem_cartao` conta o que ESCAPA da
        rede — se ele passasse a contar também o que ela pega, os dois números
        mediriam a mesma coisa e nenhum diria onde está o buraco.
        """
        contexto = [retrieved(PUDIM, "Pudim")]

        resgatados = ChatService._rescue_products_named_in_text(
            "Temos **Pudim**.", contexto, []
        )
        chat_service._contar_cartao_do_turno(
            contexto, [SimpleNamespace(id=pid) for pid in resgatados]
        )

        assert resgatados == [PUDIM]
        assert chat_service._turnos_sem_cartao == 0
