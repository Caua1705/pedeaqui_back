"""Todo campo de dia da semana do contrato diz que 0 é SEGUNDA — armadilha 1.

`0 = segunda` é o `datetime.weekday()` do Python. O `getDay()` do JavaScript
devolve `0 = domingo`, e os dois não conversam sozinhos. O sintoma de errar é
mudo: **o lojista configura segunda no painel e a loja abre no domingo** — ou
fica fechada na segunda, sem erro em lugar nenhum, porque a tela mostra de
volta o número que ele digitou. Nenhum teste deste repositório pega, e não tem
como pegar: do lado de cá o número é consistente.

## O que estava faltando, e a assimetria é o achado

A convenção existia, em docstring de classe, nos **dois schemas de ESCRITA**
(`BusinessHourInput`, `CashbackWeekdayInput`). Os **cinco de LEITURA** não
diziam nada — nem no schema, nem no campo.

Quem escreve copia o exemplo e acerta. **Quem lê um `weekday: 3` e o entrega a
`new Date()` erra — e ler é exatamente o lado do sintoma.** O pior dos cinco
era `RestaurantInfoResponse.current_weekday`: o backend dizendo ao app que dia
é hoje, sem dizer em que numeração.

## Por que no CAMPO, e por que contra o documento GERADO

No campo porque é onde um cliente gerado a partir do OpenAPI mostra a frase a
quem está usando aquele número — quem lê `BusinessHourResponse.weekday` não
tem por que ir procurar a docstring de `BusinessHourInput`.

Contra o `/openapi.json` gerado — e não contra os `.py` — pela armadilha 16: **o
painel consome aquele documento.** Uma frase que exista no schema e não chegue
ao arquivo não protege ninguém, e é o mesmo motivo pelo qual
`PaymentErrorContractTests` mora ali.

## O que este arquivo trava, e é o oitavo campo

Os sete de hoje estão certos. O que ninguém vigiava é o **próximo**: campo de
dia da semana novo nasce sem descrição, e nasce silencioso. A varredura é por
FORMA (nome `weekday`/`current_weekday`, tipo inteiro), então ela alcança o
campo que ainda não existe — que é a única metade que importa.
"""

import json
from pathlib import Path

import pytest

from src.core.constants import DESCRICAO_DE_WEEKDAY


CAMINHO_DO_CONTRATO = Path(__file__).resolve().parents[1] / "openapi.json"
NOMES = ("weekday", "current_weekday")


def campos_de_weekday() -> list[tuple[str, str, dict]]:
    """Todo campo inteiro de dia da semana publicado, por schema."""
    contrato = json.loads(CAMINHO_DO_CONTRATO.read_text(encoding="utf-8"))
    achados = []
    for nome, schema in sorted(contrato["components"]["schemas"].items()):
        for campo, definicao in (schema.get("properties") or {}).items():
            if campo in NOMES and definicao.get("type") == "integer":
                achados.append((nome, campo, definicao))
    return achados


class TestOVarredorEnxerga:
    """O verde por ausência é indistinguível do verde por acerto: um filtro
    trocado devolveria zero campos, e zero campos passariam em tudo abaixo."""

    def test_acha_os_campos_de_weekday_das_tres_familias(self):
        encontrados = {f"{nome}.{campo}" for nome, campo, _ in campos_de_weekday()}

        # Horário de funcionamento, cashback por dia, e o "que dia é hoje".
        assert "BusinessHourInput.weekday" in encontrados
        assert "CashbackWeekdayInput.weekday" in encontrados
        assert "RestaurantInfoResponse.current_weekday" in encontrados
        assert len(encontrados) >= 7


class TestTodoCampoDizAConvencao:
    def test_nenhum_campo_de_weekday_fica_sem_descricao(self):
        mudos = [
            f"{nome}.{campo}"
            for nome, campo, definicao in campos_de_weekday()
            if not definicao.get("description")
        ]

        assert mudos == [], (
            "campo de dia da semana sem descricao no contrato. O painel consome "
            "o /openapi.json (armadilha 16), e um `weekday: 3` sem convencao "
            "entregue a `new Date().getDay()` fecha a loja no dia errado."
        )

    def test_a_frase_e_a_MESMA_em_todos(self):
        """Uma segunda redação da mesma convenção é o começo de duas
        convenções. A frase sai de `DESCRICAO_DE_WEEKDAY`, um lugar só."""
        divergentes = [
            f"{nome}.{campo}"
            for nome, campo, definicao in campos_de_weekday()
            if definicao.get("description") != DESCRICAO_DE_WEEKDAY
        ]

        assert divergentes == []

    @pytest.mark.parametrize("pedaco", ["0 = SEGUNDA", "getDay", "domingo"])
    def test_a_frase_diz_as_tres_coisas_que_precisam_ser_ditas(self, pedaco):
        """Não basta dizer o nosso número: quem erra está do lado do
        JavaScript, e a frase precisa nomear o `getDay()` para que quem lê
        reconheça o próprio caso. Dizer só "0 = segunda" deixa o leitor
        concluir que os dois lados combinam."""
        assert pedaco in DESCRICAO_DE_WEEKDAY


class TestOContratoNoDiscoAcompanhaOCodigo:
    def test_o_openapi_gerado_tem_a_frase_do_codigo(self):
        """`scripts/export_openapi.py --check` já cobra isso no CI para o
        documento inteiro. Aqui a asserção é estreita de propósito: se ela
        falhar sozinha, o que faltou foi regravar o contrato depois de mexer
        na convenção — e a mensagem diz isso, em vez de um diff de 300 KB."""
        assert campos_de_weekday(), "nenhum campo de weekday no openapi.json"
        assert all(
            definicao.get("description") == DESCRICAO_DE_WEEKDAY
            for _nome, _campo, definicao in campos_de_weekday()
        ), "rode `python scripts/export_openapi.py` depois de mexer na convencao"
