# Busca vetorial: por que NÃO existe índice ANN

**Decisão: não criar índice `ivfflat` nem `hnsw` em
`ai_product_embeddings.embedding`.** Medido em 17/08/2026, contra o schema
real, no tamanho de cardápio real; **reafirmado em 26/08/2026 por um motivo
diferente e mais forte** (ver "A recusa de 26/08/2026"); e **remedido em
02/09/2026** com o filtro por filial e com o custo de construir, que faltavam
(ver a última seção). Este documento existe para a pergunta não voltar do
zero.

**Ao ler as tabelas: elas não se comparam entre si.** Cada uma é de uma
execução, e a bancada varia entre execuções — a mesma consulta em 161 produtos
deu 52,04 ms em 17/08 e 43,85 ms em 02/09. O que vale é a comparação **dentro**
de uma tabela, onde os três cenários enfrentam as mesmas condições.

Em uma linha: o ganho é 4 ms de 52, e o preço é o assistente voltar a poder
dizer "não temos" sobre um produto que existe.

Reproduzir:

```bash
docker compose -f docker-compose.test.yml up -d
python scripts/bench_busca_vetorial.py --produtos 161 --restaurantes 1
```

A consulta medida é a de `AIRepository.similarity_search` — a mesma que o
Rapi (texto) e a voz usam, com `top_k=5` e o piso de
`AI_SEARCH_MIN_SIMILARITY = 0,30`.

---

## O resultado, no cardápio do Júnior

161 produtos indexados, 300 consultas por cenário:

| Cenário | Mediana | p95 | Servidor | O planejador leu |
|---|---|---|---|---|
| **sem índice (hoje)** | 52,04 ms | 54,51 ms | 3,96 ms | `Seq Scan` |
| ivfflat `lists=12` | 51,98 ms | 54,30 ms | 3,67 ms | `Seq Scan` |
| hnsw `m=16` | 52,03 ms | 55,74 ms | 3,58 ms | `Seq Scan` |

**Ganho real: +0,1% e +0,0%.** Isso é ruído de medição, e a coluna da direita
explica por quê: **o planejador não usa o índice.** Com 161 linhas ele lê a
tabela inteira e acha certo — porque está certo. O índice seria criado,
ocuparia disco e seria mantido em **toda escrita**, sem nunca ser lido.

E as escritas não são raras: o container `reindex` varre o cardápio a cada
minuto e reescreve todo vetor que ficou atrás do produto.

---

## A varredura não é o gargalo — e não é nem perto

Das 52 ms de uma consulta, **o banco usa 4**. As outras 48 são o vetor de
1536 dimensões viajando como ~20 KB de texto (`CAST(:embedding AS vector)`)
e sendo convertido em 1536 floats antes de a execução começar — custo que
não aparece no `Execution Time` do `EXPLAIN` e que é **idêntico nos três
cenários**.

Ou seja: **um índice infinitamente rápido economizaria no máximo 4 ms de 52.**
Discutir ANN aqui é otimizar 7% do problema.

Se um dia a latência da busca virar prioridade, o alvo é esse custo fixo —
parâmetro binário em vez de texto, ou mais acerto no cache de embedding
(`chat_cache.py`, TTL de 60 min) —, não o índice.

---

## Crescer a plataforma não muda isso. Crescer UM cardápio muda.

Esta é a parte contraintuitiva, e é o que mais economiza tempo saber.

`ai_product_embeddings` é compartilhada por todos os restaurantes, então a
suspeita natural é que a varredura piore conforme a base cresça. **Não
piora.** Com 30 restaurantes de 161 produtos (4.830 linhas), o Postgres
troca de estratégia sozinho:

| Cenário (30 restaurantes) | Mediana | Servidor | O planejador leu |
|---|---|---|---|
| sem índice | 52,22 ms | 3,85 ms | `Bitmap Heap Scan` |
| ivfflat `lists=48` | 48,43 ms | 3,22 ms | `Bitmap Heap Scan` |
| hnsw `m=16` | 51,88 ms | 5,06 ms | `Bitmap Heap Scan` |

