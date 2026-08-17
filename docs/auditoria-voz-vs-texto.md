# Auditoria: agente de TEXTO × agente de VOZ

**Branch auditada:** `experimento/voz` (commit `036e63f`), árvore limpa.
**Natureza:** leitura de código. Nenhum arquivo de código foi alterado.
**Data:** 15/08/2026.

Onde não consegui provar algo pelo código, está escrito **não verificado**.

---

## 1. Mapa

### 1.1 Onde a credencial efêmera é emitida

| O quê | Onde |
|---|---|
| Rota | `src/api/voice.py:48` (`POST /voice/session`) |
| Serviço | `src/ai/voice/realtime_client.py:82` (`issue_client_secret`) |
| Endpoint da OpenAI | `realtime_client.py:47` — `https://api.openai.com/v1/realtime/client_secrets` |

O corpo enviado está em `realtime_client.py:90-99`:

| Campo | Valor | Prova |
|---|---|---|
| `session.type` | `"realtime"` | `realtime_client.py:92` |
| `session.model` | `"gpt-realtime-mini"` | `realtime_client.py:48`, usado em `:93` |
| `session.instructions` | `instructions_for(restaurant_context)` | `realtime_client.py:94` → `voice_prompt.py:49` |
| `session.audio.output.voice` | `"marin"` | `realtime_client.py:49`, usado em `:95` |
| `session.tools` | `[SEARCH_TOOL]` | `realtime_client.py:96` |
| `session.tool_choice` | `"auto"` | `realtime_client.py:97` |
| **TTL** | **não é definido pelo nosso código** | não há campo de expiração em `realtime_client.py:90-99` |

Sobre o TTL: quem decide é a OpenAI. Numa emissão real feita durante esta
auditoria, `expires_at` veio **585 segundos** à frente do relógio. É um valor
observado, não um contrato — e não está em lugar nenhum do código.

### 1.2 A lista de tools declaradas (completa)

Uma só, em `realtime_client.py:56-79`:

```python
{
    "type": "function",
    "name": "buscar_no_cardapio",
    "description": (
        "Busca produtos no cardapio deste restaurante. Use SEMPRE antes de "
        "falar de qualquer produto, preco ou ingrediente. Os produtos "
        "encontrados aparecem na tela do cliente automaticamente."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "consulta": {
                "type": "string",
                "description": (
                    "O que o cliente quer, em palavras dele. "
                    "Exemplos: 'sobremesa de chocolate', 'algo vegetariano', "
                    "'a picanha'."
                ),
            }
        },
        "required": ["consulta"],
        "additionalProperties": False,
    },
}
```

**Um único parâmetro: `consulta`, string.** Não há preço, faixa, categoria,
quantidade nem qualquer outro filtro.

### 1.3 Onde o prompt do agente de texto é montado

| Etapa | Onde |
|---|---|
| Texto do sistema | `src/ai/prompts/system_prompt.py:1` (`SYSTEM_PROMPT`) |
| Template | `src/ai/prompts/chat_prompt.py:6` (`build_chat_prompt`) — system em `:10`, human em `:12-25` com 4 slots: `restaurant_context`, `conversation`, `retrieved_products`, `user_message` |
| Cadeia | `src/ai/services/chat_llm_service.py:26` (`build_chain`), saída estruturada em `:29` |
| Chamada | `src/services/chat_service.py:221` |

O prompt de voz é **outro arquivo, sem nenhuma relação de código** com esse:
`src/ai/voice/voice_prompt.py:22` (`VOICE_INSTRUCTIONS`) e `:49`
(`instructions_for`). Não há import cruzado nos dois sentidos.

### 1.4 O que a voz reusa do ChatService

| Membro do `ChatService` | Privado? | Usado em |
|---|---|---|
| `_get_active_restaurant` | **sim** | `search_service.py:54` e `src/api/voice.py:50` |
| `_build_restaurant_context` | **sim** (`@staticmethod`) | `src/api/voice.py:51` |
| `retrieval_service` (atributo) | não | `search_service.py:56` |
| `_hydrate_products` | **sim** | `search_service.py:61` |

