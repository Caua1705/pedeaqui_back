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

Vai abrir uma janela preta com texto. **Essa janela tem que ficar aberta** —
é ela que imprime as comandas. Pode minimizar; não pode fechar.

> Nessa pasta existe uma subpasta chamada `_internal`. Ela é parte do
> programa: se alguém apagar, ele para de abrir. Não mexa nela.

Quando estiver tudo certo, você vai ver na janela algo como:

```
Rapidex Impressao 1.0.0
Servidor: https://api.pederapidex.com
Demais setores imprimem em 'EPSON TM-T20' (impressora padrao)
conectando ao servidor
conectado. Aguardando pedidos.
```

A linha **`conectado. Aguardando pedidos.`** é a que confirma que funcionou.

---

## 5. Testar antes de ir embora

Faça um pedido de teste e aceite no painel. Na janela do programa deve
aparecer:

```
pedido #5471 recebido, buscando as vias para imprimir
pedido #5471: via 'Via do cliente' impressa em 'EPSON TM-T20'
pedido #5471 impresso por completo (2 vias)
```

E o papel sai na impressora.

---

## 6. Depois de reiniciar o computador

O programa abre sozinho. Não precisa fazer nada.

Para conferir: reinicie o computador e veja se a janela preta aparece
sozinha depois que a área de trabalho carregar. Pode demorar alguns segundos.

---

## 7. Não está imprimindo — o que fazer

Faça nesta ordem. Pare quando resolver.

1. **A janela preta está aberta?** Se não, dê dois cliques no
   `RapidexImpressao.exe` (item 4).

2. **A janela mostra `conectado. Aguardando pedidos.`?**
   - Se mostra `PARANDO: ...credencial...` → o e-mail ou a senha do
     `config.ini` estão errados. Corrija (item 3) e abra de novo.
   - Se mostra `reconectando em N segundos` repetidamente → a internet está
     fora, ou o servidor está fora. Teste abrir um site qualquer no
     navegador.

3. **A impressora está ligada, com papel e sem luz vermelha piscando?**
   Se o programa mostrar `a via 'X' NAO foi impressa`, é isso.

4. **O nome da impressora no `config.ini` está exatamente igual ao do
   Windows?** Um espaço a mais já quebra. Confira pelo item 3.

5. **Ainda não resolveu:** mande o arquivo de registro para quem cuida do
   sistema.

### Como mandar o registro (log)

1. Tecla Windows + R, digite e dê Enter:

   ```
   %LOCALAPPDATA%\Rapidex Impressao
   ```

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
