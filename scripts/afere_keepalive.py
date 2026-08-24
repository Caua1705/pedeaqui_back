"""Mede o que a decomposicao do `llm_call_ms` chama de "handshake".

EXISTE PORQUE O NUMERO DE 1000 ms NUNCA FOI MEDIDO — foi INFERIDO. Ele saiu
do residuo entre o turno 1 e o turno 2 de uma bateria em producao (3616 ms
para 85 tokens contra 1744 ms para 67: ~1,6 s que nao cabem na geracao), e
naquele par duas coisas mudavam ao mesmo tempo:

    o POOL estava frio          (DNS + TCP + TLS ainda por pagar)
    o PROCESSO era novo         (primeira chamada ESTRUTURADA da imagem)

Uma bateria feita em rajada nao separa as duas: os turnos seguintes vem com
menos de 5 s de intervalo, entao o pool nunca esfria de novo e a segunda
causa nunca se repete. As duas viraram um numero so, e a decisao de subir o
`keepalive_expiry` foi construida em cima dele.

    ./venv/Scripts/python scripts/afere_keepalive.py            # sem chave
    docker compose exec pedeaqui-api python scripts/afere_keepalive.py

**Nao escreve nada e nao precisa de banco.** Sem `OPENAI_API_KEY` no
ambiente ele roda as duas primeiras partes, que ja respondem metade da
pergunta e nao custam um centavo:

1. **Quanto custa abrir a conexao** — DNS, TCP e TLS cronometrados
   separados. Este e o TETO do que o keepalive pode economizar; se der ~100
   ms, o 1000 ms da decomposicao e outra coisa.
2. **Quanto tempo a OpenAI deixa a conexao ociosa de pe** — sem isso,
   `keepalive_expiry` alto troca handshake por `RemoteProtocolError` +
   retry, que e mais lento que o handshake que veio evitar. Um POST sem
   `Authorization` responde 401 pela MESMA conexao TLS que uma chamada paga
   usaria: para a pergunta "o socket ainda esta aberto?", o corpo da
   resposta e irrelevante.

Com `OPENAI_API_KEY` ele roda tambem:

3. **O prefill de verdade**, pela cadeia estruturada de producao, com o pool
   frio e com o pool quente — que e o unico jeito de separar o socket do
   resto.

RODE NA VPS, e nao na maquina de quem programa. O handshake sao tres idas e
voltas ate a borda da Cloudflare, e a diferenca entre estar a 40 ms e a 300
ms dela e a diferenca entre este trabalho valer e nao valer. O container
`pedeaqui-api` ja tem a pilha do lock e esta na posicao de rede certa.
"""

import os
import socket
import ssl
import statistics
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import httpx2

HOST = "api.openai.com"
PORTA = 443
URL = f"https://{HOST}/v1/responses"

# A escada de ociosidade. Comeca em 6 s — logo acima do `keepalive_expiry`
# default de 5,0 s — porque abaixo disso quem fecharia a conexao seriamos
# NOS, e a medicao responderia sobre a nossa propria configuracao em vez de
# sobre a da OpenAI.
ESPERAS_OCIOSAS = (6, 15, 30, 60, 120, 240)


def _cronometrar_abertura(repeticoes: int = 5) -> dict[str, list[float]]:
    """DNS, TCP e TLS em milissegundos, cada um por conta propria.

    Em socket cru e nao pelo `httpx2`, de proposito: o cliente HTTP soma as
    tres fases numa medida so, e e justamente a reparticao entre elas que
    decide o que fazer. DNS lento pede cache de resolucao; TLS lento pede
    keepalive; TCP lento nao tem conserto do nosso lado.
    """
    fases: dict[str, list[float]] = {"dns": [], "tcp": [], "tls": []}
    for _ in range(repeticoes):
        inicio = time.perf_counter()
        infos = socket.getaddrinfo(HOST, PORTA, socket.AF_INET, socket.SOCK_STREAM)
        resolvido = time.perf_counter()

        familia, tipo, protocolo, _, endereco = infos[0]
        conexao = socket.socket(familia, tipo, protocolo)
        conexao.settimeout(10)
        conexao.connect(endereco)
        conectado = time.perf_counter()

        contexto = ssl.create_default_context()
        contexto.set_alpn_protocols(["h2", "http/1.1"])
        cifrado = contexto.wrap_socket(conexao, server_hostname=HOST)
        pronto = time.perf_counter()
        cifrado.close()

        fases["dns"].append((resolvido - inicio) * 1000)
        fases["tcp"].append((conectado - resolvido) * 1000)
        fases["tls"].append((pronto - conectado) * 1000)
    return fases


