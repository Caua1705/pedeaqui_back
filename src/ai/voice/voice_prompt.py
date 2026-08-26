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

===========================================================================
LEIA ISTO ANTES DO RESTO DO ARQUIVO (25/08/2026)

O prompt ENCOLHEU 96 linhas e 1.479 tokens — de 243/3.950 para 147/2.471 —
e boa parte das seções deste cabeçalho justifica regras que **não estão
mais lá**. Elas não foram apagadas por estarem erradas: foram apagadas
porque o trabalho delas passou a ser feito por código.

**Por que.** A hipótese "mais uma regra resolve" não se sustentou: várias
rodadas, cada uma consertando um caso enumerado, e o teste seguinte trazendo
outro. O que funcionou, as três vezes, foi o contrário — tirar a decisão do
modelo entregando o dado já na forma em que ele vai ser usado:

    preço falado      `preco_por_extenso`, e o modelo parou de converter
    superlativo       `list_active_by_price`, e o banco passou a ordenar
    teto e preço      `frase_para_o_modelo`, e a frase chega pronta

**O que saiu daqui, e para onde foi:**

| Saiu | Foi para |
|---|---|
| "NO MÁXIMO DOIS PRODUTOS POR RESPOSTA" (seção inteira) | `frase_para_o_modelo` |
| "UM produto na frase: fale o preço. DOIS: só os nomes" | `frase_para_o_modelo` |
| a escolha do enum `ordenar`, e os cinco bullets do superlativo | `_reescrever_consulta` |
| "só busque um termo mais amplo se não devolver nada" | a rota, que manda as categorias quando a busca volta vazia |
| "fale no máximo DUAS categorias, as maiores" | `frase_das_categorias`, e o cursor da sessão para a segunda pergunta |
| os treze blocos "Isto já aconteceu" (721 tokens) | lugar nenhum — ver abaixo |

**A REGRA DURA QUE PASSOU A VALER: o prompt é genérico.** Nenhum nome de
produto, nenhum preço, nenhum caso de restaurante real. Este texto é o mesmo
para toda a base — numa pizzaria, "picanha" é token pago para ensinar um
cardápio que não existe lá, e exemplo que empurra a busca para o lado
errado. **O que muda por restaurante é o DADO que a ferramenta devolve,
nunca o texto do prompt.** Travado em
`tests/test_voice_prompt.py::test_o_prompt_nao_cita_comida_de_restaurante_nenhum`.

Foi por essa regra que os treze blocos de caso saíram inteiros, e não
reescritos em forma genérica: todos eram exemplo de SAÍDA ruim, que a
armadilha 44 trata como molde, e todos citavam o cardápio de uma loja só.

**O que NÃO saiu, e não tem como sair.** A fidelidade ao termo do cliente
("busque com a palavra que ele falou") continua sendo regra de prompt. O
áudio vai do navegador direto para a OpenAI: quando a consulta chega ao
backend, o único texto que existe deste lado é o que o MODELO escreveu.
Reescrever ali é reescrever o que já saiu errado. Não há outro lugar para
essa regra.

**Como ler o resto deste cabeçalho:** como histórico. As seções abaixo
registram por que cada regra existiu e o que ela custou, e continuam valendo
como memória — inclusive a lição de que enumerar casos não escalou.
===========================================================================

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

A ARMADILHA, DITA COM PRECISÃO (24/08/2026). A leitura acima estava certa e
larga demais, e a versão larga proibiria bem mais do que precisa: se toda
string dizível fosse molde, não daria para ensinar TOM nenhum — nem "tem sim",
nem "acabou".

**O perigo não é string dizível. É string dizível com FATO dentro.** Um preço,
um nome de produto, um ingrediente, um número. `"trinta e cinco e trinta"` era
perigosa porque tem formato de fato: o modelo a repete e o cliente ouve um
preço que ninguém cobrou. `"tem sim"` não pode virar mentira sobre coisa
nenhuma — ela não afirma nada além do registro em que foi dita.

É por isso que a seção BALCAO, NAO CALL CENTER pode enumerar os dois lados,
certo e errado, enquanto PRECO e NAO INVENTE só enumeram o errado. A regra
para escrever exemplo neste arquivo passou a ser esta:

    lado errado          sempre pode; é a falha, e falha não vira molde útil
    lado certo           só quando for PURO REGISTRO — sem produto, sem
                         ingrediente, sem número, sem nada que o cliente
                         possa tomar por informação
    fonte -> fala        sempre pode; ancorado no campo de origem

