"""把資料源的欄位與參數包成公式分析用的 Scope。"""

from __future__ import annotations

from collections.abc import Sequence

from tmig.formula.analyze import Analyzer, Target
from tmig.ir.models import IR, DataField, Datasource, Parameter


def _strip(name: str) -> str:
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


class DatasourceScope:
    """公式中的參照：[欄位內部名稱或顯示名稱]、[Parameters].[參數內部名稱或顯示名稱]、[資料源].[欄位]。"""

    def __init__(
        self, ds_name: str, fields: Sequence[DataField], parameters: Sequence[Parameter]
    ) -> None:
        self.ds_name = _strip(ds_name)
        self._fields: dict[str, Target] = {}
        self._captions: dict[str, Target] = {}
        self._by_id: dict[str, Target] = {}
        for field in fields:
            target = Target(
                "field",
                field.id,
                field.datatype,
                field.role,
                field.calc.formula if field.calc is not None else None,
            )
            self._fields[_strip(field.name)] = target
            self._captions.setdefault(field.caption, target)
            self._by_id[field.id] = target
        self._params: dict[str, Target] = {}
        for param in parameters:
            target = Target("param", param.id, param.datatype, "param")
            self._params[_strip(param.name)] = target
            self._params.setdefault(param.caption, target)
            self._by_id[param.id] = target

    @classmethod
    def for_datasource(
        cls, datasource: Datasource, parameters: Sequence[Parameter]
    ) -> DatasourceScope:
        return cls(datasource.name, datasource.fields, parameters)

    def lookup(self, parts: tuple[str, ...]) -> Target | None:
        if len(parts) == 2:
            owner, name = parts
            if owner == "Parameters":
                return self._params.get(name)
            if owner != self.ds_name:
                return None
            parts = (name,)
        if len(parts) != 1:
            return None
        name = parts[0]
        return self._fields.get(name) or self._captions.get(name) or self._params.get(name)

    def target(self, target_id: str) -> Target:
        return self._by_id[target_id]


def analyzer_for(ir: IR, datasource_id: str) -> Analyzer:
    datasource = next(ds for ds in ir.datasources if ds.id == datasource_id)
    return Analyzer(DatasourceScope.for_datasource(datasource, ir.parameters))
