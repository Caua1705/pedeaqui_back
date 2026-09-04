# De quem é a transformação de imagem — levantamento para decidir

> ## ✅ A TRANSFORMAÇÃO FUNCIONA — MEDIDO EM 05/09/2026
>
> **Este arquivo esteve suspenso por algumas horas, e a suspensão estava
> errada.** Fica registrado porque a armadilha é do PAINEL, e quem abrir aquela
> tela amanhã vai ler a mesma linha:
>
>     Storage Image Transformations: Unavailable in plan
>
> **Essa linha descreve o item COBRÁVEL do recurso, não uma rota bloqueada.** O
> endpoint responde normalmente. Medido com `curl` contra o bucket de produção:
>
> | Objeto | Original | Transformado | Razão |
> |---|---|---|---|
> | `brand/logo.webp` | 22.742 B | 1.241 B | **18×** |
> | uma foto de picanha | 104.074 B | 3.992 B | **26×** |
>
> `200`, com corpo transformado de verdade — o endpoint chega a devolver **jpeg
> no lugar do webp** de origem, o que também quer dizer que a derivada não é só
> menor: ela pode ser de outro formato que o original.
>
> E a checagem que fecha a leitura sem precisar de mais nenhuma medição: se a
> rota estivesse bloqueada, **os 14,19 GB de Cached Egress teriam sido erro, e
> não imagem.** Eles foram imagem.
>
> ### A pergunta que a suspensão abriu está respondida
>
> Ela era: os 8 sítios do front que reescrevem para `/render/image/` servem
> transformado, ou o navegador está caindo no `src` com o original?
>
> **Servem transformado.** O `srcset` é o que já funciona — e não é novo: o
> herói, os destaques e as fotos do cardápio usam isso há muito tempo. Duas
> consequências para quem for mexer:
>
> - **não "limpe" o `srcset`.** Ele é a economia que já está acontecendo;
> - **o desperdício está exatamente onde este levantamento diz que está** — nos
>   7 sítios do app e nos 4 do painel que **não** transformam (tabela na seção
>   1). Não é uma suspeita distribuída: é uma lista de 11 linhas.
>
> **A, B e C voltam a ser escolhas válidas**, e a ordem front → backend da
> seção 2 continua valendo pelo motivo dela, que nunca teve a ver com plano.
>
> O que fica em aberto da ressalva antiga é só isto, e agora é pergunta de
> fatura e não de viabilidade: **se a linha é cobrável e o plano não a inclui,
> como as transformações que estão acontecendo entram na conta?** Não bloqueia
> escolher A, B ou C; entra na conta de quanto cada uma economiza de fato.

Escrito em 04/09/2026, depois do levantamento do laço de egress. **Não é
proposta de implementação: é o mapa e a conta, para escolher onde mexer.**

O laço (a suíte E2E do `pedeaqui_front` baixando o bucket de produção a cada
teste) é assunto do outro repositório e não muda nada aqui. Este arquivo é
sobre o desperdício que sobra **depois** de o laço morrer, em toda visita
legítima: 174 KB servidos onde 12 KB bastariam.

---

## O que está sendo decidido, em uma frase

O backend devolve `image_url` apontando para o **original**. A transformação
(`/render/image/...?width=`) é montada **no consumidor, por sítio de
chamada, opcionalmente**. A pergunta é se essa responsabilidade continua lá.

---

## 1. O mapa: quem emite e quem transforma

### Backend — 9 pontos de emissão, 16 rotas

Todo `image_url`/`logo_url`/`cover_url` da API sai de um lugar só,
`build_storage_url` (`src/utils/storage.py:4`), que monta **sempre** a forma
`/storage/v1/object/public/<bucket>/<path>` — o original, sem parâmetro
nenhum:

| Ponto de emissão | Schema |
|---|---|
| `src/services/menu_service.py:305` | `ProductResponse` |
| `src/services/menu_service.py:401` | `BannerResponse` (hero e destaque) |
| `src/services/menu_service.py:441` | `PublicCouponResponse` |
| `src/services/coupon_service.py:441` | `CustomerCouponResponse` |
| `src/services/coupon_service.py:831` | `CouponTemplateResponse` |
| `src/services/restaurant_service.py:86` | `RestaurantInfoRestaurantResponse` |
| `src/services/restaurant_service.py:206,208` | `RestaurantPublicResponse` (logo + capa) |
| `src/services/admin_menu_service.py:782` | `AdminProductResponse` |
| `src/services/admin_menu_service.py:452` | `ProductImageResponse` |

Medido no `openapi.json` gerado (não contado à mão): **16 rotas** devolvem,
direta ou transitivamente, uma URL de imagem. **Oito delas são `/admin/*`** —
e uma é `POST /chat`, que carrega foto de produto na resposta do Rapi.

### `pedeaqui_front` — a transformação existe, e é opt-in

`scripts/utils/image-cdn.js` reescreve `/object/public/` →
`/render/image/public/` com `width` + `resize=contain` + `quality=72`. O
arquivo tem a medição no cabeçalho: `arroz-branco.webp` sai de **88 kB** para
**10,7 kB em 160px**. É a ordem de grandeza do seu 174 → 12.

Onde ela é aplicada (8 sítios):

| Sítio | Modo |
|---|---|
| `screens/home-screen.js:82` (herói) | `fluid` [480…1440] |
| `screens/home-screen.js:94` (slides) | `fluid` |
| `screens/home-screen.js:287` (arte de cupom) | `box` 168×90 |
| `screens/home-screen.js:320` (destaque) | `fluid` |
| `restaurant-page.js:1052` (grade do cardápio) | `box` 110 |
| `restaurant-page.js:2404` (miniatura da sacola) | `box` 48 |
| `screens/product-screen.js:55` (herói do produto) | `fluid` |
| `screens/coupon-detail-screen.js:121` (arte grande) | `fluid` |

Onde ela **não** é aplicada, e o original inteiro vai para a rede (7 sítios):

| Sítio | O que serve cru |
|---|---|
| `restaurant-assistant.js:673` | foto de produto no resultado do Rapi |
| `restaurant-assistant.js:1015` | foto no detalhe de produto do Rapi |
| `restaurant-club.js:193` | arte de cupom no clube |
| `restaurant-page.js:1393` | logo do restaurante |
| `screens/profile-screen.js:321` | foto do item no detalhe do pedido |
| `screens/profile-screen.js:582` | logo do restaurante no pedido |
| `screens/product-screen.js:64` | herói do produto **sem `preview`** — cai em `productImage()` sem `box` nem `fluid`, e `responsiveImageAttrs` devolve string vazia |

O último é o mais instrutivo: **o mesmo herói tem os dois comportamentos**,
conforme o caminho pelo qual a tela foi aberta. Ninguém decidiu isso.

### `rapidex-admin` — zero transformação

Quatro sítios, todos com o original:
`coupons/CouponsPage.tsx:437`, `coupons/ArtePicker.tsx:106`,
`menu/ProductRow.tsx:187`, `menu/ProductImageField.tsx:216,277`.

E há uma decisão escrita que fecha essa porta de propósito, em
`rapidex-admin/src/api/types.ts:454`:

> `image_url` vem pronta do backend (`build_storage_url`) — o painel não monta
> URL de bucket.

Ou seja: **o painel não vai montar a transformação sozinho sem contrariar a
própria regra dele.** E a lista de produtos do Júnior tem 161 itens: uma tela
de `/admin/products` serve hoje o cardápio de fotos inteiro em tamanho de
upload, dentro de uma miniatura. É provavelmente o pior sítio dos 15, e é o do
lojista — quem abre o painel todo dia.

---

## 2. O fato que decide a ORDEM, não o mérito

**Se o backend passar a devolver a URL já transformada, o `srcset` do front de
hoje some — sem erro e sem log.**