**Três dos quatro pontos de acoplamento são privados.** O `ChatService` é
construído inteiro duas vezes por interação de voz: uma em
`src/api/voice.py:50` (rota da sessão) e outra em `search_service.py:46` (cada
tool call).

---

## 2. Busca

### 2.1 O caminho da voz até o SQL

```
page.html:161   fetch POST /voice/search
  → src/api/voice.py:63    buscar()
  → search_service.py:48   VoiceSearchService.buscar()
  → search_service.py:56   chat_service.retrieval_service.retrieve_products()
      → retrieval_service.py:41   EmbeddingService.generate_embedding()
          → embedding_service.py:15  OpenAIEmbeddings.embed_query()
      → retrieval_service.py:56   AIRepository.similarity_search()
          → ai_repository.py:27      o SQL
  → search_service.py:61   chat_service._hydrate_products()
      → product_repository.py:84  list_active_by_ids()
      → menu_service.py            product_response()
```

O caminho do texto entra na mesma função em `chat_service.py:135`.

### 2.2 É o mesmo caminho?

**Sim, é literalmente a mesma função.** Item a item:

| Parâmetro | Texto | Voz | Prova |
|---|---|---|---|
| Função de busca | `retrieve_products` | idem | `chat_service.py:135` / `search_service.py:56` |
| Modelo de embedding | `settings.EMBEDDING_MODEL` | idem | `embedding_service.py:12`; default `text-embedding-3-small` em `config.py:51` |
| Tabela | `ai_product_embeddings` JOIN `products` | idem | `ai_repository.py:36-37` |
| `top_k` | 5 | 5 | default em `retrieval_service.py:29`; **nenhum dos dois chamadores passa o valor** (`chat_service.py:135-138`, `search_service.py:56-59`) |
| Filtro de restaurante | `ape.restaurant_id` e `p.restaurant_id` | idem | `ai_repository.py:48-50` |
| `is_active` / `is_available` | os dois exigidos | idem | `ai_repository.py:51-52` |
| Ordenação | distância cosseno `<=>` | idem | `ai_repository.py:53` |

**Limiar de similaridade: não existe, para nenhum dos dois.** A similaridade é
*calculada* (`ai_repository.py:45`) e nunca usada em `WHERE`. Só há
`ORDER BY ... LIMIT :top_k` (`ai_repository.py:53-54`). Consequência prática:
os dois agentes sempre recebem até 5 produtos, por mais irrelevantes que
sejam — não existe "não encontrei nada" vindo da busca, só lista vazia quando
o restaurante não tem nenhum embedding.

**Uma divergência dentro do caminho comum.** `_with_current_prices`
(`retrieval_service.py:77`, chamado em `:73`) carimba o preço vigente e roda
para os dois. Mas a voz **descarta esse resultado**: `search_service.py:60` lê
apenas `produto["id"]`. Ou seja, toda tool call de voz paga uma consulta
(`sellable_prices_by_id`, `product_repository.py:56`) cujo resultado é jogado
fora, e o preço que a voz usa vem depois, da hidratação
(`search_service.py:82`).

### 2.3 Teto de preço ou faixa

**Não existe em lugar nenhum.** Nem na tool (`realtime_client.py:64-78`), nem
na assinatura de `retrieve_products` (`retrieval_service.py:25-30`), nem no
SQL (`ai_repository.py:27-66`).

Onde ele entraria, se um dia entrar:

1. **Assinatura da tool**, `realtime_client.py:64-78` — um campo novo em
   `properties` (não em `required`, senão o modelo é obrigado a inventar um
   teto quando o cliente não pediu nenhum).
2. **Assinatura da busca**, `retrieval_service.py:25-30` — parâmetro opcional,
   repassado adiante.
3. **`WHERE` do SQL**, `ai_repository.py:48-52`.

O ponto 3 é obrigatório e não é detalhe de implementação: filtrar **depois**
da busca reduziria uma lista que já veio cortada em 5
(`ai_repository.py:54`), então "tem algo até R$ 50" devolveria zero produtos
sempre que os 5 mais parecidos fossem caros — mesmo com o cardápio cheio de
opções baratas. O teto tem que entrar antes do `LIMIT`.

