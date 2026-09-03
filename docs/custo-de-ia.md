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
reaproveita a `INTERNAL_API_KEY`, que está depreciada e que o `startup_checks`
pede para remover do `.env`. É **opcional**: sem ela, só esta rota responde 503.

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