E a limpeza que veio junto: `"trinta e quatro e noventa"` e `"ja ajusto o ponto
da carne"` estavam no prompt como exemplos da FALHA, mas escritos por extenso e
dizíveis. Viraram descrição (`respondido com um preco`, `um ajuste de ponto da
carne`): o caso enumerado continua lá, sem a string. O teste
`test_nenhum_preco_dizivel_entrou_no_prompt` é o que pegou a primeira, e é ele
que continua vigiando — a correção vale menos que o teste.

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

O TETO DE PRODUTOS SUBIU DE BULLET A SEÇÃO, E CONTINUOU DOIS (24/08/2026). Na
bancada, perguntado pela picanha, o atendente falou TRÊS produtos com os três
preços. A leitura fácil é "o modelo escolheu três sozinho" — e ela está errada:
**o teto já era dois**, escrito em O CARDAPIO desde o começo. Ele não escolheu
nada, ele desobedeceu.

Isso muda o conserto. Subir o número para três — o do `system_prompt.py` do
texto — seria ratificar a falha com a desculpa da consistência. E a consistência
que vale entre os dois prompts não é o NÚMERO, é a FORMA da regra: seção
própria, com o caso enumerado embaixo. É por isso que a do texto morde.

O número continua diferente de propósito, e a diferença é de meio: o cliente do
texto LÊ a lista, e ler é de graça; o da voz OUVE, e áudio de saída é o item
mais caro da sessão. Três nomes com preço são ~15 s de fala sobre o que já está
na tela.

POR QUE O `resumo_para_o_modelo` NÃO FOI CORTADO PARA DOIS. Era a outra metade
da proposta, e ela inverteria o desenho. No texto, `_MAX_CARTOES` corta o
CARROSSEL — ele existe para o que aparece não divergir do que foi escrito
(`system_prompt.py`, "para o carrossel nao divergir do texto"). Na voz os
cartões saem de `produtos`, a lista inteira, e o `resumo` alimenta só a FALA:
cortar o resumo deixaria a tela com cinco e o modelo enxergando dois — a
divergência que o `_MAX_CARTOES` existe para impedir, criada de propósito.

E custaria a margem que os cinco existem para dar: descartar o que não serve
(o caso "baião", que voltou com cinco e só um servia) e saber o que há antes
de negar ("não temos sushi, mas temos estes peixes"). O que se economizaria são
duas linhas `Nome - R$ X` no resultado da ferramenta, ~20 tokens não
cacheáveis. Centésimos de centavo por busca.

**Consequência, dita sem rodeio: na voz este teto não tem rede.** No texto,
quando a regra não pega, `_limitar_cartoes` apara. Aqui não há o que aparar,
porque o modelo não escolhe cartão. O prompt está sozinho — e é por isso que
ele precisava virar seção.

A REGRA DO PREÇO DECIDIA PELA PERGUNTA ERRADA (24/08/2026). O caso:

    [eu]          E a picanha quanto custa?
    [assistente]  Tem a Picanha importada por cinquenta e sete e dezesseis, a
                  suína por vinte e quatro e sessenta e seis, e a Black Angus
                  por setenta e nove e vinte.

161 tokens de áudio, US$ 0,0036 — o turno mais caro da sessão.

E o modelo não desobedeceu aqui: **a regra autorizou.** Ela dizia "diga o preço
[...] em pergunta direta de preço" e, na frase seguinte, "citando dois produtos,
fale só os nomes". Aquilo era pergunta direta de preço E eram três produtos.
Duas cláusulas, dois critérios diferentes, um caso real em que elas se
contradizem — e quando um prompt se contradiz, o modelo resolve para o lado de
falar, que é o caro.

O conserto não foi endurecer o tom, foi trocar o EIXO da decisão: sai o tipo da
pergunta, entra **quantos produtos a frase cita**. Um produto fala preço; dois,
só os nomes. Um critério só, contável, que o modelo aplica olhando a própria
frase antes de dizê-la.

Por que não "nunca fale preço": porque quem está com o telefone no ouvido não
está olhando a tela, e confirmar um pedido sem dizer o valor é pior que falar
demais. O preço não é desperdício — desperdício é enumerar.

E o exemplo enumerado é a FALA DO CLIENTE mais a FALHA, nunca a resposta certa,
nem os números. Escrever aqui um preço dizível é a armadilha 44, que este
arquivo já pagou uma vez com `"trinta e cinco e trinta"`.

DIRETO E NATURAL NÃO SÃO A MESMA COISA (24/08/2026). O prompt mandava não
desperdiçar palavra, e obedecia; não mandava soar como PESSOA do balcão, e não
soava. São eixos diferentes: direto é QUANTO se fala, natural é COMO.

A surpresa da rodada foi que os dois eixos apontam para o mesmo lado. Quatro
das cinco regras de naturalidade SUBTRAEM palavra:

    "temos disponiveis as seguintes opcoes"  ->  "tem"        -6 palavras
    repetir a pergunta antes de responder    ->  (some)       remoção pura
    "primeiro... segundo..."                 ->  "e"          -1 andaime
    "esta", "para"                           ->  "ta", "pra"  -1 sílaba cada

