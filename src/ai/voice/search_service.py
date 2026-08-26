"""A ferramenta de busca do atendimento por voz.

TUDO AQUI E REUSO. Nao ha uma linha de busca vetorial nem de montagem de
produto neste arquivo, e e proposital: se a voz reimplementasse
qualquer um dos dois, ele estaria medindo outro sistema, e a conclusao nao
valeria para o produto.

O que e reusado, e de onde:

| O que | De onde | Por que importa |
|---|---|---|
| busca vetorial + cache + preco vigente | `RetrievalService.retrieve_products` | mesma consulta, mesmo filtro por FILIAL e por produto vendavel |
| hidratacao dos produtos | `ChatService._hydrate_products` | **nome e preco que chegam ao cliente vem do banco**, nunca do modelo |
| 404 de restaurante | `ChatService._get_active_restaurant` | mesma barreira antes de gastar embedding |
| 404 de filial | `ChatService._get_active_branch` | mesma barreira, e a mesma recusa de adivinhar a loja |

A diferenca de desenho em relacao ao chat de texto, e ela e o ponto do
desenho: **aqui o modelo nao escolhe produto.** No texto ele devolve
`selected_product_ids` e `_validate_selected_product_ids` descarta o que ele
inventou. Na voz, quem manda os cartoes para a tela e a NOSSA busca — o
modelo so recebe um resumo para falar. Nao ha id passando pelo modelo, entao
nao ha id para ele inventar.

FRAGILIDADE CONHECIDA: `_hydrate_products`, `_get_active_restaurant` e
`_get_active_branch` sao privados do `ChatService`. Chamar de fora e feio e
acopla a voz a nomes que ninguem prometeu manter. A alternativa era copiar as
seis linhas, que e pior — copia nao acompanha conserto, e o filtro por filial
da revisao 20260820_0026 e exatamente o tipo de conserto que uma copia teria
deixado para tras num dos dois agentes. Antes de crescer daqui, os tres sobem
para um lugar compartilhado antes de qualquer outra coisa.
"""

import hashlib
import logging
import re
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from src.ai.services.retrieval_service import TOP_K_PADRAO
from src.services.chat_service import ChatService
from src.utils.money_por_extenso import preco_por_extenso
from src.utils.normalization import fold_for_match


logger = logging.getLogger("uvicorn.error")

# O corte da descricao. Generoso o bastante para caber "Baiao e batata frita.
# Serve 2 pessoas." e curto o bastante para o paragrafo do lojista nao virar
# custo em toda busca.
_LIMITE_DA_DESCRICAO = 120

# O que ocupa o campo quando nao ha o que por. Um traco, e nao campo vazio: com
# quatro campos posicionais, "a | b |  | d" e mais facil de ler errado do que
# "a | b | - | d".
_VAZIO = "-"

# AS QUATRO ORDENACOES. Cada valor mapeia para (crescente, loja_inteira).
#
# ELAS DEIXARAM DE SER UM PARAMETRO DO MODELO em 25/08/2026, e a tabela ficou.
# Antes o enum viajava na declaracao da ferramenta e o modelo escolhia o valor;
# hoje quem escolhe e `_reescrever_consulta`, lendo as palavras que o cliente
# usou. A tabela continua sendo o vocabulario interno dos dois caminhos, e o
# nome de cada valor continua dizendo o que ele faz.
ORDENACOES = {
    "mais_barato_da_busca": (True, False),
    "mais_caro_da_busca": (False, False),
    "mais_barato_da_loja": (True, True),
    "mais_caro_da_loja": (False, True),
}

# Quantos a busca por significado traz quando o pedido e ordenado. Cinco nao
# serve: ordenar os cinco mais parecidos com "bebida" devolve a mais barata
# DAQUELES CINCO, que nao e a mais barata das bebidas. Quarenta cobre o cardapio
# de um restaurante pequeno inteiro e continua sendo uma consulta so.
_TOP_K_ORDENADO = 40

# Quantos sobram depois de ordenar, e quantos a consulta por preco puro traz.
# Cinco, o mesmo teto do resumo: alem disso seria produto que nunca vai ser dito
# nem mostrado.
_LIMITE_ORDENADO = 5

# Quantas categorias `listar_categorias` devolve. Doze cobre o cardapio de uma
# churrascaria inteira e ja e mais do que cabe numa frase falada — o teto existe
# para o cardapio grande nao virar trinta segundos de recitacao, e nao para
# economizar consulta.
_TETO_DE_CATEGORIAS = 12

# Quantos produtos a FRASE cita. Dois, e o numero e o mesmo de sempre — o que
# mudou e QUEM o aplica. Ate 25/08/2026 o teto era uma secao inteira do prompt
# pedindo ao modelo que ignorasse tres dos cinco produtos que a ferramenta
# mandava; agora a frase ja vem com dois, e a secao saiu.
TETO_FALADO = 2

