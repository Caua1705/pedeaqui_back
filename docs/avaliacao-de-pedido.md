# Avaliação de pedido

Implementado em 20/08/2026.

Atraso e pedido errado são a reclamação nº 1 do consumidor de delivery, e até
esta frente existir o restaurante só descobria quando o cliente reclamava em
outro lugar. A avaliação é o único canal de retorno **direto**.

---

## 1. O que o cliente avalia

**Uma nota geral 1–5 (obrigatória), comentário livre opcional (500 chars) e
uma etiqueta de problema que só aparece com nota ≤ 3.**

### Por que não há nota separada de comida, entrega e embalagem

O argumento de taxa de resposta é real, mas não é o decisivo. O decisivo é que
**o formulário teria que mudar de forma por `order_type`**: pedido de
`pickup` não tem entrega, e uma nota de entrega nula ficaria indistinguível de
"não respondeu". A média por dimensão passaria a depender do mix de retirada
da loja — defeito que só aparece no relatório, meses depois, e que ninguém
consegue explicar olhando a tela.

### Por que existe a etiqueta de problema

Uma nota geral diz que algo está errado e não diz o quê. Com "3 estrelas" no
painel, ninguém conserta nada.

A etiqueta é lista **fechada** (`REVIEW_PROBLEM_TAGS`) e não texto livre
porque o painel a **agrega**: *"7 das 12 notas baixas desta semana foram
atraso"* é a frase que faz alguém mexer na operação. Texto livre não soma.

Ela custa **zero** para quem dá 5 estrelas — que é a maioria das respostas —
porque só é perguntada quando a nota é baixa. Mandar `problem_tag` com nota 4
ou 5 responde **422**: um 5 estrelas com "atrasou" não é opinião complexa, é
front mandando campo que a tela não devia ter mostrado, e aceitar isso
envenena o agregado que é a única razão de o campo existir.

`outro` existe para a etiqueta que falta não virar nota baixa sem motivo — e a
frequência dele é o sinal de que a lista precisa crescer.

---

## 2. Como o cliente chega

`PUT /restaurants/{slug}/orders/track/{tracking_token}/review`

O caminho é a **tela de acompanhamento que já existe**. O cliente já tem o
link (app, ou `localStorage` no pedido de convidado), a tela já consulta o
pedido, e quando o status vira `completed` ela mostra o formulário. Nenhum
canal novo, nenhuma entrega nova, e o token que autentica é o mesmo que já
autentica aquela tela.

**`PUT` e não `POST`**, e a escolha é o contrato: um pedido tem no máximo uma
avaliação (`uq_order_reviews_order_id`), e mandar de novo **troca** a que
estava lá. Quem apertou uma estrela por engano manda de novo; quem tem rede
ruim e reenviou não cria duas notas. É o mesmo desenho de
`AIFeedbackRepository.create`, que troca o voto em vez de duplicar.

---

## 3. Quem pode avaliar, e quando

| Regra | Decisão |
|---|---|
| Credencial | O `tracking_token`, comparado por hash — 256 bits, não enumerável, sem rota de reemissão (armadilha 19) |
| Status | Só `completed`. 409 nos outros |
| `cancelled` / `rejected` | **Não avaliáveis** |
| Janela | 14 dias **contados da entrega** |
| Quantidade | Uma por pedido, editável enquanto a janela estiver aberta |
| Token inválido | 404, nunca 403 |

### Por que `cancelled` e `rejected` ficam de fora

Não houve entrega. A nota de um pedido que nunca saiu entraria na média do
restaurante medindo outra coisa, e reclamação de cancelamento é assunto de
outro canal.

### Por que a janela conta da ENTREGA e não da criação do pedido

Um pedido esquecido em `out_for_delivery` por três semanas, e só então marcado
`completed`, teria a janela **já fechada no instante em que ela deveria
abrir** — e o cliente veria "prazo encerrado" para um pedido que acabou de
chegar, sem nada explicando.

