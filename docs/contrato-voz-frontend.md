# Contrato das rotas `/voice` — atendimento por voz

Documento para quem vai implementar o front. Tudo o que é preciso saber está
aqui; não é necessário acesso ao código do backend.

**Estado em 15/08/2026.** Base da API: a mesma do resto do app do cliente
(`https://api.pederapidex.com` em produção, `http://localhost:8000` em
desenvolvimento).

---

## 1. Como a coisa funciona, em um parágrafo

O áudio **não passa pelo nosso backend**. O navegador abre uma conexão WebRTC
direto com a OpenAI e conversa com ela. O backend faz três coisas: emite uma
credencial de curta duração para o navegador poder abrir essa conexão, responde
à busca no cardápio quando o modelo pede, e mantém um registro da sessão para
cobrança e corte. O modelo de voz é da OpenAI; os produtos e os preços são
nossos.

---

## 2. Pré-condições

Três coisas precisam ser verdade, ou a emissão falha:

| Condição | Como o front descobre que falhou |
|---|---|
| A voz está ligada na plataforma | Todas as rotas `/voice` respondem **404** |
| A voz está ligada **naquele restaurante** | `POST /voice/session` responde **403** |
| Existe um cliente logado | `POST /voice/session` responde **401** |

A habilitação é por restaurante e é decisão do backend — não existe rota para
consultá-la nem para ligá-la. **O front deve tratar 403 como "este restaurante
não tem voz" e simplesmente não mostrar o botão / esconder a funcionalidade.**

Não há endpoint de "a voz está disponível aqui?". Se você precisar decidir se
mostra o botão antes de o usuário clicar, a única forma hoje é tentar emitir e
tratar o 403 — o que gasta uma emissão. Vale pedir esse endpoint ao backend se
a UI precisar dele.

---

## 3. O fluxo completo, em ordem

```
1. POST  /voice/session                       (nosso backend, com Bearer)
      → guarda: sessao_id, credencial.value, limites, saudacao

2. getUserMedia({audio:true})                 (navegador)

3. POST  https://api.openai.com/v1/realtime/calls   (OpenAI, direto)
      Authorization: Bearer <credencial.value>
      Content-Type: application/sdp
      corpo: o SDP offer
      → lê o cabeçalho Location  →  call_id

4. POST  /voice/session/{sessao_id}/connected (nosso backend)
      corpo: { "call_id": "<o do passo 3>" }

5. setRemoteDescription(answer)               (navegador)
      → a conversa começa; liga os temporizadores do passo 8

5b. no `onopen` do data channel:                (OpenAI, pelo canal)
      response.create com { response: { instructions: "...saudacao..." } }
      → o atendente cumprimenta o cliente pelo nome, sozinho (§6.4)

6. durante a conversa, a cada tool call:
   POST  /voice/search                        (nosso backend)
      → desenha os cartões com `produtos`
      → devolve `resumo` à OpenAI pelo data channel

7. ao encerrar (por qualquer motivo):
      pára as faixas do microfone, fecha a conexão
   POST  /voice/session/{sessao_id}/ended     (nosso backend)
      corpo: { "motivo": "...", + o consumo acumulado (§4.3.1) }
```

### O que guardar entre uma chamada e outra

| O quê | De onde vem | Para que serve |
|---|---|---|
| `sessao_id` | resposta do passo 1 | montar as URLs dos passos 4 e 7 |
| `credencial.value` | resposta do passo 1 | o `Bearer` do passo 3, **e só dele** |
| `limites` | resposta do passo 1 | os temporizadores do passo 8 |
| `saudacao` | resposta do passo 1 | o texto do passo 5b |
| `call_id` | cabeçalho `Location` do passo 3 | o corpo do passo 4 |
| `restaurant_id` | o contexto da tela | corpo dos passos 1 e 6 |
| `branch_id` | a filial que o cliente escolheu | corpo dos passos 1 e 6, **obrigatório nos dois** |
| o token do cliente | o login do app | header dos passos 1 (e só dele) |

**`credencial.value` nunca vai para o nosso backend, e o token do cliente nunca
vai para a OpenAI.** São dois segredos com destinos diferentes.

### O passo 8: encerrar a sessão