# A NEGATIVA, escrita aqui e nao montada pelo modelo (25/08/2026).
#
# O caso: o cliente perguntou "voces tem picanha?" numa churrascaria, o modelo
# nao entendeu a palavra, chamou `listar_categorias` em vez da busca — e
# respondeu "nao tem no cardapio, mas tem Executivos e Bebidas". Dois turnos
# depois negou bebida, tendo acabado de listar "Bebidas" como categoria.
#
# O padrao e o pior possivel: sem entender a palavra, ele NEGA o produto em vez
# de dizer que nao entendeu — a NAO INVENTE ao contrario, e o cliente ouve que
# a churrascaria nao tem picanha.
#
# Enquanto a negativa fosse texto que o modelo escrevia, ele conseguia
# escreve-la sem ter buscado. Sendo uma frase que so a BUSCA devolve, "nao
# temos" passa a ter uma origem unica e verificavel: sem busca no turno, o
# modelo nao tem a frase, e a saida que sobra e "nao entendi, pode repetir?".
#
# E o quarto movimento da mesma familia de `frase_para_o_modelo`: tirar a
# decisao do modelo entregando o dado ja na forma em que ele vai ser usado.
#
# Sem o ponto final: a mesma frase continua em "..., mas tem X e Y".
_NEGATIVA = "Aqui a gente nao tem isso"

# As expressoes que pedem ORDENACAO, e o que cada uma quer dizer (crescente).
# Sao reconhecidas na consulta e ARRANCADAS dela: "a bebida mais barata" busca
# "bebida" ordenado, e nao "bebida mais barata" — o texto do superlativo nao se
# parece com produto nenhum e so empurra a similaridade para baixo.
_PALAVRAS_DE_ORDEM = {
    "mais barato": True,
    "mais barata": True,
    "mais baratos": True,
    "mais baratas": True,
    "menor preco": True,
    "mais em conta": True,
    "sai por menos": True,
    "mais caro": False,
    "mais cara": False,
    "mais caros": False,
    "mais caras": False,
    "maior preco": False,
}

# Palavra que nao nomeia comida. Sai da consulta porque a busca e por
# significado e nenhuma delas tem significado de prato.
#
# E ela faz a SEGUNDA decisao, que e a que importa: se depois de arrancar o
# superlativo e o ruido nao sobrar palavra nenhuma, o cliente falou do cardapio
# INTEIRO ("manda o mais caro") e a ordenacao e "_da_loja". Sobrando alguma
# ("a bebida mais barata"), ha assunto, e e "_da_busca".
#
# A PERGUNTA DE PRECO ENTROU EM 25/08/2026, e ela e um grupo proprio dentro da
# mesma lista. "Quanto custa a picanha?" chegava aqui inteira e ia para o
# embedding como "quanto custa picanha": a busca deixava de ser pelo produto e
# passava a ser por uma pergunta sobre ele, e o que volta de uma consulta assim
# nao e o mesmo que volta de "picanha". Sem erro e sem log — o modelo recebia
# outros produtos e falava deles.
#
# Estas palavras nao pedem ordenacao (quem pede esta em `_PALAVRAS_DE_ORDEM`) e
# nao mudam o assunto: elas so dizem que a pergunta e sobre o preco daquilo, e
# o preco ja volta em todo resultado da busca.
_RUIDO = {
    "o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das",
    "em", "no", "na", "que", "qual", "quais", "tem", "voces", "voce",
    "aqui", "me", "manda", "mandar", "quero", "queria", "gostaria", "ver",
    "tudo", "todos", "todas", "e", "ai", "cardapio", "menu", "loja",
    "opcao", "opcoes", "coisa", "produto", "produtos", "por", "pra",
    "para", "com", "sem",
    # A pergunta de preco. "vale" ficou DE FORA de proposito: e a unica destas
    # que aparece em nome de comida ("Vale do Sol"), e arrancar palavra de nome
    # de produto e pior do que deixar uma palavra a mais na consulta.
    "quanto", "quantos", "custa", "custam", "custo", "preco", "precos",
    "valor", "sai", "fica", "ta", "esta",
}

_NAO_LETRA = re.compile(r"[^0-9a-z]+")


def _palavras(texto: str) -> list[str]:
    """As palavras de `texto`, dobradas para COMPARAR: sem acento, minusculas."""
    return [palavra for palavra in _NAO_LETRA.split(fold_for_match(texto)) if palavra]


