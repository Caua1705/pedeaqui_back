"""As checagens de boot que derrubam a API.

O que se testa aqui e a LISTA de erros, nao o logging: `validate_settings`
so junta o que `collect_configuration_errors` devolveu.
"""

import logging
import multiprocessing
import sys

import pytest

from src.core.config import settings
from src.core.startup_checks import (
    StartupConfigurationError,
    collect_configuration_errors,
    collect_runtime_warnings,
    detect_worker_count,
    validate_settings,
)


def _contar_workers_no_filho(fila) -> None:
    """Roda no processo SPAWNADO. Precisa ser de modulo para ser picklavel."""
    from src.core.startup_checks import detect_worker_count

    fila.put(detect_worker_count(sys.argv, {}))


def _settings_with(monkeypatch, **overrides):
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    return settings


class TestOsDoisSegredosDeAssinatura:
    """Lojista e cliente nao podem compartilhar chave.

    O caso concreto: com ADMIN_AUTH_SECRET ausente, o token do painel era
    assinado com CUSTOMER_AUTH_SECRET. Quem tivesse um dos dois segredos
    conseguiria forjar token do outro publico — so o campo `purpose`
    separava, e ele viaja dentro do proprio token forjado.
    """

    def test_valores_iguais_derrubam_o_boot(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
            CUSTOMER_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
        )
        errors = collect_configuration_errors(settings)
        assert any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_espaco_em_branco_nao_disfarca_o_valor_repetido(self, monkeypatch):
        # Um ' ' no fim da linha do .env nao pode transformar "mesmo segredo"
        # em "segredos diferentes".
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET=" o-mesmo-segredo-dos-dois-lados ",
            CUSTOMER_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
        )
        errors = collect_configuration_errors(settings)
        assert any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_valores_diferentes_passam(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="segredo-do-painel-do-lojista",
            CUSTOMER_AUTH_SECRET="segredo-do-app-do-cliente",
        )
        errors = collect_configuration_errors(settings)
        assert not any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_validate_settings_levanta_e_nomeia_a_variavel(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="segredo-repetido",
            CUSTOMER_AUTH_SECRET="segredo-repetido",
        )
        with pytest.raises(StartupConfigurationError, match="ADMIN_AUTH_SECRET"):
            validate_settings(settings)


class TestQuantosWorkers:
    """A leitura do `--workers`, isolada da mensagem.

    As duas fontes são as que uvicorn e gunicorn de fato leem, e a
    precedência copia a do uvicorn: a opção de linha ganha do ambiente.
    """

    def test_sem_nada_e_um_worker(self):
        assert detect_worker_count(["uvicorn", "main:app"], {}) == 1

    def test_workers_com_espaco(self):
        assert detect_worker_count(["uvicorn", "main:app", "--workers", "4"], {}) == 4

    def test_workers_com_igual(self):
        assert detect_worker_count(["uvicorn", "main:app", "--workers=4"], {}) == 4

    def test_w_curto_do_gunicorn(self):
        assert detect_worker_count(["gunicorn", "-w", "8", "main:app"], {}) == 8

    def test_web_concurrency_quando_a_opcao_nao_vem(self):
        assert detect_worker_count(["uvicorn", "main:app"], {"WEB_CONCURRENCY": "3"}) == 3

    def test_a_opcao_ganha_do_ambiente(self):
        """Mesma precedência do uvicorn (`config.py`: `if workers is None and
        "WEB_CONCURRENCY" in os.environ`). Invertida, o aviso mentiria na
        máquina que tem a variável exportada e passou a mandar a opção."""
        argv = ["uvicorn", "main:app", "--workers", "2"]

        assert detect_worker_count(argv, {"WEB_CONCURRENCY": "9"}) == 2

    def test_valor_que_nao_e_numero_nao_derruba(self):
        """É um aviso de boot: recusar `--workers abc` é trabalho do uvicorn,
        e levantar aqui trocaria um problema por um maior."""
        assert detect_worker_count(["uvicorn", "--workers", "abc"], {}) == 1

    def test_workers_no_fim_da_linha_sem_valor(self):
        assert detect_worker_count(["uvicorn", "main:app", "--workers"], {}) == 1

    def test_zero_e_negativo_nao_viram_aviso(self):
        assert detect_worker_count(["uvicorn", "--workers", "0"], {}) == 1
        assert detect_worker_count(["uvicorn", "--workers", "-2"], {}) == 1


