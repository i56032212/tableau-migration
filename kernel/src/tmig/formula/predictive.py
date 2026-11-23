"""預測模型函式 MODEL_QUANTILE、MODEL_PERCENTILE（Tableau 2020.3 起）的參數：模型的設定、分位數、目標與預測變數。

Tableau 2021.2 說明文件（2026-11-23 查閱）：

- 函式（https://help.tableau.com/v2021.2/pro/desktop/en-us/functions_functions_predictivemodeling.htm ）：
  ``MODEL_QUANTILE(model_specification (optional), quantile, target_expression, predictor_expression(s))``、
  ``MODEL_PERCENTILE(model_specification (optional), target_expression, predictor_expression(s))``；目標是
  彙總時預測變數也要是彙總
- 模型（predictions_choosing_model.htm）：預設是線性迴歸（OLS，``"model=linear"``），另有正規化線性迴歸
  （``"model=rl"``）與高斯過程迴歸（``"model=gp"``）
- 正規化與擴增（predictions_regularization_and_augmentation.htm）：``"model=rl, lambda=0.05"``（L2，預設 0.5）；
  預測變數前的 ``"augmentation=on|off"``：線性迴歸預設只擴增有序的維度（Legendre 多項式到 3 次），
  量值擴增時是 2 次的 Hermite 多項式；``lambda`` 不適用於線性迴歸
- 有序、無序（predictions_resolve_errors.htm）：預測變數前的 ``"ordered"``、``"unordered"``

這裡只拆解寫法（tmig.formula.functions 檢查、tmig.sqlgen.query 查詢、tmig.planner.predictive 計算）。
"""

from __future__ import annotations

from dataclasses import dataclass

from tmig.formula.ast import LiteralNode, Node
from tmig.formula.types import FormulaSemanticError

NAMES = frozenset({"MODEL_QUANTILE", "MODEL_PERCENTILE"})
MODELS = frozenset({"linear", "rl", "gp"})
OPTIONS = frozenset({"ordered", "unordered", "augmentation=on", "augmentation=off"})
"""預測變數前的設定（字串常值）。"""


@dataclass(frozen=True)
class Predictor:
    node: Node
    options: frozenset[str]
    """這個預測變數前面的設定，例如 {"unordered"}、{"augmentation=on"}。"""


@dataclass(frozen=True)
class ModelCall:
    name: str
    model: str
    """linear、rl 或 gp。"""
    settings: dict[str, str]
    """模型設定字串中的其他設定（例如 lambda）。"""
    quantile: Node | None
    """MODEL_QUANTILE 的分位數；MODEL_PERCENTILE 為 None。"""
    target: Node
    predictors: tuple[Predictor, ...]


def _string(node: Node) -> str | None:
    if isinstance(node, LiteralNode) and node.kind == "string":
        return str(node.value)
    return None


def _pairs(text: str, name: str, pos: int) -> dict[str, str]:
    """「model=rl, lambda=0.05」→ {"model": "rl", "lambda": "0.05"}（不分大小寫）。"""
    found: dict[str, str] = {}
    for part in text.split(","):
        item = part.strip().lower()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep:
            raise FormulaSemanticError(
                f"{name} 的模型設定「{text}」無法辨識", pos, unsupported=True
            )
        found[key.strip()] = value.strip()
    return found


def model_call(name: str, args: tuple[Node, ...] | list[Node], pos: int) -> ModelCall:
    """拆解 MODEL_* 的參數；寫法不對時丟出 FormulaSemanticError。"""
    items = list(args)
    settings: dict[str, str] = {}
    if items and _string(items[0]) is not None:
        text = _string(items.pop(0)) or ""
        settings = _pairs(text, name, pos)
        if not settings:
            raise FormulaSemanticError(
                f"{name} 的模型設定「{text}」無法辨識", pos, unsupported=True
            )
    model = settings.pop("model", "linear")
    if model not in MODELS:
        raise FormulaSemanticError(f"{name} 的模型「{model}」無法辨識", pos, unsupported=True)
    quantile: Node | None = None
    if name == "MODEL_QUANTILE":
        if not items:
            raise FormulaSemanticError(f"{name} 缺少分位數", pos)
        quantile = items.pop(0)
    if not items or _string(items[0]) is not None:
        raise FormulaSemanticError(f"{name} 缺少目標運算式", pos)
    target = items.pop(0)
    predictors: list[Predictor] = []
    options: set[str] = set()
    for item in items:
        text = _string(item)
        if text is not None:
            for option in (o.strip().lower().replace(" ", "") for o in text.split(",")):
                if option and option not in OPTIONS:
                    raise FormulaSemanticError(
                        f"{name} 的預測變數設定「{text}」無法辨識", pos, unsupported=True
                    )
                if option:
                    options.add(option)
            continue
        predictors.append(Predictor(item, frozenset(options)))
        options = set()
    if options:
        raise FormulaSemanticError(
            f"{name} 的設定「{', '.join(sorted(options))}」後面沒有預測變數", pos
        )
    if not predictors:
        raise FormulaSemanticError(f"{name} 至少要有一個預測變數", pos)
    return ModelCall(name, model, settings, quantile, target, tuple(predictors))


__all__ = ["MODELS", "NAMES", "OPTIONS", "ModelCall", "Predictor", "model_call"]
