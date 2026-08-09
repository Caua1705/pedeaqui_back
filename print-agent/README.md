# Agente de impressão

Roda **no computador do restaurante**, não no servidor. Fica escutando o
stream de pedidos da API e, quando um pedido é **aceito**, imprime as vias
nas impressoras térmicas da loja.

Mora neste repositório para não sair de sincronia com a API que ele consome,
mas não faz parte do build da API: está no `.dockerignore`, tem
`requirements.txt` próprio e os testes dele não rodam junto com os do
backend.

## O que ele NÃO faz

Ele não decide nada sobre o conteúdo. Não quebra linha, não alinha, não sabe
o que é um adicional, não sabe que a via de produção não leva preço e não
sabe que pedido não pago não vai para a cozinha.

Tudo isso é decidido pela API, em `src/services/print_layout.py`, e chega
pronto em `GET /admin/orders/{id}/print-jobs`. O agente recebe texto já
quebrado na largura certa e o manda para a impressora.

A razão é operacional: existe **um agente por loja**, instalado à mão,
atualizado quando alguém lembra. Regra que mora nele só se corrige indo até
a loja. Regra que mora na API se corrige com um deploy e vale para todo
mundo na mesma hora.

O que sobra para o agente é o que só pode ser feito nesta máquina: falar
ESC/POS com a impressora que está ligada nela — selecionar fonte, codepage e
corte, que são estados da impressora e não cabem dentro de um texto.

## Como funciona

```
  API                                          agente
   |                                             |
   |  POST /admin/auth/login  ---------------->  | (token, 12h)
   |  POST /admin/orders/stream-ticket ------->  | (ticket, 30s)
   |  GET  /admin/orders/stream?ticket=... <--   | conexão aberta
   |                                             |
   |  --- event: order.status_changed ------->   | status == accepted?
   |                                             |
   |  GET /admin/orders/{id}/print-jobs <----    |
   |  --- [{sector_name, font_size, content}] -> | ESC/POS -> impressora
```

O ticket de 30 segundos parece burocracia e não é: a rota do stream foi
desenhada para o `EventSource` do navegador, que não manda cabeçalho, então
ela só aceita credencial na querystring. Mandar o token de 12h ali o
deixaria no log de acesso do proxy. O agente entra pela mesma porta do
painel — não existe uma entrada separada para ele, e não deveria existir.

## Instalação

Requer **Python 3.11+** no computador da loja.

```powershell
cd C:\print-agent
py -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.ini.example config.ini
notepad config.ini
```

### Nome das impressoras

O `config.ini` mapeia **setor → nome da impressora no Windows**. O nome tem
que ser exatamente o que aparece em *Configurações > Bluetooth e
dispositivos > Impressoras e scanners*. Para listar pelo PowerShell:

```powershell
Get-Printer | Select-Object Name, DriverName, PortName
```

A impressora precisa aceitar dados **RAW** — o driver "Generic / Text Only"
serve, e o driver do fabricante também. Um driver gráfico (o que imprime
como se fosse Word) transforma os bytes de controle em texto e a via sai com
`GS ! 0x11` escrito nela.

O nome do setor é o que está no painel, em *Setores de impressão*. Acento e
caixa não importam: `Cozinha`, `cozinha` e `COZINHA` são a mesma linha. Os
dois nomes fixos que a API sempre pode mandar:

- `Via do cliente` — a via com preços, endereço e total (normalmente o caixa)
- `SEM SETOR` — a via de resgate, com item cujo setor não serve para esta
  filial (produto apontado para outra loja, ou setor desativado). Se ela
  aparecer no dia a dia, o cadastro de setores está incompleto — o log diz
  qual produto.

### Credencial

Prefira **e-mail e senha** a um token colado:

```ini
[api]
email = impressora.centro@exemplo.com
password = ...
```

O token de acesso da API vale **12 horas**. Um agente que sobe no boot com
token fixo para de imprimir na manhã seguinte, e ninguém liga o defeito à
expiração. Com e-mail e senha ele refaz o login sozinho.

Crie um usuário dedicado ao agente, com papel `attendant` e **preso à filial
desta loja** — assim ele só enxerga os pedidos daqui:

```bash
python scripts/create_admin_user.py \
  --restaurant-slug junior-da-picanha \
  --name "Impressora Centro" \
  --email impressora.centro@exemplo.com \
  --role attendant
```

### Conferindo antes de valer

```powershell
venv\Scripts\python -m print_agent --dry-run
```

O `--dry-run` faz tudo — login, stream, busca das vias — e registra no log o
que **seria** enviado a cada impressora, sem gastar bobina. É como se
confere a instalação antes da inauguração. Aceite um pedido pelo painel e
veja as linhas aparecerem.

Depois, sem o `--dry-run`, para valer:

```powershell
venv\Scripts\python -m print_agent
```

## Rodando como serviço do Windows

O agente precisa subir sozinho no boot: o computador do balcão é desligado
no fim do expediente e ligado por quem abre a loja, que não vai abrir
terminal nenhum.

### NSSM (recomendado)

