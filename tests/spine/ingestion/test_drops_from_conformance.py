"""Source-neutral `DROPS_FROM` endpoint contract for every current Adapter."""

from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.flare_adapter import FlareTxtAdapter, read_flare_dir

_LEGAL_PRODUCERS = {
    NodeType.MONSTER,
    NodeType.DROP_TABLE,
    NodeType.INTERACTABLE,
    NodeType.EVENT,
    NodeType.BATTLE_ENCOUNTER,
}
_LEGAL_PRODUCTS = {NodeType.ITEM, NodeType.CURRENCY}


def _assert_drop_endpoints(snapshot) -> set[tuple[str, str]]:
    graph = snapshot.to_graph()
    endpoints: set[tuple[str, str]] = set()
    for relation in graph.all_relations():
        if relation.type is not EdgeType.DROPS_FROM:
            continue
        source = graph.get_node(relation.src_id)
        product = graph.get_node(relation.dst_id)
        assert source is not None
        assert product is not None
        assert source.type in _LEGAL_PRODUCERS
        assert product.type in _LEGAL_PRODUCTS
        endpoints.add((source.id, product.id))
    return endpoints


def _aureus_drop_workbook() -> dict[str, list[dict]]:
    return {
        "items": [{"item_id": "item:pelt", "name": "Pelt"}],
        "currencies": [{"currency_id": "gold", "name": "Gold"}],
        "drop_tables": [
            {
                "drop_table_id": "drops:wolf",
                "entries": [{"item": "item:pelt", "chance": 1.0}],
            }
        ],
        "monsters": [
            {
                "monster_id": "monster:wolf",
                "name": "Wolf",
                "drop_table_id": "drops:wolf",
                "gold_min": 1,
                "gold_max": 2,
                "currency": "gold",
            }
        ],
    }


def test_aureus_drop_relations_use_legal_producer_to_product_endpoints():
    snapshot = AureusCsvAdapter().to_ir(_aureus_drop_workbook(), file_ref="drops")

    assert _assert_drop_endpoints(snapshot) == {
        ("monster:wolf", "item:pelt"),
        ("monster:wolf", "gold"),
    }


def test_flare_drop_relations_use_legal_producer_to_product_endpoints():
    workbook = read_flare_dir("scenarios/flare_sample")
    snapshot = FlareTxtAdapter().to_ir(workbook, file_ref="scenarios/flare_sample")

    assert _assert_drop_endpoints(snapshot) == {
        ("enemies:goblin", "items:32"),
        ("enemies:skeleton", "items:32"),
        ("enemies:skeleton", "items:56"),
    }
