// 裝置專屬版面（Tableau 的 Device Layouts）：依檢視的畫面較短的一邊選用手機、平板或桌面的版面。

import { useEffect, useState } from "react";
import type { Dashboard, Device, DeviceLayout } from "./types";

/**
 * 檢視的裝置：網址的 :device（或 device）參數優先；否則依畫面較短的一邊——500 像素以下為手機、
 * 501 到 800 為平板、超過 800 為桌面（與 Tableau 相同）。
 */
export function deviceFor(width: number, height: number, search = window.location.search): Device {
  const params = new URLSearchParams(search);
  const forced = (params.get(":device") ?? params.get("device") ?? "").toLowerCase();
  if (forced === "phone" || forced === "tablet" || forced === "desktop") return forced;
  const short = Math.min(width, height);
  return short <= 500 ? "phone" : short <= 800 ? "tablet" : "desktop";
}

/** 目前的裝置；視窗大小改變時重新判斷。 */
export function useDevice(): Device {
  const [device, setDevice] = useState<Device>(() => deviceFor(window.innerWidth, window.innerHeight));
  useEffect(() => {
    const update = () => setDevice(deviceFor(window.innerWidth, window.innerHeight));
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);
  return device;
}

/** 裝置用的版面：手機沒有自己的版面時用平板的；都沒有時為 null（預設版面）。 */
export function deviceLayout(dashboard: Dashboard, device: Device): DeviceLayout | null {
  const order: Device[] = device === "phone" ? ["phone", "tablet"] : [device];
  for (const kind of order) {
    const found = dashboard.layouts?.find((layout) => layout.device === kind);
    if (found) return found;
  }
  return null;
}

/** 以裝置的版面取代 dashboard 的區塊與尺寸（其餘不變）；沒有裝置的版面時回傳原本的 dashboard。 */
export function withDeviceLayout(dashboard: Dashboard, device: Device): Dashboard {
  const layout = deviceLayout(dashboard, device);
  if (!layout) return dashboard;
  const shown: Dashboard = { ...dashboard, zones: layout.zones, size: layout.size, layouts: [] };
  if (layout.sizing === "default") return shown;
  if (layout.sizing === "fixed") return { ...shown, sizing: "fixed", min_size: null, max_size: null };
  if (layout.sizing === "automatic") return { ...shown, sizing: "automatic", min_size: null, max_size: null };
  // 符合寬度：寬度隨畫面、高度固定（以上下限都是版面高度的範圍尺寸表示）
  const height = layout.size.height;
  return { ...shown, sizing: "range", min_size: { width: 1, height }, max_size: { width: 100000, height } };
}
