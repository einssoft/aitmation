// DMN view (ADR-0034 §6): dmn-js from bpmn.io for decision tables, loaded
// lazily so the shell stays small - only the workshop pays for it. The
// dmn-js watermark the license requires stays visible. Processes are
// drawn by the card canvas (FlowCanvas, ADR-0040); bpmn-js is no longer
// bundled - the BPMN file remains the format, the canvas its only view.
import { useEffect, useRef } from "react";
import "dmn-js/dist/assets/diagram-js.css";
import "dmn-js/dist/assets/dmn-js-shared.css";
import "dmn-js/dist/assets/dmn-js-decision-table.css";
import "dmn-js/dist/assets/dmn-js-decision-table-controls.css";
import "dmn-js/dist/assets/dmn-js-literal-expression.css";
import "dmn-js/dist/assets/dmn-js-drd.css";
import "dmn-js/dist/assets/dmn-font/css/dmn.css";
import DmnViewer from "dmn-js/lib/NavigatedViewer";

export interface Highlight {
  /** Steps that ran (visited): coloured green with the run count. */
  visited: Record<string, number>;
  /** Steps that never ran in the proving run: coloured grey. */
  untouched: string[];
  /** Steps whose case failed at that point: red. */
  failed: string[];
}

export interface NodeBadge {
  text: string;
  /** Mantine colour name; drives the chip background. */
  color: "teal" | "blue" | "violet" | "orange" | "red" | "gray" | "yellow";
  title?: string;
}

export function DmnCanvas({ xml }: { xml: string }) {
  const host = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!host.current) return;
    const viewer = new DmnViewer({ container: host.current });
    viewer
      .importXML(xml)
      .then(() => {
        // Open the decision table view directly - the DRD of a single table says nothing.
        const views = viewer.getViews();
        const table = views.find((v) => v.type === "decisionTable");
        if (table) viewer.open(table);
      })
      .catch(() => undefined);
    return () => viewer.destroy();
  }, [xml]);
  return (
    <div style={{ position: "relative" }}>
      <style>{`.dmn-js-parent { background: var(--mantine-color-body); }`}</style>
      <div ref={host} style={{ minHeight: 320, border: "1px solid var(--mantine-color-default-border)", borderRadius: 8, overflow: "auto" }} />
    </div>
  );
}
