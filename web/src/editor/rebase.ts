// 引擎在編輯器之外改了工作表之後，讓編輯器手上的定義跟上。
//
// 編輯器每次變更都把整張工作表存回引擎，歷程（復原、重做）的每一步也是整張工作表。引擎自己也會改到工作表：
// 刪除別的工作表時拿掉工具提示裡的它、刪除 dashboard 時拿掉它的動作在目標工作表上的篩選、復原刪除時把這些
// 放回來、計算欄位的層級改變時調整膠囊的彙總、取代參照、套用欄位的預設屬性…。這時編輯器手上的每一份定義
// 都是舊的：照舊存回去，不是被引擎擋下（參照到不存在的工作表或動作），就是把引擎的改變蓋掉。
//
// rebase(base, theirs, ours)：base 是編輯器所知引擎上的定義，theirs 是引擎上現在的定義，ours 是編輯器手上的
// 一份（歷程中的一步）。回傳「ours 加上引擎從 base 改成 theirs 的那些改變」：
// - ours 與 base 相同的地方換成 theirs 的（整份與 base 相同時結果就是 theirs）
// - ours 自己改過的地方留著：使用者的變更不會不見；兩邊改到同一個值時以 ours 為準，存回時由引擎檢查
// - 清單：有 id 的項目（篩選、Table Calculation…）依 id 對應，工具提示中的工作表依它顯示的工作表對應；
//   引擎拿掉的項目拿掉、加上的項目放回相對的位置，兩邊都有的往下比對。欄位的用法（膠囊）整個當作一個值，
//   不逐項合併：引擎換掉的那個膠囊，在 ours 裡找到一樣的才換
// - 欄位的用法不看位置：引擎把某個欄位的用法換了（計算欄位改成列層級之後膠囊加上 SUM、取代參照換成另一個
//   欄位），ours 裡同樣的用法不論在哪個架上都跟著換——同一個膠囊在歷程較早的一步可能還在另一個架上

type Json = unknown;
type Dict = Record<string, Json>;

const isDict = (value: Json): value is Dict => typeof value === "object" && value !== null && !Array.isArray(value);

/** 內容相同（物件不論鍵的順序）。 */
function same(a: Json, b: Json): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((item, i) => same(item, b[i]));
  if (isDict(a) && isDict(b)) {
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((key) => key in b && same(a[key], b[key]));
  }
  return false;
}

/** 欄位的用法（FieldUse：架上與標記卡上的膠囊）的鍵，與 kernel 的 tmig.edit.USE_KEYS 相同。 */
const USE_KEYS = new Set(["field", "agg", "date", "continuous", "table_calc"]);

/** 欄位的用法：整個是一個值（不能把這個膠囊的彙總套到另一個欄位的膠囊上）。 */
const isUse = (value: Json): boolean => isDict(value) && "field" in value && Object.keys(value).every((key) => USE_KEYS.has(key));

/** 清單中的項目以什麼辨識：有 id 的物件是 id；工具提示中的工作表（沒有 id）是它顯示的工作表。 */
function identity(item: Json): string | null {
  if (!isDict(item) || isUse(item)) return null;
  if (typeof item.id === "string") return `id:${item.id}`;
  if (typeof item.worksheet === "string") return `worksheet:${item.worksheet}`;
  return null;
}

export function rebase<T>(base: T, theirs: T, ours: T): T {
  if (same(base, theirs)) return ours;
  if (same(ours, base)) return theirs;
  const merged = merge(base, theirs, ours);
  const rules: UseRule[] = [];
  collectUseRules(base, theirs, rules);
  return (rules.length > 0 ? applyUseRules(merged, rules) : merged) as T;
}

/** 引擎對一種欄位用法的改變：欄位相同、被改掉的屬性原本的值相同（match）的用法，改成 set。 */
interface UseRule {
  match: Dict;
  set: Dict;
}

/** 比對引擎改之前與之後的定義，找出同一個位置上被換掉的欄位用法。 */
function collectUseRules(base: Json, theirs: Json, rules: UseRule[]): void {
  if (same(base, theirs)) return;
  if (isUse(base) && isUse(theirs)) {
    const before = base as Dict;
    const after = theirs as Dict;
    const changed = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((key) => !same(before[key], after[key]));
    const rule: UseRule = { match: { field: before.field }, set: {} };
    for (const key of changed) {
      rule.match[key] = before[key] ?? null;
      rule.set[key] = after[key] ?? null;
    }
    if (!rules.some((other) => same(other, rule))) rules.push(rule);
    return;
  }
  if (Array.isArray(base) && Array.isArray(theirs)) {
    if ([...base, ...theirs].every((item) => identity(item) !== null)) {
      const after = new Map(theirs.map((item) => [identity(item)!, item]));
      for (const item of base) if (after.has(identity(item)!)) collectUseRules(item, after.get(identity(item)!), rules);
    } else if (base.length === theirs.length) base.forEach((item, i) => collectUseRules(item, theirs[i], rules));
    return;
  }
  if (isDict(base) && isDict(theirs)) for (const key of Object.keys(base)) if (key in theirs) collectUseRules(base[key], theirs[key], rules);
}