Ele passa a usar o índice B-tree que **já existe**,
`ai_product_embeddings_restaurant_product_unique (restaurant_id, product_id)`,
para recortar as 161 linhas daquele restaurante, e só então calcula
distância. O ANN continua ignorado, e o tempo não se move.

**O que dimensiona a conta é o tamanho de UM cardápio, não o da base.**

---

## Onde o índice passaria a pagar

Um restaurante só, com 5.000 produtos indexados (400 consultas):

| Cenário | Mediana | p95 | Servidor | O planejador leu | Linhas devolvidas |
|---|---|---|---|---|---|
| sem índice | 46,42 ms | **110,19 ms** | 45,89 ms | `Seq Scan` | 5,00 (mínimo 5) |
| ivfflat `lists=50` | 51,84 ms | 53,48 ms | **0,73 ms** | `Index Scan` | 5,00 (mínimo 5) |
| hnsw `m=16` | 51,86 ms | 54,09 ms | 1,30 ms | `Index Scan` | **4,99 (mínimo 0)** |

Aqui o planejador **escolhe** o índice e o trabalho do servidor cai de ~46 ms
para menos de 1. Duas leituras importam:

- **A mediana quase não melhora, mas a cauda sim** (110 ms → 53 ms). É o teto
  de ~50 ms do parágrafo anterior: enquanto a varredura couber dentro do
  custo de transportar o vetor, ela é invisível. Passando disso, aparece —
  primeiro no p95.
- **A última coluna é o preço do ANN.** Uma consulta em 400 com `hnsw`
  devolveu **zero produtos** onde a busca exata devolveu cinco. Não é
  hipótese de manual: aconteceu na medição. Para o cliente isso é o Rapi
  dizendo "não achei" sobre um produto que existe no cardápio — sem erro, sem
  log, sem nada.

**Quando revisitar:** um único restaurante passando de ~3.000 produtos
ativos indexados, ou o p95 da busca subindo sem o cardápio ter mudado. Até
lá, exatidão de graça vale mais que 4 ms.

**E revisitar não é autorizar.** O gatilho acima diz apenas que o argumento de
LATÊNCIA virou de lado; o `mínimo 0` da última coluna continua valendo no dia
seguinte, e ele é o que de fato recusa. A conta inteira está em "A recusa de
26/08/2026".

```sql
-- o número que decide, restaurante a restaurante
SELECT r.slug, count(*) AS produtos_indexados
  FROM ai_product_embeddings ape
  JOIN restaurants r ON r.id = ape.restaurant_id
 GROUP BY r.slug
 ORDER BY 2 DESC;
```

---

## A recusa de 26/08/2026, e por que ela não é sobre latência

A pergunta voltou — tipo de índice, custo de construir, efeito na precisão — e
foi **recusada**. O registro fica aqui para ela não voltar do zero uma terceira
vez, e porque o motivo que decidiu **não é o mesmo** que este documento vinha
dando.

O argumento antigo é de desempenho: das 52 ms de uma consulta o banco usa 4, e
um índice infinitamente rápido economizaria 4 ms de 52. Ele continua verdadeiro
e continua fraco — **é um argumento que vence hoje e perde no dia em que um
cardápio crescer.**

O que decidiu é a última coluna da tabela de 5.000 produtos, e é sobre
correção:

> Uma consulta em 400 com `hnsw` devolveu **zero produtos** onde a busca exata
> devolveu cinco.

ANN é aproximado por definição: ele visita parte do grafo, não a tabela inteira.
Devolver menos é o comportamento correto dele, não um defeito de configuração —
`ef_search` desloca a fronteira, não a apaga.

