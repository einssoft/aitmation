// The designer's palette (ADR-0062): every building block the solution has
// in sight - triggers, AI tasks, people, the core's actions, tools (HTTP,
// MCP, device, read), decision tables and the flow shapes - searchable.
// A click places the block next to the selected card and links it.
import { Badge, Button, Group, ScrollArea, Stack, Text, TextInput, UnstyledButton } from "@mantine/core";
import { useMemo, useState } from "react";
import type { DesignerCatalog, NodeKind, StepConfig } from "../api/client";
import { useI18n } from "../i18n";
import { KIND_STYLE } from "./canvasKinds";

export interface PaletteItem {
  key: string;
  group: string;
  kind: NodeKind;
  ref?: string;
  source?: string;
  label: string;
  hint?: string;
  effect?: string;
  config?: StepConfig;
  seconds?: number;
  /** opens the "new element" dialog instead of placing a card */
  create?: "ai" | "decision";
}

const GROUP_ORDER = ["trigger", "ai", "people", "send", "data", "files", "tools", "decisions", "flow"];

export function paletteItems(catalog: DesignerCatalog, t: (key: string, params?: Record<string, string | number>) => string): PaletteItem[] {
  const items: PaletteItem[] = [];
  for (const trig of catalog.triggers) {
    items.push({
      key: `start:${trig.id}`, group: "trigger", kind: "start", source: trig.id,
      label: t(`solutions.new.source_${trig.id}`),
      config: trig.id === "schedule" ? { every: "day", at: "08:00" } : undefined,
    });
  }
  for (const b of catalog.briefings) items.push({ key: `ai:${b.id}`, group: "ai", kind: "ai", ref: b.id, label: b.id, hint: b.role });
  items.push({ key: "ai:new", group: "ai", kind: "ai", label: t("solutions.designer.newBriefing"), create: "ai" });
  items.push({ key: "gate", group: "people", kind: "gate", label: t("solutions.designer.gate"), hint: t("solutions.designer.gateHint"), config: { approving: "approved" } });
  for (const a of catalog.actions) {
    items.push({
      key: `action:${a.id}`, group: a.group, kind: "action", ref: a.id, effect: a.effect,
      label: t(`solutions.designer.action.${a.id}`), hint: t(`solutions.designer.actionHint.${a.id}`),
      config: { inputs: {} },
    });
  }
  for (const tool of catalog.tools) {
    items.push({
      key: `tool:${tool.id}`, group: "tools", kind: "action", ref: tool.id, effect: tool.effect,
      label: tool.name, hint: `${tool.kind} · ${tool.description}`, config: { inputs: {} },
    });
  }
  for (const d of catalog.decisions) items.push({ key: `decision:${d.id}`, group: "decisions", kind: "decision", ref: d.id, label: d.name || d.id, hint: d.outputs.join(", ") });
  items.push({ key: "decision:new", group: "decisions", kind: "decision", label: t("solutions.designer.newDecision"), create: "decision" });
  items.push({ key: "xor", group: "flow", kind: "xor", label: t("solutions.card.kind.xor"), hint: t("solutions.designer.xorHint") });
  items.push({ key: "parallel", group: "flow", kind: "parallel", label: t("solutions.card.kind.parallel"), hint: t("solutions.designer.parallelHint") });
  items.push({ key: "timer", group: "flow", kind: "timer", label: t("solutions.card.kind.timer"), seconds: 3600, hint: t("solutions.designer.timerHint") });
  for (const p of catalog.processes) items.push({ key: `call:${p.id}`, group: "flow", kind: "call", ref: p.id, label: `↪ ${p.title}`, hint: p.id });
  items.push({ key: "end", group: "flow", kind: "end", label: t("solutions.card.kind.end") });
  return items;
}

export function DesignerPalette({ catalog, onPick, height }: {
  catalog: DesignerCatalog | null;
  onPick: (item: PaletteItem) => void;
  height: number | string;
}) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const items = useMemo(() => (catalog ? paletteItems(catalog, t) : []), [catalog, t]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? items.filter((i) => `${i.label} ${i.ref ?? ""} ${i.hint ?? ""}`.toLowerCase().includes(q)) : items;
  }, [items, query]);
  if (!catalog) return <Text size="xs" c="dimmed" p="xs">…</Text>;
  return (
    <Stack gap={6} h={height} style={{ minHeight: 0 }}>
      <TextInput size="xs" placeholder={t("solutions.designer.search")} value={query} onChange={(e) => setQuery(e.currentTarget.value)} />
      <ScrollArea style={{ flex: 1 }} type="auto" offsetScrollbars>
        <Stack gap={8}>
          {GROUP_ORDER.map((group) => {
            const inGroup = shown.filter((i) => i.group === group);
            if (!inGroup.length) return null;
            return (
              <Stack key={group} gap={2}>
                <Text size="xs" fw={700} c="dimmed" tt="uppercase">{t(`solutions.designer.group.${group}`)}</Text>
                {inGroup.map((item) => (
                  <UnstyledButton
                    key={item.key}
                    onClick={() => onPick(item)}
                    title={item.hint}
                    style={{ padding: "4px 6px", borderRadius: 6, border: "1px solid var(--mantine-color-default-border)" }}
                  >
                    <Group gap={6} wrap="nowrap">
                      <span style={{ color: KIND_STYLE[item.kind].color, width: 14, textAlign: "center" }}>{item.create ? "＋" : KIND_STYLE[item.kind].glyph}</span>
                      <Text size="xs" style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{item.label}</Text>
                      {item.effect === "external" && <Badge size="xs" color="orange" variant="light">{t("solutions.designer.external")}</Badge>}
                      {item.effect === "read" && <Badge size="xs" color="blue" variant="light">{t("solutions.panel.read")}</Badge>}
                    </Group>
                  </UnstyledButton>
                ))}
              </Stack>
            );
          })}
          {!shown.length && <Text size="xs" c="dimmed">{t("solutions.designer.nothing")}</Text>}
        </Stack>
      </ScrollArea>
      <Button size="compact-xs" variant="subtle" onClick={() => setQuery("")} disabled={!query}>{t("solutions.designer.clear")}</Button>
    </Stack>
  );
}