O [NSSM](https://nssm.cc/download) transforma qualquer executável em serviço
e cuida de reiniciar o processo se ele morrer.

```powershell
# Como Administrador, na pasta onde o nssm.exe foi extraído
.\nssm.exe install PedeAquiPrintAgent "C:\print-agent\venv\Scripts\python.exe" "-m print_agent"
.\nssm.exe set PedeAquiPrintAgent AppDirectory "C:\print-agent"
.\nssm.exe set PedeAquiPrintAgent DisplayName "PedeAqui - Agente de Impressao"
.\nssm.exe set PedeAquiPrintAgent Description "Imprime as comandas dos pedidos aceitos"
.\nssm.exe set PedeAquiPrintAgent Start SERVICE_AUTO_START

# Reinicia sozinho se cair, esperando 10s entre tentativas
.\nssm.exe set PedeAquiPrintAgent AppExit Default Restart
.\nssm.exe set PedeAquiPrintAgent AppRestartDelay 10000

# Espera o processo terminar o evento em andamento antes de matar
.\nssm.exe set PedeAquiPrintAgent AppStopMethodConsole 15000

.\nssm.exe start PedeAquiPrintAgent
```

`AppDirectory` importa: sem ele o serviço sobe com o diretório de trabalho
em `C:\Windows\System32`. O agente resolve os caminhos do `config.ini` a
partir da pasta do próprio config justamente por isso, mas outras coisas
(como o `venv`) esperam o diretório certo.

Conferindo e operando depois:

```powershell
Get-Service PedeAquiPrintAgent
Restart-Service PedeAquiPrintAgent
Get-Content C:\print-agent\logs\print-agent.log -Tail 50 -Wait
```

### Sem NSSM: Agendador de Tarefas

Se não puder instalar o NSSM, o Agendador de Tarefas resolve — não
reinicia o processo em caso de falha tão bem, mas sobe no boot:

```powershell
$acao = New-ScheduledTaskAction `
  -Execute "C:\print-agent\venv\Scripts\pythonw.exe" `
  -Argument "-m print_agent" `
  -WorkingDirectory "C:\print-agent"
$gatilho = New-ScheduledTaskTrigger -AtStartup
$config = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "PedeAquiPrintAgent" `
  -Action $acao -Trigger $gatilho -Settings $config `
  -User "SYSTEM" -RunLevel Highest
```

Use `pythonw.exe` (e não `python.exe`) aqui: ele não abre janela de console.

**Impressora de rede + conta SYSTEM:** uma impressora instalada só no perfil
de um usuário não existe para o `SYSTEM`. Se a térmica for de rede, instale-a
para todos os usuários, ou rode o serviço com a conta de quem a instalou
(`nssm set PedeAquiPrintAgent ObjectName .\usuario senha`). Impressora USB
local normalmente não sofre disso.

## Log

`logs/print-agent.log`, rotacionado em 5 MB × 5 arquivos. Em arquivo porque
o agente roda como serviço: não há console para onde olhar, e quando alguém
pergunta "por que a comanda não saiu ontem às 21h" a resposta está aqui.

O que procurar:

| Linha | Significa |
|---|---|
| `pedido #1234 impresso (3 vias)` | tudo certo |
| `pedido #1234 saiu incompleto` | alguma via falhou; o pedido **não** foi marcado como impresso |
| `setor 'X' nao tem impressora no config.ini` | setor criado no painel depois da instalação |
| `desisti da via ... Papel, energia ou cabo?` | a impressora recusou N vezes seguidas |
| `reconectando em 1.2s (conexao durou 900s...)` | **normal** — ver abaixo |
| `o servidor avisou que o replay foi truncado` | o agente ficou fora mais de 1h; confira o painel |

## Reconexão

O servidor fecha o stream **a cada 15 minutos de propósito** e manda um
heartbeat a cada 20 segundos para o Cloudflare (e qualquer proxy) não
derrubar a conexão ociosa. Ver `reconectando em ...` no log dezenas de vezes
por noite é o comportamento esperado, não defeito.

Por isso o backoff só cresce quando a conexão cai **rápido** (menos de 30
segundos): uma conexão que durou o tempo normal zera a espera. Sem essa
regra, o agente que roda a noite inteira chegaria de manhã esperando um
minuto entre tentativas por ter reconectado normalmente centenas de vezes.

Na reconexão o agente manda `Last-Event-ID`, e o servidor repete o que
aconteceu durante a queda — é o que faz um pedido aceito durante uma queda
de 40 segundos ainda imprimir.

## Não reimprimir

`state/printed-orders.json` guarda os ids já impressos (7 dias). É o que
impede o pior defeito possível deste agente: reconectar às 22h e cuspir a
fila do dia inteiro.

É necessário porque o `Last-Event-ID` faz o servidor **repetir** eventos de
propósito, e porque o stream entrega ao menos uma vez. **Não apague este
arquivo** com a loja aberta.

Um pedido só entra nele quando **todas** as vias saíram. Meia comanda
impressa é pior que nenhuma: a cozinha prepara o que viu e o resto do pedido
some.

## Limitações conhecidas

- **Queda longa perde pedido.** O replay do servidor volta no máximo 1 hora.
  Se o agente ficar fora mais que isso, os pedidos aceitos no intervalo não
  imprimem — o log registra `replay foi truncado` e alguém precisa
  reimprimir pelo painel. Não há varredura de recuperação de propósito: ela
  poderia despejar horas de comandas de uma vez na volta.
- **Não há reimpressão sob demanda no agente.** A rota
  `GET /admin/orders/{id}/print-jobs` é um GET puro e pode ser chamada de
  novo à vontade, mas quem dispara isso hoje é só o stream.

## Testes

```powershell
venv\Scripts\pip install -r requirements-dev.txt
cd C:\print-agent
venv\Scripts\pytest
```

Não são coletados pelo `pytest` da raiz do repositório (que tem
`testpaths = tests`): a API e o agente são dois projetos com ciclos de vida
diferentes, e o agente nem importa FastAPI.

O que os testes cobrem: o parser de SSE, a memória de impressos, a tradução
para ESC/POS, a leitura do config e o laço de decisão (o que dispara
impressão, para onde vai cada via, o que acontece quando a impressora
falha). A impressora e a API são dublês.

O que **não** é coberto e precisa de conferência manual, uma vez por
instalação: que o Windows aceita os bytes RAW naquela impressora. É o que o
`--dry-run` seguido de uma impressão de verdade resolve.