Isto não é opcional — **enquanto a conexão estiver aberta, ela está sendo
faturada por minuto, mesmo em silêncio.** O front precisa encerrar em quatro
situações:

1. **Teto de duração:** `limites.duracao_maxima_s` após a conexão abrir,
   aconteça o que acontecer.
2. **Inatividade:** `limites.inatividade_s` sem o evento
   `input_audio_buffer.speech_started`. Avise por fala
   `limites.aviso_antes_s` antes de cortar (ver §7.3). Se o cliente falar no
   intervalo, zere o contador e cancele o aviso.
3. **Aba escondida ou janela sem foco:** `visibilitychange` com
   `document.hidden`, e `blur` da janela.
4. **O usuário clicou em parar.**

Em todos os casos, na ordem: pare os timers → feche o data channel → feche a
`RTCPeerConnection` → **`microfone.getTracks().forEach(t => t.stop())`** →
avise o backend (`/ended`).

O `stop()` das faixas não pode faltar: sem ele o indicador de gravação do
navegador continua aceso depois de a sessão acabar, e o microfone continua
captando.

---

## 4. As rotas, uma a uma

### 4.1 `POST /voice/session` — emite a credencial

**Exige `Authorization: Bearer <access_token do cliente>`.** É a única rota
`/voice` que exige.

Rate limit: **3 por minuto e 20 por hora**, por IP.

**Requisição**

```json
{ "restaurant_id": "0f9c8d2e-...", "branch_id": "6a1f...-..." }
```

| Campo | Tipo | Obrigatório |
|---|---|---|
| `restaurant_id` | string (UUID) | sim |
| `branch_id` | string (UUID) | **sim, desde 20/08/2026** |

**`branch_id` não tem queda para a filial padrão, e isso é decisão.** O
cardápio é por loja desde a revisão `20260820_0026`; um default faria o modelo
oferecer, **em áudio e com preço**, um produto que aquela loja não vende — e
não há tela onde o cliente pudesse notar. Ver
[`cardapio-por-filial.md`](cardapio-por-filial.md).

**Resposta 200**

```json
{
  "sessao_id": "8e1b...-...",
  "credencial": {
    "value": "ek_68a...",
    "expires_at": 1786805373,
    "session": { "type": "realtime", "model": "gpt-realtime-mini", "...": "..." }
  },
  "saudacao": "Olá, João! Como posso te ajudar hoje?",
  "limites": {
    "duracao_maxima_s": 300,
    "inatividade_s": 45,
    "aviso_antes_s": 10
  }
}
```

| Campo | Tipo | O que é |
|---|---|---|
| `sessao_id` | string (UUID) | **nosso** id da sessão. Usado nas rotas `/connected` e `/ended`. Não tem relação com o `call_id` da OpenAI |
| `credencial` | objeto | devolvido pela OpenAI, repassado inteiro |
| `credencial.value` | string | o segredo efêmero. Começa com `ek_`. É o `Bearer` da chamada à OpenAI |
| `credencial.expires_at` | número (unix epoch, segundos) | quando o **segredo** expira — medido em ~585 s. É a janela para **abrir** a conexão, **não** o teto da conversa |
| `credencial.session` | objeto | a configuração da sessão (modelo, voz, ferramentas, instruções). Informativo; o front não precisa usar |
| `saudacao` | string | a frase que o atendente fala **sozinho** na abertura, com o primeiro nome do cliente já dentro. Ver §6.4 |
| `limites` | objeto | os tetos que o front tem de aplicar |
| `limites.duracao_maxima_s` | número (segundos) | teto absoluto da sessão |
| `limites.inatividade_s` | número (segundos) | silêncio que encerra |
| `limites.aviso_antes_s` | número (segundos) | quanto antes do corte avisar por fala |

Os três valores de `limites` vêm do servidor a cada emissão. **Não os fixe no
front** — eles podem mudar sem deploy do app.

**Erros**