**E "devolver zero" tem um significado específico neste sistema.** Desde
25/08/2026 a negativa do atendente de voz não é mais escrita pelo modelo: a
busca vazia devolve a frase pronta (`_NEGATIVA`, em
`src/ai/voice/search_service.py`), e "não temos" passou a ter uma origem só —
uma busca que voltou vazia **naquele turno**. Foi o conserto da armadilha 46,
onde uma churrascaria disse não ter picanha.

Um índice ANN reabre essa porta por dentro. A busca vazia deixaria de
significar "não tem" e passaria a significar "não tem, **ou** o grafo não
passou por lá" — e as duas chegam ao cliente como a mesma frase, sem erro, sem
log, sem tela onde conferir. Trocaríamos 4 ms por uma negativa que volta a não
ter dono, três semanas depois de ela ter ganhado um.

Isso vale a pena escrever porque é o tipo de custo que não aparece em nenhum
`EXPLAIN`: quem medir de novo vai reencontrar os 4 ms e não vai reencontrar
isto.

### O que fica registrado, para o dia em que a conta mudar

**O gatilho não mudou, e sozinho ele não basta mais.** Continua sendo um único
restaurante passando de ~3.000 produtos ativos indexados — o Júnior, que é o
maior cardápio em produção, tem 136. Mas o gatilho só diz que o **argumento de
latência** virou; a pergunta da recall continua aberta e precisa de resposta
própria antes de qualquer índice ser criado. Bater os 3.000 não é autorização.

Se aquele dia chegar, o que já está medido:

- **Tipo**: `hnsw` com `vector_cosine_ops` (`m=16`, `ef_construction=64`).
  Não `ivfflat`: ele depende de `lists` calibrado ao tamanho da tabela, e
  `lists` defasado degrada em silêncio conforme o cardápio cresce — ninguém
  recalibra um índice que ninguém está olhando. O operador **tem** que ser
  `vector_cosine_ops`, porque a consulta ordena por `<=>`; com outro operador o
  planejador simplesmente não escolhe o índice, e ele fica ocupando disco e
  sendo mantido em toda escrita sem ser lido uma vez.
- **Custo de construir**: **medido em 02/09/2026**, e a resposta é *ordem de
  grandeza de segundos*:

  | Cardápio | `ivfflat` | `hnsw m=16` |
  |---|---|---|
  | 161 produtos | 0,01 s | 0,12 s |
  | 5.000 produtos | 0,94 s | 2,65 s |
  | 5.000 produtos (outra execução, mesma tarde) | 0,46 s | 6,30 s |

  **A terceira linha é o aviso, não ruído a ignorar**: a mesma construção, no
  mesmo tamanho, variou de 2,65 s a 6,30 s. Máquina de desenvolvimento tem
  outra carga. Para a decisão isso basta — segundos cabem dentro do `alembic
  upgrade` do entrypoint, com a API fora do ar, e este **não** é o caso da
  armadilha 5, que é de índice que leva minutos.
- **Custo de manter, esse sim conhecido**: o índice é mantido em **toda
  escrita**, e o container `reindex` reescreve todo vetor que ficou atrás do
  produto a cada minuto.
- **Como criar, se um dia for criado**: `CREATE INDEX` sem `CONCURRENTLY` trava
  escrita na tabela, e rodaria dentro do `alembic upgrade` do entrypoint — com
  a API fora do ar (armadilha 5). Ou fora do movimento, ou `CREATE INDEX
  CONCURRENTLY` à mão seguido de `alembic stamp`.

---

## Ressalvas honestas desta medição

- **Os vetores são sintéticos**, agrupados em 12 temas para a similaridade
  cair na faixa real (0,17–0,66, a mesma que fixou `AI_SEARCH_MIN_SIMILARITY`).
  O custo da varredura depende do número de linhas e da dimensão, não do
  conteúdo — mas a **recall** do ANN depende, e o `mínimo 0` da tabela acima
  é indicativo, não uma taxa de erro medida em produção.
