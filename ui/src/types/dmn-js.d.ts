// dmn-js ships without type declarations; this is the slice the workshop uses.
declare module "dmn-js/lib/NavigatedViewer" {
  export default class DmnViewer {
    constructor(options: { container: HTMLElement });
    importXML(xml: string): Promise<{ warnings: unknown[] }>;
    getViews(): { type: string; element: unknown }[];
    open(view: { type: string; element: unknown }): Promise<void>;
    destroy(): void;
  }
}
