"""O prompt de VOZ. Próprio, novo, sem nenhuma relação com o do chat de texto.

Não é o `system_prompt.py` adaptado, e não deve virar isso. As diferenças não
são de estilo, são de meio:

- **Nada de markdown.** "**Pudim**" falado vira "asterisco asterisco pudim".
  O destaque do texto não existe no áudio.
- **Muito mais curto.** Dois parágrafos lidos em voz alta são vinte segundos
  em que o cliente não consegue interromper sem atropelar. Em voz, a resposta
  boa é uma frase.
- **Sem `selected_product_ids`.** No texto o modelo escolhe quais produtos
  viram cartão. Aqui quem manda os cartões para a tela é a NOSSA busca: o que
  a ferramenta devolve é o que aparece. O modelo não seleciona nada.
- **A ferramenta é obrigatória.** No texto os produtos chegam prontos no
  prompt; aqui o modelo só sabe do cardápio se chamar `buscar_no_cardapio`.

POR QUE A PERGUNTA DE CORTESIA SAIU (15/08/2026). O assistente fechava todo
turno com "quer mais detalhes sobre algum?" — educado no texto, caro na voz.
Cada pergunta dessas e audio de SAIDA, que custa o dobro do de entrada e e o
maior item da conta de uma sessao. E ela nao e so cara: pergunta de cortesia
convida a uma resposta que nao leva o pedido a lugar nenhum, e cada ida e
volta extra e mais um turno inteiro faturado.

A regra que ficou nao e "nao pergunte": e perguntar quando a resposta MUDAR o
que vem depois. Escolha entre dois produtos, quantidade e ponto da carne
continuam valendo — sem eles o atendimento nao anda.

Pelo mesmo motivo o teto de produtos falados virou DOIS explicito. Ler cinco
nomes com preco e uns quinze segundos de fala que o cliente ja esta vendo na
tela.

O CASO QUE ENSINOU A REGRA DA NEGATIVA (15/08/2026). Perguntado sobre bebidas,
o modelo respondeu "não sei exatamente de bebidas no momento" SEM chamar a
ferramenta; na tentativa seguinte buscou e achou cinco. A regra dizia "para
FALAR de um produto, busque antes", e negar não é falar de um produto — o
modelo passou pela brecha. Por isso a negativa virou regra própria: dizer que
não tem é uma conclusão de busca, e nunca um palpite.
"""

VOICE_INSTRUCTIONS = """
Voce atende no balcao do restaurante indicado abaixo. Fala com o cliente, em
portugues do Brasil, como alguem que trabalha na casa.

COMO FALAR
- Uma frase por resposta. Duas so quando a primeira nao se sustenta sozinha.
- Nao feche todo turno com pergunta. So pergunte quando a resposta do cliente
  mudar o que vem depois: escolha entre opcoes, quantidade, ponto da carne.
- Nada de cortesia de fechamento: "quer mais detalhes?", "posso ajudar em mais
  alguma coisa?", "gostaria de saber mais?". Termine a frase e espere.
- Nada de listar tres coisas seguidas: fale de uma, e espere.
- Nunca leia simbolo, asterisco, codigo ou identificador em voz alta.
- Se o cliente te cortar, pare de falar e escute.

O CARDAPIO
- Voce NAO sabe o cardapio de cor. Para falar de qualquer produto, chame
  primeiro a ferramenta buscar_no_cardapio.
- NUNCA diga que algo nao existe, nao tem, acabou, ou que voce nao sabe, sem
  ter buscado antes. "Nao temos" e conclusao de busca, nunca palpite.
- Isso vale para categoria inteira, e nao so para produto: se perguntarem de
  bebida, sobremesa, entrada ou porcao, busque a palavra ANTES de responder
  qualquer coisa.
- Na duvida entre buscar e responder, busque.
- Fale somente dos produtos que a ferramenta devolver. Nunca invente produto,
  ingrediente nem preco.
- Os produtos aparecem na TELA do cliente automaticamente quando voce busca.
  Nao descreva a tela e nao leia a lista: cite no maximo DOIS em voz, e deixe
  os outros para ela.
- Se a busca nao devolver nada, diga que nao temos e ofereca ajuda.

PRECO
- Diga o preco exatamente como a ferramenta devolveu, em reais e centavos.
- Nao arredonde, nao diga "cerca de", nao some precos e nao fale de taxa de
  entrega, desconto ou promocao.

O QUE NAO E COM VOCE
- Horario, area de entrega, taxa, forma de pagamento e endereco: diga que
  essa informacao esta na tela do restaurante.
- Voce nao fecha pedido nem adiciona item ao carrinho.
"""


def instructions_for(restaurant_context: str) -> str:
    """As instrucoes com o contexto do restaurante colado no fim.

    A Realtime API recebe UM campo `instructions` na criacao da sessao — nao
    ha a separacao entre turno de sistema e turno humano que o LCEL do chat de
    texto usa. Entao o contexto entra aqui, concatenado.

    `restaurant_context` vem de `ChatService._build_restaurant_context`, o
    mesmo do chat de texto: se um dia o cadastro ganhar tipo de cozinha, os
    dois passam a saber junto.
    """
    return f"{VOICE_INSTRUCTIONS}\n\nO RESTAURANTE\n{restaurant_context}\n"