| Código | Corpo | Significa |
|---|---|---|
| 401 | `{"detail":"Token ausente"}` | não veio `Authorization: Bearer` |
| 401 | `{"detail":"Token expirado"}` | o token do cliente venceu — refaça o login |
| 401 | `{"detail":"Token invalido"}` | token malformado ou de outro público |
| 403 | `{"detail":"Conta inativa"}` | o cliente existe mas está desativado |
| 403 | `{"detail":"O atendimento por voz nao esta disponivel neste restaurante."}` | a voz não está ligada nessa loja |
| 404 | `{"detail":"Restaurante não encontrado"}` | `restaurant_id` inexistente ou restaurante inativo |
| 404 | `{"detail":"Filial não encontrada para este restaurante"}` | `branch_id` de outra loja ou de outro restaurante. Acontece **antes** de a credencial ser emitida: sessão de voz custa por minuto |
| 422 | corpo padrão de validação do FastAPI | `restaurant_id` ou `branch_id` ausente, ou não é um UUID |
| 429 | `{"detail":"Muitas requisições. Tente novamente em instantes."}` | **rate limit** — mais de 3/min ou 20/h vindos do mesmo IP |
| 429 | `{"detail":"Voce ja usou 5 conversas por voz nas ultimas 24 horas. Tente mais tarde."}` | **cota do cliente** — o número aparece no texto |
| 429 | `{"detail":"O atendimento por voz deste restaurante atingiu o limite do dia."}` | **cota do restaurante** |
| 502 | `{"detail":"A OpenAI recusou a emissao da credencial"}` | a OpenAI respondeu erro |
| 503 | `{"detail":"Nao consegui falar com a OpenAI"}` | rede/timeout falando com a OpenAI |

**`saudacao` nunca vem vazia.** Quando o cadastro não entrega um primeiro
nome que dê para falar — o campo é texto livre, e há `"12345"` e e-mail
inteiro dentro dele — vem uma variação sem nome, e não a frase com um buraco
onde o nome estaria. O front não decide isso e não monta essa frase.

**Os três 429 têm o mesmo código e corpos diferentes.** Se a UI precisar
distinguir, é pelo texto de `detail` — não há campo de código de erro. Vale
pedir um ao backend se isso for importante para a tela.

O 429 **não traz `Retry-After`** de forma confiável. Não construa contagem
regressiva em cima dele.

---

### 4.2 `POST /voice/session/{sessao_id}/connected` — reporta o `call_id`

**Não exige autenticação.**

`{sessao_id}` é o `sessao_id` do passo 1.

**Requisição**

```json
{ "call_id": "rtc_abc123..." }
```

| Campo | Tipo | Obrigatório | Restrição |
|---|---|---|---|
| `call_id` | string | sim | 1 a 200 caracteres |

**Resposta 200**

```json
{ "registrado": true }
```

| Campo | Tipo | O que é |
|---|---|---|
| `registrado` | boolean | `true` se a sessão foi encontrada e o `call_id` gravado; `false` se não existe sessão com esse id |

**Atenção: `false` vem com HTTP 200, não com 404.** Se você precisa saber que
deu errado, teste o campo, não o status.

**Erros**

| Código | Corpo | Significa |
|---|---|---|
| 422 | corpo padrão de validação | `sessao_id` não é UUID, ou `call_id` vazio/maior que 200 |

Esta chamada é **best-effort do ponto de vista da conversa**: se ela falhar, a
sessão de áudio continua funcionando normalmente. O que se perde é a
capacidade de o servidor desligar aquela sessão remotamente. Não aborte o fluxo
por causa dela; registre e siga.

---

### 4.3 `POST /voice/session/{sessao_id}/ended` — reporta o fim

**Não exige autenticação.**

**Requisição**

```json
{
  "motivo": "teto de 300s atingido",
  "input_audio_tokens": 4210,
  "input_text_tokens": 180,
  "output_audio_tokens": 9130,
  "output_text_tokens": 64,
  "cached_tokens": 1024,
  "duration_seconds": 96
}
```

| Campo | Tipo | Obrigatório | Restrição |
|---|---|---|---|
| `motivo` | string | sim | 1 a 200 caracteres, texto livre |
| `input_audio_tokens` | inteiro | **não** | 0 a 2.000.000.000 |
| `input_text_tokens` | inteiro | **não** | 0 a 2.000.000.000 |
| `output_audio_tokens` | inteiro | **não** | 0 a 2.000.000.000 |
| `output_text_tokens` | inteiro | **não** | 0 a 2.000.000.000 |
| `cached_tokens` | inteiro | **não** | 0 a 2.000.000.000 |
| `duration_seconds` | inteiro | **não** | 0 a 2.000.000.000 |

