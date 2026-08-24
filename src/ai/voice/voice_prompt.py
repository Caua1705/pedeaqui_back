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

POR QUE A LOJA ENTRA NO CONTEXTO (20/08/2026). O cardápio passou a ser da
FILIAL (revisão `20260820_0026`): a busca já devolve só o que aquela loja
vende, e `/voice/session` já recusa filial que não existe. O que faltava era
o modelo SABER disso — e faltava justamente na regra da negativa, que é a
única frase do prompt em que ele fala do cardápio inteiro de uma vez.

"Não temos pudim", dito sobre o restaurante, é falso desde que a segunda loja
entrou: o pudim pode estar na outra unidade. E é uma negativa que ninguém
conserta depois — o cliente que ouviu isso não volta para conferir, e não há
tela onde o "nesta loja" pudesse estar escrito pequeno embaixo. Por isso a
negativa ficou explicitamente por loja, aqui e no resumo que a ferramenta
devolve (`search_service.resumo_para_o_modelo`): os dois dizem a mesma coisa,
porque o modelo lê os dois e o mais frouxo é o que vale.

E o nome da loja entra para o modelo se SITUAR, não para ser falado. Cada
palavra dita é áudio de saída, o item mais caro da sessão, e o cliente
escolheu a loja na tela antes de apertar o microfone: repeti-la em voz alta é
pagar para informar o que ele acabou de escolher.

O ANÚNCIO DA BUSCA SAIU (24/08/2026). O atendente dizia "vou buscar a
informação correta para você, só um instante" antes de chamar a ferramenta.
São ~3,2 s de áudio de saída, ~64 tokens, **US$ 0,0013 por busca** — e numa
bancada de catorze turnos com cinco buscas isso deu US$ 0,0065, o mesmo que o
turno mais caro da sessão inteira. Áudio pago para não dizer nada. A busca
acontece em silêncio; quem espera é o cliente, e ele já sabe que perguntou.

O PREÇO DEIXOU DE SER SEMPRE (24/08/2026). Ele custa: "cinquenta e sete reais
e dezesseis centavos" são ~2,5 s de saída (~50 tokens, US$ 0,0010), e a forma
curta do balcão — "cinquenta e sete e dezesseis" — corta ~44% disso sem perder
o número. Numa conversa de dois minutos com quatro preços ditos, é entre 5% e
10% da sessão.

**Mas tirar o preço do áudio inteiro seria errado**, e a razão é a mesma que
faz o aviso de inatividade ser falado: quem está com o telefone no ouvido não
está olhando a tela. Preço é o que decide o pedido. Por isso a regra ficou
sendo QUANDO, e não SE: preço quando ele muda a decisão (produto sendo
confirmado, pergunta direta), nome só quando são dois produtos e a tela mostra
os valores ao lado.

E o medo do arredondamento errado não se resolve calando — se resolve na regra
de COPIAR o valor exato, que é a que ficou.

A NEGATIVA POR NOME, E O LIMIAR QUE NÃO EXISTIU (24/08/2026). Perguntado por
"baião", o atendente respondeu "temos banana à milanesa por 35 reais e 30
centavos". A busca não escolhe: ela devolve os cinco mais próximos, e o prompt
manda falar o que a ferramenta devolveu — então ele obedeceu.

A primeira proposta foi um limiar só da voz: abaixo de X, o resumo diria "nada
com esse nome" em vez de afirmar. **Foi medido e recusado.** No cardápio real
(`scripts/afere_limiar_de_similaridade.py`, 24/08/2026):

    "baião"              -> 0,502  Baião de dois        (acerto)
    "algo vegetariano"   -> 0,374  Filé ao poivre vert  (pergunta legítima)

Não há corte entre 0,374 e 0,502. Qualquer número que pegasse o erro mataria
"algo vegetariano" — a mesma sobreposição que barrou subir o
`AI_SEARCH_MIN_SIMILARITY` do `/chat`, e pelo mesmo motivo.

E a medição mostrou outra coisa: **a busca estava certa.** "baião" acha "Baião
de dois" no topo. Se o modelo tivesse consultado "baião", teria recebido o
produto certo — logo ele consultou outra palavra. O defeito é de percepção do
áudio, não de relevância da busca, e limiar nenhum alcança isso.

Por isso a regra que ficou é condicionada ao que a FERRAMENTA devolveu, e não
a um número nosso: se o cliente disse um nome e nenhum dos nomes que voltaram
é aquele, isso o modelo compara sozinho. Mais uma regra para ele perguntar o
nome de novo quando não tiver entendido — uma pergunta curta custa menos que
oferecer o produto errado.

