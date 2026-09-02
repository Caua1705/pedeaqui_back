"""Nenhuma rota `/admin` deixa o cliente escolher restaurante ou filial.

Irmao de `tests/test_papeis_das_rotas.py`: aquele audita **o que** o lojista
pode fazer, este audita **onde** ele pode mexer. O custo de errar aqui e o maior
do sistema — um restaurante lendo pedido, cliente ou faturamento de outro —, e
chega em silencio.

VERMELHO, e nao aviso. Ao contrario das divergencias ORM x schema, aqui nao ha
divida herdada: o numero certo e ZERO, e zero que cresce e regressao.

## Por que as iscas deste arquivo nao sao decoracao

O varredor foi corrigido **tres vezes** antes de chegar a zero, e as tres
correcoes foram no mesmo sentido — acusar MENOS:

1. o indice era por nome puro de funcao, e `list_orders` existe em
   `admin_orders.py` e em `customers.py`. A varredura seguia o corpo da rota do
   CLIENTE e acusava a do admin;
2. so contava `restaurant_id=` NOMEADO, e `list_categories` passa
   `scope.restaurant_id` posicional;
3. so reconhecia o sufixo `_and_restaurant`, e existe `_by_restaurant`.

Todas eram falso positivo, e consertar era certo. Mas o efeito acumulado e um
varredor **mais permissivo**, e permissivo demais nao acusa nada nunca — e
"nenhuma" some do relatorio como se fosse boa noticia.

**Por isso as iscas.** Elas sao o que separa "nao ha achado" de "o varredor
parou de achar".
"""

import unittest

from scripts.escopo_das_rotas import (
    _Indice,
    _classificar_assinatura_do_entregador,
    _procurar,
    achados,
    auditar,
)


# A rota plantada e o service dela, no formato que o varredor le. Duas
# ISCAS e um PADRAO LEGITIMO, para cobrar os dois lados.
ENDPOINTS_PLANTADOS = '''
def rota_sem_conferencia_nenhuma(branch_id, scope, db):
    return ServicoPlantado(db).sem_conferencia(scope, branch_id)


def rota_so_com_ensure_branch(branch_id, scope, db):
    return ServicoPlantado(db).so_ensure_branch(scope, branch_id)


def rota_certa(branch_id, scope, db):
    return ServicoPlantado(db).do_jeito_certo(scope, branch_id)


def rota_do_entregador_sem_recorte(courier, db):
    return ServicoPlantado(db).entregador_sem_recorte(courier)


def rota_do_entregador_certa(courier, db):
    return ServicoPlantado(db).entregador_do_jeito_certo(courier)
'''

SERVICO_PLANTADO = '''
class ServicoPlantado:
    def __init__(self, db):
        self.branch_repository = BranchRepository(db)

    def sem_conferencia(self, scope, branch_id):
        return self.branch_repository.get_by_id(branch_id)

    def so_ensure_branch(self, scope, branch_id):
        scope.ensure_branch_allowed(branch_id)
        return self.branch_repository.get_by_id(branch_id)

    def do_jeito_certo(self, scope, branch_id):
        scope.ensure_branch_allowed(branch_id)
        return self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )

    def entregador_sem_recorte(self, courier):
        return self.branch_repository.list_open_orders(courier.branch_id)

    def entregador_do_jeito_certo(self, courier):
        return self.branch_repository.list_open_orders_by_courier(courier_id=courier.id)
'''


def _indice_plantado(tmp_path):
    (tmp_path / "endpoints_plantados.py").write_text(ENDPOINTS_PLANTADOS, encoding="utf-8")
    (tmp_path / "servico_plantado.py").write_text(SERVICO_PLANTADO, encoding="utf-8")
    return _Indice(diretorios=[tmp_path])


def _conferencias(tmp_path, funcao: str) -> tuple[bool, bool, bool]:
    """(filial, restaurante, seguiu tudo) — a leitura do lado do PAINEL."""
    filial, restaurante, _, completo = _procurar_plantada(tmp_path, funcao)
    return filial, restaurante, completo


