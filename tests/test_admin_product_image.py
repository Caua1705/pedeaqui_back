"""Upload da imagem de produto (BLOCO B5 da Fase 3).

O risco aqui nao e o arquivo grande — e o arquivo que MENTE. O bucket serve
os objetos publicamente, entao um HTML aceito como "image/png" viraria
script executando no dominio do Storage. Por isso o tipo sai dos bytes e
nao do content-type do multipart, e e isso que a maior parte destes testes
prova.

O segundo risco e a ordem: o objeto vai para o Storage antes do commit. Se
fosse ao contrario, uma falha do Storage deixaria o produto apontando para
um arquivo que nao existe.
"""

import unittest
import uuid
from types import SimpleNamespace

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.api.middleware.body_size import BodySizeLimitMiddleware
from src.integrations.supabase_storage_client import (
    SupabaseStorageError,
    SupabaseStorageNotConfiguredError,
)
from src.services.admin_menu_service import AdminMenuService
from src.utils.images import detect_image_extension


RESTAURANT_ID = uuid.uuid4()
OTHER_RESTAURANT_ID = uuid.uuid4()

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 64
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"0" * 64


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeMenuRepository:
    def __init__(self, product):
        self.product = product

    def get_product(self, product_id, restaurant_id):
        if self.product.id == product_id and self.product.restaurant_id == restaurant_id:
            return self.product
        return None


class FakeRestaurantRepository:
    def __init__(self, slug="pizzaria-do-ze"):
        self.restaurant = SimpleNamespace(id=RESTAURANT_ID, slug=slug)

    def get_by_id(self, restaurant_id):
        return self.restaurant if self.restaurant.id == restaurant_id else None


class FakeStorageClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def upload(self, object_path, content, content_type):
        if self.error is not None:
            raise self.error
        self.calls.append((object_path, content, content_type))
        return object_path


def make_product(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "image_path": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def build_service(product, storage_client=None):
    service = AdminMenuService(FakeDb(), storage_client=storage_client or FakeStorageClient())
    service.repository = FakeMenuRepository(product)
    service.restaurant_repository = FakeRestaurantRepository()
    return service


def scope(restaurant_id=RESTAURANT_ID):
    return AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=None)


class ImageDetectionTests(unittest.TestCase):
    def test_known_formats_are_recognized_by_their_bytes(self):
        self.assertEqual(detect_image_extension(PNG_BYTES), "png")
        self.assertEqual(detect_image_extension(JPEG_BYTES), "jpg")
        self.assertEqual(detect_image_extension(WEBP_BYTES), "webp")

    def test_html_disguised_as_image_is_rejected(self):
        # Este e o ataque real: o content-type do multipart e escolhido pelo
        # cliente, e o bucket serve o objeto publicamente.
        self.assertIsNone(detect_image_extension(b"<html><script>alert(1)</script>"))

    def test_svg_is_rejected(self):
        # SVG e XML e aceita <script> dentro; nao tem assinatura binaria.
        self.assertIsNone(detect_image_extension(b"<svg xmlns='http://www.w3.org/2000/svg'>"))