O QUE O `consulta=` NO LOG REVELOU (24/08/2026). A linha da busca passou a
gravar o texto que o modelo consultou, e ele respondeu a pergunta de uma vez:

    [eu]          Quero um baião.
    [tool]        buscar_no_cardapio {"consulta":"feijoada"}
    [assistente]  Aqui não temos feijoada, mas temos o feijão tropeiro...

**Ele ouviu certo e reescreveu a consulta sozinho.** A transcrição registrou
"baião"; a busca acha "Baião de dois" em 0,502. Não era o áudio (a hipótese de
"ouviu errado") nem a relevância (a hipótese do limiar): era o modelo
traduzindo "baião" por "um prato de feijão" antes de buscar — e depois negando
um produto que ninguém tinha pedido.

Daí a regra do TERMO LITERAL, e o caso enumerado dentro dela. Critério não
morde; enumeração morde — a mesma lição do `system_prompt.py` do texto.

E daí a seção NAO INVENTE, que na mesma sessão teve três provas:

    "Tem sobremesa?"          -> "trinta e quatro e noventa", SEM ter buscado
    "Qual é o seu nome?"      -> "o preço exato é 24 e 90"
    "almoçar e sobremesa?"    -> "já ajusto o ponto da carne" (ninguém falou
                                 de carne em nenhum momento da sessão)

Preço falso dito com confiança é pior que resposta errada: o cliente chega no
checkout com outro valor. Por isso a regra dura — sem busca NAQUELE turno, não
se fala preço — e por isso a seção ficou alta na página, antes de O CARDAPIO.

O EXEMPLO DE PREÇO QUE ESTE ARQUIVO ENSINOU A ERRAR (24/08/2026). A versão
anterior desta seção trazia, como ilustração da forma curta, a frase
`"trinta e cinco e trinta"`. Na sessão seguinte apareceram `"trinta e quatro e
noventa"` e `"vinte e quatro e noventa"` — mesma forma, mesmo ritmo, números
que não vieram de lugar nenhum. Na sessão anterior, sem essa linha, os dois
preços falados eram reais.

Não é prova, e o `system_prompt.py` do texto tem `Exemplo: "R$ 23,90"` sem
sintoma. A diferença é onde o exemplo está ancorado: no texto ele ilustra a
**fonte** (o campo `price`, como ele já vem escrito); aqui ele ilustrava a
**fala**, e era uma string pronta para ser dita, solta no prompt. Exemplo de
saída falada é molde; exemplo de campo de origem é referência. O que ficou é o
par fonte -> fala: `"R$ 43,50"` vira `"quarenta e três e cinquenta"`.

POR QUE O "BUSQUE CALADO" MUDOU DE SEÇÃO (24/08/2026). A regra funcionou —
cinco respostas daquela sessão saíram sem áudio nenhum, que é exatamente o que
uma busca silenciosa produz. Mas vazou uma vez, em "só um momento, vou buscar
as opções de entrada agora".

Três razões, e a segunda é a que importa: a enumeração não cobria a frase (ela
morde nas strings enumeradas, e as minhas vieram de uma amostra só); **ela era
a única regra do prompt que proibia sem autorizar o substituto** — as irmãs
terminam em "e espere", a dela terminava numa sequência, e sobrava silêncio
que o modelo é treinado a preencher; e estava em O CARDAPIO, que é sobre o que
é verdade dos produtos, não sobre quando falar.

Por isso ela subiu para COMO FALAR, a lista de frases proibidas cresceu, e o
silêncio passou a ser explicitamente autorizado em vez de sobrar.

A SAUDAÇÃO AUTOMÁTICA, E POR QUE ELA NÃO ESTÁ NO PROMPT (24/08/2026). Quando o
cliente aperta o botão de falar, o atendente cumprimenta sozinho, antes de a
pessoa dizer qualquer coisa. A frase sai daqui (`saudacao_para`), viaja na
resposta do `/voice/session` e é falada por um `response.create` que a página
dispara com a instrução daquele turno só.

**Ela não entra no `VOICE_INSTRUCTIONS`, e são duas razões.**

A primeira é a armadilha 44 deste repositório: frase pronta no prompt é molde,
e molde o modelo preenche sozinho depois — foi assim que `"trinta e cinco e
trinta"` virou preço inventado na sessão seguinte. Instrução de turno único
não fica no prefixo, e por isso não vira formulário para o resto da conversa.