### 2.4 Cache

Dois, os dois em memória do processo (`chat_cache.py`, instância no fim do
arquivo). **A voz passa pelos dois**, porque usa a mesma função.

| Cache | TTL | Chave | Prova |
|---|---|---|---|
| Embedding da pergunta | 3600 s | `{restaurant_id}:{pergunta normalizada}` | `chat_cache.py:16`, chave em `:119` |
| Resultado da busca | 1200 s | `{restaurant_id}:g{geração}:{pergunta normalizada}` | `chat_cache.py:17`, chave em `:129` |

A chave da voz é a string `consulta` **gerada pelo modelo**, e não o que o
cliente falou (`page.html:155` → `src/api/voice.py:63`). O modelo reformula a
mesma intenção de formas diferentes a cada turno, então a taxa de acerto do
cache na voz deve ser bem menor que na de texto. **Não medido** — é raciocínio
sobre o mecanismo, não medição.

### 2.5 Latência da tool call

Medido nesta auditoria, em `scratchpad/medir_latencia.py` (fora do repositório).

**Embedding — chamadas reais à OpenAI, `text-embedding-3-small`:**

| Chamada | Tempo |
|---|---|
| 1ª (fria, inclui handshake TLS) | 2656 ms |
| 2ª | 983 ms |
| 3ª | 383 ms |

**Consulta pgvector — banco de teste local, vetores aleatórios:**

| Produtos indexados | Mediana | Mín | Máx |
|---|---|---|---|
| 50 | 49,2 ms | 47,8 | 52,7 |
| 200 | 55,2 ms | 51,8 | 57,9 |
| 1000 | 104,4 ms | 96,0 | 156,8 |

Sobre esses números:

- **Não há índice ANN.** Verifiquei: nenhum `ivfflat` ou `hnsw` em `alembic/`.
  A busca é varredura sequencial, e o crescimento de 50 → 1000 produtos
  (49 ms → 104 ms) é compatível com isso.
- O tempo do SQL inclui **mandar o vetor de consulta como literal de texto**
  (`ai_repository.py:260`, `_format_vector`): 1536 floats viram ~20 KB de
  string por consulta. Parte fixa do custo, independente do tamanho do
  catálogo.
- Banco local, sem rede. Contra o Supabase soma o RTT.

**Estimativa da tool call inteira** (cache frio, conexão já quente):
`0,4–1,0 s` de embedding + `0,05–0,10 s` de SQL + hidratação **não medida**
(duas consultas por chave primária com `selectinload`). Ordem de grandeza:
**meio segundo a um segundo e meio** de silêncio no meio da conversa.

Com acerto no cache de busca, o embedding e o SQL somem e sobra só
`sellable_prices_by_id` + hidratação — poucos milissegundos.

**Não verificado, e importa:** cada requisição constrói um `EmbeddingService`
novo (`retrieval_service.py:21`, via `ChatService(db)` em
`search_service.py:46`), que constrói um `OpenAIEmbeddings` novo. Não conferi
se a biblioteca reaproveita conexão HTTP entre instâncias. Se não
reaproveitar, **toda tool call paga o handshake** e o número que vale é o da
primeira linha da tabela — 2,6 s — e não o da terceira.

---

## 3. Divergências, item a item

