"""Auditoria de indices (scripts/audit_indexes.py).

Testa as funcoes puras, nao a consulta ao banco. O que elas decidem:

1. Se dois indices sao a MESMA coisa apesar dos nomes diferentes. E a
   deteccao que o `if_not_exists=True` nao faz — ele casa por nome — e o
   modo de falha silencioso que o script existe para achar.
2. Qual dos dois nomes deve sobreviver. Esta regra ja nasceu errada uma vez:
   olhando "declarado por uma revisao" antes da convencao de nome, ela
   escolhia o nome legado sempre que uma revisao o recriava no downgrade.
"""

import importlib.util
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def load_audit_module():
    """Carrega o script por caminho: scripts/ nao e um pacote importavel."""
    path = ROOT_DIR / "scripts" / "audit_indexes.py"
    spec = importlib.util.spec_from_file_location("audit_indexes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_audit_module()


def index_row(name, definition, **overrides):
    values = {
        "schema_name": "public",
        "table_name": "order_items",
        "index_name": name,
        "is_primary": False,
        "is_unique": False,
        "constraint_type": None,
        "definition": definition,
        "size_bytes": 1024,
    }
    values.update(overrides)
    return values


class NormalizeDefinitionTests(unittest.TestCase):
    def test_same_index_under_two_names_is_recognized(self):
        # O caso real: `idx_order_items_order_id` criado a mao e
        # `ix_order_items_order_id` que a 20260810_0012 adota.
        legacy = "CREATE INDEX idx_order_items_order_id ON public.order_items USING btree (order_id)"
        canonical = "CREATE INDEX ix_order_items_order_id ON public.order_items USING btree (order_id)"

        self.assertEqual(
            audit.normalize_definition(legacy, "idx_order_items_order_id"),
            audit.normalize_definition(canonical, "ix_order_items_order_id"),
        )

    def test_different_column_lists_are_not_confused(self):
        two = "CREATE INDEX ix_a ON public.orders USING btree (restaurant_id, created_at)"
        three = "CREATE INDEX ix_b ON public.orders USING btree (restaurant_id, branch_id, created_at)"

        self.assertNotEqual(
            audit.normalize_definition(two, "ix_a"),
            audit.normalize_definition(three, "ix_b"),
        )

    def test_partial_index_is_not_the_same_as_the_full_one(self):
        # `uq_orders_provider_payment` (20260730_0004) e parcial. Comparar so
        # a lista de colunas na mao perderia essa diferenca e mandaria
        # derrubar um indice que cobre outra coisa.
        partial = (
            "CREATE UNIQUE INDEX ix_a ON public.orders USING btree "
            "(payment_provider, provider_payment_id) WHERE (provider_payment_id IS NOT NULL)"
        )
        full = (
            "CREATE UNIQUE INDEX ix_b ON public.orders USING btree "
            "(payment_provider, provider_payment_id)"
        )

        self.assertNotEqual(
            audit.normalize_definition(partial, "ix_a"),
            audit.normalize_definition(full, "ix_b"),
        )

    def test_unique_is_not_the_same_as_non_unique(self):
        unique = "CREATE UNIQUE INDEX ix_a ON public.orders USING btree (tracking_token)"
        plain = "CREATE INDEX ix_b ON public.orders USING btree (tracking_token)"

        self.assertNotEqual(
            audit.normalize_definition(unique, "ix_a"),
            audit.normalize_definition(plain, "ix_b"),
        )

    def test_ordering_is_part_of_the_definition(self):
        ascending = "CREATE INDEX ix_a ON public.orders USING btree (created_at)"
        descending = "CREATE INDEX ix_b ON public.orders USING btree (created_at DESC)"

        self.assertNotEqual(
            audit.normalize_definition(ascending, "ix_a"),
            audit.normalize_definition(descending, "ix_b"),
        )


class PickSurvivorTests(unittest.TestCase):
    DEFINITION = "CREATE INDEX x ON public.order_items USING btree (order_id)"

    def test_conventional_name_wins_over_the_legacy_one(self):
        found = [
            index_row("idx_order_items_order_id", self.DEFINITION),
            index_row("ix_order_items_order_id", self.DEFINITION),
        ]

        keep = audit.pick_survivor(found, declared_indexes=set())

        self.assertEqual(keep["index_name"], "ix_order_items_order_id")

    def test_convention_wins_even_when_the_legacy_name_is_also_declared(self):
        # A 20260810_0012 recria `idx_order_items_order_id` no downgrade,
        # entao os DOIS nomes aparecem nas revisoes. A regra antiga olhava
        # "declarado" primeiro e escolhia o legado aqui.
        found = [
            index_row("idx_order_items_order_id", self.DEFINITION),
            index_row("ix_order_items_order_id", self.DEFINITION),
        ]
        declared = {"idx_order_items_order_id", "ix_order_items_order_id"}

        keep = audit.pick_survivor(found, declared_indexes=declared)

        self.assertEqual(keep["index_name"], "ix_order_items_order_id")

    def test_declared_breaks_the_tie_between_two_conventional_names(self):
        found = [
            index_row("ix_order_items_order", self.DEFINITION),
            index_row("ix_order_items_order_id", self.DEFINITION),
        ]

        keep = audit.pick_survivor(found, declared_indexes={"ix_order_items_order_id"})

        self.assertEqual(keep["index_name"], "ix_order_items_order_id")

    def test_choice_is_stable_when_nothing_distinguishes_the_names(self):
        found = [
            index_row("idx_b", self.DEFINITION),
            index_row("idx_a", self.DEFINITION),
        ]

        self.assertEqual(
            audit.pick_survivor(found, set())["index_name"],
            audit.pick_survivor(list(reversed(found)), set())["index_name"],
        )

    def test_reason_names_both_criteria_when_both_hold(self):
        keep = index_row("ix_order_items_order_id", self.DEFINITION)

        reason = audit.survivor_reason(keep, {"ix_order_items_order_id"})

        self.assertIn("convencao", reason)
        self.assertIn("revisao", reason)


class MigrationDeclaredNamesTests(unittest.TestCase):
    """Le as revisoes de verdade, nao um fixture.

    E o que garante que a heuristica continua enxergando as revisoes do
    projeto: se alguem passar a montar o nome do indice numa variavel, o
    indice some desta lista e reaparece no relatorio como "sem dono".
    """

    def test_indexes_created_by_revisions_are_found(self):
        indexes, _ = audit.migration_declared_names()

        self.assertIn("ix_order_items_order_id", indexes)
        self.assertIn("ix_orders_restaurant_created_at", indexes)
        self.assertIn("ix_products_restaurant_category", indexes)

    def test_tables_created_by_revisions_are_found(self):
        # Serve para separar tabela do baseline (que veio dos .sql aplicados
        # a mao) de tabela que nasceu sob o Alembic.
        _, tables = audit.migration_declared_names()

        self.assertIn("printing_sectors", tables)
        self.assertIn("idempotency_keys", tables)
        self.assertNotIn("orders", tables)
        self.assertNotIn("order_items", tables)


if __name__ == "__main__":
    unittest.main()