`orders` não tem coluna de conclusão; o marco sai de `order_status_history`,
que os loaders do pedido já trazem, então não custa consulta. **Pedido
`completed` sem marco no histórico** (migrado, ou marcado por um caminho que
não gravou histórico) **deixa avaliar**, com warning no log: o custo desse
erro é uma nota atrasada, e o outro lado é recusar a avaliação de um pedido
legítimo por causa de uma linha que faltou.

### Avaliação em massa, sem exigir login

1. **O token** é a defesa principal, e já estava construída. Quem não fez o
   pedido não tem como chegar.
2. **Rate limit por IP**: `10/minute;60/hour`. Mais apertado que os
   `30/minute` da consulta com o mesmo token, **porque esta rota escreve**.
3. **`uq_order_reviews_order_id`** fecha inflar a mesma nota.

**Efeito de graça do hash:** o lojista **não consegue** avaliar os próprios
pedidos pelo painel — o banco só tem o hash e o painel não o expõe.

**O que isto NÃO fecha:** o lojista que faz pedidos de verdade pelo app e se
dá 5 estrelas. Fraude de nota não se resolve sem verificação de identidade, e
é o custo de a rota ser pública. Fica registrado.

---

## 4. Quem vê

`GET /admin/reviews`, com `start_date`, `end_date`, `branch_id`, `max_rating`.

**Papel `GERENCIA`, e não `SOMENTE_DONO`.** A divisão em `admin_reports` é
"dinheiro do restaurante inteiro é do dono"; avaliação não diz quanto entrou.
Quem conserta atraso e pedido errado é quem toca a loja, e nota que só o dono
vê não vira conserto no balcão.

O recorte por filial sai de `orders` pelo `JOIN` — `order_reviews` não tem
`branch_id`. Por isso o filtro mora numa função só (`_scope`): uma consulta
daquele arquivo que esquecesse o `JOIN` devolveria as avaliações de todos os
restaurantes.

### Duas propriedades do agregado que não são óbvias

**Total e média saem do histograma**, não de um `COUNT`/`AVG` paralelo. Com
consultas separadas, bastaria um `WHERE` divergir para a tela mostrar média
4,2 sobre barras que somam 4,6 — e não há como depurar isso olhando o painel.

**`max_rating` não entra no agregado.** Se entrasse, o lojista que clica em
"só notas baixas" veria a média desabar e concluiria que a semana piorou,
quando só apertou um filtro de lista.

Período sem avaliação devolve média `None` e não `0.0`: média zero se lê como
"todo mundo odiou", que é o oposto de "ninguém avaliou".

A resposta leva `order_number` e **não leva nome nem telefone** — os dois já
estão em `GET /admin/orders/{id}`, e repetir dado pessoal numa segunda tela é
superfície a mais sem leitor novo.

---

## 5. LGPD

O comentário é o único campo livre, e tem **duas defesas, porque nenhuma cobre
sozinha.**

**(a) Exclusão de conta.** `CustomerAnonymizationService._clear_review_comments`
apaga o comentário das avaliações dos pedidos da pessoa, exatamente como já faz
com `order.notes`. Isto foi possível porque a avaliação chega por
`orders.customer_id` — que é justamente o que o `ai_feedback` **não** tinha, e
o motivo de lá a saída ter sido só retenção.

**(b) Retenção de 12 meses**, no container `limpeza`. Alcança o que a exclusão
nunca vai alcançar: o **pedido de convidado**, com `customer_id` nulo, cujo
texto não pende de conta nenhuma.

**A nota fica nas duas.** É número, não identifica ninguém e é o histórico de
qualidade do restaurante: apagá-la reescreveria a média do lojista a cada
exclusão de conta e todo mês no expurgo.

Por isso este é o **único dos cinco expurgos que faz `UPDATE` e não
`DELETE`**. E o `WHERE comment IS NOT NULL` existe para o número relatado não
mentir, reescrevendo `NULL` por `NULL` toda noite.

**12 meses e não os 90 dias do `ai_feedback`:** lá o texto não tem nenhuma
rota que o leia, então encurtar era de graça; aqui ele tem leitor de verdade —
o lojista, na aba de avaliações — e um ano cobre a sazonalidade inteira.

**Exportação:** a avaliação entra em `GET /me/export` como quinto bloco. É
dado da pessoa, e sem ela o direito de acesso ficaria incompleto justamente no
campo que a exclusão depois apaga.

