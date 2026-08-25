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
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from src.services.chat_service import ChatService
from src.utils.money import format_money_br
from src.utils.money_por_extenso import preco_por_extenso


logger = logging.getLogger("uvicorn.error")

# O corte da descricao. Generoso o bastante para caber "Baiao e batata frita.
# Serve 2 pessoas." e curto o bastante para o paragrafo do lojista nao virar
# custo em toda busca.
_LIMITE_DA_DESCRICAO = 120

# O que ocupa o campo quando nao ha o que por. Um traco, e nao campo vazio: com
# quatro campos posicionais, "a | b |  | d" e mais facil de ler errado do que
# "a | b | - | d".
_VAZIO = "-"


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
        """
        restaurant = self.chat_service._get_active_restaurant(restaurant_id)
        branch = self.chat_service._get_active_branch(restaurant.id, branch_id)

        encontrados = self.chat_service.retrieval_service.retrieve_products(
            restaurant_id=restaurant.id,
            branch_id=branch.id,
            question=consulta,
            max_price=preco_maximo,
        )
        product_ids = [uuid.UUID(str(produto["id"])) for produto in encontrados]
        produtos = self.chat_service._hydrate_products(branch.id, product_ids)

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
        logger.info(
            "[Voz] busca | restaurant_id=%s | branch_id=%s | consulta=%.120s "
            "| consulta_digest=%s | topo=%s | preco_maximo=%s | encontrados=%d "
            "| hidratados=%d",
            restaurant_id,
            branch_id,
            consulta,
            _digest(consulta),
            "-" if topo is None else f"{topo:.3f}",
            preco_maximo,
            len(encontrados),
            len(produtos),
        )
        return produtos

    @staticmethod
    def resumo_para_o_modelo(produtos: list) -> str:
        """O texto que volta para a Realtime como resultado da ferramenta.

        Quatro campos por produto, um produto por linha:

            nome | preco em digitos | preco como se fala | descricao

        O que cada campo significa esta escrito no PROMPT, e nao aqui: o
        prompt e cacheado a partir do turno 2, e este texto e cobrado inteiro
        em toda busca. Explicacao de formato e coisa que se diz uma vez.

        A NEGATIVA E POR LOJA, e nao por restaurante (revisao
        20260820_0026). "Nenhum produto encontrado" foi escrito quando o
        cardapio era do restaurante e a frase era verdadeira; hoje a busca ja
        chegou aqui filtrada por filial, e o produto que ela nao achou pode
        estar na outra unidade. O modelo le esta string e o prompt de voz
        junto — dizer "nesta loja" nos dois e o que impede o mais frouxo dos
        dois de ser o que ele repete em audio.

        Continua sem id, sem imagem e sem grupo de opcao, e no maximo cinco:
        isso esta na tela do cliente, vindo do JSON completo da rota.

        O preco em digitos sai de `format_money_br`, o MESMO do chat de texto.
        Antes era um `:.2f` local, que produzia "R$ 23.90" com ponto enquanto
        o texto dizia "R$ 23,90" com virgula.

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
        """
        if not produtos:
            return "Nenhum produto encontrado nesta loja."

        linhas = [_linha_do_produto(produto) for produto in produtos[:5]]
        return "Produtos encontrados nesta loja:\n" + "\n".join(linhas)


def _linha_do_produto(produto) -> str:
    """`nome | preco | preco falado | descricao`, com "-" no que faltar."""
    falado = preco_por_extenso(produto.price)
    return " | ".join(
        (
            produto.name,
            format_money_br(produto.price) if falado else _VAZIO,
            falado or _VAZIO,
            _descricao_curta(produto.description),
        )
    )


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