O `motivo` é gravado e aparece no log de operação. Use algo legível e
consistente: `"o cliente clicou em Parar"`, `"silencio por 45s"`,
`"a aba saiu de vista"`, `"a janela perdeu o foco"`,
`"teto de 300s atingido"`.

**Resposta 200**

```json
{ "encerrado": true }
```

| Campo | Tipo | O que é |
|---|---|---|
| `encerrado` | boolean | `true` se a sessão existia e estava aberta; `false` se não existe ou já havia sido encerrada |

**Erros**

| Código | Corpo | Significa |
|---|---|---|
| 422 | corpo padrão de validação | `sessao_id` não é UUID; `motivo` vazio ou maior que 200; algum contador negativo, fracionário ou acima de 2.000.000.000 |

Mande esta chamada com `keepalive: true` no `fetch`, para ela sobreviver à aba
sendo fechada no mesmo instante. E **não espere a resposta** antes de parar o
microfone — parar o microfone vem primeiro.

---

#### 4.3.1 Os seis contadores: de onde eles saem

**O backend não vê a conversa.** Ela acontece entre o navegador e a OpenAI, e
o consumo — que é o que a fatura cobra — chega só ao navegador, no evento
`response.done`. Sem este corpo, não existe resposta para "quanto esse
restaurante gastou".

Cada `response.done` traz um `usage`. **Some os de todos os `response.done` da
sessão** e mande o total uma vez, no `/ended`:

```js
// a cada response.done
const uso = evento.response.usage || {};
const entrada = uso.input_token_details || {};
const saida = uso.output_token_details || {};

acumulado.input_audio_tokens  += entrada.audio_tokens  || 0;
acumulado.input_text_tokens   += entrada.text_tokens   || 0;
acumulado.output_audio_tokens += saida.audio_tokens    || 0;
acumulado.output_text_tokens  += saida.text_tokens     || 0;
acumulado.cached_tokens       += entrada.cached_tokens || 0;
```

`duration_seconds` é medido pelo front: da abertura da conexão de áudio até o
encerramento, em segundos inteiros.

Três coisas que valem saber:

- **`cached_tokens` é subconjunto da entrada, não uma quinta parcela.** É a
  fatia dos tokens de entrada que veio do cache e saiu mais barata. Mande-o em
  separado como está acima; não o desconte de `input_*` nem o some ao total.
- **Todos são opcionais, e nenhum deles muda o encerramento.** Sessão que
  encerra sem reportar número nenhum continua encerrando normalmente — o campo
  fica nulo e o relatório de custo conta essa sessão como "sem número". Não
  invente zero para preencher: zero significa "reportou zero".
- **Reportar continua valendo quando o servidor já fechou a sessão.** Se a
  varredura de teto de duração fechou a linha antes, a resposta vem
  `{"encerrado": false}` e **os números são gravados assim mesmo** — essa é
  justamente a sessão mais longa, e portanto a mais cara.

**Não mande texto da conversa, transcrição, nem nada além destes números.** O
corpo aceita só o que está na tabela acima.

---

### 4.4 `POST /voice/search` — a ferramenta de busca

**Não exige autenticação.**

É esta rota que o front chama quando o modelo pede uma busca (ver §7).

**Requisição**

```json
{
  "restaurant_id": "0f9c8d2e-...",
  "branch_id": "6a1f...-...",
  "consulta": "sobremesa de chocolate",
  "preco_maximo": 50
}
```

| Campo | Tipo | Obrigatório | Restrição |
|---|---|---|---|
| `restaurant_id` | string (UUID) | sim | — |
| `branch_id` | string (UUID) | **sim, desde 20/08/2026** | a mesma filial da sessão |
| `consulta` | string | sim | 1 a 500 caracteres |
| `preco_maximo` | número ou `null` | não | se vier, tem de ser **maior que zero** |

`preco_maximo` é o valor em reais (`50` = R$ 50,00). Mande `null` ou omita
quando o modelo não preencher.

**Resposta 200**

```json
{
  "produtos": [ { "...": "..." } ],
  "resumo": "Produtos encontrados: Pudim - R$ 12,50; Brownie - R$ 15,00"
}
```

