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


logger = logging.getLogger("uvicorn.error")


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

        # `restaurant_id` na linha porque sem ele nao ha como atribuir uso de
        # voz a um restaurante — e cada tool call e um pedaco do custo de uma
        # sessao faturada. `branch_id` ao lado por outro motivo: e a unica
        # forma de conferir DEPOIS que a busca falou da loja certa, ja que a
        # consulta em si nao vai para o log. O digest da consulta segue a mesma regra do chat de
        # texto: correlaciona requisicoes sem colocar no log o que a pessoa
        # falou.
        logger.info(
            "[Voz] busca | restaurant_id=%s | branch_id=%s | consulta_digest=%s "
            "| preco_maximo=%s | encontrados=%d | hidratados=%d",
            restaurant_id,
            branch_id,
            _digest(consulta),
            preco_maximo,
            len(encontrados),
            len(produtos),
        )
        return produtos

    @staticmethod
    def resumo_para_o_modelo(produtos: list) -> str:
        """O texto que volta para a Realtime como resultado da ferramenta.

        A NEGATIVA E POR LOJA, e nao por restaurante (revisao
        20260820_0026). "Nenhum produto encontrado" foi escrito quando o
        cardapio era do restaurante e a frase era verdadeira; hoje a busca ja
        chegou aqui filtrada por filial, e o produto que ela nao achou pode
        estar na outra unidade. O modelo le esta string e o prompt de voz
        junto — dizer "nesta loja" nos dois e o que impede o mais frouxo dos
        dois de ser o que ele repete em audio.

        So nome e preco, e no maximo cinco. O modelo NAO precisa de id, imagem
        nem grupo de opcao — isso ja esta na tela do cliente, vindo do JSON
        completo da rota. Mandar o objeto inteiro para o modelo seria pagar
        token de audio para ele nao usar nada disso.

        O preco sai de `format_money_br`, o MESMO do chat de texto. Antes era
        um `:.2f` local, que produzia "R$ 23.90" com ponto enquanto o texto
        dizia "R$ 23,90" com virgula — e o prompt de voz manda dizer o preco
        "exatamente como a ferramenta devolveu" (`prompt_de_voz.py`). O modelo
        estava sendo instruido a copiar um numero em formato que nao e o do
        resto do sistema.
        """
        if not produtos:
            return "Nenhum produto encontrado nesta loja."

        linhas = [f"{produto.name} - {format_money_br(produto.price)}" for produto in produtos[:5]]
        return "Produtos encontrados: " + "; ".join(linhas)


def _digest(consulta: str) -> str:
    return hashlib.sha256(consulta.encode("utf-8")).hexdigest()[:12]
