#!/usr/bin/env bash
# 下載 tools/geodata/build.py 的來源：Natural Earth（GitHub 上的官方版本庫）與 npm 套件。
# 用法：tools/geodata/fetch.sh <目錄>     已下載的檔案不重新下載
set -euo pipefail
dir="${1:?用法：fetch.sh <目錄>}"
mkdir -p "$dir/ne" "$dir/npm"
base="https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
# 屬性（地名、代碼）取自 10m 的國家與州；形狀取自挖去大湖泊的版本（_lakes），湖泊與海洋同樣是水域
for layer in ne_10m_admin_0_countries ne_10m_admin_1_states_provinces ne_10m_populated_places \
  ne_110m_admin_0_countries_lakes ne_50m_admin_0_countries_lakes ne_10m_admin_0_countries_lakes \
  ne_10m_admin_1_states_provinces_lakes; do
  [ -s "$dir/ne/$layer.geojson" ] || curl -fsSL -o "$dir/ne/$layer.geojson" "$base/$layer.geojson"
done
cd "$dir/npm"
for package in cities.json@1.1.66 zipcodes@8.0.0 us-atlas@3.0.1 i18n-iso-countries@7.14.0; do
  name="${package%@*}"
  if [ ! -d "$name" ]; then
    tarball="$(npm pack --silent "$package")"
    mkdir -p "$name"
    tar -xzf "$tarball" -C "$name" --strip-components=1
    rm "$tarball"
  fi
done
echo "來源在 $dir；mapshaper：npm install --prefix <目錄> mapshaper@0.7.101"