def _conferencia_do_entregador(tmp_path, funcao: str) -> tuple[bool, bool]:
    """(entregador, seguiu tudo) — a leitura do lado do ENTREGADOR."""
    _, _, entregador, completo = _procurar_plantada(tmp_path, funcao)
    return entregador, completo


def _procurar_plantada(tmp_path, funcao: str) -> tuple[bool, bool, bool, bool]:
    indice = _indice_plantado(tmp_path)
    corpo = indice.funcoes[("endpoints_plantados", funcao)]
    return _procurar(corpo, None, indice, 0, set())


class _EntregadorPlantado:
    """Faz as vezes de `Courier` na classificacao de assinatura."""


class AAuditoriaTests(unittest.TestCase):
    """O resultado, e a garantia de que ele nao e vazio."""

    def setUp(self):
        self.linhas = auditar()
        self.encontrados = achados(self.linhas)

    def test_nenhuma_rota_admin_aceita_restaurant_id_do_cliente(self):
        """O restaurante sai do TOKEN, ponto.

        Uma rota que o aceitasse nao seria vazamento por si — o service
        confrontaria com o token —, mas seria a rota em que o proximo `if`
        esquecido vira vazamento. `AdminOrderService.list_orders` registra
        exatamente essa historia: o `restaurant_id` era slug na URL e foi
        RETIRADO, e nao consertado.
        """
        assert not self.encontrados["restaurante_do_cliente"], [
            linha["rota"] for linha in self.encontrados["restaurante_do_cliente"]
        ]

    def test_toda_rota_admin_recebe_o_escopo(self):
        assert not self.encontrados["sem_escopo"], (
            "Rota /admin sem `AdminScope` e fora da lista de excecoes:\n"
            + "\n".join(f"    {linha['rota']}" for linha in self.encontrados["sem_escopo"])
            + "\n\nSe a rota nao precisa mesmo de escopo, acrescente-a a "
            "`SEM_ESCOPO_ESPERADO` em scripts/escopo_das_rotas.py COM O MOTIVO."
        )

    def test_todo_branch_id_do_cliente_e_conferido_contra_o_RESTAURANTE(self):
        """A conferencia que o dono depende, e a unica que o protege.

        `ensure_branch_allowed` NAO cobre este caso: ele so recusa quando
        `scope.branch_id` esta preenchido, e o do dono e sempre nulo. Numa rota
        que so o chame, o dono do restaurante A alcanca a filial do B.
        """
        grupo = self.encontrados["filial_sem_conferencia_de_restaurante"]
        assert not grupo, (
            "Rota com `branch_id` do cliente que nao confere o RESTAURANTE:\n"
            + "\n".join(f"    {linha['rota']}  ({linha['funcao']})" for linha in grupo)
            + "\n\nO padrao e `AdminSettingsService._get_branch`: "
            "`ensure_branch_allowed` E um repositorio com `scope.restaurant_id`."
        )

    def test_todo_branch_id_do_cliente_e_conferido_contra_a_FILIAL(self):
        """A conferencia que o gerente preso a uma loja depende."""
        grupo = self.encontrados["filial_sem_conferencia_de_filial"]
        assert not grupo, (
            "Rota com `branch_id` do cliente sem `ensure_branch_allowed` nem "
            "`resolve_branch_filter`:\n"
            + "\n".join(f"    {linha['rota']}  ({linha['funcao']})" for linha in grupo)
        )

    def test_toda_rota_do_entregador_recebe_o_entregador_autenticado(self):
        """Sem `Courier` na assinatura a rota nao tem de quem recortar nada —
        e o link com o codigo nem foi conferido."""
        grupo = self.encontrados["entregador_sem_identidade"]
        assert not grupo, [linha["rota"] for linha in grupo]

    def test_nenhuma_rota_do_entregador_aceita_identificador_do_cliente(self):
        """`restaurant_id`, `branch_id` e `courier_id` nao viajam em rota
        `/courier`: o escopo dele e a atribuicao, que sai do banco."""
        grupo = self.encontrados["entregador_aceita_id_do_cliente"]
        assert not grupo, [linha["rota"] for linha in grupo]

    def test_toda_consulta_do_entregador_e_recortada_por_ele(self):
        """O `WHERE courier_id = :c` visto de fora. Sem ele, a rota lista o
        pedido de todo motoboy da plataforma — sem erro e sem log."""
        grupo = self.encontrados["entregador_sem_conferencia"]
        assert not grupo, (
            "Rota /courier cuja cadeia nao passa `courier.id` a consulta nenhuma:\n"
            + "\n".join(f"    {linha['rota']}  ({linha['funcao']})" for linha in grupo)
            + "\n\nSe a rota nao consulta nada mesmo, acrescente-a a "
            "`SEM_CONSULTA_DO_ENTREGADOR_ESPERADA` em scripts/escopo_das_rotas.py "
            "COM O MOTIVO."
        )

    def test_a_cadeia_de_toda_rota_foi_seguida_ate_o_fim(self):
        """Cadeia nao seguida e ACHADO, nunca "ok".

        Se o varredor deixar de resolver um tipo — um service novo construido
        de outro jeito, um `__init__` que ele nao entende —, a rota aparece
        aqui em vez de sumir do relatorio. Um varredor que responde "conferido"
        para o que ele nao leu transforma ignorancia em garantia.
        """
        grupo = self.encontrados["nao_consegui_seguir"]
        assert not grupo, [linha["rota"] for linha in grupo]

    def test_a_auditoria_enxerga_rotas_admin_de_verdade(self):
        """ANTI-VACUIDADE, e a licao vem de `tests/rotas_do_app.py`.

        Todos os testes acima afirmam a AUSENCIA de algo numa lista. Com a
        lista vazia, os cinco passam e a auditoria inteira deixa de existir sem
        um vermelho sequer — foi exatamente o que aconteceu quando o starlette
        parou de copiar as rotas para `app.routes`.

        Os dois numeros sao baixos de proposito: eles nao acompanham o
        crescimento do painel, so recusam o zero e o quase-zero.
        """
        assert len(self.linhas) > 50, f"so {len(self.linhas)} rotas /admin encontradas"
        com_filial = [linha for linha in self.linhas if linha["aceita_filial"]]
        assert len(com_filial) > 20, (
            f"so {len(com_filial)} rotas com `branch_id` do cliente. Se o painel "
            "mudou tanto assim, o numero desce; se o varredor parou de enxergar "
            "o `branch_id`, ele sobe vermelho aqui em vez de sumir calado."
        )


