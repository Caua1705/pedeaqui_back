# Custo de IA por restaurante

## A pergunta

**A comissão paga a conta do assistente?** Até esta frente ninguém sabia: a
fatura da OpenAI chega com um número só para a plataforma inteira, e não havia
como saber de quem foi o gasto.

O que existia respondia meia pergunta cada:

| Onde | O que dava | O que faltava |
|---|---|---|
| `ai_voice_sessions` + `scripts/voice_usage_report.py` | tokens da **voz**, por restaurante | tokens, nunca dinheiro — quatro contadores com preços diferentes |
| linha de log `[AI /chat usage]` | tokens do **texto**, por turno | morria no `docker logs`, que gira |

Agora há uma linha por chamada, com o custo já calculado, em
`ai_usage_events` (revisão `20260902_0044`).

---

## 1. Como eu leio

```bash
curl -s -H "X-Internal-Key: $PLATFORM_METRICS_KEY" \
  "https://api.pederapidex.com/internal/ai-usage?start_date=2026-08-01&end_date=2026-08-31" \
  | python -m json.tool
```

Sem parâmetro nenhum, ela devolve os **últimos 30 dias** — que é o ciclo da
fatura da OpenAI, com quem este número é conferido.

| Parâmetro | Default |
|---|---|
| `start_date` | 29 dias antes de `end_date` |
| `end_date` | hoje, **inteiro** (o dia todo entra) |
| `restaurant_id` | todos, um bloco por restaurante |

A resposta, por restaurante:

```json
{
  "start": "2026-08-01T00:00:00-03:00",
  "end":   "2026-09-01T00:00:00-03:00",
  "restaurants": [
    {
      "restaurant_id": "…", "restaurant_name": "Júnior da Picanha",
      "calls": 412, "text_calls": 380, "voice_calls": 32,
      "cost_usd": "1.284300",
      "text_cost_usd": "0.194300", "voice_cost_usd": "1.090000",
      "input_tokens": 812340, "output_tokens": 91220,
      "calls_without_price": 0
    }
  ],
  "total_cost_usd": "1.284300"
}
```

**Olhe `calls_without_price` antes do total.** Ela conta as chamadas cujo
modelo não estava em `src/ai/custo.py` no dia: elas somam token e não somam
dólar. Número grande ali significa "falta preço na tabela", e **não** "custou
pouco".

**Em dólar, não em real.** O preço é publicado em dólar e a fatura vem em
dólar; converter exigiria uma cotação que não temos e cuja escolha (do dia? do
fechamento? do pagamento do cartão?) é decisão de contabilidade.

**O dono da verdade continua sendo a fatura.** Isto é o rateio dela.

---

## 2. Por que a rota não está no painel

Ela é `/internal/ai-usage`, com chave própria e `include_in_schema=False`.

Quanto o assistente custa **à plataforma** é a nossa margem. Publicar isso no
painel a poria na mesa de negociação da comissão — com a agravante de o lojista
ver o número antes de sentar. É o raciocínio da armadilha 17, que mantém
`platform_commission_percent` fora de todo schema do painel; a diferença é que
lá o risco é o lojista **editar** quanto paga, e aqui é ele **saber** a margem.

Por isso também não entra no `/openapi.json`: o painel consome o documento
(armadilha 16), e rota que não é do painel não entra no contrato dele.

`PLATFORM_METRICS_KEY` é **segredo novo para público novo** (armadilha 32) — não
reaproveita nenhum outro segredo do ambiente. É **opcional**: sem ela, só esta
rota responde 503.

A comparação é `hmac.compare_digest` e não `!=` (armadilha 18).

---

## 3. O que é gravado, e quando

| Superfície | Uma linha por | Onde o gancho está |
|---|---|---|
| `text` | **turno** do `/chat` | `ChatService._invoke_llm` |
| `voice` | **sessão** de voz | `VoiceSessionService.encerrar` |

**A voz é por sessão e não por turno** porque não existe turno visível daqui: o
áudio vai do navegador direto para a OpenAI e o backend não vê os eventos da
conversa. O número chega uma vez só, no `POST /voice/session/{id}/ended`.

