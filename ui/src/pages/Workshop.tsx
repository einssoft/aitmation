// Werkstatt → Vorlagen (Designer, Stufe 1): every loaded template as a
// graph rendered from its declaration - steps as nodes, outcomes as edges,
// process: targets as doors into the next template, dispatch fanned out to
// each document type's chain. Read-only; editing is a later ADR (the
// process wizard as meta-template).
import { Alert, Anchor, Badge, Card, Code, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useEffect, useMemo, useState } from "react";
import { type DoctypeRow, type TemplateDetail, type TemplateRow, type TemplateStep } from "../api/client";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";

const KIND_COLOR: Record<TemplateStep["kind"], string> = {
  trigger: "var(--mantine-color-gray-5)",
  agent_step: "var(--mantine-color-blue-6)",
  human_gate: "var(--mantine-color-orange-6)",
  system_action: "var(--mantine-color-teal-6)",
  timer: "var(--mantine-color-violet-6)",
};

const NODE_W = 150;
const NODE_H = 46;
const COL_GAP = 96;
const ROW_GAP = 30;

interface Placed {
  id: string;
  label: string;
  kind: TemplateStep["kind"] | "end" | "process";
  x: number;
  y: number;
}

/** Columns by distance from the start step; end/process targets get a
 * final column so the picture reads left to right. */
function layout(detail: TemplateDetail): { nodes: Placed[]; edges: { from: string; to: string; label: string; dashed: boolean }[]; width: number; height: number } {
  const byId = new Map(detail.steps.map((s) => [s.id, s]));
  const depth = new Map<string, number>([[detail.start, 0]]);
  const queue = [detail.start];
  while (queue.length) {
    const id = queue.shift()!;
    const step = byId.get(id);
    if (!step) continue;
    for (const edge of step.next) {
      if (edge.kind === "step" && edge.target && !depth.has(edge.target)) {
        depth.set(edge.target, (depth.get(id) ?? 0) + 1);
        queue.push(edge.target);
      }
    }
  }
  for (const step of detail.steps) if (!depth.has(step.id)) depth.set(step.id, 0);
  const maxDepth = Math.max(...depth.values());
  const columns: string[][] = Array.from({ length: maxDepth + 2 }, () => []);
  for (const step of detail.steps) columns[depth.get(step.id) ?? 0]?.push(step.id);

  const exits: Placed[] = [];
  const edges: { from: string; to: string; label: string; dashed: boolean }[] = [];
  const exitId = (edge: { kind: string; target: string | null }) => (edge.kind === "end" ? "__end" : `__process:${edge.target}`);
  for (const step of detail.steps) {
    for (const edge of step.next) {
      if (edge.kind === "step" && edge.target) edges.push({ from: step.id, to: edge.target, label: edge.outcome, dashed: false });
      else {
        const id = exitId(edge);
        if (!exits.find((e) => e.id === id)) exits.push({ id, label: edge.kind === "end" ? "" : edge.target ?? "", kind: edge.kind === "end" ? "end" : "process", x: 0, y: 0 });
        edges.push({ from: step.id, to: id, label: edge.outcome, dashed: edge.kind === "process" });
      }
    }
    const routes = step.config.routes as Record<string, string> | undefined;
    if (routes) {
      for (const [doctype, chain] of Object.entries(routes)) {
        const id = `__process:${chain}`;
        if (!exits.find((e) => e.id === id)) exits.push({ id, label: chain, kind: "process", x: 0, y: 0 });
        edges.push({ from: step.id, to: id, label: doctype, dashed: true });
      }
    }
  }
  columns[maxDepth + 1] = exits.map((e) => e.id);
  // Parallel edges (two outcomes to the same target) become one arrow
  // with both labels - fewer crossings, no overlapping text.
  const merged = new Map<string, { from: string; to: string; label: string; dashed: boolean }>();
  for (const e of edges) {
    const key = `${e.from}>${e.to}`;
    const seen = merged.get(key);
    if (seen) seen.label = `${seen.label}, ${e.label}`;
    else merged.set(key, { ...e });
  }
  edges.length = 0;
  edges.push(...merged.values());

  const nodes: Placed[] = [];
  const height = Math.max(...columns.map((c) => c.length)) * (NODE_H + ROW_GAP) + ROW_GAP;
  columns.forEach((ids, col) => {
    const total = ids.length * (NODE_H + ROW_GAP) - ROW_GAP;
    ids.forEach((id, row) => {
      const step = byId.get(id);
      const exit = exits.find((e) => e.id === id);
      nodes.push({
        id,
        label: step ? step.id : exit?.label ?? "",
        kind: step ? step.kind : exit?.kind ?? "end",
        x: 20 + col * (NODE_W + COL_GAP),
        y: (height - total) / 2 + row * (NODE_H + ROW_GAP),
      });
    });
  });
  return { nodes, edges, width: 40 + columns.length * (NODE_W + COL_GAP) - COL_GAP, height };
}

