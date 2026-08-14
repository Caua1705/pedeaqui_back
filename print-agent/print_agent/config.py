"""Leitura do config.ini.

Uma decisao vale para o arquivo inteiro: **nome de setor e comparado sem
acento e sem caixa**. O nome chega da API como o lojista digitou no painel
("Cozinha", "COZINHA", "Praca Quente"), e o `configparser` ja rebaixa a
chave para minusculas. Comparar cru faria a comanda da "Cozinha" nao achar
a impressora cadastrada como "cozinha" — e o defeito apareceria so no dia em
que alguem renomeasse o setor no painel.

## Dois formatos aceitos, e por que

O formato longo (`[api]` + `[printers]` + `[printing]` + `[agent]` + ...) e o
original e continua valendo inteiro. Ele e o que a loja com varias pracas
precisa: uma impressora por setor.

O formato curto (`[rapidex]`) existe para a instalacao de balcao, onde a loja
tem UMA impressora e quem preenche o arquivo esta com o pendrive na mao:

    [rapidex]
    api_url = https://api.pederapidex.com
    email   = impressora.centro@exemplo.com
    password = ...
    printer = EPSON TM-T20

`printer` do formato curto e exatamente o `default` do `[printers]`: a
impressora que recebe todo setor nao mapeado. Nao e um mecanismo novo — e o
fallback que ja existia, com um nome que cabe numa instrucao por telefone.
Por isso o formato curto NAO muda comportamento nenhum: uma loja de uma
impressora ja funcionava assim, escrevendo `default = ...`.

Os dois podem conviver: `[rapidex]` da a base, e um `[printers]` ao lado
acrescenta as pracas. Onde os dois dizem a mesma coisa, o formato longo
vence, por ser o mais especifico.
"""

import configparser
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from print_agent import paths


# Status do pedido que dispara a impressao. Vem de ORDER_STATUSES da API.
DEFAULT_TRIGGER_STATUS = "accepted"

# Codepage padrao da impressora. 2 = CP850 (multilingue latino), aceito por
# quase toda termica de balcao. Se a sua imprime "JoÃ£o" no lugar de "João",
# e este numero (e o `encoding` abaixo) que precisa mudar, nao o texto.
DEFAULT_CODEPAGE = 2
DEFAULT_ENCODING = "cp850"

# Espera entre tentativas de reconexao do stream. O teto e baixo de
# proposito: com a loja aberta, um minuto sem stream e um pedido que nao
# imprimiu.
DEFAULT_RECONNECT_MIN_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 60.0

# Quantas vezes uma via e reenviada antes de o agente desistir e gritar no
# log. Papel acabado e impressora desligada sao o caso comum, e os dois se
# resolvem em segundos quando alguem esta no balcao.
DEFAULT_PRINT_ATTEMPTS = 3
DEFAULT_PRINT_RETRY_SECONDS = 5.0

# Por quantos dias o agente lembra que ja imprimiu um pedido. Sete cobre o
# fim de semana prolongado; alem disso o arquivo so cresceria.
DEFAULT_STATE_RETENTION_DAYS = 7

# 1 MB por arquivo, 5 arquivos: 5 MB no teto absoluto. O agente roda por
# meses sem ninguem tocar nele, e a maquina do balcao costuma ser a mais
# apertada da loja. 1 MB tambem e o tamanho que o lojista consegue mandar por
# WhatsApp quando alguem pedir o log.
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


class ConfigError(Exception):
    """config.ini ausente, incompleto ou contraditorio.

    Sobe ate o `__main__`, que a transforma em mensagem de uma linha e
    codigo de saida 2. Um agente que sobe com configuracao errada e pior
    que um agente que nao sobe: ele fica no ar sem imprimir nada e ninguem
    percebe ate o cliente reclamar.
    """


