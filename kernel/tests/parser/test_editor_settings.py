"""以前只能在編輯器設定、解析器不認得的 .twb 寫法：分頁顏色、篩選控制項的自訂、頁面的顯示歷程、表計算的自訂排序
順序、Story 的格式、活頁簿佈景主題、「隱藏指示器」。

寫法取自 GitHub 上公開的報表（只留下相關的元素）：
- 分頁顏色 <window tab-color>：https://github.com/Suhwan-Im/TIL/blob/HEAD/14.%20Tableau/ （DAY13 Failure Equipment
  Monitoring.twb）、https://github.com/Bava7/Students-profile-analysis-Tableau-/blob/HEAD/SPA%20project%20(%20tableau).twb
- 篩選控制項：https://github.com/smartinternz02/SPS-9541-Horticulture/blob/HEAD/ （show-exclude、show-search、
  show-filter-state、show-mode，2021.1）、https://github.com/JAM5BOCsAdi/Data_Analyst_Tableau/blob/HEAD/Full_Course/Filters.twb
  （show-all、show-apply，2024.3）、https://github.com/nikhitamoolya/Netflix-content-analysis/blob/HEAD/Netflix%20project.twb
  （show-morefewerbutton）
- 顯示歷程 <page-trail-options>：https://github.com/kbetts/ISMWW-Tableau/blob/HEAD/Example2/Example2_Complete.twb
  （軌跡、不畫標記、點線）、https://github.com/MohammadAbuShams/CO2-Emissions-and-Economic-Growth-1990-2020/blob/HEAD/Book1.twb
  （size='3'）、https://github.com/puboe/vast-challenge/blob/HEAD/VAST/Tableu/proxMobileOut.twb （start、end）
- 表計算的自訂排序：https://github.com/LSH0414/Project/blob/HEAD/ （조축회_거래.twb，INDEX() 依 MAX(value) 遞減，
  2022.4）、https://github.com/Apress/pro-tableau/blob/6e5b38b3c4b70975be8e416dd251be54b999adce/Source%20Code/Chapter%209/Bump%20chart/Bump%20chart.twb
- Story 的格式：https://github.com/StephenKinggg/Data_Science_Course_Repo/blob/HEAD/2.%20Classes_Lab/6.%20DVwTableau/Pre(Story)_05.12.21.twb
  （2021.3）、https://github.com/50hands/DataAnalysis-and-NLP-Abhinav/blob/HEAD/Mobility%20Data/19%20Google%20Mobility%20Data%20for%20Counties.twb
- 佈景主題 <style-theme>：https://github.com/harshita0410/automobile_project/blob/HEAD/Automobile%20World.twb （classic）
- 隱藏指示器：https://github.com/aadityapanchal/NHL-Analytics/blob/HEAD/tableau-dashboard/Shot_analysis.twb （2021.3）
"""

from __future__ import annotations

from pathlib import Path

from tmig.ir import ControlOptions, FieldUse, PageHistory, StoryFormat, iter_zones
from tmig.ir.models import TableCalcSort

from . import builder as b
from .builder import instance, ref, worksheet
from .test_extensions import BASIC, parse, reasons, slices
from .test_story import point, story


def _regions_sheet(name: str = "With Region", table_extra: str = "", view_extra: str = "") -> str:
    return worksheet(
        name,
        BASIC,
        rows=ref("none:Category:nk"),
        cols=ref("sum:Sales:qk"),
        view_extra=view_extra or slices("none:Region:nk"),
        table_extra=table_extra,
    )


def test_tab_colors(tmp_path: Path) -> None:
    """工作表的分頁顏色 → Worksheet.tab_color；dashboard 的分頁顏色 IR 沒有欄位，記為略過。"""
    board = """
    <dashboard name='Unit Score Report'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones><zone h='100000' id='1' name='Unit Part' w='100000' x='0' y='0' /></zones>
    </dashboard>"""
    windows = """<windows>
      <window class='worksheet' name='Unit Part' tab-color='#F66273' />
      <window class='worksheet' name='Plain' />
      <window class='dashboard' maximized='true' name='Unit Score Report' tab-color='#4f779a' />
    </windows>"""
    sheets = _regions_sheet("Unit Part") + _regions_sheet("Plain")
    xml = b.workbook(worksheets=sheets, dashboards=board).replace("<windows />", windows)
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    assert {w.name: w.tab_color for w in ir.worksheets} == {"Unit Part": "#f66273", "Plain": None}
    assert any(
        "dashboard／Story「Unit Score Report」的分頁顏色 #4f779a" in i.reason for i in ir.ignored
    )


