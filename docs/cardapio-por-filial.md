# O cardápio passa a ser da filial

Documento para quem implementa o **app do cliente** e o **painel do lojista**.
Tudo o que é preciso saber está aqui; não é necessário acesso ao código do
backend.

**Estado em 20/08/2026.** Revisões de banco `20260820_0026` e `20260820_0027`.
Base da API: a mesma do resto do app (`https://api.pederapidex.com` em
produção, `http://localhost:8000` em desenvolvimento).

É o **passo 3 de três**, e o último. O passo 1 (`91a68a4`) criou a tela de
escolha de filial ([`contrato-filiais-frontend.md`](contrato-filiais-frontend.md));
o passo 2 (`20260818_0025`) passou a operação para a filial
([`operacao-por-filial.md`](operacao-por-filial.md)). Aqueles dois documentos
dizem, cada um, que "os produtos continuam sendo do restaurante". **Não
continuam mais.**

> **AS ROTAS `/voice/*` CITADAS AQUI NÃO EXISTEM MAIS.** O assistente de voz
> saiu do projeto em 06/09/2026. Este documento é o registro do passo 3 e não
> foi reescrito: onde ele diz `/voice/session` ou `/voice/search`, leia "as
> rotas do assistente falado, enquanto elas existiram". **Tudo o que ele diz
> sobre o `/chat` e sobre o cardápio continua valendo palavra por palavra** — e
> a exigência de `branch_id` no `/chat`, que é a parte que o app consome, não
> mudou.

---

## 1. O que mudou, em um parágrafo

Cada filial tem os **próprios produtos, os próprios preços e a própria
disponibilidade**. Não há herança e não há sobrescrita: a picanha do Centro e
a picanha da Aldeota são duas linhas independentes, e mudar o preço de uma não
toca a outra. `GET /restaurants/{slug}/menu?branch_id=...` passa a resolver a
resposta inteira — produtos, categorias, preços e o bloco `settings`. As duas
superfícies que recomendavam produto sem saber de qual loja (`/chat` e a busca
da voz) passaram a **exigir** a filial.

---

## 2. As duas coisas que mais vão gerar chamado

### 2.1 Voltar a imagem sem rodar `alembic downgrade`

**É o pior modo de falha deste passo, e ele não dá erro.**

O `docker-entrypoint.sh` roda `alembic upgrade head` antes do Uvicorn, então
as duas revisões se aplicam sozinhas no deploy. Se algo der errado depois e
alguém voltar a imagem anterior **sem descer o banco junto**, o código antigo
passa a falar com o schema novo. Duas consequências, e a segunda é a cara:

- `POST /admin/products` responde **500**: `products.branch_id` é `NOT NULL`
  sem default, e o INSERT do código antigo não manda a coluna. Barulhento, e
  por isso o menos perigoso.
- **`GET /menu` devolve os produtos das DUAS lojas numa lista só.** O código
  antigo consulta por `restaurant_id`, que continua existindo e continua
  preenchido. A resposta é 200, o JSON é válido, e o cliente vê a mesma
  picanha duas vezes com dois preços — sem uma linha de log dizendo por quê.

**Rollback de imagem exige `alembic downgrade 20260818_0025` junto.** E o
downgrade **apaga o cardápio das filiais não-padrão**, inclusive o que o
lojista tiver criado depois do deploy.

### 2.2 `total_usage_limit` do cupom é compartilhado entre as lojas

Cupom continua sendo do **restaurante** — ver §7. A consequência que o lojista
precisa saber antes de criar campanha: um cupom de "100 primeiros" pode ser
consumido inteiro por uma das lojas, e a outra fica sem. Isso já era verdade
antes deste passo e não mudou; o que mudou é que agora existe a expectativa de
que "cada loja é independente", e cupom é a exceção.

---

## 3. O que muda para o app do cliente

### 3.1 `GET /restaurants/{slug}/menu` — `branch_id` resolve tudo

```
GET /restaurants/{restaurant_slug}/menu?branch_id=<uuid da filial>
```