def _reescrever_consulta(consulta: str) -> tuple[str, str | None]:
    """A consulta que vai para a busca, e a ordenacao que o cliente pediu.

    ISTO ERA DUAS DECISOES DO MODELO ate 25/08/2026, e as duas custavam prompt:
    escolher o valor do enum `ordenar` (que saiu da declaracao da ferramenta) e
    saber que "mais barato" nao se responde com busca por similaridade. Nenhuma
    das duas era natural para ele — a segunda precisou de cinco bullets, e o
    caso que a motivou foi um superlativo respondido de memoria.

    O que o backend consegue fazer e o que o backend faz: ler as palavras que
    chegaram e decidir sozinho. O que ele NAO consegue e conferir se aquelas
    palavras sao as que o cliente falou — o audio vai do navegador direto para
    a OpenAI e nunca passa por aqui. Fidelidade ao termo do cliente continua
    sendo regra de prompt porque nao ha outro lugar para ela.

    As palavras saem do texto ORIGINAL, com acento. A forma dobrada serve so
    para RECONHECER: quem busca e o embedding, e "baiao" sem acento nao e a
    mesma entrada que "baiao" com ele.
    """
    dobrada = " ".join(_palavras(consulta))

    crescente = None
    arrancar: set[str] = set()
    for expressao, e_crescente in _PALAVRAS_DE_ORDEM.items():
        if expressao in dobrada:
            crescente = e_crescente
            arrancar.update(expressao.split())

    restante = []
    for original in consulta.split():
        dobrado = " ".join(_palavras(original))
        if not dobrado or dobrado in _RUIDO or dobrado in arrancar:
            continue
        restante.append(original)

    # Sem superlativo o caminho e o de sempre, e a consulta so perde o ruido.
    # Consulta que era SO ruido volta inteira: cortar tudo deixaria a busca sem
    # entrada, e uma pergunta estranha ainda e melhor que uma vazia.
    if crescente is None:
        return (" ".join(restante) or consulta.strip()), None

    if not restante:
        return "", ("mais_barato_da_loja" if crescente else "mais_caro_da_loja")
    return " ".join(restante), ("mais_barato_da_busca" if crescente else "mais_caro_da_busca")


