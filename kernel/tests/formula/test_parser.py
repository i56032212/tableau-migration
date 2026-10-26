import json
from pathlib import Path
from typing import Any

import pytest

from tmig.formula.ast import FieldRef, from_json, strip_positions, to_json, walk
from tmig.formula.lexer import FormulaSyntaxError
from tmig.formula.parser import parse_formula
from tmig.formula.sexpr import to_sexpr

CORPUS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "corpus.json").read_text(encoding="utf-8")
)["cases"]
GOOD = [c for c in CORPUS if "sexpr" in c]
BAD = [c for c in CORPUS if "error" in c]


@pytest.mark.parametrize("case", GOOD, ids=[c["formula"][:40] for c in GOOD])
def test_corpus_parses(case: dict[str, Any]) -> None:
    assert to_sexpr(parse_formula(case["formula"])) == case["sexpr"]


@pytest.mark.parametrize("case", BAD, ids=[c["formula"][:40] or "(empty)" for c in BAD])
def test_corpus_errors(case: dict[str, Any]) -> None:
    with pytest.raises(FormulaSyntaxError) as info:
        parse_formula(case["formula"])
    assert case["error"] in info.value.message


@pytest.mark.parametrize("case", GOOD, ids=[c["formula"][:40] for c in GOOD])
def test_json_roundtrip(case: dict[str, Any]) -> None:
    node = parse_formula(case["formula"])
    again = from_json(json.loads(json.dumps(to_json(node))))
    assert again == strip_positions(node)


def test_error_position_points_at_problem() -> None:
    with pytest.raises(FormulaSyntaxError) as info:
        parse_formula("SUM([Sales]) + * 2")
    assert info.value.pos == 15


def test_positions_and_resolver() -> None:
    node = parse_formula("SUM([Sales]) / [Parameters].[Top N]")
    refs = [n for n in walk(node) if isinstance(n, FieldRef)]
    assert [(r.parts, r.pos) for r in refs] == [(("Sales",), 4), (("Parameters", "Top N"), 15)]
    data = to_json(node, lambda ref: {"id": "x_" + ref.parts[-1].lower().replace(" ", "_")})
    assert data["left"]["args"][0] == {
        "node": "field",
        "ref": "[Sales]",
        "parts": ["Sales"],
        "id": "x_sales",
    }
    assert data["right"]["ref"] == "[Parameters].[Top N]"