| Campo | Tipo | O que é |
|---|---|---|
| `produtos` | array de objetos | os produtos encontrados, **para a tela**. No máximo 5. Formato completo em §8 |
| `resumo` | string | o texto que volta **para o modelo**. Nunca mostre na tela |

`produtos` pode vir **vazio** (`[]`) — é resposta legítima quando nada casa.
Nesse caso `resumo` é exatamente `"Nenhum produto encontrado nesta loja."`
— a negativa é da **filial** que você mandou em `branch_id`, e não do
restaurante: o produto pode existir na outra loja.

Quando há produtos, `resumo` tem o formato
`"Produtos encontrados: <nome> - R$ <valor>; <nome> - R$ <valor>"`, com no
máximo cinco itens e o preço em formato brasileiro (vírgula decimal).

**A separação entre `produtos` e `resumo` é intencional e importante:** o
modelo só vê `resumo`, os cartões só usam `produtos`, e os dois vêm da mesma
leitura do banco. É isso que impede o modelo de falar um preço diferente do que
o cartão mostra. **Não mande `produtos` para o modelo** e não monte cartão a
partir do que o modelo falou.

**Erros**

| Código | Corpo | Significa |
|---|---|---|
| 404 | `{"detail":"Restaurante não encontrado"}` | `restaurant_id` inexistente ou inativo |
| 404 | `{"detail":"Filial não encontrada para este restaurante"}` | `branch_id` de outra loja |
| 422 | corpo padrão de validação | `consulta` vazia ou > 500, `preco_maximo` ≤ 0, `restaurant_id`/`branch_id` não-UUID ou ausente |

**Não há 401, 403 nem 429 aqui.** Esta rota não confere login, não confere se
a voz está ligada no restaurante e não tem rate limit próprio.

Se a busca falhar por qualquer motivo, **não deixe o modelo esperando calado**:
devolva a ele um `function_call_output` com um texto curto de falha (ex.:
`"A busca falhou."`) e siga. Ver §7.4.

---

### 4.5 `GET /voice/test` — **removida**

Existiu até 24/08/2026 e servia uma bancada de teste interna. O HTML dela foi
apagado do repositório, a rota passou a responder 500, e as duas coisas foram
removidas juntas em vez de consertadas: a bancada viva é externa ao backend.

**São quatro rotas em `/voice`, não cinco.** Nada mudou nas outras.

---

## 5. Tabela consolidada de erros

| Situação | Rota | Código | Corpo |
|---|---|---|---|
| Sem header `Authorization` | `/voice/session` | 401 | `{"detail":"Token ausente"}` |
| Token vencido | `/voice/session` | 401 | `{"detail":"Token expirado"}` |
| Token inválido | `/voice/session` | 401 | `{"detail":"Token invalido"}` |
| Conta do cliente desativada | `/voice/session` | 403 | `{"detail":"Conta inativa"}` |
| Restaurante sem voz habilitada | `/voice/session` | 403 | `{"detail":"O atendimento por voz nao esta disponivel neste restaurante."}` |
| Restaurante inexistente/inativo | `/voice/session`, `/voice/search` | 404 | `{"detail":"Restaurante não encontrado"}` |
| Corpo inválido | todas | 422 | corpo padrão do FastAPI |
| Rate limit por IP | `/voice/session` | 429 | `{"detail":"Muitas requisições. Tente novamente em instantes."}` |
| Cota do cliente estourada | `/voice/session` | 429 | `{"detail":"Voce ja usou N conversas por voz nas ultimas 24 horas. Tente mais tarde."}` |
| Cota do restaurante estourada | `/voice/session` | 429 | `{"detail":"O atendimento por voz deste restaurante atingiu o limite do dia."}` |
| OpenAI recusou | `/voice/session` | 502 | `{"detail":"A OpenAI recusou a emissao da credencial"}` |
| OpenAI inacessível | `/voice/session` | 503 | `{"detail":"Nao consegui falar com a OpenAI"}` |
| Sessão inexistente | `/connected`, `/ended` | **200** | `{"registrado": false}` / `{"encerrado": false}` |
| Voz desligada na plataforma | todas | 404 | rota não existe |

Todo erro do backend usa o envelope `{"detail": ...}`. O 422 do FastAPI traz
`detail` como **array de objetos**, não string — trate os dois formatos.