A segunda é o nome do cliente, que muda a cada sessão. O prefixo das
instruções é justamente o que a OpenAI mantém em cache — numa sessão medida,
33.920 dos 35.742 tokens de texto de entrada — e nome dentro do prefixo é um
prefixo diferente por cliente, ou seja, cache nenhum.

E ela é CURTA porque é áudio de saída, o item mais caro da conta, em toda
sessão — inclusive nas que o cliente abandona no segundo seguinte.

O CUMPRIMENTO QUE VIROU BUSCA, E O PRODUTO QUE NAO SAIA DE CENA (24/08/2026).
Duas falhas da mesma sessão, e as duas na porta de entrada da conversa:

    [eu]          Olá, tudo bem?
    [tool]        buscar_no_cardapio {"consulta":"sobremesa"}
    [assistente]  Temos pudim, que custa um centavo, e também temos brownie...

    [eu]          Tem sobremesa aí?     -> "aqui temos a picanha suína por..."
    [eu]          Qual é o seu nome?    -> "você quer a picanha suína?"

A primeira é a NAO INVENTE furando antes de a conversa começar: ele não tinha o
que buscar e escolheu um assunto sozinho. A regra ficou colada em "na duvida
entre buscar e responder, busque" **de propósito** — é essa linha que empurra
para buscar num cumprimento, e a exceção precisa ser lida junto dela, não três
parágrafos depois.

A segunda é o oposto: em vez de assunto novo inventado, o assunto velho que não
sai. Pergunta nova é pergunta nova, e o produto do turno anterior sai de cena
quando o cliente muda de assunto — inclusive para não virar confirmação de um
pedido que ninguém fez.

Nos dois casos o exemplo enumerado é a FALA DO CLIENTE e a FALHA, nunca a frase
certa: escrever aqui como a resposta boa soa é o molde da armadilha 44, que
este arquivo já pagou uma vez.

E vale notar que a saudação automática derruba boa parte do primeiro caso
sozinha: com o atendente cumprimentando primeiro, "olá, tudo bem?" deixa de ser
a porta de entrada.
"""

import random
import re

from src.models.branch_model import Branch


VOICE_INSTRUCTIONS = """
Voce atende no balcao de UMA loja do restaurante indicado abaixo. Fala com o
cliente, em portugues do Brasil, como alguem que trabalha na casa.

COMO FALAR
- Uma frase por resposta. Duas so quando a primeira nao se sustenta sozinha.
- Nao feche todo turno com pergunta. So pergunte quando a resposta do cliente
  mudar o que vem depois: escolha entre opcoes, quantidade, ponto da carne.
- Nada de cortesia de fechamento: "quer mais detalhes?", "posso ajudar em mais
  alguma coisa?", "gostaria de saber mais?". Termine a frase e espere.
- Nada de listar tres coisas seguidas: fale de uma, e espere.
- Nunca leia simbolo, asterisco, codigo ou identificador em voz alta.
- Nao diga o nome da loja em voz alta: o cliente ja escolheu ela na tela.
- Se o cliente te cortar, pare de falar e escute.
- Enquanto voce busca, FIQUE CALADO. O silencio da busca e esperado e dura
  pouco; nao e sua funcao preencher. Nao anuncie, nao narre, nao avise.
- Nada destas frases, nem parecidas com elas: "so um momento", "so um
  instante", "vou verificar", "vou buscar", "deixa eu ver", "deixa eu
  conferir", "ja te digo", "agora mesmo", "um segundo". Chame a ferramenta e
  so volte a falar com o resultado na mao.

NAO INVENTE
- Voce so sabe o que a ferramenta devolveu NESTE turno, e o que o cliente
  falou. Fora isso voce nao sabe nada.
- Sem ter buscado neste turno, voce NAO fala preco nenhum: nem numero, nem
  "por volta de", nem o que voce lembra de antes na conversa. Voce nao tem
  preco na memoria.
- Nunca traga assunto que o cliente nao trouxe. Ponto da carne, tamanho,
  acompanhamento, quantidade, bebida junto: so se ELE falar primeiro.
- Pergunta nova apaga o produto anterior. Assim que o cliente muda de assunto,
  o produto de que voce falou antes saiu de cena: nao o traga de volta, nao
  confirme pedido que ninguem fez, e nao responda a pergunta nova com ele.
- Isto ja aconteceu, e nao pode se repetir:
    "Tem sobremesa ai?"  respondido com a picanha do turno anterior
    "Qual e o seu nome?" respondido com "voce quer a picanha suina?"
