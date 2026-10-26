# 資料來源與授權

`source/orders.csv.gz` 取自 Tableau 的範例資料「Sample - Superstore」（Orders 工作表），來源為 [tableau/extensions-api](https://github.com/tableau/extensions-api) 的 `Samples/DataApp/Tableau Superstore Data App/Data/Datasources/Sample - Superstore.xls`。該 repo 以 Apache License 2.0 授權：

> Copyright (c) 2026 Salesforce, Inc.
> Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

轉換方式：只保留 Orders 工作表，日期轉為 ISO 格式（yyyy-mm-dd），其餘值不變，以 gzip 壓縮。

`superstore_demo.twbx` 內的 .twb 是為了測試而手寫的仿真 workbook（並非由 Tableau 產生），.hyper 萃取由 `scripts/build_demo_fixture.py` 以 Tableau Hyper API 從上述 CSV 建立。