| Situação | O que acontece |
|---|---|
| `branch_id` informado | produtos, categorias e `settings` **daquela** filial |
| `branch_id` omitido | vale a filial padrão (principal se houver, senão a primeira ativa em ordem alfabética) |
| `branch_id` de outro restaurante | **404** |
| restaurante sem filial ativa | **200**, com `products: []`, `categories: []` e `settings: null` |

**Campo novo na raiz: `branch_id`** (uuid | null). É de qual filial a resposta
inteira está falando.

**`settings_branch_id` continua na resposta, com o mesmo valor, e está
obsoleto.** Ele nasceu no passo 2, quando só o bloco `settings` era da filial —
o nome descrevia a verdade daquele dia. Hoje ele mente por omissão: quem lê
"settings_branch_id" não imagina que os PRODUTOS também são dessa filial. Ele
fica pelo tempo de o painel e o app trocarem de campo; **use `branch_id`**.

**Campo novo em cada produto: `branch_id`.** Vem igual ao da raiz, e serve de
conferência: uma tela que o compare percebe na hora que está misturando duas
cargas.

**Campo novo em cada categoria: `branch_id`.** Mesma coisa.

**`payment_methods` saiu de dentro de `settings`.** Era dado morto desde que
`branch_payment_methods` existe, e podia discordar do que a filial de fato
aceita — um app que acreditasse nele mostrava ao cliente uma forma de
pagamento que `POST /orders` recusa com 400. A fonte da verdade é
`GET /restaurants/{slug}/info?branch_id=...`.

### 3.2 As rotas de produto e de categoria ganharam `branch_id`

```
GET /restaurants/{slug}/products/{product_slug}?branch_id=<uuid>
GET /restaurants/{slug}/categories/{category_slug}/products?branch_id=<uuid>
```

O parâmetro é **opcional**, e sem ele vale a filial padrão.

**Os links já divulgados continuam funcionando.** A migração deixou as linhas
que já existiam **na filial padrão**, com os mesmos ids e os mesmos slugs —
então `/restaurants/junior-da-picanha/products/picanha` abre exatamente o
produto que sempre abriu.

**O que muda com o tempo:** um produto que o lojista criar SÓ numa filial não
padrão responde **404** pelo link sem parâmetro. É a resposta correta — sem
loja escolhida não há preço a mostrar, e escolher uma loja por conta própria é
o defeito que o passo 2 fechou.

### 3.3 O mesmo slug existe nas duas lojas

O índice único passou de `(restaurant_id, slug)` para **`(branch_id, slug)`**,
em produtos e em categorias. As duas lojas têm `picanha`, e é assim que tem
que ser: o slug é a URL que o cliente lê, e `picanha-2` contaria a ele que
está na "segunda" loja.

**O front não pode usar o slug como identificador global.** A chave de um
produto na tela é `id`; o slug só identifica dentro de uma filial.

### 3.4 `POST /orders` recusa produto de outra filial

Pedido com `branch_id` da Aldeota e `product_id` do Centro responde **400**
com `"Produto inválido ou indisponível"` — a mesma mensagem de produto
esgotado, e pelo mesmo motivo de sempre: a rota é pública e não pode virar
oráculo de quais UUIDs existem.

**Consequência prática para o app:** carrinho montado numa loja **não migra**
para outra. Se o cliente trocar de filial, o carrinho tem que ser remontado
com os produtos da nova — os ids são diferentes, e os preços também podem ser.

### 3.5 `/chat` e as rotas de voz passaram a EXIGIR `branch_id`

```jsonc
POST /chat
{ "restaurant_id": "...", "branch_id": "...", "session_id": "...", "message": "..." }

POST /voice/session
{ "restaurant_id": "...", "branch_id": "..." }

POST /voice/search
{ "restaurant_id": "...", "branch_id": "...", "consulta": "...", "preco_maximo": null }
```

**Obrigatório, sem queda para a filial padrão.** Chamada sem o campo responde
**422** com o nome dele.

O motivo de não ser opcional: um `branch_id` que caísse na filial padrão daria
a MESMA resposta errada de antes — o Rapi oferecendo, **com preço e com foto**,
um produto que aquela loja não vende — só que escondida atrás de um caminho
que parece configurado. Na voz é pior ainda: o modelo **fala** o nome e o preço
em áudio, e o cliente aceita de ouvido; não há tela onde ele pudesse notar.

