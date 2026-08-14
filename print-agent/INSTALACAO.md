# Rapidex Impressão — instalação no computador da loja

Guia para instalar o programa que imprime as comandas. Não precisa saber
programar. Leva uns 5 minutos.

O que você precisa ter em mãos antes de começar:

- o **pendrive** com a pasta do programa;
- o **e-mail e a senha** do usuário do painel criado para esta impressora
  (peça a quem cuida do sistema — não é o login do dono da loja);
- a **impressora térmica ligada** no computador e já funcionando no Windows.

---

## 1. Liberar a pasta no antivírus — ANTES de copiar o programa

⚠️ **Este item vem primeiro e não pode ser pulado.**

O Windows Defender costuma apagar o programa na hora em que ele é copiado,
porque é um arquivo novo que ele nunca viu e que não tem assinatura digital
paga. **O programa não tem vírus.** Mas se você copiar primeiro e liberar
depois, o arquivo já foi apagado e o instalador falha dizendo que não
encontrou o `RapidexImpressao.exe`.

Liberando a pasta antes, isso não chega a acontecer.

### Passo A — criar a pasta que vai ser liberada

1. Tecla Windows + R, digite o texto abaixo e dê Enter:

   ```
   %LOCALAPPDATA%
   ```

2. Na janela que abrir, clique com o botão direito num espaço vazio →
   **Novo** → **Pasta**.
3. Dê à pasta exatamente este nome, com o espaço e sem acento:

   ```
   Rapidex Impressao
   ```

É nessa pasta que o instalador vai pôr o programa. Ela precisa existir agora
só para você conseguir apontar o antivírus para ela.

### Passo B — adicionar a exclusão

1. Tecla Windows, digite `Segurança do Windows` e abra.
2. **Proteção contra vírus e ameaças**.
3. Em *Configurações de proteção contra vírus e ameaças*, clique em
   **Gerenciar configurações**.
4. Role até **Exclusões** → **Adicionar ou remover exclusões**.
5. **Adicionar uma exclusão** → **Pasta**.
6. Escolha a pasta que você criou no passo A:

   ```
   %LOCALAPPDATA%\Rapidex Impressao
   ```

Se o antivírus da loja for outro (Avast, AVG, McAfee, Kaspersky), o caminho é
o mesmo: procure por *Exclusões*, *Exceções* ou *Lista branca* e adicione essa
pasta. Se a loja tiver **dois** antivírus, faça nos dois.

---

## 2. Instalar

1. Espete o pendrive.
2. Abra a pasta do pendrive.
3. Dê **dois cliques** em `instalar.bat`.
4. Espere as quatro etapas terminarem e aperte uma tecla quando pedir.

O instalador copia o programa para dentro do computador, faz ele abrir
sozinho toda vez que o Windows ligar, e abre o programa.

### Se aparecer a tela azul "O Windows protegeu o seu computador"

É normal, e é diferente do antivírus: aqui ele só está pedindo confirmação,
não apagando nada.

1. Clique em **Mais informações** (o link pequeno, no meio da tela).
2. Aparece um botão **Executar assim mesmo**. Clique nele.

Acontece uma vez só.

### Se o instalador disser que não encontrou o `RapidexImpressao.exe`

O antivírus apagou o arquivo antes de você chegar aqui. Volte ao **item 1**,
confirme que a exclusão está mesmo na lista, copie de novo a pasta do
pendrive e rode o `instalar.bat` outra vez.

---

## 3. Preencher a configuração

Na primeira vez, o programa abre e diz que precisa de configuração. É o
esperado.

1. Aperte a tecla Windows + R, digite o texto abaixo e dê Enter:

   ```
   %LOCALAPPDATA%\Rapidex Impressao
   ```

2. Dê dois cliques no arquivo `config.ini`. Ele abre no Bloco de Notas.

3. Preencha as três linhas:

   ```
   email = coloque-aqui-o-email
   password = coloque-aqui-a-senha
   printer = coloque-aqui-o-nome-da-impressora
   ```

4. **Salve** (Ctrl + S) e feche o Bloco de Notas.

### Como descobrir o nome exato da impressora

O nome tem que ser **idêntico** ao que o Windows mostra, com maiúsculas,
espaços e tudo:

1. Tecla Windows + R, digite `control printers` e dê Enter.
2. Ache a impressora térmica na lista.
3. Clique nela com o botão direito → **Propriedades da impressora**.
4. O nome está no topo da janela. Copie e cole no `config.ini`.

---

## 4. Abrir o programa

Dê dois cliques em:

```
%LOCALAPPDATA%\Rapidex Impressao\RapidexImpressao.exe
```

**O programa não abre janela nenhuma.** Ele fica como um **círculo colorido
na bandeja**, ao lado do relógio, no canto inferior direito da tela.

> Nessa pasta existe uma subpasta chamada `_internal`. Ela é parte do
> programa: se alguém apagar, ele para de abrir. Não mexa nela.

### Achar o ícone (faça isso uma vez)

O Windows **esconde ícones novos** por padrão. Se não estiver à vista:

