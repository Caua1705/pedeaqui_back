"""A ferramenta de busca do experimento de voz.

TUDO AQUI E REUSO. Nao ha uma linha de busca vetorial nem de montagem de
produto neste arquivo, e e proposital: se o experimento reimplementasse
qualquer um dos dois, ele estaria medindo outro sistema, e a conclusao nao
valeria para o produto.

O que e reusado, e de onde:

| O que | De onde | Por que importa |
|---|---|---|
| busca vetorial + cache + preco vigente | `RetrievalService.retrieve_products` | mesma consulta, mesmo filtro por restaurante e por produto vendavel |
| hidratacao dos produtos | `ChatService._hydrate_products` | **nome e preco que chegam ao cliente vem do banco**, nunca do modelo |
| 404 de restaurante | `ChatService._get_active_restaurant` | mesma barreira antes de gastar embedding |

A diferenca de desenho em relacao ao chat de texto, e ela e o ponto do
experimento: **aqui o modelo nao escolhe produto.** No texto ele devolve
`selected_product_ids` e `_validate_selected_product_ids` descarta o que ele
inventou. Na voz, quem manda os cartoes para a tela e a NOSSA busca — o
modelo so recebe um resumo para falar. Nao ha id passando pelo modelo, entao
nao ha id para ele inventar.

FRAGILIDADE CONHECIDA: `_hydrate_products` e `_get_active_restaurant` sao
privados do `ChatService`. Chamar de fora e feio e acopla o experimento a
nomes que ninguem prometeu manter. A alternativa era copiar as seis linhas,
que e pior — copia nao acompanha conserto. Se isto virar produto, os dois
sobem para um lugar compartilhado antes de qualquer outra coisa.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from src.services.chat_service import ChatService


logger = logging.getLogger("uvicorn.error")


class VozBuscaService:
    def __init__(self, db: Session):
        # UM ChatService, e nao um RetrievalService solto ao lado: os dois
        # reusos saem do mesmo objeto, entao nao ha como o experimento buscar
        # por um caminho e hidratar por outro.
        self.chat_service = ChatService(db)

    def buscar(self, restaurant_id: uuid.UUID, consulta: str) -> list:
        """Os produtos que a busca encontrou, hidratados do banco.

        Devolve a lista inteira: diferente do chat de texto, nao ha selecao do
        modelo no meio. O que a busca acha e o que aparece na tela.
        """
        restaurant = self.chat_service._get_active_restaurant(restaurant_id)

        encontrados = self.chat_service.retrieval_service.retrieve_products(
            restaurant_id=restaurant.id,
            question=consulta,
        )
        product_ids = [uuid.UUID(str(produto["id"])) for produto in encontrados]
        produtos = self.chat_service._hydrate_products(restaurant.id, product_ids)

        logger.info(
            "[Experimento voz] busca | encontrados=%d | hidratados=%d",
            len(encontrados),
            len(produtos),
        )
        return produtos

    @staticmethod
    def resumo_para_o_modelo(produtos: list) -> str:
        """O texto que volta para a Realtime como resultado da ferramenta.

        So nome e preco, e no maximo cinco. O modelo NAO precisa de id, imagem
        nem grupo de opcao — isso ja esta na tela do cliente, vindo do JSON
        completo da rota. Mandar o objeto inteiro para o modelo seria pagar
        token de audio para ele nao usar nada disso.
        """
        if not produtos:
            return "Nenhum produto encontrado."

        linhas = [f"{produto.name} - R$ {produto.price:.2f}" for produto in produtos[:5]]
        return "Produtos encontrados: " + "; ".join(linhas)