def normalize_sector(name: str) -> str:
    """Nome de setor reduzido ao que da para comparar.

    Sem acento, sem caixa e sem espaco sobrando — "Praça Quente",
    "praca quente" e "PRACA  QUENTE" viram a mesma chave.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


@dataclass(frozen=True)
class Config:
    api_base_url: str
    # Credenciais. Ver `_read_credentials`: token sozinho serve para teste,
    # e-mail e senha sao o que aguenta rodar como servico.
    token: str | None
    email: str | None
    password: str | None

    printers: dict[str, str] = field(default_factory=dict)
    default_printer: str | None = None

    # Pasta onde o config.ini foi lido. Guardada porque o menu da bandeja
    # abre essa pasta, e ela nem sempre e a do executavel: `--config` aponta
    # para outro lugar, e o log pode estar numa terceira pasta.
    config_dir: Path = field(default_factory=paths.base_dir)

    state_file: Path = field(default_factory=paths.default_state_path)
    state_retention_days: int = DEFAULT_STATE_RETENTION_DAYS

    log_file: Path = field(default_factory=paths.default_log_path)
    log_level: str = "INFO"
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT

    codepage: int = DEFAULT_CODEPAGE
    encoding: str = DEFAULT_ENCODING
    cut: bool = True
    feed_lines: int = 4

    trigger_status: str = DEFAULT_TRIGGER_STATUS
    reconnect_min_seconds: float = DEFAULT_RECONNECT_MIN_SECONDS
    reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS
    print_attempts: int = DEFAULT_PRINT_ATTEMPTS
    print_retry_seconds: float = DEFAULT_PRINT_RETRY_SECONDS
    # Dry run imprime no log em vez de na impressora. E como se confere a
    # instalacao numa maquina sem termica ligada.
    dry_run: bool = False

    # Alerta sonoro quando uma comanda sai. Ligado por padrao: hoje o som
    # depende do painel estar aberto numa aba do navegador, e o agente e o
    # unico programa que esta sempre rodando. Desligavel porque quem tem o
    # computador na sala junto com a impressora nao quer o bipe repetido.
    sound: bool = True
    sound_file: Path | None = None

    def printer_for(self, sector_name: str) -> str | None:
        """Impressora do setor, ou a padrao, ou None.

        `None` NAO e silencio: quem chama loga erro. Um setor sem impressora
        e configuracao pela metade, e a comanda que nao sai e um item que a
        cozinha nao vai preparar.
        """
        return self.printers.get(normalize_sector(sector_name), self.default_printer)


# Modelo criado na primeira execucao, quando nao ha config.ini nenhum.
#
# Formato curto de proposito: quem le isto esta em pe no balcao com um
# pendrive. As opcoes avancadas (uma impressora por setor, codepage, tempos
# de reconexao) estao no config.ini.example, e sao raras.
CONFIG_TEMPLATE = """\
; Configuracao do Rapidex Impressao.
;
; Preencha os quatro campos abaixo e salve. Depois feche e abra o programa.
; Linhas que comecam com ; sao comentario e podem ficar como estao.

[rapidex]

; 1. Endereco do servidor. Normalmente nao muda.
api_url = https://api.pederapidex.com

; 2. E-mail e senha do usuario do painel criado para ESTA impressora.
;    Use um usuario dedicado ao agente, nao o do dono da loja.
;
;    IMPORTANTE: e por aqui que o programa continua funcionando sozinho.
;    Ele refaz o login automaticamente quando precisa.
email =
password =

; 3. Nome EXATO da impressora, como aparece no Windows em
;    Configuracoes > Bluetooth e dispositivos > Impressoras e scanners.
;    Copie e cole o nome, com maiusculas e espacos iguais.
printer =