1. Clique na **setinha `^`** logo à esquerda do relógio.
2. O círculo do Rapidex está lá dentro.
3. **Arraste o círculo para fora**, para junto do relógio. Ele fica fixo ali
   para sempre e todo mundo passa a ver o estado sem clicar em nada.

### O que confirma que funcionou

Alguns segundos depois de abrir, aparece um **balão** dizendo
*"Conectado. As comandas vão sair sozinhas."*, e o círculo fica **verde**.

A cor é o estado, e ela é visível do outro lado do balcão:

| Cor | O que significa | O que fazer |
|---|---|---|
| 🟢 **Verde** | Conectado. Pedido aceito vira comanda. | Nada. |
| 🟡 **Amarelo** | Sem conexão; tentando sozinho. | Se ficar amarelo mais de 2 minutos, confira a internet. |
| 🔴 **Vermelho** | Parado. Não volta sozinho. | Uma caixa de aviso já explicou o motivo. Item 7. |

Passar o mouse por cima do ícone mostra o mesmo em texto.

---

## 5. Testar antes de ir embora

Faça um pedido de teste e aceite no painel.

- O papel sai na impressora.
- O computador **apita três vezes** quando a comanda termina de sair.

Esses dois juntos são a prova de que a instalação está pronta. (Se o apito
incomodar, dá para desligar: `sound = nao` na seção `[agent]` do
`config.ini`.)

---

## 6. Depois de reiniciar o computador

O programa abre sozinho. Não precisa fazer nada.

Para conferir: reinicie o computador, espere a área de trabalho carregar e
veja se o círculo verde voltou para a bandeja. Pode demorar alguns segundos.

---

## 7. Não está imprimindo — o que fazer

Faça nesta ordem. Pare quando resolver.

1. **O círculo está na bandeja?** Clique na setinha `^` ao lado do relógio
   para conferir. Se não estiver em lugar nenhum, o programa foi fechado: dê
   dois cliques no `RapidexImpressao.exe` (item 4).

2. **De que cor está o círculo?**
   - 🔴 **Vermelho** → o e-mail ou a senha do `config.ini` estão errados.
     Corrija (item 3) e abra o programa de novo.
   - 🟡 **Amarelo** que não fica verde → a internet está fora, ou o servidor
     está fora. Teste abrir um site qualquer no navegador.
   - 🟢 **Verde** e mesmo assim não imprime → siga para o item 3.

3. **A impressora está ligada, com papel e sem luz vermelha piscando?**
   Quando uma via não sai, o programa mostra uma **caixa de aviso** dizendo
   qual impressora falhou. Ela aparece uma vez por impressora: se você
   fechou sem ler, o motivo está no registro (mais abaixo).

4. **O nome da impressora no `config.ini` está exatamente igual ao do
   Windows?** Um espaço a mais já quebra. Confira pelo item 3.

> **Para fechar o programa de propósito**, clique com o botão **direito** no
> círculo → **Sair**. Não existe outra forma, e não deveria: fechar é parar
> de imprimir.

5. **Ainda não resolveu:** mande o arquivo de registro para quem cuida do
   sistema.

### Como mandar o registro (log)

1. Clique com o botão **direito** no círculo da bandeja → **Abrir a pasta do
   registro**. A pasta certa abre sozinha.

   (Se o programa nem estiver aberto: tecla Windows + R, digite
   `%LOCALAPPDATA%\Rapidex Impressao` e dê Enter.)

2. Ache o arquivo **`rapidex-impressao.log`**.
3. Mande esse arquivo por WhatsApp ou e-mail.

Esse arquivo tem o histórico do que aconteceu: quando conectou, quando caiu,
quais pedidos chegaram e quais imprimiram. **Não tem senha nem dado de
cliente dentro** — pode mandar sem preocupação.

---

## 8. Desinstalar

Dê dois cliques em `desinstalar.bat` (está no pendrive).

Ele fecha o programa, tira da inicialização automática e apaga a pasta.

⚠️ **Apaga também o registro e a configuração.** Se quiser guardar o
`rapidex-impressao.log`, copie antes — o desinstalador mostra o caminho e
espera você apertar uma tecla antes de apagar qualquer coisa.

---

## 9. Loja com mais de uma impressora

O padrão manda tudo para uma impressora só. Se a loja tem uma impressora por
praça (cozinha, bar, chapa), abra o `config.ini` e troque a linha `printer`
por uma seção assim:

```ini
[printers]
via do cliente = EPSON TM-T20 Caixa
cozinha = EPSON TM-T20 Cozinha
bar = EPSON TM-T20 Bar
sem setor = EPSON TM-T20 Cozinha
default = EPSON TM-T20 Cozinha
```

À esquerda vai o nome do setor **como está cadastrado no painel**
(Configurações → Setores de impressão); à direita, o nome da impressora no
Windows. Acento e maiúscula não importam do lado esquerdo.

A linha `default` é importante: é para onde vai qualquer setor que alguém
criar no painel depois da instalação. Sem ela, esse setor não imprime.

O arquivo `config.ini.example`, na pasta do projeto, tem todas as opções
avançadas comentadas (tipo de acentuação da impressora, tempo entre
tentativas, corte de papel).