`branch_id` de outra loja, ou de outro restaurante, responde **404** — e
responde **antes** de gastar embedding, chamada de LLM ou credencial de voz.

### 3.6 O fluxo que o app deve seguir

1. `POST .../branches/availability` → o cliente escolhe a filial, **guarde o
   `id`**.
2. `GET .../menu?branch_id=<escolhida>` → cardápio **e** números daquela loja.
3. `/chat` e `/voice/*` com o mesmo `branch_id`.
4. `POST .../delivery/estimate` e `POST /orders` como antes, com o mesmo
   `branch_id`.

**O `branch_id` escolhido no passo 1 passa a acompanhar TODAS as chamadas.**
É a mudança de forma do app: antes ele era um parâmetro de uma tela; agora é
estado de sessão.

---

## 4. O que muda para o painel do lojista

### 4.1 O escopo de filial passou a valer no cardápio

Até aqui, um gerente preso a uma filial editava o cardápio do **restaurante
inteiro** — e o código registrava isso como decisão. Não era: não havia coluna
para restringir. Agora há, e toda leitura e toda escrita de cardápio passam
pelo escopo do token.

**404, nunca 403**, pela mesma regra do resto do painel: um 403 confirmaria
que a filial do lado existe.

### 4.2 As rotas de cardápio

| Rota | O que mudou |
|---|---|
| `GET /admin/categories` | aceita `?branch_id=`; cada item traz `branch_id` |
| `POST /admin/categories` | **`branch_id` obrigatório no corpo** |
| `PATCH /admin/categories/reorder` | **`branch_id` obrigatório**; a lista completa exigida é a da FILIAL |
| `GET /admin/products` | aceita `?branch_id=`; cada item traz `branch_id` e `catalog_key` |
| `POST /admin/products` | **sem `branch_id`** — a filial vem da categoria; aceita `catalog_key` |
| `PATCH /admin/products/{id}` | `category_id` só aceita categoria da MESMA filial (400); aceita `catalog_key` |
| todas as de id | conferem a filial da linha; fora do escopo → 404 |

**Por que categoria pede filial e produto não.** `category_id` já determina a
loja. Pedir os dois abriria a possibilidade de um corpo com os dois em
desacordo, e o único desfecho possível para esse corpo seria um 400 que não
precisa existir.

**Produto não muda de filial.** Mover a linha levaria junto os grupos de
opção, o setor de impressão e a chave de catálogo, e deixaria o histórico de
pedido apontando para um produto que aquela loja não vende mais. Quem quer o
item na outra loja **cria um lá com a mesma `catalog_key`**.

### 4.3 O 409 de nome duplicado passou a ser por filial

Cadastrar "Picanha" na segunda loja **não** colide mais com a "Picanha" da
primeira. Colide só com outra "Picanha" da mesma loja.

E o 409 sai igual venha da conferência ou do índice — ver §4.9.

### 4.4 `catalog_key`: a mesma picanha nas duas lojas

Campo **opcional** em produto, texto livre, único dentro da filial.

**Ele não tem semântica nenhuma de herança.** Nada no cardápio, no pedido ou
no Rapi o lê para decidir preço ou disponibilidade. Ele responde uma pergunta
só: *"quanto vendi de picanha nas duas lojas?"*.

- **Repetir entre lojas é o uso.** É exatamente para isso que ele serve.
- **Repetir dentro da mesma loja é 409.** Duas linhas da mesma loja com a
  mesma chave fariam o relatório contar a mesma venda duas vezes.
- **Nulo é o estado normal** de um produto sem par em outra loja.

**Não é o `code`.** Aquele é o código que o lojista imprime e busca no painel:
texto livre, sem unicidade, e entra no conteúdo indexado do Rapi.

**A tela que o painel precisa ganhar:** ao criar um produto numa loja, oferecer
"é o mesmo item de outra loja?" e copiar a `catalog_key` do produto escolhido.
Sem isso, todo produto criado depois do deploy nasce sem chave e some do
agrupamento do relatório.