| Comportamento | Voz | Texto | Prova |
|---|---|---|---|
| `restaurant_context` com nome e descrição | **sim** | **sim** | mesma função: `chat_service.py:183`, chamada em `:140` (texto) e `src/api/voice.py:51` (voz) |
| Preço da linha viva de `products`, fora do cache | **sim, por outro caminho** | **sim** | texto: `retrieval_service.py:73` → `:107`. Voz: descarta isso (`search_service.py:60`) e usa o preço da hidratação (`search_service.py:82`), que também é do banco |
| Regra "não citar produto que não veio da busca" — **no prompt** | **sim** | **sim** | `voice_prompt.py:31` / `system_prompt.py:16` |
| Regra "não citar produto que não veio da busca" — **no código** | **não existe** | **sim** | texto: `_validate_selected_product_ids`, `chat_service.py:238`, chamada em `:145`. Voz: nada equivalente |
| `selected_product_ids` + resgate de seleção vazia | **não** | **sim** | `chat_service.py:145` e `:149`. Na voz o modelo não devolve id nenhum — por desenho (`search_service.py:51-52`) |
| Detecção de divergência entre preço falado e preço real | **não** | **sim** | `_log_price_divergence`, `chat_service.py:312`, chamada em `:155`. Sem equivalente na voz |
| Truncagem e achatamento da `description` do lojista | **sim** | **sim** | mesma função: `chat_service.py:207-209` |

### 3.1 A divergência que não estava na lista

**O formato do preço não é o mesmo nos dois agentes.**

- Texto: `format_money_br` → `"R$ 23,90"`, vírgula (`retrieval_service.py:107`,
  helper em `src/utils/money.py`).
- Voz: `f"{produto.name} - R$ {produto.price:.2f}"` → `"R$ 23.90"`, **ponto**
  (`search_service.py:82`).

O prompt de voz manda dizer o preço "exatamente como a ferramenta devolveu"
(`voice_prompt.py:38`) — e o que a ferramenta devolve está em outro formato
do que o resto do sistema usa. `format_money_br` existe e não é chamado ali.

### 3.2 Sobre a regra anti-invenção

Vale registrar que a ausência da validação na voz **não é o mesmo buraco** que
seria no texto. No texto o modelo devolve ids, e sem
`_validate_selected_product_ids` um id inventado viraria cartão na tela. Na
voz o modelo não devolve id nenhum: os cartões vêm da nossa busca
(`src/api/voice.py:74`). O que ele *pode* fazer é **falar** de um produto que
não existe — e aí não há código nenhum entre isso e o ouvido do cliente, porque
a saída é áudio.

---

## 4. O que o backend enxerga da voz

### 4.1 A fala e a resposta chegam ao backend?

**Não.** O áudio vai do navegador direto para a OpenAI
(`page.html:96-107`, `POST https://api.openai.com/v1/realtime/calls`).

A única coisa que chega ao backend é a string `consulta` **gerada pelo
modelo** (`page.html:155` → `:162` → `src/api/voice.py:63`) — não é o que o
cliente falou, é a reformulação que o modelo escolheu.

As transcrições existem como eventos da Realtime e são tratadas em
`page.html:129-134`, mas só vão para o painel de log **no navegador**.
Nenhum `fetch` as envia para lugar nenhum.

### 4.2 Fica gravado alguma coisa? Com `restaurant_id`?

| Registro | Onde | Tem `restaurant_id`? |
|---|---|---|
| `[Voz] busca \| encontrados=%d \| hidratados=%d` | `search_service.py:63-67` | **não** |
| `[Voz] credencial emitida \| modelo=%s \| expira_em=%s` | `realtime_client.py:133-137` | **não** |

**Nenhuma linha de log da voz carrega `restaurant_id`, `session_id` ou a
consulta.** É impossível, hoje, atribuir uma interação de voz a um
restaurante.

Compare com o texto, que loga `restaurant_id`, `session_id`,
`message_chars` e um digest sha256 da mensagem (`chat_service.py:81-84`).

**Tabela: nada.** `ai_feedback` só é escrito por `create_feedback`
(`chat_service.py:51`), que só é alcançável por `POST /chat/feedback`
(`src/api/chat.py:24`). A voz não tem rota de feedback.

**Ruído no log alheio:** como a voz usa `retrieve_products` e
`_hydrate_products`, ela emite linhas com o prefixo do chat de texto —
`[AI /chat perf] embedding_ms`, `retrieval_ms`, `context_products`
(`retrieval_service.py:44,66,74`) e `hydration_ms` (`chat_service.py:356`).
Quem grepar `[AI /chat perf]` para medir o chat de texto está medindo os dois
misturados, sem nenhuma forma de separar.

### 4.3 Tokens e uso da sessão de voz

