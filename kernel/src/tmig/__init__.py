"""Tableau 移轉核心函式庫。

核心只讀寫檔案、不依賴服務與前端；各子套件對應 Spec 的任務：

- ``ir``：IR v0 資料模型與 JSON Schema（A1）
- ``parser``：.twbx → IR（A2–A6）
- ``census``：功能盤點（S3）
- ``formula``：公式引擎（B1–B3）
- ``planner``、``sqlgen``：邏輯查詢計畫與 SQL 產生（D1–D4）
- ``data``：Hyper 匯入與 DuckDB 執行器（C1–C2）
- ``render``：Vega-Lite 規格產生（F1）
- ``verify``：預期結果比對與差異報告（E2–E3）
"""

__version__ = "0.1.0"
