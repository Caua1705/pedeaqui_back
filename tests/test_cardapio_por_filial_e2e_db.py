"""O cardápio por filial pela HTTP, ponta a ponta.

Os testes unitários provam que cada repositório recebeu `branch_id`. Este
prova a única coisa que eles não conseguem provar: **que as superfícies não
discordam.**

Enquanto o cardápio foi do restaurante, "quais produtos existem" tinha UMA
resposta e não havia como duas rotas divergirem. Agora há — e as divergências
possíveis são todas silenciosas:

    GET  .../menu?branch_id=B          →  produtos de B, e só de B
    GET  .../products/{slug}           →  o produto da filial padrão
    POST .../orders com produto de A   →  400, e não pedido aceito
    POST /voice/search para B          →  nada de A (ver test_voice_fumaca_db)
    GET  /admin/products               →  só o que o token alcança

**Nenhuma delas falha sozinha.** Um `/menu` que devolvesse os dois cardápios
juntos responde 200; um `POST /orders` que aceitasse produto da outra loja
responde 200 e grava o pedido. É por isso que este arquivo existe.

A rede aqui é a do Júnior depois da migração: duas lojas, o mesmo item em
cada uma, `catalog_key` compartilhada — e uma exclusividade de cada lado, que
é o que a operação real produz na primeira semana.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.printing_sector_model import PrintingSector
from src.services.admin_auth_service import AdminAuthService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _abre_a_semana_inteira(db: Session, filial) -> None:
    for weekday in range(7):
        db.add(BranchBusinessHour(
            branch_id=filial.id,
            weekday=weekday,
            opens_at="00:00",
            closes_at="23:59",
            prep_time_min=20,
            prep_time_max=30,
            is_closed=False,
            sort_order=0,
        ))
    db.flush()


def _admin(db: Session, restaurante, filial, role: str) -> AdminUser:
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        name=f"Usuario {role}",
        email=f"{role}-{fab._sufixo()}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _como(usuario: AdminUser) -> dict:
    return {"Authorization": f"Bearer {AdminAuthService.create_access_token(usuario)}"}


@pytest.fixture
def rede(db: Session):
    """Duas lojas com cardápios independentes, e um item em comum.

    - **Picanha** nas duas, mesma `catalog_key`, PREÇOS DIFERENTES. É o caso
      que a rede real produz e o que torna "qual das duas?" uma pergunta com
      consequência em dinheiro.
    - **Costela** só no Centro; **Tapioca** só na Aldeota. As exclusividades
      são o que distingue "filtrou" de "por acaso deu igual".
    """
    restaurante = fab.criar_restaurante(db, "Junior da Picanha")
    centro = fab.criar_filial(db, restaurante, "Centro")
    centro.is_main = True
    aldeota = fab.criar_filial(db, restaurante, "Aldeota")
    db.flush()
    fab.criar_configuracoes(db, restaurante)

    catalogo = {}
    for filial, preco_da_picanha, exclusivo in (
        (centro, Decimal("89.90"), "Costela"),
        (aldeota, Decimal("99.90"), "Tapioca"),
    ):
        categoria = fab.criar_categoria(db, restaurante, nome="Carnes", filial=filial)
        picanha = fab.criar_produto(
            db, restaurante, categoria, nome="Picanha",
            preco=preco_da_picanha, catalog_key="picanha",
        )
        # O MESMO slug nas duas lojas, que e o estado que a migracao deixa: a
        # copia leva o slug da origem, e o indice unico passou a ser
        # `(branch_id, slug)`. E o que a rota publica de produto tem que
        # desambiguar.
        picanha.slug = "picanha"
        catalogo[(filial.name, "Picanha")] = picanha
        catalogo[(filial.name, exclusivo)] = fab.criar_produto(
            db, restaurante, categoria, nome=exclusivo, preco=Decimal("40.00")
        )
        catalogo[(filial.name, "categoria")] = categoria

        _abre_a_semana_inteira(db, filial)
        db.add(BranchPaymentMethod(
            branch_id=filial.id,
            payment_flow="delivery",
            method_type="cash",
            label="Dinheiro",
            enabled=True,
            requires_gateway=False,
            sort_order=0,
        ))

    dono = _admin(db, restaurante, None, "owner")
    gerente_do_centro = _admin(db, restaurante, centro, "manager")
    db.flush()
    db.expire_all()

    return {
        "restaurante": restaurante,
        "centro": centro,
        "aldeota": aldeota,
        "catalogo": catalogo,
        "dono": dono,
        "gerente_do_centro": gerente_do_centro,
    }


def _pedido(rede, filial, produto) -> dict:
    return {
        "branch_id": str(filial.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(produto.id), "quantity": 1}],
    }


# ---------------------------------------------------------------------------
# O cardápio público
# ---------------------------------------------------------------------------


class TestOMenuPublico:
    def test_cada_filial_devolve_o_proprio_cardapio(self, cliente_http, rede):
        slug = rede["restaurante"].slug

        centro = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
        aldeota = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")

        assert centro.status_code == 200, centro.text[:300]
        assert sorted(p["name"] for p in centro.json()["products"]) == ["Costela", "Picanha"]
        assert sorted(p["name"] for p in aldeota.json()["products"]) == ["Picanha", "Tapioca"]

    def test_o_preco_da_picanha_e_o_da_loja_pedida(self, cliente_http, rede):
        """A divergência que o passo inteiro existe para permitir.

        Antes disto havia UM preço; agora há dois, e o cliente tem que ver o
        da loja em que ele vai pedir.
        """
        slug = rede["restaurante"].slug

        centro = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
        aldeota = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")

        assert _preco_de(centro, "Picanha") == 89.90
        assert _preco_de(aldeota, "Picanha") == 99.90

    def test_sem_branch_id_vale_a_filial_padrao(self, cliente_http, rede):
        """A mesma regra de `/info` e de `/delivery/estimate`.

        Sem esta queda, todo link de cardápio já divulgado responderia erro no
        dia do deploy.
        """
        slug = rede["restaurante"].slug

        sem_parametro = cliente_http.get(f"/restaurants/{slug}/menu")

        assert sem_parametro.json()["branch_id"] == str(rede["centro"].id)
        assert sorted(p["name"] for p in sem_parametro.json()["products"]) == [
            "Costela", "Picanha",
        ]

    def test_a_resposta_diz_de_qual_filial_ela_e(self, cliente_http, rede):
        """`branch_id` na raiz, e `settings_branch_id` ecoando o mesmo valor.

        O segundo é obsoleto e fica só pelo tempo de o painel trocar de campo
        — renomear no lugar quebraria painel e app de uma vez (armadilha 16).
        """
        slug = rede["restaurante"].slug

        resposta = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")

        corpo = resposta.json()
        assert corpo["branch_id"] == str(rede["aldeota"].id)
        assert corpo["settings_branch_id"] == corpo["branch_id"]
        for produto in corpo["products"]:
            assert produto["branch_id"] == corpo["branch_id"]

    def test_o_payment_methods_morto_saiu_da_resposta(self, cliente_http, rede):
        """Quem manda em forma de pagamento é `branch_payment_methods`.

        Enquanto o jsonb foi ecoado aqui, um app que acreditasse nele mostrava
        ao cliente uma forma que `POST /orders` recusa com 400 (armadilha 15).
        """
        slug = rede["restaurante"].slug

        resposta = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")

        assert "payment_methods" not in resposta.json()["settings"]

    def test_filial_de_outro_restaurante_e_404(self, cliente_http, rede, db):
        vizinho = fab.criar_restaurante(db, "O Vizinho")
        filial_do_vizinho = fab.criar_filial(db, vizinho, "Unica")
        db.flush()

        resposta = cliente_http.get(
            f"/restaurants/{rede['restaurante'].slug}/menu?branch_id={filial_do_vizinho.id}"
        )

        assert resposta.status_code == 404


def _preco_de(resposta, nome: str) -> float:
    return next(p["price"] for p in resposta.json()["products"] if p["name"] == nome)


# ---------------------------------------------------------------------------
# O link público do produto
# ---------------------------------------------------------------------------


class TestOLinkDoProduto:
    def test_o_mesmo_slug_existe_nas_duas_lojas_e_cada_uma_devolve_o_seu(
        self, cliente_http, rede
    ):
        """A consequência direta do índice `(branch_id, slug)`.

        Com unicidade por restaurante, a picanha da segunda loja teria que se
        chamar `picanha-2` — e o slug é a URL que o cliente lê.
        """
        slug = rede["restaurante"].slug
        do_centro = rede["catalogo"][("Centro", "Picanha")]
        da_aldeota = rede["catalogo"][("Aldeota", "Picanha")]
        assert do_centro.slug == da_aldeota.slug == "picanha"

        centro = cliente_http.get(
            f"/restaurants/{slug}/products/picanha?branch_id={rede['centro'].id}"
        )
        aldeota = cliente_http.get(
            f"/restaurants/{slug}/products/picanha?branch_id={rede['aldeota'].id}"
        )

        assert centro.json()["id"] == str(do_centro.id)
        assert centro.json()["price"] == 89.90
        assert aldeota.json()["id"] == str(da_aldeota.id)
        assert aldeota.json()["price"] == 99.90

    def test_sem_branch_id_o_link_antigo_abre_o_produto_da_filial_padrao(
        self, cliente_http, rede
    ):
        """A promessa feita ao front: link divulgado continua funcionando.

        A migração deixou as linhas que já existiam NA filial padrão, com os
        mesmos ids e slugs — então um link sem parâmetro resolve exatamente
        onde sempre resolveu.
        """
        slug = rede["restaurante"].slug
        do_centro = rede["catalogo"][("Centro", "Picanha")]

        resposta = cliente_http.get(f"/restaurants/{slug}/products/picanha")

        assert resposta.status_code == 200
        assert resposta.json()["id"] == str(do_centro.id)
        assert resposta.json()["price"] == 89.90

    def test_produto_exclusivo_da_filial_nao_padrao_e_404_pelo_link_sem_parametro(
        self, cliente_http, rede
    ):
        """E é a resposta correta: sem loja escolhida não há preço a mostrar.

        Escolher uma loja por conta própria é o defeito que o passo 2 fechou.
        """
        slug = rede["restaurante"].slug
        tapioca = rede["catalogo"][("Aldeota", "Tapioca")]

        sem_parametro = cliente_http.get(f"/restaurants/{slug}/products/{tapioca.slug}")
        com_parametro = cliente_http.get(
            f"/restaurants/{slug}/products/{tapioca.slug}?branch_id={rede['aldeota'].id}"
        )

        assert sem_parametro.status_code == 404
        assert com_parametro.status_code == 200


# ---------------------------------------------------------------------------
# O pedido
# ---------------------------------------------------------------------------


class TestOPedido:
    def test_produto_de_uma_filial_nao_fecha_pedido_na_outra(self, cliente_http, rede):
        """O segundo modo de falha silencioso do passo, e o mais direto.

        Sem o filtro por filial em `_get_valid_products`, este POST responde
        200: o produto existe, está ativo, está disponível, e é do mesmo
        restaurante. O pedido entra na Aldeota com a costela do Centro e o
        preço do Centro, e quem descobre é a cozinha.
        """
        slug = rede["restaurante"].slug
        costela_do_centro = rede["catalogo"][("Centro", "Costela")]

        recusado = cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], costela_do_centro),
        )

        assert recusado.status_code == 400, recusado.text[:300]
        assert "Produto" in recusado.json()["detail"]

    def test_o_produto_da_propria_filial_fecha_normalmente(self, cliente_http, rede):
        slug = rede["restaurante"].slug
        tapioca = rede["catalogo"][("Aldeota", "Tapioca")]

        aceito = cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], tapioca),
        )

        assert aceito.status_code == 200, aceito.text[:300]

    def test_a_picanha_de_cada_loja_entra_com_o_preco_dela(self, cliente_http, rede):
        """Duas linhas de `products`, dois preços, e o pedido pega o certo.

        Nenhum preço vem do cliente: o corpo manda só `product_id`, e é o
        `branch_id` do pedido que decide qual das duas linhas o servidor pode
        ler.
        """
        slug = rede["restaurante"].slug

        no_centro = cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["centro"], rede["catalogo"][("Centro", "Picanha")]),
        )
        na_aldeota = cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], rede["catalogo"][("Aldeota", "Picanha")]),
        )

        assert no_centro.json()["subtotal"] == 89.90
        assert na_aldeota.json()["subtotal"] == 99.90


# ---------------------------------------------------------------------------
# O painel
# ---------------------------------------------------------------------------


class TestOPainel:
    def test_o_dono_ve_as_duas_lojas_e_cada_linha_diz_qual(self, cliente_http, rede):
        resposta = cliente_http.get("/admin/products", headers=_como(rede["dono"]))

        assert resposta.status_code == 200, resposta.text[:300]
        filiais = {item["branch_id"] for item in resposta.json()["items"]}
        assert filiais == {str(rede["centro"].id), str(rede["aldeota"].id)}

    def test_o_filtro_de_filial_so_restringe(self, cliente_http, rede):
        resposta = cliente_http.get(
            f"/admin/products?branch_id={rede['aldeota'].id}", headers=_como(rede["dono"])
        )

        nomes = sorted(item["name"] for item in resposta.json()["items"])
        assert nomes == ["Picanha", "Tapioca"]

    def test_o_gerente_preso_a_uma_loja_ve_so_a_dele(self, cliente_http, rede):
        resposta = cliente_http.get(
            "/admin/products", headers=_como(rede["gerente_do_centro"])
        )

        nomes = sorted(item["name"] for item in resposta.json()["items"])
        assert nomes == ["Costela", "Picanha"]

    def test_o_gerente_nao_edita_o_produto_da_outra_loja(self, cliente_http, rede):
        """A regra que era INEXPRIMÍVEL antes deste passo.

        Enquanto o cardápio não tinha filial, não havia coluna para restringir
        e o gerente do Centro editava o cardápio da rede inteira — o
        `admin_menu_service.py` registrava isso como limitação. Agora é 404,
        porque para quem está preso ao Centro a Aldeota não existe.
        """
        tapioca = rede["catalogo"][("Aldeota", "Tapioca")]

        resposta = cliente_http.patch(
            f"/admin/products/{tapioca.id}",
            json={"name": "Tapioca de Coco"},
            headers=_como(rede["gerente_do_centro"]),
        )

        assert resposta.status_code == 404, resposta.text[:300]

    def test_o_gerente_nao_LE_o_produto_da_outra_loja(self, cliente_http, rede):
        """A conferência vale para leitura, não só para escrita.

        A tela de edição traz o preço, e o preço de cada loja é dela. Uma
        rota de leitura aberta seria a forma mais silenciosa de vazar o
        cardápio da filial do lado.
        """
        tapioca = rede["catalogo"][("Aldeota", "Tapioca")]

        resposta = cliente_http.get(
            f"/admin/products/{tapioca.id}", headers=_como(rede["gerente_do_centro"])
        )

        assert resposta.status_code == 404, resposta.text[:300]

    def test_o_gerente_nao_aponta_o_setor_do_produto_da_outra_loja(
        self, cliente_http, rede, db
    ):
        """Setor e produto podem ser os DOIS da loja que ele não alcança.

        `_ensure_sector_is_in_branch` compara produto com setor e passaria
        feliz aqui: os dois são da Aldeota. Quem barra é o escopo do token.
        """
        cozinha_da_aldeota = PrintingSector(branch_id=rede["aldeota"].id, name="Cozinha")
        db.add(cozinha_da_aldeota)
        db.flush()
        tapioca = rede["catalogo"][("Aldeota", "Tapioca")]

        resposta = cliente_http.patch(
            f"/admin/products/{tapioca.id}/printing-sector",
            json={"printing_sector_id": str(cozinha_da_aldeota.id)},
            headers=_como(rede["gerente_do_centro"]),
        )

        assert resposta.status_code == 404, resposta.text[:300]

    def test_o_gerente_nao_cria_categoria_na_outra_loja(self, cliente_http, rede):
        resposta = cliente_http.post(
            "/admin/categories",
            json={"branch_id": str(rede["aldeota"].id), "name": "Sobremesas"},
            headers=_como(rede["gerente_do_centro"]),
        )

        assert resposta.status_code == 404, resposta.text[:300]

    def test_o_mesmo_nome_de_produto_pode_existir_nas_duas_lojas(self, cliente_http, rede):
        """O 409 de slug passou a ser por filial.

        Por restaurante, a segunda loja não conseguiria cadastrar a própria
        "Costela" — e o lojista teria que inventar um nome para satisfazer um
        índice.
        """
        criada = cliente_http.post(
            "/admin/products",
            json={
                "category_id": str(rede["catalogo"][("Aldeota", "categoria")].id),
                "name": "Costela",
                "price": "70.00",
            },
            headers=_como(rede["dono"]),
        )

        assert criada.status_code == 201, criada.text[:300]
        assert criada.json()["branch_id"] == str(rede["aldeota"].id)

    def test_produto_nasce_na_filial_da_categoria(self, cliente_http, rede):
        """Não há `branch_id` no corpo: a categoria já diz a loja."""
        criada = cliente_http.post(
            "/admin/products",
            json={
                "category_id": str(rede["catalogo"][("Centro", "categoria")].id),
                "name": "Fraldinha",
                "price": "60.00",
            },
            headers=_como(rede["dono"]),
        )

        assert criada.json()["branch_id"] == str(rede["centro"].id)

    def test_produto_nao_muda_de_filial_por_troca_de_categoria(self, cliente_http, rede):
        """400 com frase, e não o 500 da violação de FK.

        Mover a linha levaria junto grupos de opção, setor de impressão e a
        chave de catálogo, e deixaria o histórico de pedido apontando para um
        produto que aquela loja não vende mais.
        """
        costela = rede["catalogo"][("Centro", "Costela")]
        categoria_da_aldeota = rede["catalogo"][("Aldeota", "categoria")]

        resposta = cliente_http.patch(
            f"/admin/products/{costela.id}",
            json={"category_id": str(categoria_da_aldeota.id)},
            headers=_como(rede["dono"]),
        )

        assert resposta.status_code == 400, resposta.text[:300]
        assert "outra filial" in resposta.json()["detail"]


# ---------------------------------------------------------------------------
# A chave de catálogo
# ---------------------------------------------------------------------------


class TestAChaveDeCatalogo:
    def test_a_mesma_chave_se_repete_entre_lojas_e_nao_dentro_de_uma(
        self, cliente_http, rede
    ):
        """Repetir entre lojas é o USO; repetir dentro de uma é o defeito.

        Duas linhas da mesma loja com a mesma chave fariam o relatório contar
        a mesma venda duas vezes — o oposto do que a coluna existe para fazer.
        """
        colidindo = cliente_http.post(
            "/admin/products",
            json={
                "category_id": str(rede["catalogo"][("Centro", "categoria")].id),
                "name": "Picanha Premium",
                "price": "120.00",
                "catalog_key": "picanha",
            },
            headers=_como(rede["dono"]),
        )
        assert colidindo.status_code == 409, colidindo.text[:300]

        # A MESMA chave na outra loja passa: é para isso que ela serve.
        na_outra = cliente_http.post(
            "/admin/products",
            json={
                "category_id": str(rede["catalogo"][("Aldeota", "categoria")].id),
                "name": "Costela da Casa",
                "price": "70.00",
                "catalog_key": "costela",
            },
            headers=_como(rede["dono"]),
        )
        assert na_outra.status_code == 201, na_outra.text[:300]


# ---------------------------------------------------------------------------
# O setor de impressão
# ---------------------------------------------------------------------------


class TestOSetorDeImpressao:
    def test_apontar_produto_para_setor_de_outra_filial_e_400(self, cliente_http, rede, db):
        """A porta que a FK composta fecha, com frase em vez de traceback.

        Este era o estado que produzia a via "SEM SETOR" da armadilha 13 — e
        ele não era um bug de código, era o único desenho possível enquanto o
        produto pendia de restaurante e o setor de filial.
        """
        cozinha_da_aldeota = PrintingSector(branch_id=rede["aldeota"].id, name="Cozinha")
        db.add(cozinha_da_aldeota)
        db.flush()
        costela_do_centro = rede["catalogo"][("Centro", "Costela")]

        resposta = cliente_http.patch(
            f"/admin/products/{costela_do_centro.id}/printing-sector",
            json={"printing_sector_id": str(cozinha_da_aldeota.id)},
            headers=_como(rede["dono"]),
        )

        assert resposta.status_code == 400, resposta.text[:300]
        assert "outra filial" in resposta.json()["detail"]

    def test_o_setor_da_propria_filial_e_aceito(self, cliente_http, rede, db):
        cozinha_do_centro = PrintingSector(branch_id=rede["centro"].id, name="Cozinha")
        db.add(cozinha_do_centro)
        db.flush()
        costela = rede["catalogo"][("Centro", "Costela")]

        resposta = cliente_http.patch(
            f"/admin/products/{costela.id}/printing-sector",
            json={"printing_sector_id": str(cozinha_do_centro.id)},
            headers=_como(rede["dono"]),
        )

        assert resposta.status_code == 200, resposta.text[:300]
        assert resposta.json()["printing_sector_id"] == str(cozinha_do_centro.id)


# ---------------------------------------------------------------------------
# Os relatórios
# ---------------------------------------------------------------------------


class TestOsRelatorios:
    def test_a_chave_de_catalogo_soma_as_duas_lojas_numa_linha(self, cliente_http, rede):
        """A pergunta que o passo inteiro precisava conseguir responder.

        Sem `catalog_key`, a picanha do Centro e a da Aldeota são dois
        `product_id` diferentes e o relatório lista "Picanha" duas vezes — o
        dono nunca vê quanto vendeu na rede.
        """
        slug = rede["restaurante"].slug
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["centro"], rede["catalogo"][("Centro", "Picanha")]),
        )
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], rede["catalogo"][("Aldeota", "Picanha")]),
        )

        relatorio = cliente_http.get(
            f"/admin/reports/products?{_periodo()}", headers=_como(rede["dono"])
        )

        assert relatorio.status_code == 200, relatorio.text[:300]
        picanhas = [
            item for item in relatorio.json()["products"] if item["product_name"] == "Picanha"
        ]
        assert len(picanhas) == 1
        assert picanhas[0]["quantity_total"] == 2
        assert picanhas[0]["catalog_key"] == "picanha"

    def test_com_recorte_de_filial_cada_loja_conta_a_sua(self, cliente_http, rede):
        slug = rede["restaurante"].slug
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["centro"], rede["catalogo"][("Centro", "Picanha")]),
        )
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], rede["catalogo"][("Aldeota", "Picanha")]),
        )

        so_do_centro = cliente_http.get(
            f"/admin/reports/products?{_periodo()}&branch_id={rede['centro'].id}",
            headers=_como(rede["dono"]),
        )

        picanhas = [
            item
            for item in so_do_centro.json()["products"]
            if item["product_name"] == "Picanha"
        ]
        assert picanhas[0]["quantity_total"] == 1

    def test_produto_sem_chave_continua_contado_por_linha_de_products(
        self, cliente_http, rede
    ):
        """A metade que preserva o comportamento antigo.

        Costela e Tapioca não têm chave e são produtos diferentes; se a
        identidade fosse só a chave, os dois nulos cairiam no mesmo grupo e o
        relatório somaria coisas sem relação.
        """
        slug = rede["restaurante"].slug
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["centro"], rede["catalogo"][("Centro", "Costela")]),
        )
        cliente_http.post(
            f"/restaurants/{slug}/orders",
            json=_pedido(rede, rede["aldeota"], rede["catalogo"][("Aldeota", "Tapioca")]),
        )

        relatorio = cliente_http.get(
            f"/admin/reports/products?{_periodo()}", headers=_como(rede["dono"])
        )

        por_nome = {
            item["product_name"]: item["quantity_total"]
            for item in relatorio.json()["products"]
        }
        assert por_nome["Costela"] == 1
        assert por_nome["Tapioca"] == 1


def _periodo() -> str:
    from datetime import date, timedelta

    hoje = date.today()
    return f"start_date={hoje - timedelta(days=1)}&end_date={hoje + timedelta(days=1)}"