/** 把引擎對欄位用法的改變套到一份定義裡所有相同的用法上（不論位置）。 */
function applyUseRules(value: Json, rules: UseRule[]): Json {
  if (Array.isArray(value)) return value.map((item) => applyUseRules(item, rules));
  if (!isDict(value)) return value;
  if (isUse(value)) {
    const rule = rules.find((r) => Object.keys(r.match).every((key) => same(value[key] ?? null, r.match[key])));
    return rule ? { ...value, ...rule.set } : value;
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, applyUseRules(item, rules)]));
}

function merge(base: Json, theirs: Json, ours: Json): Json {
  if (same(base, theirs)) return ours; // 引擎沒有改這裡
  if (same(ours, base)) return theirs; // 編輯器沒有改這裡：引擎的版本
  if (Array.isArray(base) && Array.isArray(theirs) && Array.isArray(ours)) return mergeLists(base, theirs, ours);
  if (isDict(base) && isDict(theirs) && isDict(ours) && ![base, theirs, ours].some(isUse)) return mergeDicts(base, theirs, ours);
  return ours; // 兩邊改到同一個值：留著編輯器的
}

function mergeDicts(base: Dict, theirs: Dict, ours: Dict): Dict {
  const result: Dict = {};
  for (const key of Object.keys(ours)) {
    if (key in base && key in theirs) result[key] = merge(base[key], theirs[key], ours[key]);
    // 引擎拿掉的鍵：編輯器沒有改過它才拿掉
    else if (!(key in base) || key in theirs || !same(ours[key], base[key])) result[key] = ours[key];
  }
  for (const key of Object.keys(theirs)) if (!(key in base) && !(key in ours)) result[key] = theirs[key];
  return result;
}

function mergeLists(base: Json[], theirs: Json[], ours: Json[]): Json[] {
  const all = [...base, ...theirs, ...ours];
  if (all.every((item) => identity(item) !== null)) return mergeKeyed(base, theirs, ours);
  // 沒有 id 的容器（例如各標記卡）：長度都相同時依位置對應，往下比對
  const containers = all.every((item) => isDict(item) && !isUse(item));
  if (containers && base.length === theirs.length && base.length === ours.length) return ours.map((item, i) => merge(base[i], theirs[i], item));
  return mergeValues(base, theirs, ours);
}

/** 引擎加上的項目放在哪裡：theirs 裡排在它前面、ours 也有的最近一項之後；沒有時放在最前面。 */
function insertAfterNeighbour(result: Json[], theirs: Json[], at: number, added: Json, found: (item: Json, neighbour: Json) => boolean): void {
  for (let i = at - 1; i >= 0; i--) {
    const index = result.findIndex((item) => found(item, theirs[i]));
    if (index >= 0) {
      result.splice(index + 1, 0, added);
      return;
    }
  }
  result.unshift(added);
}

/** 有辨識依據的項目：引擎拿掉的拿掉（編輯器改過它時也拿掉：它指向的東西已經不在了）、加上的放回去、
 * 兩邊都有的往下比對；順序以編輯器的為準。 */
function mergeKeyed(base: Json[], theirs: Json[], ours: Json[]): Json[] {
  const before = new Map(base.map((item) => [identity(item)!, item]));
  const after = new Map(theirs.map((item) => [identity(item)!, item]));
  const result: Json[] = [];
  for (const item of ours) {
    const key = identity(item)!;
    if (before.has(key) && !after.has(key)) continue;
    result.push(before.has(key) && after.has(key) ? merge(before.get(key), after.get(key), item) : item);
  }
  const present = new Set(result.map((item) => identity(item)!));
  theirs.forEach((item, at) => {
    const key = identity(item)!;
    if (before.has(key) || present.has(key)) return;
    insertAfterNeighbour(result, theirs, at, item, (mine, neighbour) => identity(mine) === identity(neighbour));
    present.add(key);
  });
  return result;
}

/** 值的清單（膠囊、文字、數字）：引擎換掉、拿掉的值，在編輯器的清單裡找到一樣的才換掉、拿掉；引擎加上的值
 * 沒有才加。 */
function mergeValues(base: Json[], theirs: Json[], ours: Json[]): Json[] {
  const result = [...ours];
  if (base.length === theirs.length) {
    // 一樣長：同一個位置上不同的就是換掉的
    const replaced = new Set<number>();
    base.forEach((old, at) => {
      if (same(old, theirs[at])) return;
      const index = result.findIndex((item, i) => !replaced.has(i) && same(item, old));
      if (index < 0) return;
      result[index] = theirs[at];
      replaced.add(index);
    });
    return result;
  }
  const kept = [...theirs];
  for (const old of base) {
    const at = kept.findIndex((item) => same(item, old));
    if (at >= 0) {
      kept.splice(at, 1); // 引擎留著的
      continue;
    }
    const index = result.findIndex((item) => same(item, old));
    if (index >= 0) result.splice(index, 1);
  }
  // kept 剩下的是引擎加上的
  theirs.forEach((item, at) => {
    const index = kept.findIndex((other) => other === item);
    if (index < 0 || result.some((mine) => same(mine, item))) return;
    kept.splice(index, 1);
    insertAfterNeighbour(result, theirs, at, item, same);
  });
  return result;
}