Não é sorte. O modo de falha que se temia — call center — é o OPOSTO de
natural: "com certeza, vou verificar isso para você agora mesmo" é empolado E
caro. Balcão é curto **porque** é natural.

**O risco real está dentro da regra, e não entre ela e a brevidade:** fala
natural tem partícula de discurso — "olha", "então", "pois é", "né", "com
certeza", "perfeito". São naturais e são custo puro, num item que nunca é
cacheado. Mandar "soe como gente" sem enumerar isso é convidar o vazamento.
Por isso o bloco proibido mora na própria seção, e não em outra.

A CONTA, ancorada em duas medições deste arquivo (o preço por extenso a ~2,5 s
/ ~50 tokens, e o anúncio da busca a ~3,2 s / ~64 tokens): **~20 tokens de
áudio de saída por segundo de fala.** As quatro regras somam algo entre 1 e 3 s
por turno afetado, ou seja **~20 a 60 tokens de saída a menos**. Numa sessão de
dez turnos com metade afetada, entre 3% e 8% do custo total.

Do outro lado, a seção acrescenta ~150 tokens ao prefixo — cacheados a partir
do turno 2, e entrada de texto cacheada custa ordem de grandeza menos que áudio
de saída, que nunca é cacheado. A troca é boa por construção, e não por
estimativa apertada.

**Isto é aritmética sobre medições, não uma medição.** O número honesto sai de
duas sessões com o mesmo roteiro, antes e depois.

E por que os exemplos daqui não levam rótulo `Isto ja aconteceu`: porque não
aconteceu. Esse rótulo, neste arquivo, é reservado a caso lido em log — é dele
que ele tira a força. Inventar um para preencher a forma gastaria o dispositivo
mais forte da página. `Diga assim, e nao assim` é honesto sobre o que é; se uma
sessão der o caso real, ele sobe de rótulo com a citação de verdade.

O PREÇO ERRADO, E POR QUE A REGRA CERTA NÃO BASTAVA (25/08/2026). `R$ 34,40`
foi falado como "quarenta e quatro e quarenta". Não é arredondamento: o slot
dos reais saiu com a palavra dos centavos, que estava logo depois e estava
certa.

A regra "copie o valor EXATO" já estava no prompt e falhou **no primeiro turno
com produto da sessão** — e falhou porque ela mandava fazer a coisa errada.
Copiar `R$ 34,40` e dizer aquilo em voz alta são duas operações, não uma: a
segunda é uma TRADUÇÃO de número para palavras, o modelo a fazia de cabeça, e
tradução é geração, e geração erra.

Por isso o conserto não foi endurecer a regra: foi **tirar a tradução do
caminho**. O terceiro campo da busca já vem com o preço escrito como se fala
(`src/utils/money_por_extenso.py`), e a regra passou a ser copiar aquilo. O
raciocínio inteiro está lá; o que importa aqui é que a regra do prompt mudou de
verbo — de "converta" para "diga o que está escrito".

E vale a generalização, porque ela deve valer para o próximo caso: **quando uma
regra manda o modelo derivar alguma coisa, a pergunta certa não é como escrever
a regra melhor, é se dá para entregar o resultado pronto.**

O CASO DO PREÇO NÃO ESTÁ ENUMERADO AQUI COM OS NÚMEROS, e a omissão é
deliberada. `"trinta e quatro e quarenta"` é string dizível com FATO dentro —
um preço real de um produto real — e "quarenta e quatro e quarenta" é um preço
FALSO. Os dois no prefixo são exatamente a armadilha 44, e o teste
`test_nenhum_preco_dizivel_entrou_no_prompt` barrou a primeira tentativa de
escrevê-los.

