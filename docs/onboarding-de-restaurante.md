# Onboarding de restaurante

Do zero ao primeiro pedido pago e impresso.

Este é o roteiro do que **existe hoje**. Onde não há ferramenta, está escrito
que não há — nenhum passo aqui descreve uma tela que ainda não foi feita. A
lista do que deveria virar tela está no fim.

**Quem faz o quê:** 🔧 = você, no servidor ou no banco. 🏪 = o lojista, na
conta dele ou no painel. Os passos 🔧 são a maioria, e isso é o diagnóstico
principal deste documento.

Documentos vizinhos: [`arquitetura.md`](arquitetura.md) (o caminho de um
pedido por dentro), [`pagamentos-e-comissao.md`](pagamentos-e-comissao.md),
[`entrega-e-horarios.md`](entrega-e-horarios.md),
[`autenticacao-e-escopo.md`](autenticacao-e-escopo.md),
[`operacao.md`](operacao.md) (deploy e logs).

---

## Os cinco que quebram em silêncio

Leia esta lista antes de começar. São os passos cuja omissão **não gera erro
em lugar nenhum** — nem no boot, nem no log, nem na tela. O restaurante entra
no ar, parece pronto, e falha no primeiro pedido de verdade.

| Passo | Se for pulado | Como aparece |
|---|---|---|
| **6.4** Segredo do webhook do Mercado Pago | O webhook responde 503 e **nenhum pedido online sai de "aguardando pagamento"** | O cliente paga, o dinheiro sai da conta dele, e o pedido nunca chega ao painel |
| **3.2** Taxa de entrega por km da filial | Todo pedido de **entrega** é recusado | "Fora da área de entrega" para um endereço a 500 m |
| **3.1** Horários da filial | Todo pedido é recusado, **inclusive retirada** | A loja aparece fechada num horário em que está aberta |
| **5.2** Setor de impressão dos produtos | Sai **só a via do cliente**; a cozinha não recebe nada | Alguém no balcão leva o papel à mão para a praça, e ninguém reclama do sistema |
| **7** Comissão negociada | Todo pedido congela **10%** (`DEFAULT_PLATFORM_COMMISSION_PERCENT`) | O relatório fecha certo, com o percentual errado. Só aparece na hora de cobrar |

Os quatro primeiros são recuperáveis. **O quinto não**: a comissão é
congelada em cada pedido no momento da criação, então corrigir o percentual
depois **não corrige os pedidos que já passaram**.

---

## 1. Criar o restaurante e as filiais

🔧 **Só dá para fazer no banco.** Não há script e não há rota: nenhuma rota
`/admin` aceita `restaurant_id`, justamente porque o restaurante sai do token
— e não existe token antes de o restaurante existir. É o ovo e a galinha da
plataforma, e a saída é o editor de SQL do Supabase.

```sql
-- 1. o restaurante
insert into restaurants (name, slug, description, primary_color, secondary_color)
values ('Júnior da Picanha', 'junior-da-picanha', 'Carnes e porções',
        '#D95C04', '#111111')
returning id;

-- 2. a filial (uma por loja física)
insert into branches (
    restaurant_id, name, slug, address, neighborhood, city, state,
    latitude, longitude, is_main
) values (
    '<id-do-restaurante>', 'Matriz', 'matriz',
    'Av. Beira Mar, 1200', 'Meireles', 'Fortaleza', 'CE',
    -3.7250, -38.4900, true
) returning id;

-- 3. as configurações do restaurante
insert into restaurant_settings (restaurant_id, min_order_value, platform_commission_percent)
values ('<id-do-restaurante>', 0, 12.00);
```

**`slug` é a URL pública e não é editável pelo painel.** Ele entra em
`/restaurants/{slug}/...` e nos links que o lojista vai divulgar. Errar aqui
custa um `UPDATE` mais todos os links já impressos em cartão e adesivo.
Confira com o lojista antes de inserir.

