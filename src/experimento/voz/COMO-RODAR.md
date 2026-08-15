# Experimento de voz — como rodar

**Branch `experimento/voz`. Não está na `main` e não deve ir para o servidor.**

## 1. Ligue o flag

No `.env` da sua máquina:

```
EXPERIMENTO_VOZ_ENABLED=true
```

Desligado (o padrão), as três rotas não existem — nem no `/docs`. Ligado, a API
avisa no boot:

```
[Experimento voz] LIGADO. /experimento/voz esta aberto, sem login e sem cota...
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
http://localhost:8000/experimento/voz
```

Cole o `restaurant_id`, clique em **Falar**, dê permissão ao microfone e
pergunte algo como *"o que tem de sobremesa?"*.

`localhost` é contexto seguro, então o microfone funciona sem HTTPS. De outra
máquina na rede **não** funciona: o navegador recusa o microfone em `http://`
que não seja localhost.

## 5. O que olhar

- O painel preto embaixo é o log cru de eventos da Realtime. É a parte mais
  útil da página — se algo não funcionar, a resposta está ali.
- Os cartões vêm de `POST /experimento/voz/buscar`, com preço do banco. O que
  o modelo **fala** e o que o cartão **mostra** são leituras diferentes; se
  divergirem, é isso que o experimento tinha que descobrir.

## Custo

`gpt-realtime-mini`: **US$ 10 / 1M tokens de áudio de entrada** e
**US$ 20 / 1M de saída**. Na prática, algo entre **US$ 0,03 e US$ 0,10 por
minuto** de conversa, dependendo de quanto cada lado fala.

Emitir a credencial (`POST /sessao`) não custa nada — o relógio começa quando
o navegador abre a sessão de áudio. **Uma aba esquecida aberta continua
faturando.** Não existe teto de duração neste experimento.