class UploadTests(unittest.TestCase):
    def test_object_goes_to_the_restaurant_folder(self):
        product = make_product()
        storage = FakeStorageClient()
        response = build_service(product, storage).upload_product_image(
            scope(), product.id, PNG_BYTES
        )

        object_path = storage.calls[0][0]
        self.assertTrue(object_path.startswith(f"pizzaria-do-ze/products/{product.id}-"))
        self.assertTrue(object_path.endswith(".png"))
        self.assertEqual(response.image_path, object_path)

    def test_content_type_comes_from_the_bytes(self):
        product = make_product()
        storage = FakeStorageClient()
        build_service(product, storage).upload_product_image(scope(), product.id, JPEG_BYTES)

        self.assertEqual(storage.calls[0][2], "image/jpeg")

    def test_each_upload_uses_a_new_path(self):
        product = make_product()
        storage = FakeStorageClient()
        service = build_service(product, storage)
        service.upload_product_image(scope(), product.id, PNG_BYTES)
        service.upload_product_image(scope(), product.id, PNG_BYTES)

        # Reusar o caminho faria o CDN continuar servindo a foto anterior, e
        # o lojista trocaria a imagem de novo achando que falhou.
        self.assertNotEqual(storage.calls[0][0], storage.calls[1][0])

    def test_product_points_to_the_new_object(self):
        product = make_product(image_path="antigo.png")
        storage = FakeStorageClient()
        build_service(product, storage).upload_product_image(scope(), product.id, PNG_BYTES)

        self.assertEqual(product.image_path, storage.calls[0][0])

    def test_invalid_format_is_refused_before_reaching_the_storage(self):
        product = make_product()
        storage = FakeStorageClient()
        with self.assertRaises(HTTPException) as raised:
            build_service(product, storage).upload_product_image(
                scope(), product.id, b"<html>nao sou imagem</html>"
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(storage.calls, [])

    def test_empty_file_is_refused(self):
        product = make_product()
        with self.assertRaises(HTTPException) as raised:
            build_service(product).upload_product_image(scope(), product.id, b"")

        self.assertEqual(raised.exception.status_code, 400)

    def test_file_above_the_limit_is_refused(self):
        product = make_product()
        # O middleware ja corta o corpo do multipart; este limite mede o
        # arquivo em si, que e o que vai para o bucket.
        oversized = PNG_BYTES + b"0" * 4_000_000
        with self.assertRaises(HTTPException) as raised:
            build_service(product).upload_product_image(scope(), product.id, oversized)

        self.assertEqual(raised.exception.status_code, 413)

    def test_product_of_another_restaurant_is_not_found(self):
        product = make_product(restaurant_id=OTHER_RESTAURANT_ID)
        storage = FakeStorageClient()
        with self.assertRaises(HTTPException) as raised:
            build_service(product, storage).upload_product_image(
                scope(), product.id, PNG_BYTES
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(storage.calls, [])

    def test_storage_without_key_answers_503(self):
        product = make_product()
        storage = FakeStorageClient(error=SupabaseStorageNotConfiguredError("sem chave"))
        with self.assertRaises(HTTPException) as raised:
            build_service(product, storage).upload_product_image(
                scope(), product.id, PNG_BYTES
            )

        self.assertEqual(raised.exception.status_code, 503)

    def test_storage_failure_keeps_the_previous_image(self):
        product = make_product(image_path="antigo.png")
        storage = FakeStorageClient(error=SupabaseStorageError("timeout"))
        with self.assertRaises(HTTPException) as raised:
            build_service(product, storage).upload_product_image(
                scope(), product.id, PNG_BYTES
            )

        self.assertEqual(raised.exception.status_code, 502)
        # Gravar image_path antes do upload deixaria o cardapio publico
        # apontando para um arquivo que nao existe.
        self.assertEqual(product.image_path, "antigo.png")


class BodyLimitTests(unittest.TestCase):
    """O teto de corpo maior vale SO para as rotas de upload."""

    def setUp(self):
        self.middleware = BodySizeLimitMiddleware(
            app=None, max_body_bytes=262_144, max_upload_bytes=3_145_728
        )

    def test_upload_route_gets_the_bigger_limit(self):
        scope_http = {"type": "http", "path": "/admin/products/abc/image"}

        self.assertEqual(self.middleware._limit_for(scope_http), 3_145_728)

    def test_every_other_route_keeps_the_json_limit(self):
        scope_http = {"type": "http", "path": "/admin/products"}

        self.assertEqual(self.middleware._limit_for(scope_http), 262_144)

    def test_limit_does_not_depend_on_the_declared_content_type(self):
        # O content-type e escolhido pelo cliente: se decidisse o limite,
        # bastaria declarar multipart em qualquer rota para libera-lo.
        scope_http = {
            "type": "http",
            "path": "/restaurants/x/orders",
            "headers": [(b"content-type", b"multipart/form-data")],
        }

        self.assertEqual(self.middleware._limit_for(scope_http), 262_144)


if __name__ == "__main__":
    unittest.main()