**`latitude` e `longitude` são da FILIAL, e são a origem do cálculo de rota.**
Nulas, o sistema não quebra: ele geocodifica o texto do endereço no Google. O
problema é que um endereço mal escrito geocodifica para o lugar errado **sem
erro nenhum**, e a partir daí toda taxa de entrega da loja sai errada. Pegue
as coordenadas no Google Maps (clique com o botão direito no ponto → o
primeiro item do menu é o par) e insira explicitamente.

**`restaurant_settings` é opcional** — `_get_or_create_settings` cria a linha
na primeira leitura do painel. Mas criar à mão é o que permite gravar a
comissão junto (passo 7), e a linha criada sozinha nasce com **10%**.

Guarde os dois UUIDs. Os passos 2, 6 e 7 precisam deles.

---

## 2. Criar os usuários

São **dois**, com papéis diferentes, e a diferença importa.

### 2.1 O usuário do lojista 🔧

```bash
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Júnior" \
  --email junior@exemplo.com \
  --role owner
```

O `-it` é necessário: a senha é pedida em prompt oculto e **não** é argumento
de linha de comando — iria para o histórico do shell e para o `ps`.

`--role owner` para o dono. Sem `--branch-id`, ele enxerga todas as filiais.
Para gerente e atendente, ver a tabela de papéis em
[`autenticacao-e-escopo.md`](autenticacao-e-escopo.md#camada-3--por-papel).

**O que quebra se for pulado:** não há como entrar no painel, e os passos 3 a
5 são todos feitos por ele.

### 2.2 O usuário do agente de impressão 🔧

```bash
docker exec -it pedeaqui-api python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Impressora Balcão" \
  --email impressora.balcao@exemplo.com \
  --role print_agent \
  --branch-id <uuid-da-filial>
```

**Papel `print_agent`, nunca `owner` nem `attendant`.** Esta senha vai ficar
em **texto puro** no `config.ini` de um computador de balcão, ao alcance
físico de qualquer pessoa que trabalhe ali. Com `print_agent` ela compra
quatro rotas: o ticket do stream, o stream, as vias de um pedido e o
heartbeat. Com `attendant` — a recomendação antiga — comprava junto o preço
do cardápio, a lista de clientes com telefone e o faturamento do restaurante
inteiro.

`--branch-id` é obrigatório aqui, e o script recusa sem ele: o agente é de UMA
máquina, numa loja. Sem filial o heartbeat responde 400 e o painel nunca
mostra esse agente como online.

**Um usuário por máquina**, não por restaurante. Duas lojas com impressora =
dois usuários.

---

## 3. Configuração da loja 🏪

Tudo neste passo é tela do painel, feita pelo lojista (ou por você junto com
ele na primeira vez). É também onde estão **dois dos cinco silenciosos**.

### 3.1 Horários de funcionamento ⚠️

Painel → Configurações → Horários.
`PUT /admin/branches/{branch_id}/business-hours`

**Duas armadilhas na mesma tela:**

1. **O PUT substitui a semana inteira.** Não é merge e não é patch: o que não
   vier no corpo deixa de existir. Se a tela mandar só o dia editado, a loja
   fica fechada nos outros seis — sem erro, sem log, só nenhum pedido
   entrando.
2. **Dia sem faixa cadastrada = dia fechado**, e isso recusa **todo** pedido
   daquele dia, inclusive retirada. Não existe "a faixa mais parecida": ou a
   hora atual cai dentro de uma faixa, ou a loja está fechada.

Faixa que vira a noite (18:00–02:00) pertence ao dia em que **começa**. À 1h
da manhã de terça, quem está aberto é a faixa da segunda.

> **`weekday` é 0 = segunda** no backend (o `datetime.weekday()` do Python).
> O `getDay()` do JavaScript é 0 = domingo. Se a loja abrir no dia errado,
> este é o primeiro lugar para olhar.

### 3.2 Entrega por km ⚠️

Painel → Configurações → Filial.
`PATCH /admin/branches/{branch_id}`

```
delivery_base_fee         taxa fixa, cobrada em qualquer distância
delivery_fee_per_km       valor por quilômetro rodado
delivery_min_fee          piso (opcional)
delivery_max_fee          teto (opcional)
delivery_max_distance_km  além disto, fora da área
```

**`delivery_base_fee` e `delivery_fee_per_km` são obrigatórios na prática.**
Com qualquer um dos dois nulo, o cálculo devolve "sem taxa" e o pedido cai no
`default_delivery_fee` — o da filial se ela tiver, senão o padrão do
restaurante — que **só vale se for maior que zero**, e nasce zero. O resultado
é toda entrega recusada como "fora da área", num endereço a 500 metros.

Quem quer entrega grátis põe `delivery_base_fee = 0` **e**
`delivery_fee_per_km = 0`. Deixar nulo não é grátis, é quebrado.

### 3.3 Formas de pagamento

Painel → Configurações → Formas de pagamento.
`POST /admin/branches/{branch_id}/payment-methods`

São **por filial**. Os valores aceitos estão em `PAYMENT_METHODS`
(`src/core/constants.py`): `pix`, `credit_card`, `debit_card`, `cash`,
`voucher`, `meal_voucher`, `other`.

Cada método é ou **online** (cobrado pelo gateway) ou **na entrega**. Quando a
filial oferece o mesmo método nos dois fluxos — pix pelo gateway e pix na
maquininha —, vale **online**, o caminho restritivo.

**Sem nenhuma forma cadastrada, nenhum pedido fecha.** Este falha alto: o
cliente vê o erro na hora.

**Só cadastre método online depois do passo 6.** Método online com o Mercado
Pago ainda não configurado é o cenário do silencioso nº 1: o cliente paga e o
pedido não anda.

### 3.4 Taxas: o padrão da marca, e a filial que diverge

São **dois lugares** desde a revisão `20260818_0025`, e a diferença é o que
poupa trabalho na quinta loja. Detalhe completo em
[`operacao-por-filial.md`](operacao-por-filial.md).

**O padrão do restaurante.** Painel → Configurações.
`PATCH /admin/settings` — **somente o dono** desde a revisão `20260814_0020`.

```
min_order_value              pedido mínimo
service_fee_enabled/amount   taxa de serviço (padrão: R$ 0,99 ligada)
estimated_delivery_time_*    prazo exibido quando não há faixa de horário
default_delivery_fee         contingência: usada quando o Google cai
```

Nenhum pedido lê esses valores direto: a filial os herda nos campos que deixou
nulos. Configure aqui uma vez e as cinco lojas nascem certas.

**A sobrescrita da filial.** `PATCH /admin/branches/{branch_id}/settings`,
também **somente o dono**. Os mesmos cinco campos, valendo só naquela loja.
Mandar `null` explícito devolve o campo a herdar.

**A operação do dia é outra coisa, e não tem padrão.** `is_open` (o "fechar
agora"), `accepts_delivery` e `accepts_pickup` são de cada filial e nada mais:

```
PATCH /admin/branches/{branch_id}/store-status   {"is_open": false}      PESSOAS
PATCH /admin/branches/{branch_id}/order-types    {"accepts_pickup": false}  GERENCIA
```

Até essa revisão eles eram do restaurante, e fechar a loja do Centro fechava a
da Aldeota junto.

**`default_delivery_fee` não é a taxa do dia a dia.** É o valor usado quando a
regra por km não pode ser aplicada — Google fora do ar, ou filial sem base/por
km. Zero **desliga** essa contingência em vez de significar entrega grátis: o
contrário faria uma queda do Google virar frete grátis para a plataforma
inteira.

---

## 4. Identidade white-label

🔧 **Só dá para fazer no banco.** Não há rota de escrita para nada disto: o
único upload de imagem que existe é o de foto de produto.

```sql
update restaurants set
    logo_path       = 'junior-da-picanha/logo.png',
    cover_path      = 'junior-da-picanha/cover.jpg',
    primary_color   = '#D95C04',
    secondary_color = '#111111'
where slug = 'junior-da-picanha';
```

Os caminhos apontam para o bucket do Supabase Storage
(`SUPABASE_STORAGE_BUCKET`), que é **público para leitura**. Suba os arquivos
pelo painel do Supabase, na pasta do slug do restaurante.

Banners promocionais do cardápio ficam em `restaurant_banners`
(`banner_type`, `is_active`, ordem) e também são só `INSERT` à mão.

**O que quebra se for pulado:** nada funcional. O app do cliente mostra a
identidade padrão da plataforma. É o passo que dá para adiar sem risco — e o
único da lista com essa propriedade.

---

## 5. Cardápio 🏪

Painel → Cardápio. É o passo mais demorado e o único que o lojista consegue
tocar sozinho depois.

### 5.1 A ordem que funciona

1. **Categorias** (`POST /admin/categories`) — o slug da categoria também
   entra na URL pública e é derivado do nome **uma vez, na criação**.
   Renomear depois não muda o slug, de propósito: link divulgado não pode
   morrer.
2. **Produtos** (`POST /admin/products`) — **somente o dono**, porque `price`
   é obrigatório na criação e preço é decisão dele.
3. **Grupos de opções e opções** — "Ponto da carne", "Adicionais". Opção sem
   `additional_price` é da gerência; opção com valor é do dono.
4. **Fotos** (`POST /admin/products/{id}/image`) — JPEG, PNG ou WEBP. O tipo
   é conferido pelos **bytes**, não pelo que o navegador declara; SVG é
   recusado de propósito.

**Nada é apagado no cardápio, só desativado.** `order_items` referencia
`products` por FK, e um DELETE quebraria o histórico que o cliente ainda
consulta. O que o painel chama de "excluir" é `is_active = false`.

### 5.2 Setor de impressão de cada produto ⚠️

Painel → Configurações → Setores de impressão, depois Cardápio.

```
POST  /admin/branches/{branch_id}/printing-sectors     cria "Cozinha", "Bar", "Chapa"
PATCH /admin/categories/{category_id}/printing-sector  aponta a categoria INTEIRA
PATCH /admin/products/{product_id}/printing-sector     exceções, produto a produto
```

**Use a rota de categoria.** Produto a produto, um cardápio de 200 itens nunca
sai do lugar — e cardápio meio configurado imprime comanda pela metade.

**`printing_sector_id` nulo significa NÃO IMPRIMIR via de produção.** É uma
decisão legítima (a lata que sai da geladeira do balcão) e é indistinguível de
"esqueci de configurar". Um cardápio inteiro sem setor imprime só a via do
cliente, e a cozinha não recebe nada — sem erro em lugar nenhum.

Produto apontando para setor **desativado** não some: vai para a via
`"SEM SETOR"` e o caso é registrado no log. Setor de **outra filial** não é
mais possível — desde a revisão `20260820_0026` produto e setor vivem na mesma
loja, e a rota que tentasse cruzá-los responde 400.

**Cada filial tem o próprio cardápio e as próprias impressoras**, então esta
conferência é filial a filial. O `check_restaurant.py` já a faz assim: uma loja
bem configurada não apaga o erro da outra.

---

## 6. Mercado Pago

O passo com mais idas e vindas, porque **metade dele acontece na conta do
lojista** e a outra metade no servidor. A conta é dele: cada restaurante tem a
própria conta no Mercado Pago, e a cobrança sai em nome dela. Não existe
credencial global da plataforma.

### 6.1 🏪 O lojista cria a aplicação

No painel do Mercado Pago dele: **Suas integrações → Criar aplicação**. Ele
copia dois valores da aba de credenciais:

- `Public Key` — vai em texto puro no banco; o próprio Mercado Pago manda
  expor no frontend;
- `Access Token` — **é a credencial da conta dele**. Nunca por WhatsApp,
  nunca em e-mail, nunca em log.

Comece pelas credenciais de **teste**. As de produção só depois de a conta
dele ser aprovada.

### 6.2 🔧 Cadastrar a credencial

```bash
docker exec -it pedeaqui-api python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --public-key TEST-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

O script pede o `access_token` em prompt oculto e depois o `webhook_secret`,
que ainda não existe neste momento — **Enter em branco pula**, e é o certo
aqui. Ele volta no passo 6.4.

O token é cifrado com Fernet, chave em `PAYMENT_CREDENTIALS_ENCRYPTION_KEY`.
**Perder essa chave torna toda credencial cadastrada ilegível.**

### 6.3 🏪 Cadastrar a Notification URL

Ainda no painel dele: **a aplicação → Webhooks → Configurar notificações**.

```
https://api.pederapidex.com/payments/webhooks/mercadopago
```

Esta URL é a mesma para todos os restaurantes. Quem separa um do outro é o
`data.id` que vem no corpo da notificação: a API descobre por ele de qual
pedido — e portanto de qual restaurante — é o pagamento, e só então escolhe o
segredo para conferir a assinatura.

Eventos: **Pagamentos** (`payment`).

### 6.4 🔧 Gravar a Assinatura secreta ⚠️

Ao salvar a Notification URL, o Mercado Pago mostra uma **"Assinatura
secreta"**. Ela aparece **uma vez**. O lojista copia e manda para você.

```bash
docker exec -it pedeaqui-api python scripts/register_restaurant_payment_credential.py \
    --restaurant-slug junior-da-picanha \
    --environment test \
    --webhook-secret-only
```

**Este é o silencioso nº 1, e é o que já mordeu.** Enquanto
`webhook_secret_encrypted` for NULL, o webhook **daquele restaurante**
responde 503. Não há aviso de boot: o segredo é por restaurante e vive no
banco, então não há como saber no startup que "algum" restaurante está sem
cadastrar. O sintoma é o pior possível — o cliente paga, o dinheiro sai da
conta dele, e o pedido fica em "aguardando pagamento" para sempre.

Confira antes de sair:

```sql
select r.slug, c.environment, c.webhook_secret_encrypted is not null as tem_segredo
from restaurant_payment_credentials c
join restaurants r on r.id = c.restaurant_id
where r.slug = 'junior-da-picanha';
```

### 6.5 ⚠️ `MERCADOPAGO_ENVIRONMENT` é GLOBAL

Teste e produção **coexistem** no banco, uma linha para cada. Mas qual das
duas está ativa é decidido por uma variável de ambiente única para a
plataforma inteira:

```
MERCADOPAGO_ENVIRONMENT=test        # padrão
MERCADOPAGO_ENVIRONMENT=production
```

**Não é por restaurante.** Virar essa chave vira para **todo mundo ao mesmo
tempo**, e qualquer restaurante que ainda não tenha a credencial de produção
cadastrada para de cobrar na hora.

Antes de virar, confira que todos estão prontos:

```sql
select r.slug,
       bool_or(c.environment = 'production') as tem_producao,
       bool_or(c.environment = 'production' and c.webhook_secret_encrypted is not null)
         as producao_com_segredo
from restaurants r
left join restaurant_payment_credentials c on c.restaurant_id = r.id
where r.is_active
group by r.slug
order by 2, 1;
```

Restaurante novo entrando depois do go-live já nasce precisando de credencial
de **produção**, não de teste.

---

## 7. Comissão da plataforma ⚠️

🔧 **Só dá para fazer no banco, e é de propósito.**

```sql
update restaurant_settings
set platform_commission_percent = 12.00
where restaurant_id = '<id-do-restaurante>';
```

`platform_commission_percent` **não aparece em nenhum schema do painel, nem
para leitura**. Publicá-lo transformaria o percentual em campo de tela, e
editá-lo seria o lojista escolhendo quanto nos paga.

**Este é o passo irreversível.** O percentual é congelado em cada pedido no
momento da criação, e o relatório de comissão só mostra o valor já congelado.
Corrigir depois **não corrige o que já passou** — os pedidos daquele período
ficam com o percentual errado para sempre, e a única saída é ajuste manual
fora do sistema.

Sem a linha preenchida, vale `DEFAULT_PLATFORM_COMMISSION_PERCENT` = **10%**.
Se o contrato negociado foi 10%, não há problema. Se foi qualquer outro
número, **faça este passo antes do primeiro pedido**.

---

## 8. Instalar o programa de impressão 🔧

Presencial, na máquina do balcão. O roteiro completo, com fotos do que a
pessoa vê, está em [`../print-agent/INSTALACAO.md`](../print-agent/INSTALACAO.md).

Resumo:

1. Gerar o pacote na sua máquina (`print-agent/build.bat`) e levar a pasta
   `dist/pendrive` inteira num pendrive.
2. Rodar `instalar.bat`. Não pede administrador: instala em `%LOCALAPPDATA%` e
   cria o atalho na Inicialização do próprio usuário.
3. Preencher o `config.ini` com `email`, `password` (o usuário `print_agent` do
   passo 2.2) e `printer` — o nome **exato** da impressora como o Windows a
   mostra.
4. Conferir: o ícone na bandeja fica **verde** e aparece o balão "Conectado".

**Use `email` + `password`, nunca `token`.** O token de acesso vale 12 horas:
um agente instalado com token fixo para de imprimir na manhã seguinte, e
ninguém liga o defeito à expiração.

**O nome da impressora tem que casar byte a byte.** Um espaço a mais e a via
não sai. Depois de o agente conectar uma vez, ele reporta as impressoras da
máquina e o painel passa a oferecer uma **lista** em vez de campo de texto —
use a lista.

**Antivírus:** o executável não tem janela, o que aumenta o falso positivo, e
o Defender já apagou uma versão anterior deste programa. Se ele sumir, o
`instalar.bat` avisa em vez de falhar em silêncio.

---

## 9. Teste de aceitação

Não considere o restaurante no ar antes destes seis. Faça **nesta ordem** —
cada um depende do anterior.

| # | O que fazer | O que provar |
|---|---|---|
| 1 | Abrir o cardápio público em `/restaurants/junior-da-picanha` | Categorias, produtos, preços e a identidade visual |
| 2 | Simular entrega para um endereço real perto da loja | A taxa por km sai, e não "fora da área" (passo 3.2) |
| 3 | Simular entrega para um endereço **fora** do raio | Recusa com a mensagem de fora de área — o limite existe |
| 4 | Fechar um pedido de verdade **pagando com pix online** | O QR aparece; o pedido nasce em "aguardando pagamento" |
| 5 | **Pagar de verdade** (pode ser R$ 1,00) | O pedido vira `paid` **sozinho**, sem ninguém tocar em nada. É o único teste do webhook (passo 6.4) |
| 6 | Aceitar o pedido no painel | O papel sai: uma via por praça **e** a do cliente, e o computador apita |

O passo 5 é o que não dá para pular nem simular. Ele é a única prova de que a
Notification URL e a Assinatura secreta estão certas, e é justamente o que
falha em silêncio.

Depois: cancele o pedido de teste no painel e **estorne no painel do Mercado
Pago do lojista**. Cancelar no nosso painel **não** estorna — o estorno só
acontece quando o gateway avisa. Fica só uma linha no log:

```
[Pagamento] pedido pago foi cancelled sem estorno automatico order_id=...
```

---

## O que hoje é manual e deveria virar tela

Em ordem de quanto dói. Os quatro primeiros são o motivo de este documento
existir: **um restaurante novo hoje exige acesso ao banco de produção e ao
shell do container**, e isso não escala nem delega.

### 1. Criar restaurante e filial

Hoje: `INSERT` no Supabase. É o passo 1, o único obrigatoriamente primeiro, e
o que mais assusta — um `INSERT` errado em produção, à mão, sem validação
nenhuma. `slug` duplicado, `state` fora do padrão e coordenada trocada de
sinal passam todos.

Deveria ser: tela de plataforma (não do lojista), com validação de slug e
busca de endereço com mapa para as coordenadas.

### 2. Cadastrar a credencial e o segredo do Mercado Pago

Hoje: dois `docker exec -it` com prompt oculto, e o segredo do webhook
viajando por WhatsApp entre o lojista e você.

Deveria ser: tela onde **o próprio lojista** cola o access token e a
assinatura secreta, com um botão "testar conexão" que faz uma chamada real ao
gateway. Isso tira você do meio da posse de uma credencial que não é sua, e
mata o silencioso nº 1 — a tela mostraria "webhook não configurado" em
vermelho.

### 3. Comissão da plataforma

Hoje: `UPDATE` no banco, e é o único passo irreversível da lista.

Deveria ser: tela de plataforma, invisível ao lojista (`platform_commission_percent`
continua fora do OpenAPI do painel), com histórico de quem mudou e quando —
porque o valor congela em cada pedido e uma mudança no dia errado é uma
discussão de fatura.

### 4. Criar usuário do painel

Hoje: `docker exec -it` para cada usuário, inclusive para trocar senha.

Deveria ser: tela de usuários no painel do lojista, onde o dono cria os
próprios gerentes e atendentes. Com os papéis já implementados, ela virou
possível — falta só a tela. O `print_agent` **não** deve aparecer nesse
seletor: é conta de máquina.

### 5. Identidade white-label

Hoje: `UPDATE` mais upload manual no Supabase Storage; banners só por
`INSERT`.

Deveria ser: tela de marca com upload de logo e capa (a rota de upload de
imagem de produto já resolveu o problema difícil — detectar o tipo pelos
bytes) e seletor de cor.

### 6. Um checklist de "este restaurante está pronto?" — meio feito

**O comando existe:**

```bash
docker exec pedeaqui-api python scripts/check_restaurant.py junior-da-picanha
docker exec pedeaqui-api python scripts/check_restaurant.py --todos
```

Ele cobre **os cinco silenciosos do topo deste documento** — horários (3.1),
taxa por km (3.2), setor de impressão (5.2), segredo do webhook (6.4) e
comissão (7) — e sai com código 1 quando algum deles está em ERRO, para
servir de porta num roteiro de instalação. Só leitura: nenhum `UPDATE`,
nenhuma migração.

Ele **não** cobre o resto do que esta seção pedia: coordenada da filial,
presença de forma de pagamento e heartbeat do agente de impressão. Os três
falham alto — o cliente ou o lojista vê o erro na hora —, e por isso ficaram
de fora da primeira versão. Entram quando alguém sentir falta.

Continua faltando a **tela**. O comando exige shell no container, então quem
instala ainda precisa ser você.

---

# FLUXOS REAIS

Esta parte é para **explicar ao lojista**, sem falar em API, rota ou banco.
Dá para ler em voz alta.

## A jornada do cliente, do cardápio ao pedido entregue

**1. Ele abre o cardápio.** Pelo link da loja ou pelo app. Vê as fotos, os
preços e os adicionais como você cadastrou. Se a loja estiver fechada — pelo
horário ou porque alguém apertou "fechar agora" —, ele vê isso antes de
montar o carrinho.

**2. Ele monta o pedido.** Escolhe os itens, o ponto da carne, os adicionais.
O sistema soma tudo do lado dele só para mostrar; **quem calcula de verdade é
o servidor**, na hora de fechar. Isso é proposital: se alguém adulterar o
preço na tela, o pedido chega ao seu painel com o valor certo, o do seu
cardápio.

**3. Ele informa o endereço.** O sistema mede a distância real de carro entre
a sua loja e a casa dele — não em linha reta — e calcula a taxa: o valor fixo
que você definiu mais o valor por quilômetro. Se passar do raio que você
configurou, ele vê "fora da área de entrega" e pode escolher retirada.

**4. Ele escolhe como pagar.** Só aparecem as formas que **aquela loja**
aceita. Se for pagar na entrega, o pedido já cai no seu painel. Se for pagar
agora, o caminho é o da próxima seção.

**5. O pedido aparece no seu painel** com som e na coluna "Novos". Você aceita
ou recusa. **Aceitar é o que manda a comanda para a impressora** — e é aí que
o pedido vira comida.

**6. Você vai mudando o status**: em preparo, saiu para entrega, entregue. O
cliente acompanha por um link privado que ele recebeu ao fazer o pedido —
ninguém consegue ver o pedido de outra pessoa mudando o número no endereço,
porque o link tem um código próprio, sorteado, que não dá para adivinhar.

**Se ele fechou o pedido sem criar conta**, esse link é a única forma de
acompanhar, e ele fica salvo no navegador dele. Não existe "me manda o link de
novo": nem nós conseguimos recuperá-lo — foi feito assim para que ninguém
consiga pedir o link do pedido de outra pessoa.

## O caminho do dinheiro, quando o pagamento é online

**1. O cliente pede para pagar.** O sistema cria a cobrança **na sua conta do
Mercado Pago** — não numa conta nossa. O dinheiro vai direto para você.

**2. Aparece o QR code do pix** na tela dele. O pedido fica marcado como
"aguardando pagamento" no seu painel, e **não pode ser aceito ainda**. Isso
protege você: a comanda não vai para a cozinha antes de o dinheiro entrar.

**3. Ele paga.** O Mercado Pago avisa o nosso sistema em segundos, sozinho —
ninguém precisa apertar nada. O pedido muda para "pago" e o botão de aceitar
libera.

> **É este aviso automático que precisa estar configurado na instalação.**
> Sem ele o dinheiro entra na sua conta normalmente, mas o pedido fica parado
> em "aguardando pagamento" e você nunca fica sabendo. É o erro mais caro da
> instalação, e o mais silencioso.

**4. Se ele desistir ou o pix vencer**, o pedido continua lá e ele pode tentar
pagar de novo, no mesmo pedido. Um segundo clique em "pagar" devolve o
**mesmo** QR code, não um novo — assim ninguém paga duas vezes por engano.

**5. Cancelar um pedido já pago NÃO devolve o dinheiro automaticamente.** Essa
parte é sua, no painel do Mercado Pago. O sistema registra que aconteceu, mas
não estorna sozinho — devolver dinheiro de cliente é decisão de gente.

**6. A comissão da plataforma** é calculada e **congelada** no momento em que
o pedido é criado, sobre o valor dos produtos (sem a taxa de entrega e sem
descontos). Congelada significa que ela não muda depois, nem se o percentual
do contrato mudar. O relatório mostra pedido a pedido.

Pedido cancelado, recusado ou estornado **não** entra na comissão.

## O caminho da comanda, do aceite ao papel

**1. Você aperta "aceitar" no painel.**

**2. O programa de impressão fica sabendo na hora.** Ele é aquele círculo
colorido ao lado do relógio, no computador do balcão, e está sempre ligado
ouvindo — não é o painel que imprime, e por isso a comanda sai mesmo que
ninguém tenha o painel aberto.

**3. O servidor monta as vias já prontas**, do jeito certo para a largura da
bobina. O programa do balcão não decide nada sobre o conteúdo: ele só entrega
à impressora o que recebeu pronto. É por isso que dá para corrigir o formato
da comanda de todas as lojas de uma vez, sem visitar nenhuma.

**4. Sai uma via por praça** — cozinha, bar, chapa — **contendo só os itens
daquela praça**, mais a via do cliente com o pedido completo e o valor. Quem
decide para onde vai cada item é o setor que você configurou no cardápio.

**5. O computador apita** quando a comanda termina de sair.

**Item sem setor configurado não é impresso na produção**, e isso é uma opção
válida — a lata de refrigerante que sai da geladeira do balcão não precisa de
comanda na cozinha. Mas se o cardápio **inteiro** estiver sem setor, sai só a
via do cliente e a cozinha não recebe nada. Vale conferir no primeiro dia.

**Se a impressora estiver sem papel ou desligada**, o programa tenta de novo
algumas vezes e depois **mostra um aviso na tela** dizendo qual impressora
falhou. O pedido continua na fila: quando o problema for resolvido e o
programa reconectar, ele tenta de novo.

**Se a internet cair**, o programa fica amarelo e volta sozinho quando ela
voltar — e **imprime os pedidos que chegaram durante a queda**. Ele lembra o
que já imprimiu, então nada sai duas vezes.

**Pedido com pagamento online ainda não confirmado não gera comanda de
produção.** A via do cliente sai; a da cozinha não. É a mesma proteção do
painel, do lado do papel: sem isso a praça prepararia o pedido de qualquer
jeito e a regra valeria só para quem olha a tela.