### 4.5 O setor de impressão agora é conferido

`PATCH /admin/products/{id}/printing-sector` e
`PATCH /admin/categories/{id}/printing-sector` respondem **400** para setor de
outra filial. Antes deste passo o vínculo era gravável — o produto pendia de
restaurante e o setor de filial — e o item caía na via **"SEM SETOR"** na hora
de imprimir.

**A via "SEM SETOR" continua existindo**, com um motivo a menos. Os que
sobraram: setor **desativado** depois de vinculado, e produto que não está
mais naquele restaurante. Nenhum dos dois tem chave estrangeira que os feche.

### 4.6 Os relatórios ganharam `branch_id` — e a regra de papel mudou junto

As seis rotas de `/admin/reports` aceitam `?branch_id=`, que **só restringe**:
quem está preso a uma filial e pedir outra recebe 404. Todas as respostas
trazem `branch_id` (nulo = "o restaurante inteiro").

| Rota | Papel antes | Papel agora |
|---|---|---|
| `/commission` | SOMENTE_DONO | **SOMENTE_DONO** (não mudou) |
| `/summary` | SOMENTE_DONO | **GERENCIA, com recorte obrigatório** |
| `/sales-by-day` | SOMENTE_DONO | **GERENCIA, com recorte obrigatório** |
| `/payment-methods` | SOMENTE_DONO | **GERENCIA, com recorte obrigatório** |
| `/products` | GERENCIA | GERENCIA (não mudou) |
| `/cancellations` | GERENCIA | GERENCIA (não mudou) |

**Isto não afrouxa nada, e o motivo importa.** Os três de dinheiro eram do dono
não por serem dinheiro: era que, **sem recorte**, ler o faturamento significava
ler o do restaurante inteiro — dar isso ao gerente do Centro era entregar-lhe o
resultado da Aldeota. Agora existe recorte, e a divisão passou a ser:

- **dono**: lê com ou sem recorte;
- **gerente**: lê **uma filial por vez**. Sem `branch_id`, e sem filial no
  próprio cadastro, recebe **403** com `"Informe branch_id: seu perfil le o
  relatorio de uma filial por vez"`;
- **atendente**: não lê, com ou sem recorte.

`/commission` ficou de fora: comissão é o percentual negociado com a
plataforma, não desempenho de loja, e não há filtro que a transforme em
assunto de quem toca o balcão.

### 4.7 `/reports/products` soma as lojas pela chave de catálogo

Sem `branch_id`, dois produtos que compartilham `catalog_key` aparecem em
**uma linha só**, com as unidades somadas. Cada linha traz `catalog_key`.

Produto **sem** chave continua contado por linha de `products`, como antes —
se a identidade fosse só a chave, dois produtos diferentes sem chave cairiam
no mesmo grupo.

`product_id` na linha aponta para **uma** das lojas quando o grupo abrange
mais de uma; o painel usa esse campo só para linkar a tela de edição. Com
`branch_id` na chamada ele volta a ser exato.

### 4.8 A tela que o painel precisa ganhar

O seletor de filial deixa de ser da tela de operação e passa a ser **do painel
inteiro**: cardápio, relatórios e impressão dependem dele. Quem está preso a
uma filial vê um seletor de um item só — a mesma tela serve para os dois casos
sem a interface conhecer a regra de escopo.

---

### 4.9 A colisão de nome responde 409, venha da conferência ou do índice

O cardápio usa **confere-depois-insere**: uma consulta pergunta se o nome já
existe e responde 409 com frase, e o índice único segura o resto. As duas
metades são necessárias — a trava protege a tabela, a conferência explica ao
lojista.

Entre as duas não há nada segurando a linha. **Dois cliques no mesmo botão
passam os dois pela consulta antes de qualquer commit**, o segundo `INSERT` bate
no índice, e até 05/09/2026 não havia um único `except IntegrityError` na
aplicação: o lojista recebia **500** sem saber se o produto tinha sido criado.
Clicava de novo e aí recebia o 409, que era a resposta certa desde o primeiro
clique. Os dados nunca correram risco — o índice fez o trabalho dele. O que
estava quebrado era a frase.