- Pergunta que nao e sobre produto nao se responde com produto nem com preco.
- Nao entendeu o que ele disse? Diga que nao entendeu e pergunte. Uma frase
  curta. Nunca preencha o buraco com o que parece plausivel.
- Isto ja aconteceu, e nao pode se repetir:
    "Tem sobremesa?" respondido com "trinta e quatro e noventa", sem busca
    "Qual e o seu nome?" respondido com um preco
    "Da pra almocar e comer sobremesa?" respondido com "ja ajusto o ponto
      da carne" — ninguem tinha falado de carne

O CARDAPIO
- Voce NAO sabe o cardapio de cor. Para falar de qualquer produto, chame
  primeiro a ferramenta buscar_no_cardapio.
- Busque com A PALAVRA QUE O CLIENTE FALOU, literal. Nao traduza, nao troque
  por sinonimo, nao "melhore" o termo. Quem decide se aquilo existe e a
  busca, nao voce.
- Nome que voce nao conhece: busque esse nome mesmo assim.
- Isto ja aconteceu, e nao pode se repetir:
    ele disse "baiao", voce buscou "feijoada"
  "baiao" busca "baiao". "x-tudo" busca "x-tudo". "guarana" busca "guarana".
- So busque um termo mais amplo se a palavra dele nao devolver nada — e, ai,
  diga que aqui nao tem o que ele pediu antes de oferecer outra coisa.
- NUNCA diga que algo nao existe, nao tem, acabou, ou que voce nao sabe, sem
  ter buscado antes. "Nao temos" e conclusao de busca, nunca palpite.
- Isso vale para categoria inteira, e nao so para produto: se perguntarem de
  bebida, sobremesa, entrada ou porcao, busque a palavra ANTES de responder
  qualquer coisa.
- A busca devolve o cardapio DESTA loja. Toda negativa sua vale so para aqui:
  diga que AQUI nao temos, nunca que o restaurante nao tem, e nunca que tem em
  outra loja.
- Na duvida entre buscar e responder, busque.
- A UNICA excecao: cumprimento nao e consulta ao cardapio. "Oi", "ola", "tudo
  bem?", "bom dia", "boa noite", "e ai": responda o cumprimento em uma frase e
  espere ele dizer o que quer. So busque se ele disser junto o que quer — "oi,
  tem sobremesa?" e pedido de sobremesa.
- Isto ja aconteceu, e nao pode se repetir:
    ele disse "Ola, tudo bem?", voce buscou "sobremesa"
- Fale somente dos produtos que a ferramenta devolver. Nunca invente produto,
  ingrediente nem preco.
- Os produtos aparecem na TELA do cliente automaticamente quando voce busca.
  Nao descreva a tela e nao leia a lista: cite no maximo DOIS em voz, e deixe
  os outros para ela.
- Se a busca nao devolver nada, diga que aqui nao temos e ofereca ajuda.
- Se o cliente pediu um produto PELO NOME e nenhum nome que a ferramenta
  devolveu e aquele, diga PRIMEIRO que aqui nao temos esse, e so depois
  ofereca o mais parecido. Nunca apresente um nome diferente como se fosse o
  que ele pediu.
- Se voce nao entendeu bem o nome que ele falou, pergunte o nome de novo antes
  de buscar. Uma pergunta curta custa menos que oferecer o produto errado.

PRECO
- Preco so sai da ferramenta, e so no turno em que voce buscou. Ver NAO
  INVENTE.
- Diga o preco so quando ele decidir alguma coisa: um produto que o cliente
  esta confirmando, ou pergunta direta de preco. Citando dois produtos, fale
  so os nomes — os valores estao na tela, ao lado.
- Copie o valor EXATO do resultado da ferramenta e mude so a forma de falar:
  "R$ 43,50" vira "quarenta e tres e cinquenta", e nunca "quarenta e tres
  reais e cinquenta centavos".
- Produto que a ferramenta devolveu sem valor: nao fale preco dele.
- Nao arredonde, nao diga "cerca de", nao some precos e nao fale de taxa de
  entrega, desconto ou promocao.

O QUE NAO E COM VOCE
- Horario, area de entrega, taxa, forma de pagamento e endereco: diga que
  essa informacao esta na tela da loja.
- Outra loja da rede: voce atende so esta, e as outras estao na tela.
- Voce nao fecha pedido nem adiciona item ao carrinho.
- Conversa fora do assunto: responda em UMA frase curta e volte ao cardapio.
  "Qual e o seu nome?" se responde dizendo o nome — nunca com produto, nunca
  com preco.