class VoiceSearchService:
    def __init__(self, db: Session):
        # UM ChatService, e nao um RetrievalService solto ao lado: os dois
        # reusos saem do mesmo objeto, entao nao ha como a voz buscar
        # por um caminho e hidratar por outro.
        #
        # `agent="/voz"` so muda o ROTULO das linhas de medicao que este
        # caminho emite: sem ele, `embedding_ms`, `retrieval_ms`,
        # `context_products` e `hydration_ms` da voz saem como `[AI /chat
        # perf]` e se misturam com as do chat de texto no mesmo grep.
        self.chat_service = ChatService(db, agent="/voz")

    def buscar(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        consulta: str,
        preco_maximo: Decimal | None = None,
    ) -> list:
        """Os produtos que a busca encontrou NAQUELA LOJA, hidratados do banco.

        Devolve a lista inteira: diferente do chat de texto, nao ha selecao do
        modelo no meio. O que a busca acha e o que aparece na tela.

        `branch_id` e obrigatorio desde a revisao 20260820_0026. A voz e o
        caminho em que o defeito era mais caro: o modelo FALA o nome e o preco
        do produto em audio, e o cliente aceita de ouvido — nao ha tela onde
        ele pudesse notar que aquilo e de outra loja.

        A ORDENACAO NAO E MAIS PARAMETRO: quem a decide e `_reescrever_consulta`,
        lendo as palavras que chegaram. Ela entrou em 25/08/2026 como enum que o
        modelo preenchia, e saiu de la no mesmo dia — duas perguntas de uma
        sessao real ("qual a bebida mais barata?", "manda o mais caro do
        cardapio") ficaram sem resposta, e nenhuma das duas era defeito de
        prompt: a busca e por SIGNIFICADO, e os cinco mais parecidos com
        "bebida" nao sao as cinco mais baratas. Superlativo e ordenacao, e
        ordenacao nao sai de similaridade.

        Sao dois caminhos, e a diferenca esta em `ORDENACOES`:

            ..._da_busca   a busca por significado roda LARGA
                           (`_TOP_K_ORDENADO`) e o resultado e ordenado por
                           preco. E o caminho de "a bebida mais barata", onde
                           o assunto importa.

            ..._da_loja    nao ha busca por significado nenhuma: o SQL ordena
                           o cardapio vendavel da filial por preco. E o
                           caminho de "o mais caro do cardapio", onde nao ha
                           assunto — a palavra "cardapio" nao se parece com
                           nada, e subir o `top_k` so tornaria o acaso mais
                           provavel.

        A ORDENACAO ACONTECE DEPOIS DA HIDRATACAO, e isso nao e detalhe de
        implementacao. O que a busca vetorial devolve traz o preco do INDICE,
        que envelhece; quem carimba o preco vigente da loja e a hidratacao.
        Ordenar antes dela devolveria "a mais barata" pelo numero velho — sem
        erro, sem log, e com cara de resposta certa.
        """
        restaurant = self.chat_service._get_active_restaurant(restaurant_id)
        branch = self.chat_service._get_active_branch(restaurant.id, branch_id)

        buscada, ordenar = _reescrever_consulta(consulta)

        # `crescente is None` e a marca de "nao foi pedida ordenacao". O `.get`
        # com queda continua valendo apesar de a chave agora ser nossa: e o que
        # mantem a funcao total se alguem acrescentar um valor em
        # `_PALAVRAS_DE_ORDEM` sem acrescentar o par em `ORDENACOES`.
        crescente, da_loja = ORDENACOES.get(ordenar or "", (None, False))

        if da_loja:
            # Sem embedding e sem cache de busca: nao ha pergunta a vetorizar.
            do_banco = self.chat_service.product_repository.list_active_by_price(
                branch_id=branch.id,
                crescente=crescente,
                limite=_LIMITE_ORDENADO,
            )
            encontrados = []
            product_ids = [uuid.UUID(str(produto.id)) for produto in do_banco]
        else:
            encontrados = self.chat_service.retrieval_service.retrieve_products(
                restaurant_id=restaurant.id,
                branch_id=branch.id,
                question=buscada,
                top_k=TOP_K_PADRAO if crescente is None else _TOP_K_ORDENADO,
                max_price=preco_maximo,
            )
            product_ids = [uuid.UUID(str(produto["id"])) for produto in encontrados]

        produtos = self.chat_service._hydrate_products(branch.id, product_ids)
        if crescente is not None and not da_loja:
            produtos = _ordenados_por_preco(produtos, crescente)[:_LIMITE_ORDENADO]

        # `.get`, e nao `[...]`: o cache de busca dura 20 minutos e guarda o
        # dict formatado. Nos primeiros 20 minutos depois de um deploy que
        # acrescente campo, o que volta de la ainda tem o formato antigo — e
        # uma linha de log nao pode derrubar a ferramenta.
        similaridades = [
            produto["similarity"] for produto in encontrados if produto.get("similarity") is not None
        ]
        topo = max(similaridades) if similaridades else None

        # `restaurant_id` na linha porque sem ele nao ha como atribuir uso de
        # voz a um restaurante — e cada tool call e um pedaco do custo de uma
        # sessao faturada. `branch_id` ao lado por outro motivo: e a unica
        # forma de conferir DEPOIS que a busca falou da loja certa.
        #
        # A CONSULTA PASSOU A IR EM TEXTO (24/08/2026), e a decisao merece o
        # paragrafo porque ela afrouxa uma regra que o chat de texto mantem.
        #
        # O que forcou: em 24/08/2026 o atendente respondeu "banana a
        # milanesa" a quem pediu "baiao". As duas explicacoes possiveis eram
        # opostas — a busca ter trazido lixo, ou o modelo ter consultado outra
        # palavra — e o log com digest nao separava as duas. A calibracao
        # depois mostrou que "baiao" acha "Baiao de dois" em 0,502, o topo da
        # lista: a busca estava certa e o modelo tinha buscado outra coisa.
        # Isso so foi possivel descobrir com o texto da consulta na mao, e ele
        # so existia numa aba de navegador que ja tinha sido fechada.
        #
        # Por que isto NAO e o mesmo que logar a fala do cliente: esta string
        # e escrita pelo MODELO, nao pela pessoa. E a reformulacao dele do que
        # entendeu ("sobremesa de chocolate"), ja limitada a 500 caracteres
        # pelo schema da rota, e cortada em 120 aqui. A fala crua continua sem
        # passar por este processo em momento nenhum — o audio vai do
        # navegador direto para a OpenAI.
        #
        # O digest fica ao lado, e nao foi substituido: ele correlaciona estas
        # linhas com as que foram gravadas antes desta mudanca.
        #
        # `topo` e a maior similaridade da busca. Ele existe pelo mesmo
        # motivo: com a consulta e o topo na mesma linha, "buscou errado" e
        # "buscou bem e nao havia nada" viram duas leituras diferentes, em
        # producao, sem bancada.
        # `buscada` ao lado de `consulta` porque a reescrita e um lugar novo
        # onde a pergunta pode virar outra coisa em silencio. Com as duas na
        # mesma linha, "o modelo mandou errado" e "nos reescrevemos errado"
        # sao duas leituras diferentes; com uma so, sao indistinguiveis.
        logger.info(
            "[Voz] busca | restaurant_id=%s | branch_id=%s | consulta=%.120s "
            "| buscada=%.120s | consulta_digest=%s | ordenar=%s | topo=%s "
            "| preco_maximo=%s | encontrados=%d | hidratados=%d",
            restaurant_id,
            branch_id,
            consulta,
            buscada or "-",
            _digest(consulta),
            ordenar or "-",
            "-" if topo is None else f"{topo:.3f}",
            preco_maximo,
            len(encontrados),
            len(produtos),
        )
        return produtos

    def listar_categorias(self, restaurant_id: uuid.UUID, branch_id: uuid.UUID) -> list[tuple[str, int]]:
        """As categorias vendaveis daquela loja, com a contagem de cada uma.

        Mesmas duas barreiras da busca — restaurante ativo e filial daquele
        restaurante — e pelo mesmo motivo: sao elas que impedem uma sessao de
        listar o cardapio de uma loja que nao e a dela.

        NAO gasta embedding e nao passa pelo cache de busca. Nao ha pergunta a
        vetorizar: a lista de categorias de uma filial e a mesma para qualquer
        forma de perguntar, e e uma consulta agregada indexada por filial.
        """
        restaurant = self.chat_service._get_active_restaurant(restaurant_id)
        branch = self.chat_service._get_active_branch(restaurant.id, branch_id)

        categorias = self.chat_service.product_repository.list_active_categories_with_counts(
            branch_id=branch.id,
            limite=_TETO_DE_CATEGORIAS,
        )
        logger.info(
            "[Voz] categorias | restaurant_id=%s | branch_id=%s | devolvidas=%d",
            restaurant_id,
            branch_id,
            len(categorias),
        )
        return categorias

    @staticmethod
    def resumo_das_categorias(categorias: list[tuple[str, int]]) -> str:
        """O texto que volta para a Realtime como resultado de `listar_categorias`.

        `Nome (N)`, uma por linha. A contagem entre parenteses e nao por
        extenso porque ela NAO e para ser dita como esta — o prompt manda usar
        o numero para escolher o que falar, e o preco falado ao lado ja ensina
        qual campo e literal e qual nao e.

        A NEGATIVA E POR LOJA, igual a do `resumo_para_o_modelo` e pelo mesmo
        motivo: o modelo le esta string e o prompt junto, e o mais frouxo dos
        dois e o que ele repete em audio. Loja sem categoria vendavel e loja
        com o cardapio inteiro esgotado — dizer "este restaurante nao tem
        cardapio" seria falso sobre a outra unidade.
        """
        if not categorias:
            return "Nenhuma categoria com produto disponivel nesta loja."

        linhas = [f"{nome} ({quantos})" for nome, quantos in categorias]
        return "Categorias desta loja:\n" + "\n".join(linhas)

    @staticmethod
    def frase_das_categorias(categorias: list[tuple[str, int]], comeco: int) -> str:
        """A frase pronta para ser DITA, com duas categorias e nada mais.

        Irma de `frase_para_o_modelo`, e pelo mesmo motivo: ate 25/08/2026 o
        teto de duas e a escolha de QUAIS duas eram tres linhas de prompt
        ("fale no maximo DUAS, as maiores, e diga que ha mais; o numero e para
        voce escolher"), pedindo ao modelo que ignorasse dez das doze linhas
        que esta mesma ferramenta mandava.

        `comeco` e o que a regra de prompt nao tinha como ter: o cliente que
        pergunta "e o que mais tem?" quer o que ainda NAO foi dito, e a lista
        e a mesma nas duas chamadas. Quem guarda o cursor e a sessao (revisao
        20260825_0042).

        DA A VOLTA no fim da lista para a fatia nunca sair pela metade: numa
        loja de tres categorias, comecar na terceira devolve a terceira e a
        primeira. Repetir uma que ele ja ouviu e melhor do que falar so uma
        quando ha mais.

        Vazia quando a loja nao tem categoria vendavel — mesmo contrato da
        busca: FRASE vazia quer dizer que nao ha o que oferecer, e o que dizer
        nesse caso depende da pergunta, que o modelo tem e nos nao.
        """
        if not categorias:
            return ""

        quantas = min(TETO_FALADO, len(categorias))
        nomes = [categorias[(comeco + passo) % len(categorias)][0] for passo in range(quantas)]

        if len(nomes) == 1:
            return f"Tem {nomes[0]}."
        if len(categorias) > len(nomes):
            return f"Tem {nomes[0]} e {nomes[1]}, e mais uns tipos."
        return f"Tem {nomes[0]} e {nomes[1]}."

    @staticmethod
    def resultado_das_categorias(categorias: list[tuple[str, int]], comeco: int) -> str:
        """O que volta de `listar_categorias`: a frase para dizer, e a lista.

        Os MESMOS dois blocos rotulados do resultado da busca, e isso e
        deliberado: sao duas ferramentas e um formato so, entao o que o prompt
        explica sobre FRASE e DADOS vale para as duas sem uma linha a mais.

        A lista inteira continua indo junto — e nao so as duas da frase —
        porque ela nao serve so para falar. Ela e o que o modelo le antes de
        dizer que a loja nao tem alguma coisa, e negar sem ter lido e a
        invencao que a ferramenta existe para fechar.
        """
        frase = VoiceSearchService.frase_das_categorias(categorias, comeco)
        return "\n".join(
            (
                f"FRASE: {frase}" if frase else "FRASE:",
                "DADOS (nao leia em voz alta; so para saber o que ha nesta loja):",
                VoiceSearchService.resumo_das_categorias(categorias),
            )
        )

    @staticmethod
    def frase_para_o_modelo(
        produtos: list,
        categorias: list[tuple[str, int]] | None = None,
    ) -> str:
        """A frase pronta para ser DITA, com o teto de dois ja aplicado.

        ELA EXISTE PARA TIRAR DUAS DECISOES DO MODELO (25/08/2026), e as duas
        eram secoes inteiras do prompt:

            quantos produtos citar   "NO MAXIMO DOIS PRODUTOS POR RESPOSTA",
                                     uma secao de 13 linhas cujo trabalho era
                                     pedir que ele ignorasse tres dos cinco
                                     produtos que esta mesma ferramenta mandava
            se fala o preco          "UM produto na frase: fale o preco. DOIS:
                                     so os nomes" — uma regra sobre a frase que
                                     ele ainda estava montando

        As duas viraram o mesmo `if` sobre o tamanho de uma lista, aqui. E o
        terceiro movimento da mesma familia: o preco parou de errar quando
        `preco_por_extenso` passou a entregar a forma falada, e o superlativo
        parou de errar quando o banco passou a ordenar. Nos tres, o conserto
        foi entregar o dado na forma em que ele vai ser usado.

        O RISCO QUE ISTO CRIA, e que nao existia antes: a frase e um MOLDE, e a
        armadilha 44 registra o que molde faz — em 24/08/2026 um exemplo de
        saida solto no prompt produziu precos que nunca vieram de busca
        nenhuma. A diferenca que salva este caso e o lugar: aquele molde vivia
        no prefixo CACHEADO, abstrato e disponivel a qualquer momento; este
        chega por turno, ja preenchido com o dado daquele turno. Se der errado,
        vai dar errado exatamente assim — o modelo repetindo a frase do turno
        anterior — e e isso que se procura na bancada.

        BUSCA VAZIA TAMBEM TEM FRASE, desde 25/08/2026, e isto inverteu o que
        estava escrito aqui ("nao ha frase pronta para a negativa: o que dizer
        depende do que o cliente pediu"). Dependia mesmo — e era o modelo que
        decidia, entao ele conseguia negar um produto SEM TER BUSCADO. Foi o
        que aconteceu com "voces tem picanha?" numa churrascaria; o relato
        esta em `_NEGATIVA`.

        Com a negativa vindo daqui, "nao temos" tem uma origem so, e ela e uma
        busca que voltou vazia NAQUELE turno. Nao e mais uma regra pedindo ao
        modelo que busque antes de negar: e a frase da negativa nao existir
        antes da busca.

        `categorias` entra na propria frase quando a busca nao achou nada,
        pelo mesmo motivo do teto: escolher qual categoria oferecer era mais
        uma decisao ("se vierem categorias junto, ofereca uma delas") sobre
        uma lista que este metodo tem inteira na mao.
        """
        if not produtos:
            return _frase_da_negativa(categorias)

        if len(produtos) == 1:
            return _frase_de_um(produtos[0])

        nomes = f"{produtos[0].name} e {produtos[1].name}"
        # "e mais alguns" no lugar de enumerar o resto: os outros ja estao na
        # TELA, com o preco ao lado. Dizer que ha mais e informacao; recitar
        # quais e o inventario que o teto existe para nao ler.
        if len(produtos) > TETO_FALADO:
            return f"Tem {nomes}, e mais alguns."
        return f"Tem {nomes}."

    @staticmethod
    def resultado_para_o_modelo(
        produtos: list,
        categorias: list[tuple[str, int]] | None = None,
    ) -> str:
        """O que volta da ferramenta: a frase para dizer, e os dados para consultar.

        DOIS BLOCOS ROTULADOS, e nao um formato posicional. O resumo antigo era
        quatro campos separados por "|", e o que cada posicao significava estava
        escrito no prompt — em nove bullets diferentes, que e o que se paga por
        um contrato implicito. Rotulo custa alguns tokens por busca e devolve
        essas nove linhas.

        `categorias` so entra quando a busca nao achou NADA, e e o que aposenta
        a regra "so busque um termo mais amplo se a palavra dele nao devolver
        nada": quem sabe que a busca voltou vazia e o backend, nao o modelo, e
        mandar a lista do que a loja TEM junto com o "nao achei" e dado em vez
        de mais uma instrucao.
        """
        frase = VoiceSearchService.frase_para_o_modelo(produtos, categorias)
        linhas = [
            # Sem espaco sobrando quando nao ha frase: "FRASE: " com um branco
            # no fim e um rotulo com conteudo invisivel, e o modelo le rotulo.
            # A busca sempre tem frase desde 25/08/2026 — a vazia e a negativa
            # —, mas o ramo fica: ele e o mesmo contrato das duas ferramentas.
            f"FRASE: {frase}" if frase else "FRASE:",
            "DADOS (nao leia em voz alta; so para responder pergunta sobre produto ja citado):",
            VoiceSearchService.resumo_para_o_modelo(produtos),
        ]
        if not produtos and categorias:
            linhas.append(VoiceSearchService.resumo_das_categorias(categorias))
        return "\n".join(linhas)

    @staticmethod
    def resumo_para_o_modelo(produtos: list) -> str:
        """O texto que volta para a Realtime como resultado da ferramenta.

        Quatro campos por produto, um produto por linha:

            nome | preco como se fala | descricao | serve N pessoas

        O que cada campo significa esta escrito no PROMPT, e nao aqui: o
        prompt e cacheado a partir do turno 2, e este texto e cobrado inteiro
        em toda busca. Explicacao de formato e coisa que se diz uma vez.

        O PRECO EM DIGITOS SAIU e o `serve` ENTROU na mesma rodada
        (25/08/2026). Os dois movimentos sao o mesmo movimento, e o motivo esta
        em `_linha_do_produto`: entregar o dado na forma em que ele vai ser
        DITO, e nao entregar duas formas do mesmo dado e torcer para o modelo
        escolher a certa.

        A NEGATIVA E POR LOJA, e nao por restaurante (revisao
        20260820_0026). "Nenhum produto encontrado" foi escrito quando o
        cardapio era do restaurante e a frase era verdadeira; hoje a busca ja
        chegou aqui filtrada por filial, e o produto que ela nao achou pode
        estar na outra unidade. O modelo le esta string e o prompt de voz
        junto — dizer "nesta loja" nos dois e o que impede o mais frouxo dos
        dois de ser o que ele repete em audio.

        Continua sem id, sem imagem e sem grupo de opcao, e no maximo cinco:
        isso esta na tela do cliente, vindo do JSON completo da rota.

        O PRECO FALADO ENTROU EM 25/08/2026, e o porque esta inteiro no
        cabecalho de `src/utils/money_por_extenso.py`: `R$ 34,40` saiu como
        "quarenta e quatro e quarenta" numa sessao real. O modelo TRADUZIA o
        numero de cabeca; agora ele copia. `None` — preco zerado, ausente ou
        fora da faixa — vira "-", e o prompt manda nao falar preco desse
        produto.

        A DESCRICAO ENTROU NA MESMA RODADA, e por um defeito que parecia ser
        outra coisa. Perguntado "e essa serve para quantas pessoas?", o
        atendente respondeu que a picanha "nao vem com a quantidade servida
        especifica" e que "normalmente e servida por peso". Lido de fora,
        parecia a NAO INVENTE ao contrario: negar o que se tem.

        **Ele nao tinha.** Este resumo mandava nome e preco, e mais nada — a
        descricao com "Serve 2 pessoas" existia so no `produtos`, que vai para
        a TELA. O modelo nao estava negando um dado que recebeu; estava sem
        resposta e preencheu o buraco com conhecimento geral.

        Por isso a descricao passou a vir. Regra de prompt sozinha nao
        resolveria: mandar "nao invente" a quem nao tem a informacao so troca
        a invencao por "esta na tela" — e quem esta com o telefone no ouvido
        nao esta olhando a tela, que e o mesmo argumento que faz o preco ser
        falado.

        Ela vem CORTADA em `_LIMITE_DA_DESCRICAO` caracteres, no espaco. O
        campo e texto livre do lojista: ha descricao de uma linha e ha
        paragrafo inteiro, e cinco paragrafos por busca sao dezenas de tokens
        cobrados em toda chamada para o modelo ler uma vez.

        E O `serve` VIROU CAMPO PROPRIO em 25/08/2026, o que parece redundante
        com a descricao e nao e. A descricao resolveu o caso por acidente:
        funcionava enquanto o lojista tivesse escrito "Serve 2 pessoas" no
        texto livre. Quem nao escreveu continuava sem resposta, e o modelo nao
        tinha como distinguir "serve uma pessoa" de "ninguem preencheu" — que
        e a distincao que decide entre responder e dizer que nao sabe.

        Enquanto a coluna estiver vazia no cadastro, este campo vem "-" e o
        assistente responde pela descricao, exatamente como antes. A migracao
        nao faz backfill de proposito (revisao 20260825_0039).
        """
        if not produtos:
            return "Nenhum produto encontrado nesta loja."

        linhas = [_linha_do_produto(produto) for produto in produtos[:5]]
        return "Produtos encontrados nesta loja:\n" + "\n".join(linhas)