# ---------------------------------------------------------------------------
# As iscas — funcoes de pytest porque precisam de `tmp_path`
# ---------------------------------------------------------------------------


def test_isca_rota_que_nao_confere_nada_e_acusada(tmp_path):
    """A isca crua: `branch_id` do cliente indo direto ao repositorio."""
    filial, restaurante, completo = _conferencias(tmp_path, "rota_sem_conferencia_nenhuma")

    assert completo, "o varredor nao conseguiu seguir a cadeia plantada"
    assert not filial
    assert not restaurante


def test_isca_que_so_chama_ensure_branch_allowed_e_acusada(tmp_path):
    """A ISCA QUE IMPORTA, e a que um varredor ingenuo deixaria passar.

    `ensure_branch_allowed` esta la, a rota "parece" conferida — e para o DONO
    ela nao confere nada, porque `scope.branch_id` e nulo e o metodo retorna na
    primeira linha. Sem a segunda conferencia, o dono do restaurante A alcanca
    a filial do B.

    Se este teste ficar verde com `restaurante=True`, o varredor passou a
    aceitar meia conferencia — e e exatamente esse o erro que ele existe para
    nao deixar passar.
    """
    filial, restaurante, completo = _conferencias(tmp_path, "rota_so_com_ensure_branch")

    assert completo
    assert filial, "a conferencia de FILIAL estava la e nao foi vista"
    assert not restaurante, (
        "o varredor considerou o restaurante conferido, e a rota plantada so "
        "chama `ensure_branch_allowed` — que e no-op para o dono"
    )


def test_o_padrao_legitimo_nao_e_acusado(tmp_path):
    """O outro lado: o padrao certo NAO pode virar vermelho.

    Se virasse, a regra deixaria de ser seguivel e o varredor viraria ruido —
    e varredor que grita no caminho certo e varredor que se aprende a ignorar.
    E o formato de `AdminSettingsService._get_branch`, copiado.
    """
    filial, restaurante, completo = _conferencias(tmp_path, "rota_certa")

    assert completo
    assert filial
    assert restaurante


