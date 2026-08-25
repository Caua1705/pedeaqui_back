# Atendimento por voz — como ligar

**São DUAS chaves, e as duas precisam estar ligadas.** Uma no ambiente, para a
plataforma inteira; outra no banco, restaurante por restaurante. Isso é
proposital: a chave global sozinha acenderia a voz para toda a base no mesmo
instante, e cada sessão custa dinheiro.

## O atalho: `bancada.bat`

Os passos 3 e 5 — subir a API e servir a página — cabem num duplo clique no
`bancada.bat` da raiz. Ele abre duas janelas (a API e o `http.server`) e o
navegador já na bancada. É `cmd` puro, sem PowerShell, então não depende de
`ExecutionPolicy`.

O que ele **não** faz são as duas chaves (passos 1 e 2) nem escolher o
restaurante (passo 4): configuração e dados continuam na mão. O resto deste
arquivo é o passo a passo, e o que explica o que o atalho está fazendo por você.

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

A bancada mora em **`tools/bancada/`**, versionada junto do backend desde
25/08/2026. Antes disso ela vivia numa pasta solta ao lado do projeto, e o
motivo de ter vindo para cá é o que aconteceu enquanto estava fora: ninguém
lembra de atualizar o que não aparece no `git status`. A página conhece o
formato de `POST /voice/search` e os seis contadores do `/ended` — quando uma
dessas coisas muda, a bancada tem que mudar no mesmo commit, e agora dá.

Ela continua se servindo sozinha:

```bash
cd tools/bancada
python -m http.server 5500
```

E abra <http://localhost:5500/bancada.html>. As portas 5500, 5501 e 5173 já
estão liberadas no CORS do `main.py` — não é preciso mexer em nada aqui.

**Estar no repositório não é estar na imagem.** O `.dockerignore` deixa
`tools/bancada/` e o `bancada.bat` de fora do `COPY . .`, pelo mesmo motivo do
`print-agent/`: é código que roda na máquina de quem desenvolve, e um HTML de
teste com campo de token não tem o que fazer viajando para produção.

`GET /voice/test` **não existe mais** (removida em 24/08/2026). Ela servia uma
cópia interna que foi apagada em `bd1f164` e ficou respondendo 500. Se você
veio de um link antigo, é `tools/bancada/bancada.html` que você quer — e note
que **a API não serve a bancada nem hoje**: quem serve é o `http.server` acima.

Cole o `restaurant_id`, a filial e o **token do cliente** (a emissão exige
login; `POST /auth/login` devolve o `access_token`), clique em **Falar**, dê
permissão ao microfone e pergunte algo como *"o que tem de sobremesa?"*.

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

Emitir a credencial (`POST /voice/session`) não custa nada — o relógio começa
quando o navegador abre a sessão de áudio. **Aba esquecida não fatura mais
para sempre:** a sessão encerra sozinha no teto de duração, na inatividade e
quando a aba sai de vista. Ver `tools/bancada/` e `session_service.py`.

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
| login de cliente | `src/api/voice.py`, `get_current_customer` |
| rate limit por IP (3/min, 20/h) | `src/api/voice.py`, `VOICE_SESSION_RATE_LIMIT` |
| cota por cliente e por restaurante | `session_service.py` |
| teto de duração, inatividade, aba escondida | a bancada (cliente) |
| desligamento pelo servidor | `realtime_client.hangup_call` |

A última só funciona quando o navegador reporta o `call_id` — ver o cabeçalho
de `session_service.py` para o que isso alcança e o que não alcança.