Hoje `src/services/unique_conflict.py` traduz, e **a frase mora lá**, lida
pelas duas pontas. Escritas separadas elas divergiriam, e a mesma colisão
passaria a ter dois textos dependendo de qual metade pegou — uma diferença que
o lojista não tem como enxergar, porque depende de uma corrida.

| Restrição | Frase |
|---|---|
| `uq_categories_branch_slug` | Já existe uma categoria com esse nome nesta filial |
| `uq_products_branch_slug` | Já existe um produto com esse nome nesta filial |
| `uq_products_branch_catalog_key` | Já existe um produto com essa chave de catálogo nesta filial |
| `uq_printing_sectors_branch_name` | Já existe um setor com esse nome nesta filial |

**Restrição fora da tabela NÃO vira 409**, e isso é o ponto: uma FK violada é
defeito nosso, e chegar ao lojista como "já existe um produto com esse nome" o
manda procurar um produto que não existe. O 500 é feio e é honesto; o 409 errado
é mentira.

`tests/test_conflito_de_unicidade_db.py` cobra que os quatro nomes existam no
banco. É a trava que a corrida não dá: ela não acontece na suíte, então uma
revisão que renomeasse um índice deixaria a tradução muda e tudo continuaria
verde.

---

## 5. O banco

### 5.1 O que entrou e o que saiu

```
categories                       products
──────────────────────────       ─────────────────────────────────
branch_id   NOVA, NOT NULL       branch_id     NOVA, NOT NULL
                                 catalog_key   NOVA, nullable

order_items                      restaurant_settings
──────────────────────────       ─────────────────────────────────
catalog_key_snapshot  NOVA       payment_methods   REMOVIDA (0027)

uq_categories_restaurant_slug  →  uq_categories_branch_slug
uq_products_restaurant_slug    →  uq_products_branch_slug
                               +  uq_products_branch_catalog_key (parcial)
```

`restaurant_id` **continua** nas duas tabelas. É redundante (a filial já diz o
restaurante) e fica porque metade das consultas filtra por ele — o que impede
os dois de divergirem são as FKs compostas abaixo.

### 5.2 As quatro FKs compostas: erro possível vira erro impossível de gravar

```
products (branch_id, category_id)        -> categories (branch_id, id)
products (branch_id, printing_sector_id) -> printing_sectors (branch_id, id)
products (restaurant_id, branch_id)      -> branches (restaurant_id, id)
categories (restaurant_id, branch_id)    -> branches (restaurant_id, id)
```

Custam três `UNIQUE` novos, em tabelas pequenas. A segunda é a que fecha a
armadilha 13: "produto da filial A apontando para a impressora da filial B"
deixa de ser um estado gravável.

`printing_sector_id` continua podendo ser nulo — FK composta com `MATCH SIMPLE`
(o padrão) passa quando qualquer coluna do par é nula. O "este produto não
imprime via de produção" não foi afetado.

### 5.3 O que a migração faz com o Júnior

O Júnior da Picanha tem **161 produtos e duas filiais**.

1. **A filial principal adota as 161 linhas existentes**, em lugar: mesmos
   ids, mesmos slugs, mesmas imagens. É o que faz todo link já divulgado
   continuar funcionando.
2. **A outra filial recebe 161 cópias**, com as categorias, os grupos de opção
   e as opções. **Nada é recadastrado à mão.** Depois do deploy: 322 produtos.
3. **Todo produto nasce com `catalog_key` = id do produto de origem**, no
   original e na cópia. Os 161 pares compartilham chave desde o primeiro
   minuto — e essa é a única parte deste passo que não daria para acrescentar
   depois sem alguém dizer à mão quais linhas são pares.
4. **O setor de impressão da cópia é remapeado pelo NOME** dentro da filial de
   destino. Sem correspondência, fica **nulo** — e é o que o
   `check_restaurant.py` aponta.
5. **Os embeddings do Rapi são copiados junto, reaproveitando o vetor.** O
   conteúdo indexado é idêntico, então o `content_hash` bate e a varredura
   seguinte cai no caminho "touched": **zero chamada paga à OpenAI**, e o Rapi
   respondendo certo nas duas lojas já no boot.