def test_filter_control_customizations(tmp_path: Path) -> None:
    """篩選控制項選單的「自訂」：show-all、show-search、show-exclude、show-apply 與 values='context'
    → ControlOptions；平台沒有的自訂記為略過；都是預設時沒有 options。公開報表中 show-exclude 只看過
    'false'（.twb 只寫與預設不同的設定，推定 Tableau 預設顯示包含／排除；平台預設不顯示）。"""
    board = f"""
    <dashboard name='Board'>
      <size maxheight='600' maxwidth='800' minheight='600' minwidth='800' sizing-mode='fixed' />
      <zones>
        <zone h='100000' id='1' type-v2='layout-basic' w='100000' x='0' y='0'>
          <zone h='40000' id='2' name='With Region' w='100000' x='0' y='0' />
          <zone h='20000' id='3' mode='dropdown' name='With Region' param='{ref("none:Region:nk")}' show-exclude='false' show-filter-state='false' show-mode='false' type-v2='filter' w='50000' x='0' y='40000' />
          <zone h='20000' id='4' mode='checkdropdown' name='With Region' param='{ref("none:Region:nk")}' show-all='false' show-morefewerbutton='true' show-search='false' type-v2='filter' values='context' w='50000' x='50000' y='40000' />
          <zone h='20000' id='5' mode='checkdropdown' name='With Region' param='{ref("none:Region:nk")}' type-v2='filter' values='database' w='50000' x='0' y='60000' />
        </zone>
      </zones>
    </dashboard>"""
    windows = f"""<windows>
      <window class='worksheet' name='With Region'>
        <cards>
          <edge name='right'><strip size='160'>
            <card param='{ref("none:Region:nk")}' show-apply='true' show-all='false' type='filter' values='cascading' />
          </strip></edge>
        </cards>
      </window>
    </windows>"""
    flt = (
        f"<filter class='categorical' column='{ref('none:Region:nk')}'>"
        "<groupfilter function='level-members' level='[none:Region:nk]' /></filter>"
    )
    sheets = _regions_sheet(view_extra=flt)
    xml = b.workbook(worksheets=sheets, dashboards=board).replace("<windows />", windows)
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    zones = [z for z in iter_zones(ir.dashboards[0].zones) if z.kind == "filter"]
    # show-exclude='false'（不顯示包含／排除）與平台的預設相同
    assert [z.options for z in zones] == [
        None,
        ControlOptions(show_all=False, show_search=False, context_values=True),
        None,
    ]
    assert ir.worksheets[0].controls[0].options == ControlOptions(show_all=False, show_apply=True)
    ignored = [i.reason for i in ir.ignored if "篩選控制項的自訂" in i.reason]
    assert ignored == [
        "工作表「With Region」篩選控制項的自訂（顯示控制項類型、顯示「顯示所有值」按鈕）尚不支援：以預設呈現",
        "工作表「With Region」篩選控制項的自訂（顯示更多／更少按鈕）尚不支援：以預設呈現",
    ]


def test_page_history(tmp_path: Path) -> None:
    """<page-trail-options>：勾選「顯示歷程」、全部的標記時 → PageHistory；只對選取的標記、沒有勾選時不顯示。"""
    pages = f"<pages><column>{ref('none:Region:nk')}</column></pages>"

    def history(options: str):
        sheet = _regions_sheet(table_extra=pages + options)
        ir = parse(tmp_path, b.workbook(worksheets=sheet))
        assert reasons(ir) == []
        return ir.worksheets[0].page_history, [i.reason for i in ir.ignored]

    trails, ignored = history(
        "<page-trail-options enabled='true' line-pattern='line-pattern-dotted' lines='true' "
        "mark-type='all' marks='false' />"
    )
    assert trails == PageHistory(show="trails")
    assert "工作表「With Region」顯示歷程的格式（軌跡的線條樣式）尚不支援：以預設呈現" in ignored
    both, _ = history(
        "<page-trail-options enabled='true' end='99' lines='true' mark-type='all' start='51' />"
    )
    assert both == PageHistory(show="both")
    marks, _ = history("<page-trail-options enabled='true' mark-type='all' size='3' />")
    assert marks == PageHistory(show="marks", length=3)
    selected, ignored = history("<page-trail-options enabled='true' size='3' />")
    assert selected is None
    assert any("只對選取的標記顯示歷程" in r for r in ignored)
    off, _ = history("<page-trail-options end='99' mark-type='all' start='85' />")
    assert off is None