`image-cdn.js:24` só reconhece a origem pela presença de
`/storage/v1/object/public/`. Uma URL que chegue já como
`/storage/v1/render/image/public/` não casa: `variant()` devolve `null`,
`responsiveImageAttrs` devolve `''`, e os 8 sítios bons acima passam a servir
**a largura que o backend escolheu** em vez da largura da caixa. A grade do
cardápio, que hoje pede 330px (110 × 3 DPR), passaria a receber o que o
backend achasse razoável para "produto".

E se alguém "consertar" isso afrouxando o reconhecedor, a linha
`image-cdn.js:56` concatena `&width=` numa URL que já tem `width=` — dois
valores para o mesmo parâmetro, e quem desempata é o Storage.

É a armadilha 47 na forma de URL: a regra é permissiva por omissão, e o valor
novo (a URL já transformada) cai do lado errado calado.

**Consequência prática: qualquer caminho que mexa no backend exige que o front
saiba ler a forma nova ANTES.** Isso não decide quem é o dono — decide que o
front se pronuncia primeiro, que é o que você já pediu a ele.

---

## 3. Os três caminhos, com o custo de cada um

### A) Backend passa a devolver a URL transformada

`build_storage_url` ganha um teto por tipo de ativo (produto, banner, arte de
cupom, logo) e devolve `/render/image/...?width=<teto>&resize=contain`.

**A favor.** Fecha os 11 sítios crus de uma vez, inclusive os 4 do painel e o
`POST /chat`, sem que nenhum consumidor faça nada. Fecha também o 12º, que
ainda não existe: consumidor novo nasce econômico. É o único caminho em que a
economia **não depende de alguém lembrar** — a família das armadilhas 46 e 47.
E tira a regra de Supabase de dentro do JavaScript, que é onde ela menos
deveria estar.

**Contra.**
- **Muda o significado de um campo que dois fronts já consomem.** O tipo não
  muda (continua `string`), então o `openapi.json` não acusa nada e a
  armadilha 16 não pega: é mudança de contrato que passa por refatoração.
- **Desliga o `srcset` do front** enquanto `image-cdn.js` não souber ler a
  forma nova (item 2). Feito na ordem errada, é uma regressão nos 8 sítios que
  hoje estão certos.
- **O backend não sabe o tamanho da caixa.** Ele pode escolher um teto; não
  pode escolher a largura certa. `srcset`/DPR/viewport são do cliente e
  continuam sendo — o teto é um piso de decência, não a resposta.
- **O original deixa de ter nome na API.** Hoje `image_url` é a única forma de
  chegar ao arquivo cheio. Se ela virar a derivada, some o caminho para
  reprocessar, auditar ou baixar o que o lojista subiu — a não ser que
  `image_path` (que já existe ao lado em quase todos os schemas) passe a ser o
  jeito oficial de reconstruí-lo, e aí o painel volta a montar URL de bucket,
  que é exatamente o que a regra dele proíbe.

### B) Continua sendo do front, e os 11 sítios são corrigidos

**A favor.** É o desenho de hoje, ele está escrito e justificado
(`image-cdn.js`, cabeçalho inteiro), e a decisão de manter o **original em
`src`** é uma rede de segurança real: se a transformação falhar ou o plano do
Storage mudar, nenhuma foto some do cardápio. Zero risco de contrato. Nenhuma
revisão, nenhum deploy do backend.

**Contra.**
- **São dois consumidores e a regra teria que existir duas vezes** — uma em JS
  no app, outra em TypeScript no painel. É a armadilha 54 pelo lado do front:
  a mesma pergunta respondida em dois arquivos que vão divergir. E no painel
  ela contraria a decisão escrita em `api/types.ts:454`.
- **Continua opt-in por sítio de chamada.** Os 11 buracos de hoje não são
  descuido de uma pessoa distraída: são o resultado previsível de uma regra que
  só vale onde alguém a escreve. O 12º sítio nasce cru.
- Não alcança `POST /chat` de graça: a foto que o Rapi devolve passa por um
  caminho de render diferente.