"""


SAUDACOES_COM_NOME = (
    "Olá, {nome}! Como posso te ajudar hoje?",
    "Oi, {nome}! O que vai ser hoje?",
    "{nome}, tudo bem? Como posso te ajudar?",
)

# A QUEDA, para cadastro que não entrega um primeiro nome dizível. Ela existe
# porque `customers.name` é texto livre: há espaço em branco, há "12345", há
# e-mail inteiro e há quem tenha digitado no campo errado. Falar isso em voz
# alta é pior do que não falar nome nenhum.
SAUDACOES_SEM_NOME = (
    "Olá! Como posso te ajudar hoje?",
    "Oi! O que vai ser hoje?",
    "Tudo bem? Como posso te ajudar?",
)

# Letra (com acento), podendo ter hífen ou apóstrofo NO MEIO: "Ana", "João",
# "Jean-Pierre", "D'Angelo" passam. Dígito, arroba e pontuação solta ficam de
# fora — é o formato do lixo, não o de um nome.
NOME_DIZIVEL = re.compile(r"^[^\W\d_]+(?:[-'][^\W\d_]+)*$")
TAMANHO_MAXIMO_DO_NOME = 20


def primeiro_nome_dizivel(nome_cadastrado: str | None) -> str | None:
    """O primeiro nome do cliente, ou `None` quando não dá para falá-lo.

    `None` não é erro: é a porta para a variação sem nome. Ver
    SAUDACOES_SEM_NOME para o que mora nesse campo de verdade.
    """
    if not nome_cadastrado:
        return None

    partes = nome_cadastrado.strip().split()
    if not partes:
        return None

    primeiro = partes[0]
    if not 2 <= len(primeiro) <= TAMANHO_MAXIMO_DO_NOME:
        return None
    if not NOME_DIZIVEL.match(primeiro):
        return None
    return primeiro


def saudacao_para(nome_cadastrado: str | None) -> str:
    """A frase que o atendente fala sozinho, antes de o cliente falar.

    Sorteada entre três para a segunda sessão do mesmo cliente não soar
    gravada. O porquê de ela não morar no `VOICE_INSTRUCTIONS` está no
    cabeçalho deste arquivo.
    """
    primeiro = primeiro_nome_dizivel(nome_cadastrado)
    if primeiro is None:
        return random.choice(SAUDACOES_SEM_NOME)
    return random.choice(SAUDACOES_COM_NOME).format(nome=primeiro)


def branch_context_for(branch: Branch) -> str:
    """A loja que este atendente atende, em uma linha.

    SO o nome, e isso e decisao. Endereco, telefone e horario tambem sao da
    filial (revisao `20260818_0025`), e nenhum deles entra: sao exatamente os
    assuntos que a secao "O QUE NAO E COM VOCE" manda devolver para a tela, e
    o que entra aqui e reenviado em TODA resposta do modelo.

    `display_name` na frente porque e o nome que o lojista escolheu mostrar ao
    cliente; `name` e o interno, e serve de queda porque `display_name` e
    anulavel. O `or` esta certo aqui e nao contradiz a herança da filial: isto
    e rotulo, nao termo comercial — nome vazio e nome ausente sao a mesma
    coisa para quem vai ler.
    """
    return f"Loja: {branch.display_name or branch.name}"


def instructions_for(restaurant_context: str, branch_context: str) -> str:
    """As instrucoes com o contexto do restaurante e o da loja colados no fim.

    A Realtime API recebe UM campo `instructions` na criacao da sessao — nao
    ha a separacao entre turno de sistema e turno humano que o LCEL do chat de
    texto usa. Entao o contexto entra aqui, concatenado.

    `restaurant_context` vem de `ChatService._build_restaurant_context`, o
    mesmo do chat de texto: se um dia o cadastro ganhar tipo de cozinha, os
    dois passam a saber junto. `branch_context` vem de `branch_context_for`, e
    e so da voz — no texto o cliente le a resposta com o cardapio da loja na
    mesma tela, e na voz nao ha essa tela.

    As duas secoes ficam SEPARADAS de proposito. Juntas numa so, "Loja:
    Aldeota" viraria mais uma linha do bloco do restaurante, e a regra do
    prompt que fala em "esta loja" nao teria a que apontar.
    """
    return (
        f"{VOICE_INSTRUCTIONS}\n\n"
        f"O RESTAURANTE\n{restaurant_context}\n\n"
        f"A LOJA\n{branch_context}\n"
    )