**E esse aviso chega duas vezes.** Ele vai com `keepalive` e o navegador o
reenvia ao fechar a aba. Por isso a linha de voz carrega `voice_session_id` com
**UNIQUE parcial**: a segunda chegada corrige a primeira em vez de dobrar o
custo da conversa. O CHECK
`(surface = 'voice') = (voice_session_id IS NOT NULL)` impede que nasça linha de
voz sem essa chave — que seria uma duplicata esperando acontecer.

### Medir não pode derrubar o que está sendo medido

As duas gravações engolem qualquer falha e seguem. O cliente já tem a resposta
dele quando elas rodam; um `/chat` que responde 500 porque a contabilidade não
gravou seria trocar a operação pela planilha.

O preço é conhecido: chamada perdida é chamada que não aparece no relatório.
Por isso a falha sai no log com nome próprio — `custo_nao_gravado=true` — e o
buraco fica visível em vez de virar um número menor sem explicação.

### Quem commita

Não é a mesma resposta nas duas, e a diferença é o que evita gravar custo de
sessão que não foi encerrada:

- **texto**: `registrar_texto` commita. No `/chat` esta é a única escrita do
  turno; deixar a transação para o `get_db` fechar sem commit descartaria a
  linha;
- **voz**: `registrar_voz` **não** commita. Ela pega carona na transação de
  `encerrar`, que já existe.

---

## 4. Os preços são uma cópia, e ela envelhece

A tabela está em `src/ai/custo.py`, conferida em **02/09/2026** contra
`developers.openai.com/api/docs/pricing`. USD por milhão de tokens:

| Modelo | Entrada | Entrada em cache | Saída |
|---|---|---|---|
| `gpt-5` | 1,25 | 0,125 | 10,00 |
| `gpt-5-mini` | 0,25 | 0,025 | 2,00 |
| `gpt-5-nano` | 0,05 | 0,005 | 0,40 |

| Modelo de voz | Texto in | Texto in cache | Áudio in | Áudio in cache | Texto out | Áudio out |
|---|---|---|---|---|---|---|
| `gpt-realtime` | 4,00 | 0,40 | 32,00 | 0,40 | 16,00 | 64,00 |
| `gpt-realtime-mini` | 0,60 | 0,06 | 10,00 | 0,30 | 2,40 | 20,00 |

**Modelo fora da tabela custa `NULL`, nunca zero.** Zero é um número que soma, e
um restaurante inteiro apareceria de graça no relatório que existe para dizer
quanto ele custa. Os tokens ficam gravados do mesmo jeito: quando a tabela for
atualizada, dá para reprocessar.

É o critério da armadilha 49 — **número desatualizado degrada a mensagem, não a
correção**.

**`cached` é SUBCONJUNTO da entrada**, nos dois lados. `input_tokens` já inclui
o que veio do cache; somar os dois conta o cache duas vezes. As duas funções
subtraem antes de multiplicar.

### A aproximação que existe, e é uma só

A Realtime manda **um** `cached_tokens`, sem dizer se o que veio do cache era
áudio ou texto. `custo_de_voz` desconta do **áudio primeiro** e o que sobrar do
texto — numa conversa falada o áudio é a quase totalidade da entrada, e o
áudio em cache é o mais barato dos dois. Ou seja, o número é um **piso** do
custo, nunca um teto. Resolver isso de verdade pede o navegador reportar o
cache separado por faixa, que é mudança de contrato com o front.

---

## 5. O que NÃO é medido, de propósito

**Embedding.** Toda busca no cardápio paga um, e ele não entra em
`ai_usage_events`. O motivo é o tamanho: `text-embedding-3-small` custa
**US$ 0,02 por milhão** de tokens, e uma pergunta de cliente tem umas duas
dezenas de tokens — **um milhão de buscas custa quarenta centavos de dólar**,
contra ~US$ 0,0005 por turno do `/chat`. Instrumentar isso exigiria contar token
com `tiktoken` no caminho quente da busca, para medir um arredondamento.

Se um dia o modelo de embedding mudar de faixa de preço, a conta muda e este
parágrafo também.

**Transcrição da voz.** O backend não a liga (armadilha 43) — quem liga é a
bancada, por `session.update`, e ela é cobrada à parte por minuto. Medição de
custo limpa se faz com ela desligada.