- **O banco é local**, em `tmpfs` e com `fsync=off`. Produção é Supabase, com
  rede no meio: lá o custo fixo por consulta é **maior**, o que só reforça a
  conclusão — a fatia do índice fica ainda menor.
- **O `Execution Time` do `EXPLAIN ANALYZE` é medido fora do laço
  cronometrado.** A instrumentação cobra por nó e por linha, e numa varredura
  de milhares de linhas ela infla justamente o número em disputa.
- **As tabelas acima foram medidas ANTES do filtro por filial** (revisão
  `20260820_0026`). **Refeitas com o filtro em 02/09/2026** — a conclusão não
  mudou de sentido, e os números estão na seção seguinte.

## A remedição de 02/09/2026, com o filtro por filial

O que ela fechou: as tabelas eram anteriores à revisão `20260820_0026`, e o
custo de construir nunca tinha sido transportado para cá. Os dois estão
resolvidos. **Nenhuma decisão mudou.**

**161 produtos, 300 consultas** — conclusão idêntica, o planejador continua
ignorando o índice:

| Cenário | Mediana | p95 | Servidor | O planejador leu | Mínimo de linhas |
|---|---|---|---|---|---|
| sem índice | 43,85 ms | 49,85 ms | 1,32 ms | `Seq Scan` | 5 |
| ivfflat `lists=12` | 44,32 ms | 50,29 ms | 1,77 ms | `Seq Scan` | 5 |
| hnsw `m=16` | 44,04 ms | 48,09 ms | 1,05 ms | `Seq Scan` | 5 |

**5.000 produtos num restaurante só, 300 consultas:**

| Cenário | Mediana | p95 | Servidor | O planejador leu | Mínimo de linhas |
|---|---|---|---|---|---|
| sem índice | 66,89 ms | 88,42 ms | 26,19 ms | `Seq Scan` | 5 |
| ivfflat `lists=50` | 44,00 ms | 48,22 ms | 0,48 ms | `Index Scan` | 5 |
| hnsw `m=16` | 44,13 ms | 45,33 ms | 0,68 ms | `Index Scan` | 5 |

### AS TABELAS DESTE DOCUMENTO NÃO SE COMPARAM ENTRE SI

É a correção de método mais importante desta remedição. A **mesma** consulta,
no **mesmo** tamanho de 161 produtos, deu **52,04 ms** em 17/08 e **43,85 ms**
em 02/09. Nada no repositório mudou para justificar 8 ms — mudou a máquina e a
carga dela.

O que é comparável é **dentro de uma execução**, onde os três cenários
enfrentam as mesmas condições. Ler a mediana de uma tabela contra a de outra
produz um "ganho" que é só ruído de bancada.

Com essa régua, em 5.000 produtos o ANN venceu **também na mediana** (66,89 →
44,00 ms), e não só na cauda como a tabela de 17/08 registrava. A parte que não
muda entre as duas medições é a que importa: **o trabalho do servidor desaba**,
26,19 ms → 0,48 ms.

### O `mínimo 0` NÃO reproduziu — e isso não absolve o ANN

A coluna "Mínimo de linhas" acima diz **5** em todos os cenários, inclusive
`hnsw`. Duas execuções, uma com 300 consultas e outra com **2.000** e semente
diferente, e a falha de recall que decidiu a recusa de 26/08 não apareceu.

Isso **não** é motivo para criar o índice. É motivo para tratá-lo pior:

- 1 em 400 na medição original e 0 em 2.300 hoje descrevem a mesma coisa — um
  evento **raro o bastante para não aparecer num benchmark e frequente o
  bastante para ter aparecido**. É a pior faixa possível: não se reproduz sob
  demanda e não se descarta;
- os vetores são sintéticos, e a ressalva desta página já diz que a recall do
  ANN depende do conteúdo;
- e o argumento de 26/08 **não é estatístico**. Ele é sobre o que a busca vazia
  *significa* desde a armadilha 46. Uma taxa de erro baixa não devolve o dono
  da negativa.
