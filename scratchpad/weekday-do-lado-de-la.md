# `weekday` do lado de lá — o que o front e o painel precisam travar

Escrito em 05/09/2026, depois de `8062eeb` (todo campo de dia da semana do
`/openapi.json` passa a publicar que 0 é SEGUNDA).

O backend fez a metade dele: a convenção deixou de ser folclore e virou
contrato. **A trava de verdade só existe do lado do consumidor**, e este
arquivo tem os dois prompts prontos para colar.

---

## Antes de colar: o estado real, medido

Fui olhar os dois repositórios antes de escrever, e o resultado muda o que
vale pedir. **Os dois já estão CERTOS hoje.** O que falta não é conserto — é
o que impede o próximo.

### `pedeaqui_front` — certo, com cicatriz, sem trava

- `scripts/services/store-info-format.js:16` — `DIAS_DO_CONTRATO` está na ordem
  do contrato (`Segunda-feira` no índice 0);
- `store-info-format.js:54` — `todayHours` casa `current_weekday` com `weekday`
  **por número**, e o comentário diz por quê ("os dois são números no
  contrato"). Nunca toca em `Date`;
- **não há um único `getDay()` em `scripts/`.**

E há uma cicatriz de produção escrita em `restaurant-page.js:996-1001`: o
helper lia `display_name`/`day_name` — nomes que nunca existiram — e caía num
mapa `1..7`. **A primeira linha do horário dizia "0" e a semana inteira saía
deslocada em um**; o `weekday: 1`, que é terça, virava "Segunda-feira".

**O que falta: nada trava a ordem de `DIAS_DO_CONTRATO`.** Girá-lo em um —
começar no domingo, que é o reflexo de quem escreve JavaScript — reproduz a
cicatriz inteira, e nenhum teste cai.

### `rapidex-admin` — certo, e com a porta única já construída

- `src/store/business-hours.ts:35-40` — `backendWeekday(date)` é a conversão,
  `(date.getDay() + 6) % 7`, com o motivo escrito;
- `business-hours.ts:59` — `weekdayDaOperacao(now)` responde "que dia é hoje
  para a LOJA", pelo fuso da operação e não pelo do aparelho. Ele existe porque
  o tablet de balcão em modo quiosque já leu a linha do dia errado;
- `business-hours.ts:18-25` e `cashback/cashback-model.ts` — as duas listas de
  dias, ambas na ordem do contrato;
- `cashback/cashback-model.test.ts:32-37` — **já trava** a ordem, com o
  `getDay()` citado no comentário. Para cashback;
- **há exatamente UM `getDay()` em todo o `src/`**, dentro de `backendWeekday`.

**O que falta: nada impede o segundo `getDay()`.** A porta única existe por
disciplina, não por trava — e é a mesma forma da armadilha 46 do backend:
enquanto a regra depender de alguém lembrar de entrar pela porta, ela é uma
recomendação.

---

## Prompt para o `pedeaqui_front`

> No backend, todo campo de dia da semana do `/openapi.json` agora publica a
> convenção no próprio campo: **0 = SEGUNDA, 6 = domingo** (o
> `datetime.weekday()` do Python). O `getDay()` do JavaScript é 0 = domingo. Os
> campos são `weekday` (em `BusinessHourResponse`, `BusinessHourDayResponse`,
> `BranchOpenPeriodResponse`, `CashbackWeekdayResponse`) e `current_weekday`
> (em `RestaurantInfoResponse`).
>
> **O app já trata isso certo hoje** — `DIAS_DO_CONTRATO` está na ordem do
> contrato, `todayHours` casa por número, e não existe `getDay()` em
> `scripts/`. Não é para consertar nada. É para travar, porque isto já quebrou
> em produção uma vez (a semana inteira deslocada em um, `restaurant-page.js`
> linhas 996-1001) e nada impede que volte.
>
> Duas travas, e a segunda é a que importa:
>
> 1. **um unitário sobre `store-info-format.js`** que fixe
>    `DIAS_DO_CONTRATO[0] === 'Segunda-feira'` e `[6] === 'Domingo'`, e que
>    prove que `todayHours` casa por número (uma fixture com
>    `current_weekday: 0` tem que achar o dia de `weekday: 0`). Girar a lista em
>    um precisa ficar vermelho;
> 2. **uma varredura que falhe se aparecer `getDay()` em `scripts/`**, no
>    espírito das que já existem em `tools/`. Hoje são zero. A regra é: número
>    de dia da semana que veio da API nunca entra em `new Date()`, e número que
>    veio de `new Date()` nunca é comparado com campo da API sem converter.
>
> Não peço a conversão centralizada porque o app não precisa dela hoje: ele
> nunca calcula "que dia é hoje" — ele lê `current_weekday` do backend, que já
> vem no fuso da operação. **Se um dia precisar, a conversão é
> `(getDay() + 6) % 7`, e ela entra num lugar só** — o `rapidex-admin` tem essa
> função pronta em `src/store/business-hours.ts:35`, com o nome
> `backendWeekday`.

---

## Prompt para o `rapidex-admin`

> O backend passou a publicar a convenção de dia da semana no campo, no
> `/openapi.json`: **0 = SEGUNDA, 6 = domingo** (`datetime.weekday()` do
> Python), contra o 0 = domingo do `getDay()`.
>
> **O painel já está certo, e mais do que o app** — `backendWeekday` em
> `src/store/business-hours.ts:35` é a conversão, `weekdayDaOperacao` resolve o
> fuso do aparelho, as duas listas de dias estão na ordem do contrato, e há
> exatamente **um** `getDay()` em todo o `src/`, dentro de `backendWeekday`.
> Não é para consertar nada.
>
> É para travar o que hoje é disciplina:
>
> 1. **uma varredura (teste, não lint) que falhe se `getDay()` aparecer em
>    `src/` fora de `business-hours.ts`.** Um só é a porta; o segundo é a
>    regressão, e ela é silenciosa — o número existe nos dois lados e nenhum
>    dos dois reclama;
> 2. **estender a trava de ordem que o cashback já tem
>    (`cashback-model.test.ts:32-37`) para o `WEEKDAYS` de
>    `business-hours.ts:18`.** Hoje a lista do cashback está travada e a do
>    horário de funcionamento não, e é a do horário que decide se a loja abre.
>
> E uma observação de desenho, para decidir e não para consertar: o painel
> calcula "que dia é hoje" sozinho (`weekdayDaOperacao`), e o backend também
> responde isso em `RestaurantInfoResponse.current_weekday`. **São duas fontes
> para a mesma pergunta.** A do painel é defensável — `/info` é rota pública e
> ele não a consome —, mas as duas podem discordar na virada da meia-noite. Se
> um dia divergirem, o `current_weekday` é o que a loja considera oficial.

---

## Sobre o `current_weekday` ser "o que mais provavelmente já está errado"

Era a aposta certa a fazer — ele é o único que **nunca** coincide com
`getDay()` (segunda é 0 de um lado e 1 do outro, então não há dia em que os
dois batam). Fui atrás dele primeiro por isso.

**E ele está certo nos dois lugares**, o que vale registrar para ninguém
gastar a busca de novo:

- **no app**, `current_weekday` é lido em um lugar só
  (`store-info-format.js:54`, e a tela que o usa em
  `screens/store-info-screen.js:244`), e nos dois é comparação **número contra
  número** com o `weekday` do mesmo payload. Não passa por `Date` em ponto
  nenhum;
- **no painel**, ele não é lido — o painel não consome `/info`;
- **no backend**, ele sai de `datetime.now(ZoneInfo(TIMEZONE)).weekday()`
  (`restaurant_service.py:80,99`), no fuso da operação e não em UTC. Se fosse
  UTC, ele estaria errado três horas por dia — não está.

O que ficou de aberto por ele é a duplicação da pergunta, no último parágrafo
do prompt do painel. Isso é desenho, não defeito.