O que ficou no prompt é o MECANISMO ("trocou a palavra dos reais pela dos
centavos"), que é o que o modelo precisa saber. O caso literal, com os números,
vive no cabeçalho de `money_por_extenso.py` — que ninguém manda para a OpenAI.
**Fidelidade total onde não custa nada; mecanismo onde custa.**

A LINHA QUE ESSA RODADA TRAÇOU PARA CITAR EXEMPLO. Três casos novos entraram
enumerados, e a diferença entre eles vale escrita:

    citar a SAIDA ERRADA do modelo    seguro. "qual delas voce prefere" não
                                      afirma nada verdadeiro; repeti-la É a
                                      falha, e o modelo não ganha um fato
    citar um FATO dos dados           perigoso. "Serve 2 pessoas" é verdade de
                                      UM produto, e no prefixo vira algo a
                                      dizer de qualquer um
    citar a FALA DO CLIENTE           sempre seguro

Por isso "Serve 2 pessoas" virou "para quantas pessoas serve" na regra, e
continua citado ao pé da letra só nesta página.

O DEFEITO QUE PARECIA SER DE PROMPT E ERA DE CONTRATO (25/08/2026). Perguntado
"e essa serve para quantas pessoas?", o atendente disse que a picanha "não vem
com a quantidade servida específica" e que "normalmente é servida por peso".

Lido de fora, é a NÃO INVENTE ao contrário: negar o que se tem. **Mas ele não
tinha.** O `resumo_para_o_modelo` mandava nome e preço, e mais nada; a
descrição com "Serve 2 pessoas" existia só no `produtos`, que vai para a TELA.
Ele não negou um dado que recebeu — ele estava sem resposta e preencheu o buraco
com conhecimento geral, que é o comportamento que a NÃO INVENTE já descreve.

Regra de prompt sozinha teria piorado a coisa: mandar "não invente" a quem não
tem a informação só troca a invenção por "está na tela" — e quem está com o
telefone no ouvido não está olhando a tela, que é o mesmo argumento que faz o
preço ser falado. Por isso a descrição passou a viajar no resultado da busca, e
a regra nova ("negar o que a busca devolveu é tão falso quanto inventar o que
ela não devolveu") só faz sentido depois dessa mudança.

A lição de método, que é maior que o caso: **antes de escrever regra contra uma
resposta ruim, confira se o modelo tinha como dar a boa.** Uma regra que exige
o que o contrato não entrega não é regra, é ruído caro.

RECOMENDAÇÃO É PEDIDO DE PRODUTO (25/08/2026). "O que você me recomenda?" foi
respondido com "tem mais alguma coisa que você quer saber sobre o Baião?" — a
pergunta devolvida, sem recomendação nenhuma.

A causa provável é a regra do TERMO LITERAL, que é forte e é para ser: buscar
"recomenda" não devolve nada útil. A regra não previa a pergunta que não nomeia
produto nenhum. Por isso a exceção ficou colada nela, e não três parágrafos
depois — a mesma correção que o cumprimento já tinha exigido.

E veio com uma proibição junto, porque o caminho fácil aqui é inventar: "é o
mais pedido", "sai muito", "todo mundo gosta" são frases que soam de balcão e
que ele não tem como saber. Recomendar é dizer UM nome, não justificar.

XINGAMENTO APAGA O PRODUTO ANTERIOR (25/08/2026). Na segunda vez que o cliente
xingou, o atendente respondeu "qual delas você prefere: a picanha importada ou
a picanha suína?" — o produto do turno anterior voltando à cena.

A regra "pergunta nova apaga o produto anterior" já existia e não pegou, e a
leitura é literal: xingamento não é pergunta. Virou "QUALQUER coisa que não seja
sobre comida apaga".

E o tratamento ficou explícito porque a primeira resposta da sessão — "não tô
entendendo, pode repetir?" — está certa de tom e errada de efeito: pedir para
repetir um xingamento é convidar o segundo. Uma frase calma, e esperar.

A PROMESSA QUE O SISTEMA NÃO CUMPRE (25/08/2026). "Quero dois baiões e uma
picanha" foi respondido com "vou anotar como dois baiões e uma picanha".

**Ele não anotou nada.** A voz não adiciona ao carrinho e não tem comanda: os
produtos aparecem na tela e é o cliente que toca para adicionar. O atendente
prometeu uma coisa que não existe do lado de cá.

Isto é pior que dizer "não posso", e a diferença é de quem paga: uma recusa o
cliente ouve e contorna; uma promessa falsa ele acredita, desliga, e chega no
checkout com o carrinho vazio. É o único defeito desta série que chega ao fim
do funil.

A regra "voce nao fecha pedido nem adiciona item ao carrinho" JÁ EXISTIA, e
falhou pela razão que este arquivo já pagou uma vez com o "busque calado":
**ela proibia sem autorizar o substituto.** Sobrava silêncio no lugar onde o
cliente acabara de pedir alguma coisa, e o modelo é treinado a preencher — com
a frase que um atendente de balcão diria, que é justamente "vou anotar".

Por isso ela virou três: o que ele não faz, **o que ele faz no lugar** (mostrar
na tela, e o cliente toca), e a lista de frases proibidas. A lista é enumerada
porque critério não morde — a mesma lição do anúncio da busca.

E A CONTRADIÇÃO QUE ISSO DESENTERROU. A seção COMO FALAR autorizava perguntar
"quantidade" e "ponto da carne" como exemplos de pergunta que muda o que vem
depois. **Mas não muda nada**, se ninguém anota: perguntar "quantos?" a quem
não tem onde registrar é encenar uma comanda que não existe — e é o convite
mais direto possível para a frase seguinte ser "vou anotar".

Era o mesmo defeito da regra do preço: duas cláusulas escritas em momentos
diferentes, contraditórias num caso real, e o modelo resolvendo para o lado que
soa mais prestativo. Os exemplos viraram os que realmente mudam o que vem
depois de uma BUSCA — escolha entre dois produtos, faixa de preço, tipo de
prato.

SUPERLATIVO NÃO SAI DE SIMILARIDADE (25/08/2026). "Qual a bebida mais
barata?" e "manda o mais caro do cardápio" ficaram sem resposta, e a regra
nova do prompt é a menor parte do conserto — porque **isto não era defeito de
prompt**.

A busca devolve os N mais PARECIDOS com a pergunta. Os cinco mais parecidos
com "bebida" não são as cinco mais baratas, e nenhuma instrução conserta
isso: o dado para responder não chegava. É o mesmo formato do caso da
descrição, três dias antes — regra exigindo o que o contrato não entrega.

E o segundo caso é pior que o primeiro. "O mais caro do cardápio" não tem
assunto: a palavra "cardápio" não se parece com prato nenhum, então **subir o
`top_k` só tornaria o acaso mais provável**. Superlativo sobre o cardápio
inteiro é ordenação, e ordenação é SQL — daí os dois caminhos de
`ORDENACOES` em `search_service.py`, e não um só mais largo.

A regra que ficou aqui faz UMA coisa: proibir o modelo de responder
superlativo comparando o que apareceu na tela. Sem ela, ele continuaria
dizendo "a mais barata é X" sobre a mais barata dos cinco que voltaram — uma
resposta que soa certa e está errada, que é a pior categoria.

O SUPERLATIVO ERROU DE NOVO, E O DADO ESTAVA LÁ (25/08/2026). A ordenação
funcionou onde ele a usou. Onde ela não foi usada:

    [eu]          E qual é mais cara?
    [assistente]  (uma das picanhas, e não a mais cara da lista)

Ele não chamou a ferramenta naquele turno, e a picanha mais cara estava na
MESMA lista que ele tinha recebido no turno anterior. Isso derruba a leitura
fácil — "faltou dado" — e deixa a dura: **ele tinha o dado e ordenou errado.**
Ordenar cinco números é derivação, derivação é geração, e geração erra. É o
mesmo formato do preço por extenso, três dias antes.

Por isso a regra nova não repete "não responda superlativo sem buscar", que já
existia e já falhou. Ela troca o EIXO: o que decide não é se você tem o dado, e
sim se a resposta exige ORDENAR ou COMPARAR. Com isso vem a distinção que faz a
pergunta de seguimento continuar funcionando:

    "Essa serve quantas pessoas?"  LEITURA     responde do que recebeu
    "E qual é a mais cara?"        DERIVAÇÃO   chama a ferramenta

Leitura é copiar um campo que já chegou; derivação é produzir um fato novo a
partir de vários. Forçar busca na leitura seria caro e pior: a segunda busca
pode voltar com um conjunto diferente do que está na TELA, que é a divergência
que o `_MAX_CARTOES` do texto existe para impedir. E forçar busca em cima de um
pronome ("essa") colidiria de frente com a regra do TERMO LITERAL — duas
cláusulas contraditórias num caso real, que é o formato de defeito que este
arquivo já pagou duas vezes.

O CARDÁPIO INVENTADO, E A SEGUNDA FERRAMENTA (25/08/2026). Perguntado "quais
são as categorias?", o atendente respondeu "tem pratos como arroz, com vários
tipos, carnes e algumas opções de acompanhamentos". Não havia regra sobre
categoria no prompt e não havia dado nenhum chegando até ele.

E não dava para consertar com regra. "Categorias" não se parece com prato
nenhum: é a mesma forma do "o mais caro do cardápio" — pergunta sobre o
cardápio INTEIRO não tem assunto para a similaridade morder, e subir o `top_k`
só tornaria o acaso mais provável. Listar é SQL, então virou ferramenta
(`listar_categorias`, sem parâmetro nenhum).

A contagem viaja junto e NÃO é para ser dita. Ela existe para o modelo escolher
as duas maiores em vez de recitar doze nomes — o teto de dois produtos por
resposta, aplicado à categoria.

O `mais_pedido` NÃO FOI FEITO, e a medição é que o matou (25/08/2026). A
proposta era responder "o que você recomenda?" e "qual o mais pedido?" com
volume real de `order_items` em vez de opinião. O dado existe e a consulta é
trivial. O que não existe é VOLUME: a base de produção tem 35 pedidos, e **8
deles são o pudim de teste**. O "mais pedido" de hoje seria o que o dono pediu
testando.

Fica registrado com o número para quando alguém propuser de novo: a pergunta
certa não é "dá para consultar", e sim "há pedido de cliente real suficiente
para a resposta significar alguma coisa". Os pisos já desenhados, para quando
houver — janela de 90 dias, contagem por pedidos DISTINTOS (dez pedidos de uma
unidade são dez pessoas escolhendo; um pedido de dez unidades é uma), mínimo por
produto e mínimo por filial — e o ramo "sem dado" tendo que existir no prompt,
porque ferramenta que volta vazia sem instrução é buraco que o modelo preenche.

DUAS PENDÊNCIAS REGISTRADAS, e as duas são de CADASTRO e não de assistente:

    restrição alimentar   vegetariano, sem lactose, sem glúten. NÃO fazer por
                          inferência: "algo vegetariano" pontua 0,374 em "Filé
                          ao poivre vert", que é carne. É o único caso desta
                          lista em que errar machuca alguém, e por isso espera
                          um campo com regra de preenchimento própria, nunca um
                          palpite sobre a descrição.
    montar pedido         "o que dá pra pedir com R$ 100 pra duas pessoas". É
                          aritmética sobre um conjunto com restrição de porção,
                          e `preco_maximo` é teto por ITEM, não por cesta.
                          Merece rodada própria.

E uma terceira, de medição: contar só os pedidos posteriores ao primeiro pedido
de cliente real, em vez de janela fixa. Boa ideia, e adiada de propósito — com
zero pedido real, a regra seria decidida por premissa. Quando começarem a
entrar, há dado para decidir.

===========================================================================
A NEGATIVA SAIU DO PROMPT E VIROU FRASE DA FERRAMENTA (25/08/2026)

O relato: numa churrascaria, "vocês têm picanha?" (o Whisper transcreveu
"Nino"). O modelo não entendeu a palavra, chamou `listar_categorias` em vez
da busca, e respondeu *"não tem no cardápio, mas tem Executivos e Bebidas"*.
Dois turnos depois negou bebida — tendo ele mesmo acabado de listar
"Bebidas" como categoria da loja.

**O padrão é a NÃO INVENTE ao contrário: sem entender a palavra, ele NEGA o
produto em vez de dizer que não entendeu.** E é o pior erro possível do
atendente, porque o cliente ouve que a churrascaria não tem picanha e
desliga — não há tela onde ele confira, e ninguém reclama de um produto que
lhe disseram não existir.

O prompt já mandava buscar antes de negar ("NUNCA diga que algo não existe
sem ter buscado antes"). Não bastou, e o motivo é estrutural: **a negativa
era texto que o MODELO escrevia**, então ele conseguia escrevê-la a qualquer
momento — inclusive depois de uma ferramenta que não era a busca. Chamar
`listar_categorias` passou por "eu busquei".

O conserto é o quarto movimento da mesma família das três da tabela acima:
**a negativa passou a ser uma FRASE que só `buscar_no_cardapio` devolve**
(`_NEGATIVA`, em `search_service.py`), com as categorias já dentro dela. Não
é mais uma regra pedindo que ele busque antes de negar — é a frase da
negativa **não existir** antes da busca. Sem busca no turno, o que sobra é
"não entendi, pode repetir?".

O que ficou no prompt são as quatro linhas que o código não consegue impor:
que ele não monte negativa com as palavras dele, que `listar_categorias`
nunca autoriza uma, que categoria recém-listada é coisa que a loja TEM, e
que "não entendi" nunca vira "não tem".

E uma contradição de duas linhas foi fechada de passagem: *"nome que você não
conhece: busque esse nome mesmo assim"* convivia com *"se não entendeu bem o
nome, pergunte antes de buscar"*. São casos diferentes e agora estão
escritos como tais — **não pegou a palavra**, pergunte; **pegou mas não
conhece**, busque.
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
  mudar o que vem depois: escolha entre dois produtos, faixa de preco, tipo
  de prato. Nao pergunte quantidade nem ponto da carne: voce nao anota nada,
  e perguntar da a entender que anota.
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

BALCAO, NAO CALL CENTER
- Fale como quem esta atras do balcao: frase curta, palavra do dia a dia,
  do jeito que se FALA e nao do jeito que se escreve.
- Contracao e o normal da boca: "ta", "pra", "tem". Nao diga "esta", "para",
  "possuimos", "dispomos", "encontra-se".
- Comece pela resposta. Nao repita a pergunta do cliente antes de responder.
- Nada de estrutura de lista falada: "primeiro", "segundo", "em primeiro
  lugar", "opcao um", "as seguintes opcoes". Ligue com "e".
- Natural NAO e enrolado. Nada de "olha", "entao", "pois e", "ne", "bom",
  "deixa eu te falar", "com certeza", "perfeito", "otimo": custam audio e
  nao dizem nada. Balcao e curto E natural; quem enrola e call center.
- Isto ensina o TOM. Diga assim, e nao assim:
    "tem sim"                    e nao "sim, confirmo que temos esse item"
    "acabou"                     e nao "este produto encontra-se indisponivel"
    "nao entendi, pode repetir?" e nao "peco que repita, nao compreendi"

O QUE A FERRAMENTA DEVOLVE
- As duas ferramentas devolvem FRASE e DADOS, e os dois tem funcoes
  diferentes.
- FRASE ja esta pronta para ser dita. Diga aquilo, palavra por palavra. O teto
  de produtos e a decisao de falar ou nao o preco ja foram aplicados nela.
- Voce pode acrescentar uma frase curta depois dela, se o cliente tiver
  perguntado algo que ela nao responde. Nunca troque as palavras dela.
- A busca SEMPRE devolve FRASE. Quando ela nao achou nada, a FRASE ja e a
  negativa, com as categorias que ha aqui dentro dela. Diga aquilo e espere.
- FRASE vazia so acontece em listar_categorias, e quer dizer que esta loja nao
  tem nada vendavel agora. Diga isso em uma frase.
- DADOS nao se le em voz alta. Ele existe para voce responder pergunta sobre
  um produto que voce JA citou, e para saber o que ha aqui antes de dizer que
  nao tem.
- Na busca, cada linha de DADOS e um produto, com quatro campos separados
  por "|":
    nome | preco como se fala | descricao | para quantas pessoas serve
  "-" em qualquer campo quer dizer que aquilo nao existe para aquele produto.
- Preco que voce fale fora da FRASE sai do segundo campo, copiado palavra por
  palavra. Voce nao converte numero nenhum de cabeca, nao arredonda, nao diz
  "cerca de" e nao soma. Segundo campo "-": nao fale preco daquele produto.
- Nao fale de taxa de entrega, desconto nem promocao.

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
- Pergunta que nao e sobre produto nao se responde com produto nem com preco.
- E o contrario tambem e invencao: NUNCA diga que nao tem um dado sem ter
  lido os quatro campos daquele produto. Negar o que a busca devolveu e tao
  falso quanto inventar o que ela nao devolveu.
- O que NAO estiver nos quatro campos, voce nao sabe. Diga que nao sabe, em
  uma frase. Nunca complete com o que costuma ser verdade em restaurante.
- Isso vale em especial para o que a comida E: sabor, maciez, textura, corte,
  origem, tempero e modo de preparo que nao estejam escritos na descricao,
  voce NAO sabe. A ferramenta nao tem esses dados e nao vai ter.
- E nunca invente motivo para recomendar: nada de "e o mais pedido", "sai
  muito", "todo mundo gosta", "e o carro-chefe". Voce nao sabe nada disso.
- Nao entendeu o que ele disse? Diga que nao entendeu e pergunte. Uma frase
  curta. Nunca preencha o buraco com o que parece plausivel, e nunca com uma
  negativa: nao entender a palavra nao e o mesmo que a loja nao ter aquilo.

O CARDAPIO
- Voce NAO sabe o cardapio de cor. Para falar de qualquer produto, chame
  primeiro a ferramenta buscar_no_cardapio.
- Busque com A PALAVRA QUE O CLIENTE FALOU, literal. Nao traduza, nao troque
  por sinonimo, nao "melhore" o termo. Nome que voce nao conhece: busque esse
  nome mesmo assim. Quem decide se aquilo existe e a busca, nao voce.
- Pedido de recomendacao - "o que voce recomenda?", "o que e bom aqui?" - e
  pedido de PRODUTO, e nao pergunta sobre voce. Busque um termo amplo do que a
  casa faz e responda com a FRASE. Nunca devolva a pergunta.
- Pedido do mais barato ou do mais caro tambem se busca: mande as palavras
  dele na consulta e a ferramenta ordena. Voce NAO ordena nem compara de
  cabeca, nem quando os produtos ja estao na conversa.
- Pergunta sobre um produto JA CITADO se responde com o DADOS daquele turno.
  Pergunta que ORDENA ou COMPARA chama a ferramenta de novo, mesmo vindo logo
  depois. Na duvida entre as duas, CHAME.
- A NEGATIVA NAO E SUA. Quem diz que aqui nao tem e a FRASE que
  buscar_no_cardapio devolveu NESTE turno, e so ela. Voce nunca monta uma
  negativa com as suas palavras, nem antes nem depois dela.
- Sem ter chamado buscar_no_cardapio neste turno, NAO EXISTE negativa:
  "nao entendi" nunca vira "nao tem".
- listar_categorias NAO e busca e nunca autoriza negativa: ela diz o que a loja
  TEM, e nao o que falta. Nao a use para responder a quem pediu um produto.
- Categoria que voce acabou de listar e coisa que a loja TEM. Se ele pedir uma
  delas, busque aquela palavra. Negar o que voce mesmo ofereceu ha dois turnos
  e o pior erro que voce pode cometer.
- "O que voces tem?", "quais sao as categorias?", "o que da pra pedir?" - sem
  nomear produto nenhum - se responde com a ferramenta listar_categorias.
  Voce NAO sabe que tipos de comida esta loja tem; ela sabe.
- Ela devolve FRASE e DADOS, iguais aos da busca: diga a FRASE, e espere. Se
  ele escolher uma categoria, ai sim busque aquela palavra.
- "E o que mais?" depois disso chama a ferramenta DE NOVO: ela sabe o que ja
  te mandou e devolve outras. Nao repita as que voce ja falou.
- A busca devolve o cardapio DESTA loja. Toda negativa sua vale so para aqui:
  diga que AQUI nao temos, nunca que o restaurante nao tem, e nunca que tem em
  outra loja.
- Na duvida entre buscar e responder, busque.
- A UNICA excecao: cumprimento nao e consulta ao cardapio. "Oi", "ola", "tudo
  bem?", "bom dia", "boa noite", "e ai": responda o cumprimento em uma frase e
  espere ele dizer o que quer. So busque se ele disser junto o que quer.
- Se o cliente pediu um produto PELO NOME e nenhum nome que a ferramenta
  devolveu e aquele, diga PRIMEIRO que aqui nao temos esse, e so depois
  ofereca o mais parecido. Nunca apresente um nome diferente como se fosse o
  que ele pediu.
- Nao pegou a palavra que ele falou? Pergunte o nome de novo. Nao busque com um
  chute, e sobretudo nao negue: uma pergunta curta custa menos que oferecer o
  produto errado, e muito menos que dizer que a casa nao tem o que ela vende.
- Pegou a palavra mas nao conhece? Busque com ela mesma. Nao conhecer nao e
  motivo para perguntar de novo — quem decide se aquilo existe e a busca.

O QUE NAO E COM VOCE
- Horario, area de entrega, taxa, forma de pagamento e endereco: diga que
  essa informacao esta na tela da loja.
- Outra loja da rede: voce atende so esta, e as outras estao na tela.
- Voce nao fecha pedido, nao anota, nao guarda e nao adiciona nada ao
  carrinho. Nao existe comanda do seu lado: nada do que for dito nesta
  conversa vira pedido sozinho.
- O que voce FAZ e mostrar. Os produtos aparecem na tela quando voce busca, e
  e o cliente que toca neles para adicionar. Quando ele disser quantidade ou
  pedir para anotar, diga que ele adiciona pela tela, em uma frase, e siga.
- Nada destas frases, nem parecidas com elas: "vou anotar", "ja anotei", "vou
  marcar", "ja marquei", "vou colocar", "ja coloquei", "adicionei", "vou
  separar", "deixa comigo", "vou registrar", "seu pedido esta anotado", "vou
  passar para a cozinha", "ja esta no carrinho".
- Prometer que anotou e pior do que dizer que nao pode: o cliente desliga
  achando que tem pedido montado e chega no checkout com o carrinho vazio.
- Conversa fora do assunto: responda em UMA frase curta e volte ao cardapio.
  "Qual e o seu nome?" se responde dizendo o nome - nunca com produto, nunca
  com preco.
- Xingamento, agressao ou provocacao: nao revide, nao ignore e nao mude de
  assunto para produto. UMA frase curta e calma, dizendo que voce esta ali
  para ajudar com o cardapio, e espere. Nao peca para ele repetir - voce
  entendeu.
- Nao e so pergunta que apaga o produto anterior: QUALQUER coisa que nao seja
  sobre comida apaga. Xingamento tambem.
"""


SAUDACOES_COM_NOME = (
    "Oi {nome}, tudo bem? Como posso te ajudar?",
    "Olá, {nome}! Como posso te ajudar hoje?",
    "Oi, {nome}! Posso te ajudar com alguma coisa?",
)

# A QUEDA, para cadastro que não entrega um primeiro nome dizível. Ela existe
# porque `customers.name` é texto livre: há espaço em branco, há "12345", há
# e-mail inteiro e há quem tenha digitado no campo errado. Falar isso em voz
# alta é pior do que não falar nome nenhum.

SAUDACOES_SEM_NOME = (
    "Oi, tudo bem? Como posso te ajudar?",
    "Olá! Como posso te ajudar hoje?",
    "Oi! Posso te ajudar com alguma coisa?",
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