**Não são contabilizados em lugar nenhum.** Não há tabela, contador nem log
de consumo. A única chamada que o backend faz à OpenAI é a emissão
(`realtime_client.py:102`), e dela só o `expires_at` é registrado
(`realtime_client.py:136`). O que acontece depois — duração da conversa,
tokens de áudio, custo — acontece inteiramente fora do alcance do backend.

### 4.4 Login, rate limit, teto

| Proteção | Existe? | Prova |
|---|---|---|
| Login | **não** | `src/api/voice.py:49` e `:63` dependem só de `get_db`; não há `get_current_customer` |
| Rate limit | **não** | nenhum `@limiter.limit` em `src/api/voice.py` (contraste: `src/api/chat.py:25` e `:35`) |
| Teto de duração da sessão | **não** | nada no corpo enviado em `realtime_client.py:90-99` |
| Teto de gasto por sessão | **não** | idem |
| Cota por cliente / restaurante | **não** | nada em `src/api/voice.py` nem em `realtime_client.py` |

A única barreira existente é `VOICE_ENABLED` (`main.py:144`,
`config.py`), com padrão `False`. Está tudo declarado no cabeçalho de
`realtime_client.py:1-38`, que diz textualmente que isso não pode ir para
produção.

---

## 5. Risco

### 5.1 O que quebra se alguém mexer no ChatService sem saber que a voz o usa

**Nenhum teste cobre as rotas de voz.** Verifiquei: nenhum arquivo em
`tests/` menciona `experimento`. A suíte inteira pode continuar verde com a
voz quebrada.

| Mudança no `ChatService` | O que quebra | Onde |
|---|---|---|
| Renomear/remover `_get_active_restaurant` | `AttributeError` em runtime, nas **duas** rotas de voz | `search_service.py:54`, `src/api/voice.py:50` |
| Mudar a assinatura ou o retorno de `_hydrate_products` | a tool call quebra; `resumo_para_o_modelo` lê `.name` e `.price` do que vier (`search_service.py:82`) | `search_service.py:61` |
| Tornar `_build_restaurant_context` não estático, ou mudar o texto que ele devolve | a emissão da credencial quebra ou passa instrução errada | `src/api/voice.py:51` |
| Mudar `ChatService.__init__` (exigir mais argumentos) | as duas rotas de voz | `search_service.py:46`, `src/api/voice.py:50` |
| Renomear o atributo `retrieval_service` | a tool call | `search_service.py:56` |
| Mudar o formato de `retrieve_products` (ex.: `id` deixar de ser chave do dict) | `search_service.py:60` levanta `KeyError` | `search_service.py:60` |

Nenhuma dessas mudanças produz erro de importação ou de tipo — todas falham
em tempo de execução, e só quando alguém abrir a página de voz.

### 5.2 O que hoje precisa ser corrigido em DOIS lugares

| Assunto | Lugar 1 (texto) | Lugar 2 (voz) |
|---|---|---|
| Formato do preço mostrado ao modelo | `retrieval_service.py:107` (`format_money_br`) | `search_service.py:82` (`:.2f`) — **já divergiram** |
| Regra "nunca invente produto" | `system_prompt.py:15-16` | `voice_prompt.py:31-32` |
| Regra "diga o preço exatamente" | `system_prompt.py:22-29` | `voice_prompt.py:38-40` |
| Assuntos fora do escopo (horário, entrega, pagamento) | `system_prompt.py:40-47` | `voice_prompt.py:42-45` |
| Persona / tom da casa | `system_prompt.py:2-12` | `voice_prompt.py:26-33` |
| Prefixo de log | `[AI /chat]` | `[Voz]` + `[AI /chat]` herdado |

**O que NÃO está duplicado, e é bom que se saiba:** `top_k`
(`retrieval_service.py:29`), os filtros do SQL (`ai_repository.py:48-52`), o
formato do contexto recuperado (`retrieval_service.py:111`), o
`restaurant_context` (`chat_service.py:183`) e a hidratação
(`chat_service.py:342`) têm **uma definição só** e servem aos dois. Mexer
nesses cinco alcança os dois agentes de uma vez — para o bem e para o mal.