function Graph({ detail, onOpen }: { detail: TemplateDetail; onOpen: (id: string) => void }) {
  const { t } = useI18n();
  const g = useMemo(() => layout(detail), [detail]);
  const pos = new Map(g.nodes.map((n) => [n.id, n]));
  return (
    <div style={{ overflowX: "auto" }}>
      <svg width={g.width} height={g.height} style={{ fontFamily: "inherit", fontSize: 12 }}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--mantine-color-dimmed)" />
          </marker>
        </defs>
        {g.edges.map((e, i) => {
          const a = pos.get(e.from);
          const b = pos.get(e.to);
          if (!a || !b) return null;
          const x1 = a.x + NODE_W;
          const y1 = a.y + NODE_H / 2;
          const x2 = b.x;
          const y2 = b.y + NODE_H / 2;
          const mx = (x1 + x2) / 2;
          // Label near the target end, staggered per source so fan-outs read.
          const siblings = g.edges.filter((o) => o.from === e.from);
          const k = siblings.indexOf(e);
          const f = 0.3 + (k % 3) * 0.14;
          const tx = x1 + (x2 - x1) * f;
          const ty = y1 + (y2 - y1) * f - 6;
          return (
            <g key={i}>
              <path d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`} fill="none" stroke="var(--mantine-color-dimmed)" strokeDasharray={e.dashed ? "5 4" : undefined} markerEnd="url(#arrow)" />
              <text x={tx} y={ty} textAnchor="middle" fill="var(--mantine-color-dimmed)" style={{ paintOrder: "stroke", stroke: "var(--mantine-color-body)", strokeWidth: 4 }}>
                {e.label}
              </text>
            </g>
          );
        })}
        {g.nodes.map((n) => {
          if (n.kind === "end") {
            return (
              <g key={n.id}>
                <circle cx={n.x + 12} cy={n.y + NODE_H / 2} r={9} fill="none" stroke="var(--mantine-color-dimmed)" strokeWidth={2} />
                <circle cx={n.x + 12} cy={n.y + NODE_H / 2} r={4} fill="var(--mantine-color-dimmed)" />
                <text x={n.x + 28} y={n.y + NODE_H / 2 + 4} fill="var(--mantine-color-dimmed)">{t("workshop.end")}</text>
              </g>
            );
          }
          const isProcess = n.kind === "process";
          return (
            <g key={n.id} style={{ cursor: isProcess ? "pointer" : "default" }} onClick={() => isProcess && onOpen(n.label)}>
              <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx={isProcess ? 4 : 10} fill={isProcess ? "var(--mantine-color-body)" : KIND_COLOR[n.kind as TemplateStep["kind"]]} stroke={isProcess ? "var(--mantine-color-dimmed)" : "none"} strokeDasharray={isProcess ? "4 3" : undefined} />
              <text x={n.x + NODE_W / 2} y={n.y + NODE_H / 2 - (isProcess ? 0 : 3)} textAnchor="middle" dominantBaseline="middle" fill={isProcess ? "var(--mantine-color-text)" : "white"} fontWeight={600}>
                {n.label}
              </text>
              {!isProcess && (
                <text x={n.x + NODE_W / 2} y={n.y + NODE_H / 2 + 13} textAnchor="middle" fill="white" opacity={0.85} fontSize={10}>
                  {t(`workshop.kind.${n.kind}`)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function TemplatesPage() {
  const { t, pick } = useI18n();
  const { members, selected } = useHousehold();
  const candidates = members.filter((m) => m.info);
  const state = candidates.find((m) => m.member.id === (selected !== "all" ? selected : candidates.find((c) => c.member.type !== "mailroom")?.member.id ?? candidates[0]?.member.id));
  const [list, setList] = useState<TemplateRow[]>([]);
  const [doctypes, setDoctypes] = useState<DoctypeRow[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [detail, setDetail] = useState<TemplateDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!state) return;
    Promise.all([state.client.templates(), state.client.doctypes()])
      .then(([tpl, dts]) => {
        setList(tpl);
        setDoctypes(dts);
        setCurrent((c) => c && tpl.some((x) => x.id === c) ? c : tpl.find((x) => x.intake)?.id ?? tpl[0]?.id ?? null);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [state]);
  useEffect(() => {
    if (!state || !current) return;
    state.client.template(current).then(setDetail).catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [state, current]);

  const byId = new Map(list.map((x) => [x.id, x]));
  const chainOf = (start: string): string[] => {
    // Follow process: targets from a template until nothing continues.
    const out = [start];
    let cursor: string | undefined = start;
    const seen = new Set<string>();
    while (cursor && !seen.has(cursor)) {
      seen.add(cursor);
      const next = detailsCache.get(cursor);
      cursor = next;
      if (cursor) out.push(cursor);
    }
    return out;
  };
  // Successor per template, learned from the details we have loaded; the
  // list endpoint is enough for names, the graph endpoint for edges.
  const [detailsCache, setDetailsCache] = useState<Map<string, string | undefined>>(new Map());
  useEffect(() => {
    if (!state || !list.length) return;
    Promise.all(list.map((x) => state.client.template(x.id).then((d) => [x.id, d] as const))).then((all) => {
      const m = new Map<string, string | undefined>();
      for (const [id, d] of all) {
        const proc = d.steps.flatMap((s) => s.next).find((e) => e.kind === "process");
        m.set(id, proc?.target ?? undefined);
      }
      setDetailsCache(m);
    }).catch(() => undefined);
  }, [state, list]);

  const apps = Array.from(new Set(list.map((x) => x.app ?? "core")));

  return (
    <Stack>
      <Title order={2}>{t("workshop.templates")}</Title>
      <Text c="dimmed" size="sm">{t("workshop.intro")}</Text>
      {error && <Alert color="red">{t("app.error", { message: error })}</Alert>}
      <Group align="flex-start" wrap="nowrap">
        <Stack gap="xs" w={260} style={{ flexShrink: 0 }}>
          {apps.map((app) => (
            <div key={app}>
              <Text size="xs" fw={700} c="dimmed" tt="uppercase">{app}</Text>
              {list.filter((x) => (x.app ?? "core") === app).map((x) => (
                <Anchor key={x.id} component="button" type="button" onClick={() => setCurrent(x.id)} fw={x.id === current ? 700 : 400} display="block" size="sm">
                  {pick(x.title) || x.id}{x.intake ? ` · ${t("workshop.intakeTag")}` : ""}
                </Anchor>
              ))}
            </div>
          ))}
        </Stack>
        <Stack style={{ flex: 1, minWidth: 0 }}>
          {detail && (
            <Card withBorder>
              <Group gap="xs" mb="xs">
                <Title order={4}>{pick(detail.title) || detail.id}</Title>
                <Code>{detail.id}</Code>
                <Badge variant="outline">v{detail.version}</Badge>
                {detail.app && <Badge variant="light" color="gray">{detail.app}</Badge>}
                {detail.intake && <Badge variant="light">{t("workshop.intakeTag")}</Badge>}
              </Group>
              {pick(detail.description) && <Text size="sm" c="dimmed" mb="sm">{pick(detail.description)}</Text>}
              <Graph detail={detail} onOpen={(id) => byId.has(id) && setCurrent(id)} />
              {Object.keys(detail.policies).length > 0 && (
                <Text size="xs" c="dimmed" mt="sm">
                  {t("workshop.policies")}: {Object.entries(detail.policies).map(([k, v]) => `${k} ${JSON.stringify(v)}`).join(" · ")}
                </Text>
              )}
            </Card>
          )}
          {doctypes.length > 0 && (
            <Card withBorder>
              <Title order={5} mb="xs">{t("workshop.chains")}</Title>
              <Table>
                <Table.Tbody>
                  {doctypes.map((d) => (
                    <Table.Tr key={d.id}>
                      <Table.Td><Text fw={500}>{pick(d.title)}</Text> <Code>{d.id}</Code></Table.Td>
                      <Table.Td>
                        <Group gap={6}>
                          {chainOf(d.chain).map((id, i) => (
                            <Group key={id} gap={6}>
                              {i > 0 && <Text c="dimmed">→</Text>}
                              <Anchor component="button" type="button" size="sm" onClick={() => setCurrent(id)}>
                                {pick(byId.get(id)?.title) || id}
                              </Anchor>
                            </Group>
                          ))}
                        </Group>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Card>
          )}
        </Stack>
      </Group>
    </Stack>
  );
}