def _relatar_abertura() -> float:
    """Imprime a reparticao e devolve o teto do que o keepalive economiza.

    A primeira resolucao de DNS paga o servidor e as seguintes saem do cache
    do sistema, entao a mediana de cinco reflete o dia a dia e o `max`
    mostra o custo da primeira. As duas colunas ficam porque um boot paga a
    primeira e todo o resto paga a mediana.
    """
    fases = _cronometrar_abertura()
    print("\n1. QUANTO CUSTA ABRIR A CONEXAO (o teto do ganho do keepalive)")
    total = 0.0
    for nome, valores in fases.items():
        mediana = statistics.median(valores)
        total += mediana
        print(
            f"   {nome:>4}: mediana {mediana:8.1f} ms"
            f"   min {min(valores):7.1f}   max {max(valores):7.1f}"
        )
    print(f"   {'SOMA':>4}: {total:16.1f} ms")
    return total


def _instrumentar_transporte() -> tuple[list[int], object, object]:
    """Conta conexoes TCP e handshakes TLS por baixo do `httpx2`.

    Instrumenta o TRANSPORTE, e nao o cliente: a pergunta e "abriu socket
    novo?", e so o socket sabe responder. Ler o pool interno do `httpx2`
    seria confiar na representacao privada de uma biblioteca que ja mudou de
    versao maior uma vez — que e a armadilha 42 pela outra ponta.

    Devolve os originais junto porque quem chama TEM que restaurar: estas
    duas funcoes sao globais do interpretador, e deixa-las trocadas
    envenenaria qualquer coisa que rodasse depois no mesmo processo.
    """
    placar = [0, 0]
    original_tcp = socket.create_connection
    original_tls = ssl.SSLContext.wrap_socket

    def contar_tcp(*args, **kwargs):
        placar[0] += 1
        return original_tcp(*args, **kwargs)

    def contar_tls(self, *args, **kwargs):
        placar[1] += 1
        return original_tls(self, *args, **kwargs)

    socket.create_connection = contar_tcp
    ssl.SSLContext.wrap_socket = contar_tls
    return placar, original_tcp, original_tls


def _bater(
    cliente,
    espera: int,
    placar: list[int],
    anteriores: tuple[int, int],
    primeira: bool = False,
) -> None:
    """Uma batida da escada. Erro vira linha da tabela, nunca fim do script.

    `RemoteProtocolError` aqui NAO e falha do teste: e o resultado. E
    exatamente o que aconteceria em producao com `keepalive_expiry` acima do
    limite da OpenAI, e e a razao de esta escada existir ANTES da mudanca.
    """
    inicio = time.perf_counter()
    status, erro = "-", ""
    try:
        resposta = cliente.post(URL, json={"model": "gpt-5-mini", "input": "x"})
        status = str(resposta.status_code)
        if primeira:
            _relatar_cabecalhos(resposta)
    except Exception as excecao:
        erro = f"{type(excecao).__name__}: {excecao}"
    ms = (time.perf_counter() - inicio) * 1000

    novos_tcp = placar[0] - anteriores[0]
    novos_tls = placar[1] - anteriores[1]
    reusou = "sim" if novos_tcp == 0 else "NAO"
    print(
        f"   {espera:>6}s {ms:>9.1f} {novos_tcp:>5} {novos_tls:>5}"
        f" {reusou:>7}  {status:>6}  {erro}"
    )
    sys.stdout.flush()


