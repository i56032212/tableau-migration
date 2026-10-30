"""報表目錄：fixtures 中的報表與 Tableau Server 上的 workbook、Published Data Source 的對應。

每個含 golden.ir.json 或 .twbx 的子目錄是一份報表。對應到 Tableau 的 workbook 時先看 cases.yaml 的
tableau_views_url 中的網址名稱（tools/tableau_server 的 fetch 會填好），其次是報表名稱；Published Data Source
依 IR 中記下的網址名稱（N8 併入時的 repository-location）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tmig.ir import IR, ExtractConnection

_VIEWS_URL = re.compile(r"/views/([^/?#]+)")


def workbook_url_name(directory: Path) -> str | None:
    """報表在 Tableau 上的網址名稱（cases.yaml 的 tableau_views_url，例如 …/views/SalesOverview）。"""
    path = directory / "cases.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    url = data.get("tableau_views_url") if isinstance(data, dict) else None
    match = _VIEWS_URL.search(str(url or ""))
    return match.group(1) if match else None


def report_directories(fixtures: Path) -> list[Path]:
    if not fixtures.is_dir():
        return []
    return sorted(
        p
        for p in fixtures.iterdir()
        if p.is_dir() and ((p / "golden.ir.json").is_file() or any(p.glob("*.twbx")))
    )


@dataclass
class Report:
    directory: Path
    ir: IR
    url_name: str | None = None

    @property
    def name(self) -> str:
        return self.directory.name

    def extract_datasources(self) -> list[str]:
        return [ds.id for ds in self.ir.datasources if isinstance(ds.connection, ExtractConnection)]


@dataclass
class Catalog:
    reports: list[Report] = field(default_factory=list)

    @classmethod
    def load(cls, fixtures: Path) -> Catalog:
        from tmig.verify.run import load_fixture_ir

        return cls(
            [
                Report(d, load_fixture_ir(d), workbook_url_name(d))
                for d in report_directories(fixtures)
            ]
        )

    def workbook(self, url_name: str | None, name: str | None = None) -> Report | None:
        """依網址名稱（優先）或報表名稱找報表；名稱重複時無法判斷，回傳 None。"""
        if url_name:
            found = [r for r in self.reports if r.url_name == url_name]
            if len(found) == 1:
                return found[0]
        if name:
            found = [r for r in self.reports if r.ir.workbook.name == name]
            if len(found) == 1:
                return found[0]
        return None

    def published(self, url_name: str | None, name: str | None = None) -> list[tuple[Report, str]]:
        """用到這個 Published Data Source 的報表與資料源 id（每份報表各有一份併入的萃取）。"""
        result: list[tuple[Report, str]] = []
        for report in self.reports:
            for ds in report.ir.datasources:
                source = ds.published
                if source is None or not isinstance(ds.connection, ExtractConnection):
                    continue
                if (url_name and source.content_url == url_name) or (
                    not url_name and name and ds.caption == name
                ):
                    result.append((report, ds.id))
        return result


__all__ = ["Catalog", "Report", "report_directories", "workbook_url_name"]
