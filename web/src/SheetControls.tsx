// 工作表上顯示的控制項（Tableau 的「顯示篩選」「顯示參數」）：單獨檢視工作表時顯示在檢視的旁邊。

import { FilterControl, ParameterControl, SetControl } from "./Controls";
import { applyFilter } from "./Dashboard";
import { stateFor, type ViewState } from "./state";
import type { IR, Worksheet, Zone } from "./types";

export function SheetControls({
  ir,
  workbook,
  worksheet,
  view,
  onView,
}: {
  ir: IR;
  workbook: string;
  worksheet: Worksheet;
  view: ViewState;
  onView: (view: ViewState) => void;
}) {
  const controls = worksheet.controls ?? [];
  if (controls.length === 0) return null;
  return (
    <aside className="sheet-controls" aria-label="工作表的控制項">
      {controls.map((control, i) => {
        if (control.kind === "parameter") {
          const parameter = ir.parameters.find((p) => p.id === control.parameter);
          if (!parameter) return null;
          const value = parameter.id in view.parameters ? view.parameters[parameter.id] : parameter.value;
          return (
            <ParameterControl
              key={`p${parameter.id}`}
              parameter={parameter}
              control={control.control as Zone["control"]}
              value={value}
              onChange={(v) => onView({ ...view, parameters: { ...view.parameters, [parameter.id]: v } })}
            />
          );
        }
        if (control.kind === "set") {
          const field = control.field?.field ?? "";
          return (
            <SetControl
              key={`s${field}`}
              ir={ir}
              workbook={workbook}
              field={field}
              value={view.sets?.[field]}
              onChange={(value) => onView({ ...view, sets: { ...(view.sets ?? {}), [field]: value } })}
            />
          );
        }
        // 與 dashboard 的篩選控制項共用元件：以控制項的設定組成區塊
        const zone = {
          id: `control${i}`,
          kind: "filter",
          x: 0,
          y: 0,
          w: 0,
          h: 0,
          worksheet: worksheet.id,
          field: control.field,
          control: control.control,
          relevant: control.relevant === true,
          children: [],
        } as unknown as Zone;
        return (
          <FilterControl
            key={`f${i}`}
            ir={ir}
            workbook={workbook}
            zone={zone}
            state={view.sheetFilters[worksheet.id]}
            onChange={(key, value) => onView({ ...view, sheetFilters: applyFilter(ir, view.sheetFilters, worksheet.id, key, value) })}
            query={control.relevant ? stateFor(ir, worksheet.id, view) : undefined}
          />
        );
      })}
    </aside>
  );
}