A partir daí as duas lojas divergem quando o lojista quiser: mudar o preço da
picanha do Centro não toca a da Aldeota.

**Restaurante com cardápio e sem NENHUMA filial cadastrada** derruba a
migração com uma mensagem que nomeia os slugs. É defeito de cadastro, não caso
de operação — e o `alembic upgrade` para com a frase em vez de deixar o
`SET NOT NULL` falhar com "column contains null values".

### 5.4 Duas revisões, e por quê

- **`20260820_0026`** — o cardápio por filial: colunas, cópia de dados,
  índices e as FKs compostas.
- **`20260820_0027`** — `DROP COLUMN restaurant_settings.payment_methods`.

Separadas porque a segunda é o único `DROP` irreversível do passo e não tem
nada a ver com cardápio: assim a decisão de descer a 0026 às 3h da manhã é
sobre uma coisa só.

**Entre uma e outra não roda nada.** As duas aplicam na mesma invocação de
`alembic upgrade head`, com a API fora do ar. Se a 0027 falhar depois de a
0026 ter passado, o container morre e o `restart: always` tenta de novo — loop
de restart, não API respondendo contra meio schema (armadilha 5). O risco real
é o outro, e está na §2.1.

---

## 6. O que rodar depois do deploy

**1. Cada loja ficou com o cardápio inteiro.** As contagens têm que bater
entre as filiais do mesmo restaurante.

```sql
SELECT r.slug, b.name, count(p.id) AS produtos
  FROM branches b
  JOIN restaurants r ON r.id = b.restaurant_id
  LEFT JOIN products p ON p.branch_id = b.id AND p.is_active IS TRUE
 WHERE b.is_active IS TRUE
 GROUP BY r.slug, b.name
 ORDER BY r.slug, b.name;
```

Para o Júnior: duas linhas com 161 cada.

**2. Ninguém ficou sem filial.** Deve voltar vazio — a migração recusaria
aplicar, mas a conferência é barata.

```sql
SELECT 'produto' AS o_que, count(*) FROM products WHERE branch_id IS NULL
UNION ALL
SELECT 'categoria', count(*) FROM categories WHERE branch_id IS NULL;
```

**3. Os pares de catálogo existem.** Cada `catalog_key` deve aparecer uma vez
por loja.

```sql
SELECT catalog_key, count(*) AS lojas
  FROM products
 WHERE catalog_key IS NOT NULL
 GROUP BY catalog_key
HAVING count(*) <> 2
 LIMIT 20;
```

Para um restaurante de duas lojas, deve voltar vazio.

**4. O setor de impressão da loja nova.** É a conferência mais importante do
dia: o remapeamento é por nome, e o que não encontrou par ficou nulo.

```
python scripts/check_restaurant.py junior-da-picanha
```

Ele agora confere **filial a filial**. Onde faltar, aponte a categoria inteira
pelo painel (`PATCH /admin/categories/{id}/printing-sector`) — produto a
produto, um cardápio de 161 itens não sai do lugar.

**5. O Rapi não gastou nada.** A varredura seguinte deve carimbar as cópias
sem gerar embedding: procure `created=0` nas linhas do worker de reindex. Se
aparecer `created=161`, o vetor não foi copiado e a conta da OpenAI vai
aparecer.

**6. O painel.** As telas de cardápio param de funcionar até mandarem
`branch_id`; `POST /admin/categories` e `PATCH /admin/categories/reorder`
respondem **422** sem ele. **O app do cliente** para de conversar com o Rapi
até mandar `branch_id` em `/chat` e `/voice/*`. Não há período de convivência,
de propósito — o mesmo raciocínio do passo 2.

---

## 7. Cupom fica por restaurante, e por quê

**Decisão: não muda.** `restaurant_coupons` continua com `UNIQUE (restaurant_id,
code)` e sem coluna de filial.

