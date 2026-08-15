# Atendimento por voz — como ligar

**São DUAS chaves, e as duas precisam estar ligadas.** Uma no ambiente, para a
plataforma inteira; outra no banco, restaurante por restaurante. Isso é
proposital: a chave global sozinha acenderia a voz para toda a base no mesmo
instante, e cada sessão custa dinheiro.

## 1. A chave mestra, no `.env`

```
VOICE_ENABLED=true
```

Desligada (o padrão), as rotas `/voice` **não existem** — nem no `/docs` — por
mais que o banco diga que algum restaurante tem voz.

## 2. O interruptor do restaurante, no banco

```sql
UPDATE restaurant_settings SET voice_enabled = true
 WHERE restaurant_id = '<uuid do restaurante>';
```

Nasce `false` em todas as linhas. Restaurante sem voz — ou sem linha em
`restaurant_settings` — recebe **403** na emissão:

```json
{"detail": "O atendimento por voz nao esta disponivel neste restaurante."}
```

**Não há campo no painel do lojista, de propósito.** Mesmo raciocínio de
`platform_commission_percent`: quem paga a conta da OpenAI é a plataforma, então
ligar a voz é decisão da plataforma. Um campo de tela aqui seria o lojista
escolhendo quanto gastamos.

## 3. Suba a API

```bash
uvicorn main:app --reload
```

Precisa de `OPENAI_API_KEY` e `DATABASE_URL` no `.env`, os mesmos que o chat de
texto já usa.

## 4. Pegue um `restaurant_id` que tenha cardápio indexado

```sql
SELECT r.id, r.name, count(ape.id) AS produtos_indexados
FROM restaurants r
JOIN ai_product_embeddings ape ON ape.restaurant_id = r.id
WHERE r.is_active IS TRUE
GROUP BY r.id, r.name
ORDER BY produtos_indexados DESC;
```

Restaurante sem embedding não devolve nada, e a voz parece quebrada quando na
verdade é o índice que está vazio.

## 5. Abra a bancada

```
http://localhost:8000/voice/test
```

Cole o `restaurant_id` e o **token do cliente** (a emissão exige login;
`POST /auth/login` devolve o `access_token`), clique em **Falar**, dê permissão ao microfone e
pergunte algo como *"o que tem de sobremesa?"*.

`localhost` é contexto seguro, então o microfone funciona sem HTTPS. De outra
máquina na rede **não** funciona: o navegador recusa o microfone em `http://`
que não seja localhost.

## 6. O que olhar

- O painel preto embaixo é o log cru de eventos da Realtime. É a parte mais
  útil da página — se algo não funcionar, a resposta está ali.
- Os cartões vêm de `POST /voice/search`, com preço do banco. O que
  o modelo **fala** e o que o cartão **mostra** são leituras diferentes; se
  divergirem, é o log que denuncia (`grep "preco divergente"`).

## Custo

`gpt-realtime-mini`, taxas de 15/08/2026: entrada de áudio **1 token por
100 ms** (600 tok/min) a **US$ 10 / 1M**; saída **1 token por 50 ms**
(1200 tok/min) a **US$ 20 / 1M**; entrada em cache a **US$ 0,30 / 1M**.

Isso dá **US$ 0,006 por minuto ouvido** e **US$ 0,024 por minuto falado**.

Uma sessão que vai até o teto de 5 minutos custa **US$ 0,09 a US$ 0,13**,
dependendo de quanto o atendente fala. A conta está em `src/core/config.py`,
junto das cotas.

Emitir a credencial (`POST /sessao`) não custa nada — o relógio começa quando
o navegador abre a sessão de áudio. **Aba esquecida não fatura mais para
sempre:** a sessão encerra sozinha no teto de duração, na inatividade e quando
a aba sai de vista. Ver `pagina.html` e `sessao_control_service.py`.

### Quanto foi de verdade, por restaurante

```
python scripts/voice_usage_report.py --desde 2026-08-01
```

Sessões, tokens e duração média no período. Os números saem de
`ai_voice_sessions`, e quem os coloca lá é o NAVEGADOR, no `/ended` — o
backend não vê a conversa e o consumo só aparece no evento `response.done`
da Realtime (§4.3.1 de `docs/contrato-voz-frontend.md`).

Duas leituras que o relatório pede: **sessão que não reportou aparece com zero
token** — a contagem de sessões é do servidor e está sempre certa, os tokens
dependem do cliente ter avisado, e a diferença entre as duas contagens é a
margem de erro do período. E **quem cobra é a fatura da OpenAI**; isto aqui é
o rateio dela.

## O que barra o abuso

| Barreira | Onde |
|---|---|
| login de cliente | `rota.py`, `get_current_customer` |
| rate limit por IP (3/min, 20/h) | `rota.py`, `VOICE_SESSION_RATE_LIMIT` |
| cota por cliente e por restaurante | `sessao_control_service.py` |
| teto de duração, inatividade, aba escondida | `pagina.html` |
| desligamento pelo servidor | `sessao_service.hangup_call` |

A última só funciona quando o navegador reporta o `call_id` — ver o cabeçalho
de `sessao_control_service.py` para o que isso alcança e o que não alcança.
