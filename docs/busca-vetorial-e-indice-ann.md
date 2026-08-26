# Busca vetorial: por que NÃO existe índice ANN

**Decisão: não criar índice `ivfflat` nem `hnsw` em
`ai_product_embeddings.embedding`.** Medido em 17/08/2026, contra o schema
real, no tamanho de cardápio real, e **reafirmado em 26/08/2026 por um motivo
diferente e mais forte** — ver "A recusa de 26/08/2026" mais abaixo. Este
documento existe para a pergunta não voltar do zero.

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
- **Custo de construir**: **não medido.** O `bench` já cronometra e imprime
  `(construcao: X.XX s)` por cenário, mas o número nunca foi transportado para
  cá, e estimá-lo seria inventar. `python scripts/bench_busca_vetorial.py
  --produtos 5000` responde.
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
- **Os números acima foram medidos ANTES do filtro por filial** (revisão
  `20260820_0026`). O script já inclui o filtro e continua executável, mas a
  tabela não foi refeita. A conclusão não muda de sentido — `p.branch_id` é
  mais uma condição sobre uma linha que a junção com `products` já visitava, e
  o que dimensiona a conta continua sendo o tamanho de **um cardápio de uma
  loja**, que ficou igual ou menor. Ainda assim, é medição não repetida: se
  alguém precisar do número exato, rode `scripts/bench_busca_vetorial.py` de
  novo.