---

## 6. A conexão com a OpenAI e o `call_id`

### 6.1 Abrindo a conexão

```js
const pc = new RTCPeerConnection();
pc.ontrack = (e) => { elementoDeAudio.srcObject = e.streams[0]; };
pc.addTrack(microfone.getTracks()[0]);
const canal = pc.createDataChannel("oai-events");

const oferta = await pc.createOffer();
await pc.setLocalDescription(oferta);

const resposta = await fetch("https://api.openai.com/v1/realtime/calls", {
  method: "POST",
  body: oferta.sdp,
  headers: {
    "Authorization": "Bearer " + credencial.value,
    "Content-Type": "application/sdp"
  }
});
const sdpAnswer = await resposta.text();
await pc.setRemoteDescription({ type: "answer", sdp: sdpAnswer });
```

O nome do data channel **é** `oai-events`.

### 6.2 De onde sai o `call_id`

Do cabeçalho **`Location`** da resposta dessa mesma requisição. Ele vem no
formato de caminho, e o `call_id` é o último segmento:

```js
const local = resposta.headers.get("Location");   // "/v1/realtime/calls/rtc_abc123"
const callId = local ? local.split("/").filter(Boolean).pop() : null;
```

### 6.3 Se o `Location` não vier

**É um caso real e previsto.** `Location` é um cabeçalho de resposta de outra
origem, e o navegador só o entrega ao JavaScript se a OpenAI o listar em
`Access-Control-Expose-Headers`. Isso não foi verificado em navegador.

Se `headers.get("Location")` devolver `null`:

- **não aborte.** A sessão de áudio funciona normalmente sem ele.
- **pule o passo 4** (`/connected`) — não mande `call_id` vazio nem inventado.
- **registre no seu log/telemetria.** O que se perde é a capacidade de o
  servidor desligar essa sessão remotamente; o teto de duração do front passa a
  ser a única proteção contra uma sessão esquecida.

Se isso acontecer sempre, avise o backend — muda a estratégia de controle de
custo.

### 6.4 A saudação, no `onopen` do data channel

O atendente **cumprimenta primeiro**, antes de o cliente falar. Quem dispara é
o front, no `onopen` do canal, com o texto que veio no passo 1:

```js
canal.onopen = () => {
  if (!saudacao) { return; }
  canal.send(JSON.stringify({
    type: "response.create",
    response: {
      instructions: "Cumprimente o cliente falando exatamente isto, e nada mais: " + saudacao,
    },
  }));
};
```

Três detalhes que não são estilo:

- **A frase vem do backend, e o front não a monta.** O nome é do cliente que o
  token autenticou; a página não tem por que saber quem é ele, e uma frase
  escolhida no javascript seria o cliente escolhendo como o atendente o chama.
- **Vai em `response.instructions`, e não no prompt da sessão.** Instrução de
  uma resposta só não entra no prefixo que a OpenAI mantém em cache — o nome
  muda a cada sessão, e nome dentro do prefixo é cache nenhum. O outro motivo
  está no cabeçalho de `src/ai/voice/voice_prompt.py`: frase pronta dentro das
  instruções da sessão vira molde, e o modelo passa a preencher o molde
  sozinho depois.
- **É uma resposta como qualquer outra**, e portanto um turno faturado. Ela
  entra na conta de `response.done` junto com o resto.

Se o cliente começar a falar por cima, a saudação é interrompida pelo VAD e a
conversa segue — é o comportamento desejado, e não precisa de tratamento.

---

## 7. A chamada de ferramenta

Tudo chega pelo data channel `oai-events`, como mensagens JSON.

### 7.1 Estado que o front precisa manter

A OpenAI aceita **uma resposta em curso por vez**. Pedir outra antes de a atual
fechar devolve o erro `conversation_already_has_active_response`. Mantenha:

- `respostaAtiva` — `true` em `response.created`, `false` em `response.done`.
- uma fila: se você quiser pedir uma resposta e `respostaAtiva` for `true`,
  guarde a intenção e dispare quando `response.done` chegar.
- um `Set` de `call_id` já atendidos.

Isso não é excesso de zelo: já quebrou em teste real.

### 7.2 Como a chamada chega — dois eventos, mesma chamada

