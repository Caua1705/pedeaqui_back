# Experimento de voz — como rodar

**Branch `experimento/voz`. Não está na `main` e não deve ir para o servidor.**

## 1. Ligue o flag

No `.env` da sua máquina:

```
VOICE_ENABLED=true
```

Desligado (o padrão), as três rotas não existem — nem no `/docs`. Ligado, a API
avisa no boot:

```
[Voz] LIGADO. /voice esta aberto, sem login e sem cota...
```

## 2. Suba a API

```bash
uvicorn main:app --reload
```

Precisa de `OPENAI_API_KEY` e `DATABASE_URL` no `.env`, os mesmos que o chat de
texto já usa.

## 3. Pegue um `restaurant_id` que tenha cardápio indexado

```sql
SELECT r.id, r.name, count(ape.id) AS produtos_indexados
FROM restaurants r
JOIN ai_product_embeddings ape ON ape.restaurant_id = r.id
WHERE r.is_active IS TRUE
GROUP BY r.id, r.name
ORDER BY produtos_indexados DESC;
```

Restaurante sem embedding não devolve nada, e o experimento parece quebrado
quando na verdade é o índice que está vazio.

## 4. Abra a página

```
http://localhost:8000/voice
```

Cole o `restaurant_id`, clique em **Falar**, dê permissão ao microfone e
pergunte algo como *"o que tem de sobremesa?"*.

`localhost` é contexto seguro, então o microfone funciona sem HTTPS. De outra
máquina na rede **não** funciona: o navegador recusa o microfone em `http://`
que não seja localhost.

## 5. O que olhar

- O painel preto embaixo é o log cru de eventos da Realtime. É a parte mais
  útil da página — se algo não funcionar, a resposta está ali.
- Os cartões vêm de `POST /voice/search`, com preço do banco. O que
  o modelo **fala** e o que o cartão **mostra** são leituras diferentes; se
  divergirem, é isso que o experimento tinha que descobrir.

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
