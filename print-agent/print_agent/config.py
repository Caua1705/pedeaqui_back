"""Leitura do config.ini.

Uma decisao vale para o arquivo inteiro: **nome de setor e comparado sem
acento e sem caixa**. O nome chega da API como o lojista digitou no painel
("Cozinha", "COZINHA", "Praca Quente"), e o `configparser` ja rebaixa a
chave para minusculas. Comparar cru faria a comanda da "Cozinha" nao achar
a impressora cadastrada como "cozinha" — e o defeito apareceria so no dia em
que alguem renomeasse o setor no painel.
"""

import configparser
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


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

DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
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

    state_file: Path = Path("state/printed-orders.json")
    state_retention_days: int = DEFAULT_STATE_RETENTION_DAYS

    log_file: Path = Path("logs/print-agent.log")
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

    def printer_for(self, sector_name: str) -> str | None:
        """Impressora do setor, ou a padrao, ou None.

        `None` NAO e silencio: quem chama loga erro. Um setor sem impressora
        e configuracao pela metade, e a comanda que nao sai e um item que a
        cozinha nao vai preparar.
        """
        return self.printers.get(normalize_sector(sector_name), self.default_printer)


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config.ini nao encontrado em {path}")

    parser = configparser.ConfigParser(interpolation=None)
    # `interpolation=None` porque senha e token podem conter '%', que o
    # ConfigParser interpretaria como interpolacao e recusaria o arquivo.
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"config.ini invalido: {exc}") from exc

    if not parser.has_section("api"):
        raise ConfigError("config.ini precisa da secao [api]")

    api = parser["api"]
    base_url = api.get("base_url", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("[api] base_url e obrigatorio")

    token, email, password = _read_credentials(api)
    printers, default_printer = _read_printers(parser)
    root = path.parent

    return Config(
        api_base_url=base_url,
        token=token,
        email=email,
        password=password,
        printers=printers,
        default_printer=default_printer,
        state_file=_resolve(root, parser, "state", "file", "state/printed-orders.json"),
        state_retention_days=_get_int(
            parser, "state", "retention_days", DEFAULT_STATE_RETENTION_DAYS
        ),
        log_file=_resolve(root, parser, "log", "file", "logs/print-agent.log"),
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
    )


def _read_credentials(api) -> tuple[str | None, str | None, str | None]:
    """Token estatico, ou e-mail e senha.

    Os dois caminhos existem por um motivo pratico: o token de acesso da API
    vale 12 horas (ADMIN_ACCESS_TOKEN_MINUTES=720). Um agente que sobe no
    boot e roda por meses com token fixo para de imprimir na manha seguinte,
    e ninguem liga o defeito a expiracao. Com e-mail e senha ele refaz o
    login sozinho.

    O token continua aceito porque e o que serve para testar a instalacao
    sem colocar senha num arquivo de texto.
    """
    token = (api.get("token") or "").strip() or None
    email = (api.get("email") or "").strip() or None
    password = api.get("password") or None

    if not token and not (email and password):
        raise ConfigError(
            "[api] informe token, ou email e password. Sem credencial nao "
            "ha stream para escutar."
        )
    return token, email, password


def _read_printers(parser: configparser.ConfigParser) -> tuple[dict[str, str], str | None]:
    """Mapa setor -> impressora do Windows.

    A chave `default` e reservada: ela nao e um setor, e a impressora que
    recebe o que nao casou com nenhuma linha. Sem ela, um setor criado no
    painel depois da instalacao simplesmente nao imprimiria.
    """
    if not parser.has_section("printers"):
        raise ConfigError(
            "config.ini precisa da secao [printers] com o mapeamento "
            "setor = nome da impressora do Windows"
        )

    printers: dict[str, str] = {}
    default_printer: str | None = None
    for key, value in parser["printers"].items():
        printer = value.strip()
        if not printer:
            continue
        if normalize_sector(key) == "default":
            default_printer = printer
            continue
        printers[normalize_sector(key)] = printer

    if not printers and default_printer is None:
        raise ConfigError("[printers] esta vazia: nenhuma via teria para onde ir")
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