**Log:** a nota vai, o comentário não. Mesma regra da mensagem de chat.

---

## 6. O QR na comanda — PENDENTE, e a ordem importa

Era a ideia inicial: o papel já sai na cozinha e vai junto com a entrega, então
um QR no rodapé levaria o cliente direto à avaliação. **Ficou de fora, e a
ordem de fazer é esta:**

> **1º — atualização remota do print agent. 2º — o QR.**
>
> Nunca o contrário.

### Por que

**O agente não suporta QR.** `escpos.py` implementa cinco comandos — `ESC @`,
`ESC t n`, `GS ! n`, `ESC d n`, `GS V`. O comando de QR é `GS ( k`, e
escrevê-lo é meia hora. **O custo não está aí.**

1. **O contrato de impressão é texto.** `PrintJobResponse` leva `content: str`
   e `font_size`, e o agente *não interpreta o `content`* — é o princípio
   declarado do módulo. QR não é texto: exigiria campo novo que o agente
   traduza.
2. **O agente é um `.exe` instalado à mão em cada balcão**, com exclusão de
   antivírus, instalador e ícone de bandeja — e `INSTALACAO.md` **não descreve
   nenhum mecanismo de atualização**. Cada loja vira uma visita.
3. O próprio `print_layout.py` diz por que isso pesa: *"corrigir um bug de
   layout viraria uma operação de campo em vez de um deploy"*. Um QR com URL
   errada só se descobre com o papel na mão.
4. **Térmica barata que não implementa `GS ( k` imprime os bytes como lixo** —
   rodapé com caracteres aleatórios, sem erro em lugar nenhum. Mesma família da
   armadilha 28.
5. Falta a URL pública do app do cliente em `config.py`. É variável nova mais
   startup check.

### As duas saídas que parecem mais baratas, e não são

**Imprimir o link como texto.** O `tracking_token` é `token_urlsafe(32)` — **43
caracteres**. Ninguém digita isso.

**Imprimir `#5471` + um PIN de 4 dígitos.** `order_number` é sequence **global
e previsível**, e parear número previsível com segredo fraco é literalmente o
buraco que a armadilha 19 fechou removendo `GET /orders/{n}?phone=`. **Não
repetir.**

### A conclusão

O caminho impresso **precisa** de QR; o QR precisa de atualização do agente; e
a atualização do agente não existe. Fazer o QR primeiro significa aprender o
processo de atualização de campo **e** estrear uma feature ao mesmo tempo —
dois riscos de uma vez, num computador de balcão que ninguém alcança
remotamente.

---

## 7. O que ficou de fora, de propósito

| Fora | Por quê |
|---|---|
| Resposta do lojista à avaliação | Vira caixa de mensagens: moderação, notificação, segundo canal de suporte |
| Nota pública no cardápio | Muda o produto. Cria incentivo para fraude de nota, exige moderação, vira ranking. Isto aqui é retorno **para o lojista**, não vitrine |
| Avaliação por item | Multiplica o formulário pelo nº de itens e mata a taxa de resposta |
| Nota de entregador | **Não existe entregador no modelo de dados.** Inventar a entidade para pendurar uma nota é a ordem errada |
| E-mail/push "avalie seu pedido" | Cota do Resend, precisa respeitar `marketing_opt_in`, e é o caminho mais curto para virar spam. Depois de medir a taxa de resposta do caminho que já existe |
| Moderação / filtro de conteúdo | Só o lojista lê. Problema que ainda não temos |
| Editar a nota depois da janela | A média do lojista mudaria retroativamente e nada no painel explicaria por quê |

---

## 8. Resíduo conhecido

**`_period_bounds` agora tem três cópias** no projeto —
`admin_report_service`, `admin_order_service` e `admin_review_service`. Não é
descuido: as três têm assinaturas e validações diferentes (aqui as datas são
obrigatórias e há teto de período; no pedido são opcionais e há 400 próprio), e
uma função única com flag para cobrir os três casos é a abstração esperta que
este projeto recusa. Se um dia as três convergirem, a extração vira uma linha.