O código do cupom viaja num story do Instagram, num panfleto, numa etiqueta —
lugares que não sabem em qual loja o cliente vai pedir. Cupom por filial faria
o mesmo código impresso funcionar num balcão e falhar no outro: exatamente a
falha silenciosa que este passo existe para eliminar, só que de cara para o
cliente.

**Sobre "um cupom de produto que só existe numa das lojas": esse caso não
existe hoje.** `discount_type` é `fixed | percent | free_delivery`, os três são
do pedido inteiro, e nenhuma tabela de cupom referencia produto. A resposta é a
regra que fica registrada para quando alguém for criar:

> **Cupom de produto referencia `catalog_key`, nunca `product_id`.**
> `product_id` agora é da filial: um cupom preso a um `product_id` valeria numa
> loja e falharia na outra sem erro nenhum — o cliente digitaria o código do
> anúncio e a tela diria "cupom inválido". Com `catalog_key`, ele vale onde
> aquele item existe e é recusado com motivo onde não existe.

O que muda de fato é o `total_usage_limit` compartilhado — ver §2.2.

---

## 8. O que este passo NÃO faz

- **Não move produto entre filiais.** Ver §4.2.
- **Não cria "produto herdado do restaurante".** Decisão do passo: sem herança
  e sem sobrescrita. O custo aceito é cadastrar o produto novo N vezes, e
  `catalog_key` é o que mantém o relatório inteiro apesar disso.
- **Não mexe em cupom.** Ver §7.
- ~~**Não atribui custo de voz por filial.**~~ Deixou de ser limitação em
  06/09/2026, por remoção: não há mais voz a atribuir.
- **Não remove `settings_branch_id`** do `/menu`. Ver §3.1.
- **Não apaga produto, e não vai apagar.** `order_items` referencia `products`
  por FK: apagar quebra o histórico que o cliente ainda consulta pelo link de
  acompanhamento. Uma rota que apagasse o produto nunca vendido e recusasse o
  já vendido seria pior que nenhuma — o lojista aprenderia "às vezes some", e o
  dia em que recusasse pareceria bug. Sumir do cardápio é `is_active` e
  `PATCH .../availability`.
- **Não usa `Idempotency-Key` na criação, e não precisa.** A chave natural
  existe e é o nome: `uq_products_branch_slug` já impede a segunda linha, e
  desde 05/09/2026 a colisão responde 409 mesmo quando é o índice que pega
  (§4.9). O mecanismo do cabeçalho ganha quando NÃO há chave natural — é o caso
  do pedido, onde dois pedidos idênticos do mesmo cliente são legítimos.

**Anotado como candidato, e não como pedido:** filtrar ou ordenar a listagem do
painel para o produto desativado parar de poluir a lista do lojista. Levantado
em 05/09/2026 a partir de uma suposição errada (a de que duas linhas podiam
nascer no cardápio, que o índice já impede), e sem lojista pedindo. Fica aqui
para quando houver um.

---

## 9. Coisas que vão quebrar se ninguém souber

- **Voltar a imagem sem descer o banco faz `/menu` devolver as duas lojas
  misturadas, com 200 e sem log.** É o pior modo de falha do passo. Ver §2.1.
- **O slug não identifica produto globalmente.** Ele é único por filial, e as
  duas lojas têm `picanha`. Quem usar slug como chave de cache no app vai
  servir o preço de uma loja na tela da outra.
- **Carrinho não migra entre filiais.** Trocar de loja com o carrinho cheio
  produz 400 no checkout, sem dizer qual produto.
- **`branch_id` é obrigatório no `/chat`** (e era nas rotas de voz, enquanto
  elas existiram). 422 na primeira chamada, e o app do cliente para de
  conversar com o Rapi até subir junto.
- **Produto criado depois do deploy nasce sem `catalog_key`.** Ele some do
  agrupamento do relatório até alguém preencher a chave — e o painel precisa
  de tela para isso (§4.4).
- **Gerente sem filial no cadastro recebe 403 nos relatórios de dinheiro.**
  Não é regressão: é o mesmo 403 de antes, agora com uma saída (mandar
  `branch_id`).
- **`payment_methods` sumiu de `settings`.** Um app que o lia recebe
  `undefined`; a fonte é `/info?branch_id=`.