def _frase_de_um(produto) -> str:
    """UM produto na frase leva o preco junto; e a regra, aplicada aqui.

    "Confirmar um pedido sem dizer o valor e pior do que falar demais" era um
    bullet do prompt e agora e esta linha. Produto sem preco vigente sai sem
    numero nenhum — `preco_por_extenso` devolve `None` para preco ausente,
    zerado ou fora da faixa, e inventar "por zero reais" seria o atendente
    oferecendo de graca o que ninguem precificou.
    """
    falado = preco_por_extenso(produto.price)
    if not falado:
        return f"Tem {produto.name}."
    return f"Tem {produto.name} por {falado}."


def _frase_da_negativa(categorias: list[tuple[str, int]] | None) -> str:
    """A negativa falada, com ate duas categorias para oferecer no lugar.

    "isso" e nao o nome do que ele pediu, e a escolha e deliberada: o termo que
    chegaria aqui e a CONSULTA — o que o modelo escreveu, ja reescrito por
    `_reescrever_consulta` —, e nao a palavra do cliente. Repeti-la produziria
    "aqui a gente nao tem bebida mais barata". "Isso" vem logo depois do que ele
    pediu e nao corre esse risco.

    A negativa vem primeiro e a oferta depois, sempre nessa ordem: apresentar
    outra coisa antes de dizer que nao tem o pedido e o que faz o cliente ouvir
    que ganhou uma resposta quando levou uma negativa.
    """
    if not categorias:
        return f"{_NEGATIVA}."

    nomes = [nome for nome, _ in categorias[:TETO_FALADO]]
    if len(nomes) == 1:
        return f"{_NEGATIVA}, mas tem {nomes[0]}."
    return f"{_NEGATIVA}, mas tem {nomes[0]} e {nomes[1]}."