; --------------------------------------------------------------------
; Alternativa a e-mail e senha: um token colado a mao.
; So para teste rapido. O token VENCE EM 12 HORAS e depois o programa
; para de imprimir ate alguem trocar. Nao use no dia a dia.
; token =
;
; Loja com mais de uma impressora: apague a linha `printer` acima e use
; uma secao [printers], uma linha por setor do painel. Veja exemplos em
; config.ini.example.
; --------------------------------------------------------------------
"""


def write_template(path: Path) -> None:
    """Cria o config.ini modelo, com os campos vazios.

    Existe para a primeira execucao nao ser um traceback. O lojista (ou quem
    instalou) abre o arquivo que acabou de aparecer ao lado do programa,
    preenche tres linhas e roda de novo.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config.ini nao encontrado em {path}")

    parser = configparser.ConfigParser(interpolation=None)
    # `interpolation=None` porque senha e token podem conter '%', que o
    # ConfigParser interpretaria como interpolacao e recusaria o arquivo.
    #
    # `utf-8-sig` e nao `utf-8`: o config.ini e editado no Bloco de Notas da
    # maquina do balcao, e o Bloco de Notas grava BOM (sempre, nas versoes
    # antigas do Windows; ao escolher "UTF-8 com BOM" em Salvar como, nas
    # novas). Lido como `utf-8`, o BOM entra no comeco da primeira linha e
    # `[rapidex]` vira `﻿[rapidex]` — o ConfigParser nao reconhece mais
    # a secao e responde "File contains no section headers" apontando para
    # uma linha que, na tela, E um cabecalho de secao. Ninguem no balcao sai
    # dessa sozinho.
    #
    # `utf-8-sig` le os dois casos: come o BOM quando existe e se comporta
    # como `utf-8` quando nao existe.
    #
    # E se nem UTF-8 for: o "Salvar como > ANSI" do Bloco de Notas (padrao nas
    # maquinas mais antigas, que sao justamente as do balcao) grava CP1252, e
    # ai o "ç" de "Impressora Cozinha Ação" e um byte 0xE7 solto que nao e
    # UTF-8 valido. Isso levanta `UnicodeDecodeError`, que NAO e
    # `configparser.Error` e NAO e `ConfigError` — escapava inteiro pelo
    # `__main__` e o lojista via um traceback de Python, exatamente o que o
    # tratamento de config ausente existe para evitar. Nome de impressora e
    # nome de setor tem acento no Brasil; isso ia acontecer.
    try:
        _read_ini(parser, path)
    except configparser.Error as exc:
        raise ConfigError(f"config.ini invalido: {exc}") from exc

    if not (parser.has_section("api") or parser.has_section("rapidex")):
        raise ConfigError(
            "config.ini precisa da secao [rapidex] (formato curto) ou [api] "
            "(formato completo)"
        )

    base_url = _read_base_url(parser)
    token, email, password = _read_credentials(parser)
    printers, default_printer = _read_printers(parser)
    root = path.parent

    return Config(
        api_base_url=base_url,
        token=token,
        email=email,
        password=password,
        printers=printers,
        default_printer=default_printer,
        config_dir=root,
        state_file=_resolve(root, parser, "state", "file", paths.STATE_NAME),
        state_retention_days=_get_int(
            parser, "state", "retention_days", DEFAULT_STATE_RETENTION_DAYS
        ),
        log_file=_resolve(root, parser, "log", "file", paths.LOG_NAME),
        log_level=_get(parser, "log", "level", "INFO").upper(),
        log_max_bytes=_get_int(parser, "log", "max_bytes", DEFAULT_LOG_MAX_BYTES),
        log_backup_count=_get_int(parser, "log", "backup_count", DEFAULT_LOG_BACKUP_COUNT),
        codepage=_get_int(parser, "printing", "codepage", DEFAULT_CODEPAGE),
        encoding=_get(parser, "printing", "encoding", DEFAULT_ENCODING),
        cut=_get_bool(parser, "printing", "cut", True),
        feed_lines=_get_int(parser, "printing", "feed_lines", 4),
        trigger_status=_get(parser, "agent", "trigger_status", DEFAULT_TRIGGER_STATUS),
        reconnect_min_seconds=_get_float(
            parser, "agent", "reconnect_min_seconds", DEFAULT_RECONNECT_MIN_SECONDS
        ),
        reconnect_max_seconds=_get_float(
            parser, "agent", "reconnect_max_seconds", DEFAULT_RECONNECT_MAX_SECONDS
        ),
        print_attempts=_get_int(parser, "agent", "print_attempts", DEFAULT_PRINT_ATTEMPTS),
        print_retry_seconds=_get_float(
            parser, "agent", "print_retry_seconds", DEFAULT_PRINT_RETRY_SECONDS
        ),
        dry_run=_get_bool(parser, "agent", "dry_run", False),
        sound=_get_bool(parser, "agent", "sound", True),
        sound_file=_read_sound_file(root, parser),
    )