def test_isca_do_entregador_que_consulta_pela_filial_e_acusada(tmp_path):
    """A isca que importa deste lado: a rota recorta pela FILIAL do motoboy,
    e nao por ele. Parece escopo — e e o gerente da loja lendo a lista de
    todos os motoboys, so que sem senha de gerente."""
    entregador, completo = _conferencia_do_entregador(tmp_path, "rota_do_entregador_sem_recorte")

    assert completo, "o varredor nao conseguiu seguir a cadeia plantada"
    assert not entregador, (
        "o varredor considerou o entregador conferido, e a rota plantada so "
        "passa `courier.branch_id` — que e a loja inteira, nao o motoboy"
    )


def test_o_padrao_legitimo_do_entregador_nao_e_acusado(tmp_path):
    entregador, completo = _conferencia_do_entregador(tmp_path, "rota_do_entregador_certa")

    assert completo
    assert entregador


def test_a_assinatura_sem_o_entregador_e_acusada():
    recebe, proibido = _classificar_assinatura_do_entregador(
        {"link_token": str, "db": object}, "/courier/{link_token}/orders", _EntregadorPlantado
    )

    assert not recebe
    assert not proibido


def test_a_assinatura_com_identificador_proibido_e_acusada():
    """Nos dois lugares em que ele poderia entrar: parametro e caminho."""
    _, por_parametro = _classificar_assinatura_do_entregador(
        {"courier": _EntregadorPlantado, "branch_id": str}, "/courier/{link_token}/x", _EntregadorPlantado
    )
    _, por_caminho = _classificar_assinatura_do_entregador(
        {"courier": _EntregadorPlantado, "courier_id": str},
        "/courier/{courier_id}/x",
        _EntregadorPlantado,
    )

    assert por_parametro
    assert por_caminho


def test_a_assinatura_certa_do_entregador_nao_e_acusada():
    recebe, proibido = _classificar_assinatura_do_entregador(
        {"link_token": str, "courier": _EntregadorPlantado, "order_id": str},
        "/courier/{link_token}/orders/{order_id}/delivered",
        _EntregadorPlantado,
    )

    assert recebe
    assert not proibido


def test_o_indice_separa_funcoes_de_modulos_diferentes(tmp_path):
    """Regressao do defeito que custou a primeira execucao.

    `list_orders` existe em `admin_orders.py` (a rota do painel) e em
    `customers.py` (o `/me/orders` do cliente). Com o indice por nome puro, o
    ultimo arquivo lido vencia e a varredura auditava o corpo errado.

    Aqui o mesmo nome mora em dois modulos plantados, e as duas entradas tem
    que existir separadas.
    """
    um = "def mesmo_nome(a):" + chr(10) + "    return 1" + chr(10)
    dois = "def mesmo_nome(a):" + chr(10) + "    return 2" + chr(10)
    (tmp_path / "um.py").write_text(um, encoding="utf-8")
    (tmp_path / "dois.py").write_text(dois, encoding="utf-8")

    indice = _Indice(diretorios=[tmp_path])

    assert ("um", "mesmo_nome") in indice.funcoes
    assert ("dois", "mesmo_nome") in indice.funcoes


def test_nao_ha_duas_classes_com_o_mesmo_nome_em_src(tmp_path):
    """A colisao de nome de CLASSE quebraria a resolucao do mesmo jeito.

    Nao ha nenhuma hoje. Se aparecer, `_classe_do_receptor` passa a resolver
    para a classe errada — e o varredor volta a auditar corpo alheio, agora
    sem ninguem perceber.
    """
    indice = _Indice()

    assert not indice.classes_repetidas, (
        "Duas classes com o mesmo nome em src/services ou src/api/endpoints: "
        f"{sorted(indice.classes_repetidas)}. O varredor de escopo resolve "
        "metodo por nome de classe e passaria a auditar o corpo errado."
    )


if __name__ == "__main__":
    unittest.main()