### C) Teto no backend, refinamento no front

O backend devolve **um teto** (a maior largura que qualquer tela usa para
aquele tipo — herói 1440, produto 960, arte de cupom 504, logo 360), e o front
continua estreitando por `srcset` onde conhece a caixa.

**A favor.** É o único que responde às duas perguntas diferentes com o dono
certo de cada uma: *"qual é o maior tamanho que faz sentido existir"* é do
backend, que conhece o ativo; *"qual é o tamanho desta caixa neste aparelho"* é
do cliente, e não tem como não ser. Consumidor que não faz nada já economiza a
maior parte dos 174 KB; consumidor que faz, economiza o resto.

**Contra.** Carrega o custo de contrato de A **e** o trabalho de front de B —
`image-cdn.js` precisa aprender a reescrever uma URL que já é `/render/image/`
(trocar o `width` em vez de concatenar outro), e o painel continua sem
`srcset`, só que agora com um teto sensato em vez do original.

E um custo que é só dele: **passam a existir dois números para a mesma tela.**
O teto do produto no backend e a lista `[360,480,640,960,1280]` de
`product-screen.js:59` precisam concordar, e nada os liga. Teto menor que o
maior `w` do front faz o browser pedir uma largura que o backend já limitou —
sem erro, com a imagem saindo menor que o pedido.

---

## 4. O que eu acho

**C, na ordem front → backend.** E, se for para escolher entre A e B puros,
**A** — pelo motivo que este repositório já pagou três vezes: uma regra que
depende de o consumidor lembrar de aplicá-la não é regra, é recomendação
(armadilhas 46, 47 e o `require_public` que saiu do `evaluate`). Os 11 sítios
crus são a prova empírica disso, e 4 deles estão num consumidor que
**declarou** que não vai montar essa URL.

Três coisas que me fazem parar antes de recomendar A puro:

1. **O front tem um argumento que não é preguiça.** Manter o original em `src`
   com a derivada em `srcset` é degradação graciosa de verdade: transformação
   fora do ar não tira foto nenhuma da tela. Em A puro isso se perde — a
   derivada vira o único caminho, e uma falha do endpoint de render é cardápio
   sem foto. Se A for escolhido, esse é o custo a aceitar de olhos abertos, e
   ele não aparece em nenhuma conta de KB.
2. **A conta pode não ser a que parece** — e esta ressalva **encolheu, mas não
   morreu** (ver a caixa no topo). O painel diz `Unavailable in plan` e a rota
   funciona: viabilidade está respondida, cobrança não. Cada largura distinta
   continua sendo um objeto novo no cache, então multiplicar tetos e larguras
   troca egress por transformação — e essa é a segunda coluna da fatura, que
   ninguém olhou ainda. Vale para dimensionar quanto A, B ou C economizam de
   fato; não vale mais como impedimento.
3. **`174 KB → 12 KB` é a economia do sítio medido, não a da rota.** Vale a
   pena refazer a conta separando o painel (161 fotos numa lista) do app (8
   imagens na Home): são ordens de grandeza diferentes e podem querer respostas
   diferentes. O painel sozinho talvez justifique A mesmo que o app não.

**O que eu NÃO faria em nenhum dos três:** mexer no backend antes de o
`image-cdn.js` saber ler a forma nova. Não porque quebra — porque **não**
quebra: fica verde, serve imagem, e os 8 sítios que hoje estão certos passam a
estar errados sem uma linha de log.

---

## 5. Pendente de resposta do front

- `image-cdn.js` aceita reescrever uma URL que já é `/render/image/`
  (substituindo o `width` em vez de concatenar)? É o pré-requisito de A e de C.
- O `src` com o original é requisito ou é conveniência? Se for requisito, A
  está descartado e a conversa é entre B e C.
- O painel abre mão de `api/types.ts:454` (e passa a montar URL de bucket), ou
  essa regra fica e o painel só se beneficia se a economia vier do backend?