---

## 6. O que esta frente NÃO construiu

Painel, alerta e limite. **Só medir**, de propósito: com o número na mão dá para
decidir o resto, e cada um deles é uma decisão de produto diferente — a que
número alertar, quem recebe, e o que acontece quando o teto é batido (o
assistente cala? responde sem busca? o lojista paga o excedente?).

O que já existe e não foi tocado: a **cota** de voz por cliente e por
restaurante (`VOICE_QUOTA_*`), que é controle de emissão e não de custo.

---

## 7. A terceira superfície: indexação do cardápio

> **Estado em 05/09/2026: revisão escrita e NÃO aplicada.** O arquivo é
> `alembic/preparadas/20260905_0057_indexacao_do_cardapio_no_custo_de_ia.py`,
> e o Alembic não o lê (armadilha 53). Nada abaixo está no ar.

### 7.1 Por que a indexação entra, se a busca não entrou

A seção 5 diz que embedding não é medido, e o argumento é o tamanho:
`text-embedding-3-small` custa **US$ 0,02 por milhão** de tokens. Ele continua
verdadeiro para a **busca** — um embedding por pergunta de cliente, no caminho
quente — e é falso para a **indexação**, por um motivo que não é o preço por
token:

**mexer na formatação de `build_product_content` invalida todo `content_hash`
gravado e reindexa o cardápio inteiro.** Está escrito lá, com todas as letras,
como consequência esperada. No Júnior da Picanha são 161 produtos de uma vez, e
hoje isso não aparece em conta nenhuma: não há onde ver que uma linha de código
comprou 161 embeddings.

Essa é a diferença que interessa a "a comissão de 4% paga a conta?": o custo da
conversa varia com o movimento, e o da indexação varia com **o que a gente
faz**. Somados num número só, o segundo é invisível dentro do primeiro.

### 7.2 O que a revisão faz

Uma CHECK, um valor a mais: `surface` passa a aceitar `indexing`.

Nenhuma coluna nova. `restaurant_id`, `branch_id`, `model`, `input_tokens`,
`output_tokens` e `cost_usd` já são exatamente o que uma chamada de embedding
grava — sem saída (o embedding é um vetor, não tokens cobrados) e sem cache.

A outra CHECK (`(surface = 'voice') = (voice_session_id IS NOT NULL)`) e o
índice de leitura `(restaurant_id, created_at)` **não mudam**, e o cabeçalho da
revisão explica por quê em cada caso.

### 7.3 A ordem NÃO é livre, e o portão a torna verificável

`AI_SURFACES` (`src/models/ai_usage_event_model.py`) é o espelho declarado desta
CHECK em `scripts/espelhos_de_enum.py` (armadilha 15), e ele compara valor a
valor:

| Ordem | O que acontece |
|---|---|
| código antes do schema | valor só no **código**: o INSERT morre na CHECK, e o portão fica vermelho até a revisão entrar |
| schema antes do código | valor só no **banco**: o portão fica vermelho até o código entrar |
| **os dois no mesmo commit** | verde |

É por isso que a revisão está parada sozinha, sem o código do lado: enquanto ela
está em `preparadas/`, o banco de teste (baseline + `upgrade head`) não a tem, e
um `AI_SURFACES` com três valores deixaria o portão vermelho em **todo commit**
até a noite da aplicação. Portão vermelho por semanas é portão que se aprende a
ignorar — a mesma razão pela qual `divergencias_orm_schema.py` é aviso e não
portão.

### 7.4 O plano — o que vai junto com a revisão

Um commit só, nesta ordem de escrita (a ordem de *execução* é a do
`upgrade`):

**1. `git mv` da revisão para `alembic/versions/`**, acertando `down_revision`
para o head do dia (o valor lá é marcador).

**2. `src/models/ai_usage_event_model.py`** — `SURFACE_INDEXING = "indexing"`,
acrescentado a `AI_SURFACES`. É o que fecha o portão do espelho.

**3. `src/ai/custo.py`** — a terceira tabela de preços e a terceira função.
Conferir os valores em `developers.openai.com/api/docs/pricing` **no dia**, e
carimbar a data no comentário como as outras duas:

```python
PRECOS_DE_EMBEDDING: dict[str, Decimal] = {   # USD por milhão de tokens
    "text-embedding-3-small": Decimal("0.02"),
    "text-embedding-3-large": Decimal("0.13"),
}

def custo_de_embedding(modelo: str, entrada: int) -> Decimal | None: ...
```

Uma função de um argumento e não uma terceira variação de `custo_de_texto`:
embedding não tem saída nem cache, e passar dois zeros em toda chamada
convidaria a pergunta "este campo vale aqui?" que a seção "por que duas funções"
já responde.

**Modelo fora da tabela custa `None`, nunca zero** — o mesmo critério das outras
duas, e ele importa mais aqui: `EMBEDDING_MODEL` sai do `.env`.

**4. De onde vem a contagem de tokens.** A resposta é **`usage.prompt_tokens` da
própria OpenAI**, e não `tiktoken`:

- é o número que ela cobra, e não a nossa estimativa dele;
- não depende do cache de BPE do `tiktoken` (que baixa na primeira chamada);
- em teste, dubla-se o transporte e a conta continua sendo a de verdade
  (armadilha 42).

O `OpenAIEmbeddings` do LangChain **descarta** o `usage`, então a indexação
passa a chamar `client.embeddings.create` direto. É aceitável **só porque a
indexação não é o caminho quente**: a busca continua no LangChain, com o cliente
compartilhado cuja medição está no cabeçalho de `embedding_service.py` (654 ms →
340 ms). Trocar os dois de uma vez arriscaria aquele número por nada.

Consequência a escrever no código: a chamada crua **não fatia texto longo**, e o
`OpenAIEmbeddings` fatia. Conteúdo de produto tem dezenas de tokens contra um
teto de 8191, então não há caso hoje — e é exatamente o tipo de diferença que
some do radar se ninguém a anotar.

**5. `src/ai/services/product_indexing.py`** — `index_product` ganha
`usage_service: AIUsageService | None = None`. Grava **só quando gerou
embedding**: o desfecho `touched` não paga nada, e uma linha de custo zero ali
inventaria uma chamada que não houve (o mesmo critério de `registrar_voz` com
sessão sem número).

Parâmetro opcional e não obrigatório porque os dois testes que já chamam
`index_product` com dublê de `EmbeddingService` continuam valendo sem mudança.

**6. `AIUsageService.registrar_indexacao`** — **não commita**, como
`registrar_voz`: os dois varredores já são "um produto, uma transação" e o
commit deles é logo abaixo da chamada. Engole falha e loga
`custo_nao_gravado=true`, como as outras duas.

**7. Os dois varredores** — `scripts/reindex_ai.py` e
`scripts/reindex_worker.py` passam o service.

**8. A leitura** — `AIUsageRepository.custo_por_restaurante` ganha
`indexing_calls` e `indexing_cost_usd` ao lado dos de texto e voz, e
`AIUsageByRestaurant` os publica.

### 7.5 Como conferir depois de aplicar

```bash
docker exec pedeaqui-api alembic current          # tem que citar a 0057
docker exec pedeaqui-api python scripts/espelhos_de_enum.py   # verde
```

E provocar uma indexação de verdade — editar a descrição de um produto no painel
— e ver a linha aparecer:

```bash
curl -s -H "X-Internal-Key: $PLATFORM_METRICS_KEY" \
  "https://api.pederapidex.com/internal/ai-usage?restaurant_id=<id>" | python -m json.tool
```

`indexing_calls` maior que zero e `calls_without_price` igual a zero. O segundo é
o que denuncia `EMBEDDING_MODEL` fora da tabela de preços.

### 7.6 Rollback

`alembic downgrade -1`. Ele **apaga as linhas de indexação**, e é deliberado:
`ADD CONSTRAINT ... CHECK` valida a tabela inteira, então recriar a CHECK antiga
com uma linha `indexing` gravada falharia no meio e deixaria a tabela sem CHECK
nenhuma. O que se perde é medição — nenhuma linha desta tabela é pedido,
comissão ou saldo de cliente.

O código tem que voltar junto, pelo mesmo motivo da seção 7.3.