---

## Tabela final de divergências

| Comportamento | Texto | Voz | Risco |
|---|---|---|---|
| Busca vetorial (modelo, tabela, `top_k`=5, filtros) | sim | sim, mesma função | — |
| Limiar de similaridade | não existe | não existe | os dois sempre recebem 5 produtos, por pior que sejam; não há "não achei" |
| Cache de embedding (1 h) e de busca (20 min) | passa | passa | na voz a chave é texto gerado pelo modelo → menos acerto (não medido) |
| Preço vindo da linha viva | sim | sim (via hidratação) | — |
| Formato do preço dado ao modelo | `R$ 23,90` | `R$ 23.90` | **divergiu**; a voz não usa `format_money_br` |
| Teto/faixa de preço na busca | não | não | "algo até R$ 50" não é respondível hoje |
| Validação de id inventado | `_validate_selected_product_ids` | não se aplica (modelo não devolve id) | na voz o modelo pode **falar** produto inexistente sem nada barrar |
| Resgate de seleção vazia | sim | não se aplica | — |
| Detecção de preço divergente | `_log_price_divergence` | **nenhuma** | preço falado errado não deixa rastro; saída é áudio |
| `restaurant_context` (nome + descrição) | sim | sim, mesma função | — |
| Truncagem da `description` do lojista | sim | sim, mesma função | — |
| Login na rota | não (público, por desenho) | não | na voz, cada requisição vira sessão faturada |
| Rate limit | `20/min;200/h` | **nenhum** | laço de `curl` emite credenciais sem limite |
| `restaurant_id` no log | sim | **não** | impossível atribuir uso de voz a restaurante |
| Digest da mensagem no log | sim | **não** (nem a consulta) | sem correlação entre requisições |
| Contabilização de tokens/custo | não | não | custo de voz é invisível ao backend |
| Feedback do cliente (`ai_feedback`) | sim | não | sem sinal de qualidade da voz |
| Cobertura de teste | 40 testes em `test_chat_service.py` | **zero** | mudança no `ChatService` quebra a voz em silêncio |

---

## O que unificar, em ordem de custo

1. **Formato do preço na voz** — trocar `:.2f` por `format_money_br` em
   `search_service.py:82`. Uma linha, e é a única divergência que já produz
   comportamento diferente hoje.
2. **`restaurant_id` e digest da consulta no log da voz** —
   `search_service.py:63` e `realtime_client.py:133`. Duas linhas, e sem isso
   nenhuma pergunta operacional sobre a voz é respondível.
3. **Prefixo de log próprio** — fazer a voz não emitir `[AI /chat perf]`.
   Exige um jeito de `retrieve_products` e `_hydrate_products` saberem quem
   as chamou; é a primeira mudança que toca código compartilhado.
4. **Promover os três métodos privados** (`_get_active_restaurant`,
   `_build_restaurant_context`, `_hydrate_products`) para um lugar
   compartilhado. Mecânico, mas mexe em `chat_service.py` e nos testes dele.
5. **Um teste de fumaça das rotas de voz** — as rotas montam, 404 para
   restaurante inexistente, e a tool devolve produto hidratado. É o que
   transforma "quebra em silêncio" em "quebra no CI".
6. **Regras compartilhadas dos dois prompts** (não inventar produto, preço
   exato, assuntos fora do escopo) num único lugar, com cada agente
   acrescentando o que é só dele. É o item mais caro e o mais discutível:
   texto e voz têm meios diferentes, e forçar um tronco comum pode piorar os
   dois. Só vale se a voz sair de experimento.
7. **Teto de preço na busca** — tool + assinatura + `WHERE`. Não é
   unificação, é recurso novo; entra aqui porque é a mudança que mais tende a
   ser feita em um lugar só e esquecida no outro.

**Fora desta lista, de propósito:** login, cota, teto de duração e
conciliação de consumo. Não são divergências entre os dois agentes — são o
que falta para a voz existir fora da máquina de quem testa, e já estão
listados em `realtime_client.py:1-38`.