def _ordenados_por_preco(produtos: list, crescente: bool) -> list:
    """Ordena pelo preco VIGENTE, e descarta quem nao tem preco.

    Produto sem valor nao e produto de graca: no topo de "o mais barato"
    ele seria o atendente oferecendo por zero o que ninguem precificou.
    Fora de uma consulta ordenada ele continua aparecendo normalmente —
    quem o remove e so este caminho, onde a ausencia de preco nao tem
    como ser ordenada.
    """
    com_preco = [
        produto for produto in produtos if produto.price is not None and produto.price > 0
    ]
    return sorted(com_preco, key=lambda produto: produto.price, reverse=not crescente)


def _linha_do_produto(produto) -> str:
    """`nome | preco falado | descricao | serve`, com "-" no que faltar.

    O CAMPO EM DIGITOS SAIU (25/08/2026), e a razao e que ele so servia de
    tentacao. O modelo nao pode fala-lo (o prompt proibe), nao pode somar e nao
    pode arredondar; a tela do cliente ja mostra o valor ao lado do produto. O
    que ele fazia era oferecer uma segunda forma do mesmo numero ao lado da
    forma pronta para falar — e foi de uma troca entre as duas que saiu
    "quarenta e quatro e quarenta" para um produto de R$ 34,40.

    E o `serve` entra ANEXADO NO FIM, e nao ao lado da descricao onde ficaria
    mais bonito. Campo novo no meio renumera todos os outros, e os ordinais
    ("o terceiro campo e o preco falado") estao escritos em dez pontos do
    `voice_prompt.py`. Anexar mantem 1..3 estaveis; inserir custa uma varredura
    inteira do prompt a cada campo novo, e uma contradicao silenciosa em cada
    bullet que a varredura esquecer.
    """
    falado = preco_por_extenso(produto.price)
    return " | ".join(
        (
            produto.name,
            falado or _VAZIO,
            _descricao_curta(produto.description),
            _serve_quantas_pessoas(produto),
        )
    )


