# 第三方資料的來源與授權

平台內建的地圖資料（`kernel/src/tmig/geo/data`，由 `tools/geodata/build.py` 產生）取自下列公開資料。
檢視器、查詢服務與桌面版都會帶上這些資料；轉散布時請一併保留本檔。

| 資料 | 用途 | 來源 | 授權 |
| --- | --- | --- | --- |
| Natural Earth 5.x（國家 110m／50m／10m、州與省 10m、人口聚居地 10m） | 國家、州／省的名稱與形狀，底圖的陸地、國界、州界與地名 | [naturalearthdata.com](https://www.naturalearthdata.com/)（[GitHub 上的官方版本庫](https://github.com/nvkelso/natural-earth-vector)） | 公有領域（Public Domain） |
| GeoNames（經由 npm 套件 [cities.json](https://github.com/lutangar/cities.json) 1.1.66） | 人口 1,000 以上的城市名稱與位置 | [geonames.org](https://www.geonames.org/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)：城市的資料來自 GeoNames |
| 美國人口普查局的郡界（經由 npm 套件 [us-atlas](https://github.com/topojson/us-atlas) 3.0.1，ISC） | 美國的郡的名稱、FIPS 代碼與形狀 | [U.S. Census Bureau, Cartographic Boundary Files 2017](https://www.census.gov/geographies/mapping-files/time-series/geo/carto-boundary-file.html) | 美國政府著作，公有領域 |
| 美國郵遞區號（npm 套件 [zipcodes](https://github.com/davglass/zipcodes) 8.0.0） | 美國郵遞區號的位置 | federalgovernmentzipcodes.us | BSD |
| 各語言的國名（npm 套件 [i18n-iso-countries](https://github.com/michaelwittig/node-i18n-iso-countries) 7.14.0） | 以各種語言（含中文）比對國名 | — | MIT |

產生資料時以 [mapshaper](https://github.com/mbloch/mapshaper)（MPL 2.0）簡化形狀、轉成 TopoJSON；mapshaper 只在
建置時使用，不隨平台散布。

GeoNames 的資料以 CC BY 4.0 授權：使用平台的地圖時，城市的位置來自 GeoNames（© GeoNames contributors）。