A ferramenta chega por **dois** eventos, medidos com o mesmo `call_id` e a
menos de 1 ms de distância:

```
response.function_call_arguments.done   { call_id, name, arguments }
response.output_item.done               { item: { type: "function_call",
                                                  call_id, name, arguments } }
```

Os dois carregam `name`, `call_id` e `arguments` completos. **Trate os dois** (a
API já mudou esses nomes antes) e deduplique **pelo `call_id`**, marcando o id
como atendido de forma síncrona, antes de qualquer `await`.

`arguments` é uma **string JSON**, não um objeto. Faça `JSON.parse` dentro de
`try`:

```js
{ "consulta": "sobremesa", "preco_maximo": 50 }
```

`preco_maximo` pode não vir. `name` sempre será `buscar_no_cardapio` — é a
única ferramenta declarada.

### 7.3 Como devolver o resultado

Duas mensagens pelo data channel, nesta ordem:

```js
canal.send(JSON.stringify({
  type: "conversation.item.create",
  item: {
    type: "function_call_output",
    call_id: callId,
    output: resultado.resumo        // a STRING de /voice/search
  }
}));

canal.send(JSON.stringify({ type: "response.create" }));
```

`output` é **uma string** — o campo `resumo` da nossa resposta, sem
transformação. Não mande o array `produtos`, não mande JSON, não reescreva o
texto.

Sem o `response.create` o modelo recebe o resultado e fica calado.

**O `response.create` respeita a fila do §7.1.** Se houver resposta ativa,
guarde e dispare no `response.done`. O `conversation.item.create` pode ir na
hora.

Para fazer o modelo dizer uma frase específica (o aviso de inatividade do
passo 8), use `response.create` com instruções:

```js
{ type: "response.create",
  response: { instructions: "Diga exatamente isto, em uma frase e nada mais: vou encerrar por inatividade." } }
```

### 7.4 Se a busca falhar

Devolva um `function_call_output` com um texto curto (`"A busca falhou."`) e o
`response.create`. Nunca deixe a chamada sem resposta — o modelo trava
esperando.

### 7.5 Outros eventos úteis

| Evento | Para quê |
|---|---|
| `response.created` / `response.done` | o controle de resposta ativa do §7.1 — e o `usage` do `response.done` é o que alimenta os contadores do §4.3.1 |
| `input_audio_buffer.speech_started` | zerar o contador de inatividade |
| `response.output_audio_transcript.done` | transcrição do que o assistente falou (`.transcript`) |
| `conversation.item.input_audio_transcription.completed` | transcrição do que o cliente falou (`.transcript`) |
| `error` | erro da API: `evento.error.code`, `evento.error.message` |

Um `error` com `code === "conversation_already_has_active_response"` **não deve
derrubar a sessão**: marque que há resposta ativa, ponha o pedido na fila e
tente de novo no próximo `response.done`.

---

## 8. O formato dos produtos

Cada item de `produtos` em `POST /voice/search`:

```json
{
  "id": "3a7f...-...",
  "restaurant_id": "0f9c...-...",
  "category_id": "b21e...-...",
  "code": "X12",
  "name": "Picanha na Chapa",
  "slug": "picanha-na-chapa",
  "description": "Serve duas pessoas",
  "price": 23.9,
  "image_path": "produtos/abc.jpg",
  "image_url": "https://.../produtos/abc.jpg",
  "is_active": true,
  "is_available": true,
  "sort_order": 0,
  "option_groups": []
}
```

| Campo | Tipo | Pode ser nulo | Observação |
|---|---|---|---|
| `id` | string (UUID) | não | id do produto |
| `restaurant_id` | string (UUID) | não | |
| `category_id` | string (UUID) | não | |
| `code` | string | **sim** | código interno do lojista |
| `name` | string | não | **o nome do cartão** |
| `slug` | string | **sim** | usado na URL pública do produto |
| `description` | string | **sim** | |
| `price` | **número** | não | reais, como `23.9`. Ver o alerta abaixo |
| `image_path` | string | **sim** | caminho interno; prefira `image_url` |
| `image_url` | string | **sim** | URL pública pronta. **Trate a ausência** — nem todo produto tem foto |
| `is_active` | boolean | sim | sempre `true` aqui (a busca já filtra) |
| `is_available` | boolean | sim | sempre `true` aqui (a busca já filtra) |
| `sort_order` | número | sim | ordem no cardápio |
| `option_groups` | array | não | grupos de opção; vem `[]` quando não há |

