"""A regra de "este produto tem como ser vendido", num lugar so.

GRUPO OBRIGATORIO EXISTE PORQUE A COZINHA NAO PRODUZ SEM AQUELA INFORMACAO.
Quando o lojista desativa a ultima opcao ativa de um deles — e ele desativa
opcao todo dia —, o produto deixa de ter como ser vendido: nao ha o que
escolher, e mandar para a chapa uma picanha sem ponto e pior que nao vender.

A regra vale em TRES lugares, e por isso mora aqui em vez de em qualquer um
deles:

- `MenuService` tira o produto do cardapio publico;
- `OrderService` recusa o pedido de quem ja o tinha no carrinho;
- `AdminMenuService` marca `unavailable_by_required_group` para o lojista
  descobrir — sem isso ele perde a venda em silencio.

**Existe uma quarta expressao da mesma regra, e ela nao tem como compartilhar
codigo com esta:** `AdminMenuRepository.product_ids_blocked_by_required_group`
faz a mesma pergunta em SQL, para a listagem do painel nao virar uma consulta
por produto. As duas precisam mudar juntas — se divergirem, a lista do painel
marca um conjunto de produtos e a tela de edicao marca outro.
"""


def blocking_required_group(product):
    """O grupo obrigatorio que tira este produto de venda, ou `None`.

    Devolve o GRUPO, e nao um booleano, porque quem chama precisa nomea-lo: o
    log e o aviso ao lojista sem o nome do grupo ("um grupo obrigatorio ficou
    vazio") mandam procurar em todos.

    Grupo desativado nao conta: o lojista desligou o passo inteiro de
    proposito, e ai nao ha exigencia nenhuma a cumprir.
    """
    for group in product.option_groups:
        if not group.is_active or not group.is_required:
            continue
        if not any(option.is_active for option in group.options):
            return group
    return None