def test_table_calc_custom_sort(tmp_path: Path) -> None:
    """<table-calc ordering-type='Field'> 的 <sort direction using> → TableCalc.sort（依 MAX(Sales) 遞減的
    INDEX()）。排序的量值不是彙總時列為不支援（會改變計算的結果）。"""
    rank = (
        "<column caption='rank' datatype='integer' name='[Calculation_1]' role='measure' "
        "type='ordinal'><calculation class='tableau' formula='INDEX()'>"
        "<table-calc ordering-type='Rows' /></calculation></column>"
    )

    def sheet(using: str) -> str:
        calc = (
            "<table-calc ordering-type='Field'>"
            f"<order field='{ref('none:Region:nk')}' />"
            f"<sort direction='DESC' using='{using}' /></table-calc>"
        )
        deps = [
            *BASIC,
            instance("[Sales]", "Max", "[max:Sales:qk]", "quantitative"),
            instance("[Calculation_1]", "User", "[usr:Calculation_1:ok]", "ordinal", calc),
        ]
        return worksheet(
            "Rank",
            deps,
            rows=ref("none:Region:nk"),
            cols=ref("usr:Calculation_1:ok"),
            deps_extra=rank,
        )

    xml = b.workbook(b.extract_datasource(columns=rank), worksheets=sheet(ref("max:Sales:qk")))
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    calc = next(t for t in ir.worksheets[0].table_calcs if t.type == "formula")
    assert calc.sort == TableCalcSort(
        by=FieldUse(field="f_sales", agg="max", continuous=True), order="desc"
    )
    xml = b.workbook(b.extract_datasource(columns=rank), worksheets=sheet(ref("none:Category:nk")))
    flagged = reasons(parse(tmp_path, xml))
    assert any("表計算的自訂排序順序不是依彙總的量值" in r for r in flagged), flagged


def test_story_format(tmp_path: Path) -> None:
    """Story 的 <style>：底色、標題、導覽與文字物件 → StoryFormat；平台沒有的（導覽的寬度、標題的底色）記為略過。"""
    style = """<style>
        <style-rule element='table'><format attr='background-color' value='#e9f3f2' /></style-rule>
        <style-rule element='story-description'>
          <format attr='background-color' value='#f7faf06a' />
        </style-rule>
        <style-rule element='story-point-caption'>
          <format attr='width' value='226' />
          <format attr='background-color' value='#4e79a7' />
          <format attr='color' value='#ffffff' />
        </style-rule>
        <style-rule element='story-title'>
          <format attr='background-color' value='#fefaf1' />
          <format attr='font-size' value='18' />
          <format attr='text-align' value='center' />
        </style-rule>
      </style>"""
    board = story(point(1, "One", "S")).replace("<size ", style + "\n      <size ", 1)
    sheet = worksheet("S", BASIC, rows=ref("none:Region:nk"), cols=ref("sum:Sales:qk"))
    ir = parse(tmp_path, b.workbook(worksheets=sheet, dashboards=board))
    assert reasons(ir) == []
    assert ir.stories[0].format == StoryFormat(
        background="#e9f3f2",
        title_size=18,
        title_align="center",
        navigator_background="#4e79a7",
        navigator_color="#ffffff",
        note_background="#f7faf06a",
    )
    assert any(
        i.reason == "Story「Tour」的格式（導覽、標題的部分設定）尚不支援：以預設呈現"
        for i in ir.ignored
    )


def test_workbook_theme_and_hidden_indicator(tmp_path: Path) -> None:
    """<style-theme name='classic'>：佈景主題的預設值沒有寫進檔案，記為略過；工作表 element='table' 沒有 field 的
    show-null-value-warning='false' 是「隱藏指示器」。"""
    hidden = """<style>
          <style-rule element='table'>
            <format attr='show-null-value-warning' value='false' />
          </style-rule>
        </style>"""
    sheets = _regions_sheet("Hidden").replace("<style />", hidden, 1) + _regions_sheet("Shown")
    xml = b.workbook(worksheets=sheets).replace(
        "<preferences />", "<preferences />\n  <style-theme name='classic' />", 1
    )
    ir = parse(tmp_path, xml)
    assert reasons(ir) == []
    assert {w.name: w.null_indicator for w in ir.worksheets} == {"Hidden": False, "Shown": True}
    assert any(
        i.reason.startswith("活頁簿佈景主題「Classic」：新平台以預設佈景主題呈現")
        for i in ir.ignored
    )
