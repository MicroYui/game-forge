from gameforge.contracts.dsl import Constraint, Predicate, Selector

_YAML = """
- id: C_newbie_gold_cap
  kind: numeric
  oracle: deterministic
  scope: {var: q, node_type: QUEST, where: {region: newbie_zone}}
  assert: "reward_gold <= 80"
  severity: major
- id: C_baiyuan_semantic
  kind: narrative
  oracle: mixed
  predicates:
    - {expr: "chapter >= 3", oracle: deterministic}
    - {expr: "semantically_reveals_identity(dialogue, baiyuan)", oracle: llm-assisted}
  assert: "chapter >= 3"
  severity: critical
"""


def test_parse_constraints_and_alias_assert():
    cs = Constraint.from_yaml(_YAML)
    assert cs[0].id == "C_newbie_gold_cap"
    assert cs[0].assert_ == "reward_gold <= 80"      # `assert` YAML key -> assert_
    assert cs[0].dsl_grammar_version == "dsl@1"


def test_predicate_level_oracle_routing():
    cs = Constraint.from_yaml(_YAML)
    assert cs[0].has_llm_predicate() is False        # pure deterministic
    assert cs[1].has_llm_predicate() is True         # one llm-assisted predicate -> whole constraint routes to LLM