class TestAvisoDoHistoricoDoChat:
    """O guard que existe para o `--workers` não chegar em silêncio.

    O histórico de conversa do `/chat` é um `dict` de módulo
    (`chat_service._SESSION_HISTORY`) e não tem caminho de Redis — ao
    contrário do rate limit e do cache de entrega, que `REDIS_URL` resolve.
    Com N workers a conversa se perde entre requisições, sem erro e sem log:
    o sintoma é o Rapi esquecendo o que a pessoa acabou de dizer.
    """

    def test_um_worker_nao_avisa(self):
        assert collect_runtime_warnings(["uvicorn", "main:app"], {}) == []

    def test_dois_ou_mais_workers_avisam(self):
        avisos = collect_runtime_warnings(["uvicorn", "main:app", "--workers", "4"], {})

        assert len(avisos) == 1
        assert "4 workers" in avisos[0]

    def test_o_aviso_nomeia_o_historico_do_chat(self):
        aviso = collect_runtime_warnings(["uvicorn", "--workers", "2"], {})[0]

        assert "/chat" in aviso
        assert "MEMORIA DO PROCESSO" in aviso

    def test_o_aviso_diz_que_redis_nao_resolve(self):
        """A parte que evita o conserto errado.

        Quem lê "em memória" no boot vai atrás de `REDIS_URL`, porque é o que
        resolve os outros dois avisos deste arquivo. Aqui não resolve, e o
        aviso tem que dizer isso ou custa uma tarde.
        """
        aviso = collect_runtime_warnings(["uvicorn", "--workers", "2"], {})[0]

        assert "REDIS_URL NAO resolve" in aviso

    def test_web_concurrency_sozinha_tambem_avisa(self):
        """O jeito silencioso de chegar a N workers: nada muda no comando."""
        avisos = collect_runtime_warnings(["uvicorn", "main:app"], {"WEB_CONCURRENCY": "2"})

        assert len(avisos) == 1

    def test_o_aviso_sai_no_log_do_boot(self, monkeypatch, caplog):
        """Ponta a ponta: `validate_settings` lê o processo de verdade."""
        monkeypatch.setattr(sys, "argv", ["uvicorn", "main:app", "--workers", "3"])

        with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
            validate_settings(settings)

        historico = [
            registro.getMessage()
            for registro in caplog.records
            if "historico de conversa" in registro.getMessage()
        ]
        assert len(historico) == 1
        assert "[Startup]" in historico[0]

    def test_um_worker_nao_polui_o_log_do_boot(self, monkeypatch, caplog):
        monkeypatch.setattr(sys, "argv", ["uvicorn", "main:app"])
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

        with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
            validate_settings(settings)

        assert not [
            registro
            for registro in caplog.records
            if "historico de conversa" in registro.getMessage()
        ]


class TestArgvSobreviveAoWorker:
    """A premissa que sustenta a detecção inteira.

    O uvicorn NÃO forka: `uvicorn/_subprocess.py` usa
    `multiprocessing.get_context("spawn")` em toda plataforma. Se `sys.argv`
    não chegasse ao filho, `detect_worker_count` devolveria 1 dentro de todo
    worker e este guard seria uma função que nunca dispara — o mesmo defeito
    silencioso que ele existe para evitar.

    Quem salva é `multiprocessing.spawn.prepare()`, que restaura `sys.argv`
    no filho a partir do pai. Isto aqui é um teste de PREMISSA sobre a
    biblioteca, e é o que quebra se um dia ela mudar.
    """

    def test_o_filho_spawnado_enxerga_o_workers_do_pai(self, monkeypatch):
        contexto = multiprocessing.get_context("spawn")
        monkeypatch.setattr(sys, "argv", ["uvicorn", "main:app", "--workers", "4"])

        fila = contexto.Queue()
        processo = contexto.Process(target=_contar_workers_no_filho, args=(fila,))
        processo.start()
        processo.join(timeout=60)

        assert fila.get(timeout=10) == 4
