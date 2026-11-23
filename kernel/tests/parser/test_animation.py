"""標記的動畫（格式＞動畫）：.twb 的設定解析成 Workbook.animation 與 Worksheet.animation。

XML 取自 GitHub 上公開的報表（只留下相關的元素）：
- 活頁簿預設依序、0.5 秒，manifest 有 _.fcp.AnimationOnByDefault.true...：
  https://github.com/harshita0410/automobile_project/blob/HEAD/Automobile%20World.twb （2022.2.1）
- 活頁簿預設關（_.fcp.MarkAnimation.true...style-rule 的 ao-off）：
  https://github.com/felicitimilne/bluefall2hwteam17/blob/HEAD/Radar_Example.twb （2021.2.2）
- manifest 只有 <AnimationOnByDefault />、沒有 style-rule：
  https://github.com/estebannoemr/BBDD1/blob/HEAD/PR%20BD1/TP5%20Visualizaci%C3%B3n%20de%20datos/Parte%203/Punto%202.twb
- 工作表的持續時間 0.30000001192092896、活頁簿預設 0.5 秒：
  https://github.com/Ankita-Patil21/Smartphone-Dashoboard-/blob/HEAD/smartphone.twb （2025.1.0）
- 給舊版讀的替代內容 _.fcp.AnimationOnByDefault.false...style：
  https://github.com/AyseUcmakli/TABLEAU/blob/HEAD/assignment_2.twb （2022.2.2）
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir.models import Animation
from tmig.parser.workbook import parse_workbook

from . import builder as b
from .builder import instance, ref, worksheet

FLAG_ON = "<_.fcp.AnimationOnByDefault.true...AnimationOnByDefault />"
PHASED = """
  <style>
    <_.fcp.MarkAnimation.true...style-rule element='animation'>
      <format attr='animation-duration' value='0.5' />
      <format attr='animation-style' value='as-phased' />
      <_.fcp.AnimationOnByDefault.false...format attr='animation-on' value='ao-on' />
    </_.fcp.MarkAnimation.true...style-rule>
  </style>"""
OFF = """
  <style>
    <_.fcp.MarkAnimation.true...style-rule element='animation'>
      <format attr='animation-on' value='ao-off' />
    </_.fcp.MarkAnimation.true...style-rule>
  </style>"""
LEGACY_ONLY = """
  <_.fcp.AnimationOnByDefault.false...style>
    <_.fcp.AnimationOnByDefault.false..._.fcp.MarkAnimation.true...style-rule element='animation'>
      <_.fcp.AnimationOnByDefault.false...format attr='animation-on' value='ao-on' />
    </_.fcp.AnimationOnByDefault.false..._.fcp.MarkAnimation.true...style-rule>
  </_.fcp.AnimationOnByDefault.false...style>"""


def _sheet(name: str, style: str = "") -> str:
    region = instance("[Region]", "None", "[none:Region:nk]")
    sales = instance("[Sales]", "Sum", "[sum:Sales:qk]", "quantitative")
    xml = worksheet(name, [region, sales], rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    return xml.replace("<style />", style or "<style />", 1)


def _parse(tmp_path: Path, manifest: str = "", style: str = "", sheets: str = ""):
    xml = b.workbook(worksheets=sheets or _sheet("Sales"))
    head = f"<document-format-change-manifest>{manifest}</document-format-change-manifest>"
    xml = xml.replace("<preferences />", f"{head}\n  <preferences />{style}", 1)
    return parse_workbook(b.write_twb(tmp_path, xml))


def test_workbook_default_sequential_with_manifest_flag(tmp_path: Path) -> None:
    """style-rule 沒有生效的 animation-on、manifest 有旗標：開，依序 0.5 秒；false 的替代內容略過。"""
    ir = _parse(tmp_path, FLAG_ON, PHASED)
    assert ir.workbook.animation == Animation(on=True, duration=0.5, style="sequential")
    assert ir.worksheets[0].animation is None
    assert not [i for i in ir.ignored if "動畫" in i.reason]


def test_workbook_default_turned_off(tmp_path: Path) -> None:
    """活頁簿預設的 ao-off：關，manifest 有旗標也一樣。只寫了關的與沒有設定相同（None 為關，示範報表的
    寫法）；同時寫了持續時間的留下設定（之後打開動畫時用）。"""
    for manifest in ("", FLAG_ON, "<AnimationOnByDefault />"):
        assert _parse(tmp_path, manifest, OFF).workbook.animation is None, manifest
    longer = OFF.replace(
        "<format attr='animation-on' value='ao-off' />",
        "<format attr='animation-on' value='ao-off' /><format attr='animation-duration' value='1' />",
    )
    ir = _parse(tmp_path, "<AnimationOnByDefault />", longer)
    assert ir.workbook.animation == Animation(on=False, duration=1.0)


def test_manifest_flag_alone_turns_animation_on(tmp_path: Path) -> None:
    """2025.2 的檔案：manifest 只有 <AnimationOnByDefault />、沒有 style-rule，活頁簿預設是開；沒有旗標的
    舊檔案是關，只有給舊版讀的替代內容（功能旗標為 false）時也是關。"""
    assert _parse(tmp_path, "<AnimationOnByDefault />").workbook.animation == Animation(on=True)
    assert _parse(tmp_path, FLAG_ON, LEGACY_ONLY).workbook.animation == Animation(on=True)
    assert _parse(tmp_path).workbook.animation is None
    assert _parse(tmp_path, "", LEGACY_ONLY).workbook.animation is None
    legacy_flag = "<_.fcp.AnimationOnByDefault.false...AnimationOnByDefault />"
    assert _parse(tmp_path, legacy_flag).workbook.animation is None


def test_sheet_settings_override_the_workbook_default(tmp_path: Path) -> None:
    """工作表的 <table><style>：只寫了的項目有值（單精度的持續時間取到千分之一秒），其餘沿用活頁簿預設。"""
    workbook_style = """
  <style>
    <style-rule element='animation'>
      <format attr='animation-duration' value='0.5' />
    </style-rule>
  </style>"""
    sheet_style = """<style>
          <style-rule element='worksheet'>
            <format attr='color' value='#000000' />
          </style-rule>
          <style-rule element='animation'>
            <format attr='animation-duration' value='0.30000001192092896' />
          </style-rule>
        </style>"""
    off_style = """<style>
          <_.fcp.MarkAnimation.true...style-rule element='animation'>
            <format attr='animation-on' value='ao-off' />
            <format attr='animation-style' value='as-synced' />
          </_.fcp.MarkAnimation.true...style-rule>
        </style>"""
    sheets = _sheet("Brand", sheet_style) + _sheet("Model", off_style) + _sheet("Plain")
    ir = _parse(tmp_path, "<AnimationOnByDefault />", workbook_style, sheets)
    assert ir.workbook.animation == Animation(on=True, duration=0.5)
    by_name = {w.name: w.animation for w in ir.worksheets}
    assert by_name == {
        "Brand": Animation(duration=0.3),
        "Model": Animation(on=False, style="simultaneous"),
        "Plain": None,
    }


def test_unknown_values_are_ignored_not_dropped(tmp_path: Path) -> None:
    """不認得的值（動畫不影響數字）記在略過的元素，其餘的設定照常。"""
    style = """
  <style>
    <style-rule element='animation'>
      <format attr='animation-on' value='ao-maybe' />
      <format attr='animation-duration' value='fast' />
      <format attr='animation-style' value='as-phased' />
    </style-rule>
  </style>"""
    ir = _parse(tmp_path, "", style)
    assert ir.workbook.animation == Animation(style="sequential")
    reasons = [i.reason for i in ir.ignored if "動畫" in i.reason]
    assert len(reasons) == 2, reasons
    assert any("animation-on＝ao-maybe" in r for r in reasons)