def _read_sound_file(root: Path, parser: configparser.ConfigParser) -> Path | None:
    """O .wav do alerta, quando o lojista escolheu um.

    `None` = os tres apitos embutidos, que e o padrao e nao depende de
    arquivo nenhum existir na maquina.

    O caminho NAO e conferido aqui, e isso e deliberado: recusar o boot por
    um .wav que nao existe seria trocar "o alerta nao tocou" por "a loja
    inteira parou de imprimir". Quem descobre o arquivo quebrado e o
    `sound.play_new_order`, que registra no log e segue.
    """
    raw = _get(parser, "agent", "sound_file", "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (root / path)


def _read_ini(parser: configparser.ConfigParser, path: Path) -> None:
    """Le o config.ini em UTF-8 e, se nao for, em CP1252.

    CP1252 e a ultima tentativa e nao a primeira porque ela NUNCA falha:
    todo byte tem um caractere correspondente. Tentada antes, ela leria um
    arquivo UTF-8 legitimo como mojibake ("Ação" -> "AÃ§Ã£o") em silencio, e
    o nome da impressora nunca casaria com o do Windows.

    A ordem certa e esta: UTF-8 e restritivo o bastante para falhar quando o
    arquivo nao e UTF-8, e so entao o CP1252 entra como palpite.
    """
    try:
        parser.read(path, encoding="utf-8-sig")
        return
    except UnicodeDecodeError:
        pass

    # `parser.read` pode ter absorvido as secoes iniciais antes de bater no
    # primeiro byte invalido. Zera para nao misturar as duas leituras.
    for section in parser.sections():
        parser.remove_section(section)

    try:
        parser.read(path, encoding="cp1252")
    except UnicodeDecodeError as exc:  # pragma: no cover - cp1252 nao rejeita byte
        raise ConfigError(
            f"nao foi possivel ler {path}: o arquivo nao parece ser texto ({exc})"
        ) from exc


def _option(parser: configparser.ConfigParser, *candidates: tuple[str, str]) -> str | None:
    """O primeiro (secao, opcao) preenchido, na ordem dada.

    A ordem dos candidatos e a precedencia: o formato longo vem primeiro
    porque e o mais especifico.
    """
    for section, option in candidates:
        if not parser.has_section(section):
            continue
        value = (parser[section].get(option) or "").strip()
        if value:
            return value
    return None


def _read_base_url(parser: configparser.ConfigParser) -> str:
    raw = _option(parser, ("api", "base_url"), ("rapidex", "api_url"))
    if not raw:
        raise ConfigError(
            "informe a URL da API: [rapidex] api_url (ou [api] base_url)"
        )
    return raw.rstrip("/")


def _read_credentials(
    parser: configparser.ConfigParser,
) -> tuple[str | None, str | None, str | None]:
    """Token estatico, ou e-mail e senha.

    Os dois caminhos existem por um motivo pratico: o token de acesso da API
    vale 12 horas (ADMIN_ACCESS_TOKEN_MINUTES=720). Um agente que sobe no
    boot e roda por meses com token fixo para de imprimir na manha seguinte,
    e ninguem liga o defeito a expiracao. Com e-mail e senha ele refaz o
    login sozinho.

    O token continua aceito porque e o que serve para testar a instalacao
    sem colocar senha num arquivo de texto. Quem sobe so com token recebe um
    aviso no boot — ver `__main__`.

    `password` nao passa por `_option` (que aplica `.strip()`): espaco no
    comeco ou no fim de uma senha e caractere, nao sujeira.
    """
    token = _option(parser, ("api", "token"), ("rapidex", "token"))
    email = _option(parser, ("api", "email"), ("rapidex", "email"))

    password = None
    for section in ("api", "rapidex"):
        if parser.has_section(section) and parser[section].get("password"):
            password = parser[section].get("password")
            break

    if not token and not (email and password):
        raise ConfigError(
            "informe token, ou email e password. Sem credencial nao ha "
            "stream para escutar."
        )
    return token, email, password


def _read_printers(parser: configparser.ConfigParser) -> tuple[dict[str, str], str | None]:
    """Mapa setor -> impressora do Windows.

    A chave `default` e reservada: ela nao e um setor, e a impressora que
    recebe o que nao casou com nenhuma linha. Sem ela, um setor criado no
    painel depois da instalacao simplesmente nao imprimiria.

    `[rapidex] printer` alimenta esse MESMO default. E a loja de uma
    impressora so: tudo cai no fallback, que e como ela ja funcionava
    escrevendo `default = ...` no formato longo.
    """
    printers: dict[str, str] = {}
    default_printer = _option(parser, ("rapidex", "printer"))

    if parser.has_section("printers"):
        for key, value in parser["printers"].items():
            printer = value.strip()
            if not printer:
                continue
            if normalize_sector(key) == "default":
                default_printer = printer
                continue
            printers[normalize_sector(key)] = printer

    if not printers and default_printer is None:
        raise ConfigError(
            "nenhuma impressora configurada: preencha [rapidex] printer "
            "(uma impressora) ou a secao [printers] (uma por setor). Sem "
            "isso nenhuma via teria para onde ir."
        )
    return printers, default_printer


def _get(parser: configparser.ConfigParser, section: str, option: str, fallback: str) -> str:
    if not parser.has_section(section):
        return fallback
    return parser[section].get(option, fallback).strip() or fallback


def _get_int(parser: configparser.ConfigParser, section: str, option: str, fallback: int) -> int:
    raw = _get(parser, section, option, str(fallback))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {option} precisa ser um numero inteiro") from exc


def _get_float(
    parser: configparser.ConfigParser, section: str, option: str, fallback: float
) -> float:
    raw = _get(parser, section, option, str(fallback))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {option} precisa ser um numero") from exc


def _get_bool(parser: configparser.ConfigParser, section: str, option: str, fallback: bool) -> bool:
    raw = _get(parser, section, option, "sim" if fallback else "nao").casefold()
    if raw in ("1", "true", "yes", "sim", "on"):
        return True
    if raw in ("0", "false", "no", "nao", "off"):
        return False
    raise ConfigError(f"[{section}] {option} precisa ser sim ou nao")


def _resolve(
    root: Path,
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    fallback: str,
) -> Path:
    """Caminho do config.ini resolvido a partir da PASTA DO CONFIG.

    Nao do diretorio de trabalho: o servico do Windows sobe com o cwd em
    C:\\Windows\\System32, e um caminho relativo escreveria o log e o estado
    la dentro — quando tivesse permissao.
    """
    raw = _get(parser, section, option, fallback)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (root / path)