def _relatar_cabecalhos(resposta) -> None:
    """`Keep-Alive: timeout=N`, quando vem, e a resposta pronta da parte 2."""
    cabecalhos = {chave.lower(): valor for chave, valor in resposta.headers.items()}
    for nome in ("server", "connection", "keep-alive"):
        print(f"   cabecalho {nome}: {cabecalhos.get(nome, '(ausente)')}")


def _relatar_ociosidade() -> None:
    """A escada: depois de N segundos parada, a conexao ainda serve?

    `keepalive_expiry=3600` de proposito. Quem tem que decidir fechar neste
    teste e a OpenAI; com o default de 5 s o cliente fecharia primeiro e a
    resposta seria sobre a nossa configuracao, nao sobre a delas.
    """
    placar, original_tcp, original_tls = _instrumentar_transporte()
    limites = httpx2.Limits(
        max_connections=10, max_keepalive_connections=10, keepalive_expiry=3600
    )
    cliente = httpx2.Client(
        limits=limites,
        timeout=httpx2.Timeout(connect=10, read=30, write=30, pool=30),
    )

    print("\n2. ATE QUANDO A OPENAI DEIXA A CONEXAO OCIOSA DE PE")
    print(f"   {'ocioso':>7} {'ms':>9} {'tcp':>5} {'tls':>5} {'reusou':>7}  status  erro")
    try:
        _bater(cliente, 0, placar, (0, 0), primeira=True)
        anteriores = (placar[0], placar[1])
        for espera in ESPERAS_OCIOSAS:
            time.sleep(espera)
            _bater(cliente, espera, placar, anteriores)
            anteriores = (placar[0], placar[1])
    finally:
        cliente.close()
        socket.create_connection = original_tcp
        ssl.SSLContext.wrap_socket = original_tls


def _relatar_prefill() -> None:
    """O prefill de verdade, pela cadeia ESTRUTURADA — que e a de producao.

    A cadeia importa e nao e detalhe. O warmup de `src/core/warmup.py` chama
    `.invoke("Responda apenas: ok")`, uma string pelada: nenhum
    `response_format`, nenhum schema. A requisicao do cliente vai por
    `with_structured_output`, que manda junto o JSON Schema de
    `ChatLLMResponse`. Se a primeira chamada ESTRUTURADA da imagem custar
    mais que as seguintes, o warmup esta aquecendo um caminho que ninguem
    percorre — e isso e hipotese testavel, nao palpite: e o que estas tres
    medidas separam.

    Custa tres geracoes minimas. E cobrado, e e barato.
    """
    if not os.getenv("OPENAI_API_KEY"):
        print("\n3. PREFILL: pulado, OPENAI_API_KEY ausente no ambiente.")
        print("   As partes 1 e 2 ja rodaram e nao dependem de chave.")
        return

    from src.ai.services.chat_llm_service import ChatLLMService

    entrada = {
        "restaurant_context": "Restaurante de teste.",
        "conversation": [],
        "retrieved_products": [],
        "user_message": "ok",
    }
    cadeia = ChatLLMService().build_chain()

    print("\n3. PREFILL PELA CADEIA ESTRUTURADA DE PRODUCAO")
    medidas = (
        ("1a estruturada (processo novo)", 0),
        ("2a seguida (pool quente)", 0),
        ("3a apos 30 s ocioso", 30),
    )
    for rotulo, ocioso in medidas:
        if ocioso:
            time.sleep(ocioso)
        inicio = time.perf_counter()
        cadeia.invoke(entrada)
        ms = (time.perf_counter() - inicio) * 1000
        print(f"   {rotulo:<32} {ms:9.1f} ms")
        sys.stdout.flush()


def main() -> None:
    print(f"httpx2={httpx2.__version__}  alvo={URL}")
    teto = _relatar_abertura()
    _relatar_ociosidade()
    _relatar_prefill()
    print(
        f"\nLEITURA: o keepalive so pode economizar os {teto:.0f} ms da parte 1,"
        "\ne so nas perguntas que chegam com o pool frio. Residuo maior que isso"
        "\nno `llm_call_ms` do turno 2 e OUTRA coisa — e a parte 3 diz se essa"
        "\noutra coisa e a primeira chamada estruturada da imagem."
    )


if __name__ == "__main__":
    main()