### O preço é número, não string

`price` chega como número JSON: `23.9`, e **não** `"23.90"`. Formate no front:

```js
"R$ " + produto.price.toFixed(2).replace(".", ",")
```

Isso é importante porque o `resumo` que vai ao modelo já vem formatado como
`R$ 23,90`. Se o cartão exibir `R$ 23.9` e o assistente falar "vinte e três e
noventa", parece que os dois discordam. **O preço do cartão vem do banco e é o
correto** — o texto do modelo é que é derivado.

### `option_groups`, quando vier preenchido

```json
{
  "id": "...", "name": "Ponto da carne", "description": null,
  "min_select": 1, "max_select": 1, "is_required": true, "sort_order": 0,
  "options": [
    { "id": "...", "name": "Ao ponto", "description": null,
      "additional_price": 0.0, "sort_order": 0 }
  ]
}
```

| Campo do grupo | Tipo | |
|---|---|---|
| `id` | string (UUID) | |
| `name` | string | |
| `description` | string ou nulo | |
| `min_select` | número | mínimo de opções a escolher |
| `max_select` | número | máximo |
| `is_required` | boolean | |
| `sort_order` | número ou nulo | |
| `options` | array | as opções |

| Campo da opção | Tipo | |
|---|---|---|
| `id` | string (UUID) | |
| `name` | string | |
| `description` | string ou nulo | |
| `additional_price` | número | acréscimo em reais |
| `sort_order` | número ou nulo | |

Para o cartão da voz, `name` + `price` + `image_url` bastam. Os
`option_groups` só importam se você levar o produto ao carrinho a partir daí.

---

## 9. Origens permitidas (CORS)

A API aceita, com credenciais:

```
https://pederapidex.com
https://www.pederapidex.com
https://admin.pederapidex.com
http://localhost:5173   http://127.0.0.1:5173
http://localhost:5500   http://127.0.0.1:5500
http://localhost:5501   http://127.0.0.1:5501
```

Mais os previews da Vercel do painel. Se o app do cliente rodar em outra
origem, ela precisa ser acrescentada no backend — não é configurável pelo
front.

**O microfone exige contexto seguro.** `getUserMedia` funciona em `https://` e
em `http://localhost`, e **não** funciona em `http://` num IP da rede local.
Testar de outro aparelho exige HTTPS ou um túnel.

---

## 10. Resumo dos números

| | |
|---|---|
| Teto de uma sessão | 300 s (vem em `limites.duracao_maxima_s`) |
| Inatividade que encerra | 45 s (`limites.inatividade_s`) |
| Aviso antes do corte | 10 s (`limites.aviso_antes_s`) |
| Validade do segredo efêmero | ~585 s — só para **abrir** a conexão |
| Cota por cliente | 5 sessões / 24 h (janela rolante) |
| Cota por restaurante | 100 sessões / 24 h |
| Rate limit da emissão | 3/min e 20/h por IP |
| Produtos por busca | no máximo 5 |
| Tamanho da `consulta` | 1 a 500 caracteres |

Os três primeiros **vêm no corpo da emissão** e podem mudar sem deploy do app —
leia-os de lá, não os fixe no código.

---

## 11. Coisas que já quebraram, para não repetir

1. **Mandar `response.create` com resposta ativa** → erro
   `conversation_already_has_active_response`. Use a fila do §7.1.
2. **Responder a mesma tool call duas vezes**, porque os dois eventos do §7.2
   chegam para a mesma chamada. Deduplique por `call_id`, de forma síncrona.
3. **Encerrar sem `getTracks().forEach(t => t.stop())`** → o indicador de
   gravação do navegador fica aceso e o microfone continua captando.
4. **Fixar os limites no front** → o backend perde a capacidade de apertar o
   teto sem um deploy do app.
5. **Montar o cartão com o que o modelo falou** → o preço do cartão tem de vir
   de `produtos`, sempre.
6. **Tratar `{"registrado": false}` como sucesso silencioso** → ele vem com
   HTTP 200; teste o campo.