def _serve_quantas_pessoas(produto) -> str:
    """`serve N pessoas`, ou "-" quando o lojista nao disse.

    "-" e o valor NULO da coluna, e o prompt le os dois como a mesma coisa: nao
    sei. Escrever "serve 1 pessoa" no lugar do nulo seria o backend inventando
    o fato que a coluna existe para parar de inventar (revisao 20260825_0039).

    Vem escrito por extenso, e nao como numero solto, pelo mesmo motivo do
    preco falado: o campo e para ser DITO, e "2" obriga o modelo a decidir
    entre "dois" e "duas". Uma decisao a menos e um erro a menos.
    """
    quantas = produto.serves_people
    if not quantas:
        return _VAZIO
    return "serve 1 pessoa" if quantas == 1 else f"serve {quantas} pessoas"


def _descricao_curta(descricao: str | None) -> str:
    """A descricao do lojista, cortada no espaco, ou "-" quando nao houver.

    Corta no ESPACO e nao no caractere: "Serve 2 pes" e pior que uma frase a
    menos, porque o modelo le o pedaco como se fosse a informacao inteira.
    """
    if not descricao or not descricao.strip():
        return _VAZIO

    limpa = " ".join(descricao.split())
    if len(limpa) <= _LIMITE_DA_DESCRICAO:
        return limpa

    cortada = limpa[:_LIMITE_DA_DESCRICAO].rsplit(" ", 1)[0]
    return f"{cortada}..." if cortada else limpa[:_LIMITE_DA_DESCRICAO]


def _digest(consulta: str) -> str:
    return hashlib.sha256(consulta.encode("utf-8")).hexdigest()[:12]
