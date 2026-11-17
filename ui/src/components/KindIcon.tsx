// The symbols of the process vocabulary (Tabler, thin lines): one per card
// kind, and one per way a process is entered (ADR-0057).
import {
  IconBolt,
  IconCalendarTime,
  IconClock,
  IconCornerDownRight,
  IconDeviceMobile,
  IconFolder,
  IconLink,
  IconMail,
  IconMessage,
  IconPlayerPlay,
  IconPlayerStop,
  IconPlus,
  IconSparkles,
  IconTable,
  IconUserCheck,
  IconX,
  type Icon,
} from "@tabler/icons-react";
import type { NodeKind } from "../api/client";
import { KIND_STYLE } from "./canvasKinds";

const KIND_ICON: Record<NodeKind, Icon> = {
  start: IconPlayerPlay,
  ai: IconSparkles,
  gate: IconUserCheck,
  action: IconBolt,
  decision: IconTable,
  call: IconCornerDownRight,
  timer: IconClock,
  xor: IconX,
  parallel: IconPlus,
  end: IconPlayerStop,
};

const ENTRY_ICON: Record<string, Icon> = {
  chat: IconMessage,
  messenger: IconDeviceMobile,
  schedule: IconCalendarTime,
  folder: IconFolder,
  mailbox: IconMail,
  email: IconMail,
  chain: IconLink,
  manual: IconPlayerPlay,
};

/** The symbol of a card kind. ``entry`` (channel or source) picks the
 *  symbol of a start card; ``tinted`` draws it in the kind's colour. */
export function KindIcon({
  kind,
  entry,
  size = 16,
  stroke = 1.75,
  tinted = true,
}: {
  kind: NodeKind;
  entry?: string;
  size?: number;
  stroke?: number;
  tinted?: boolean;
}) {
  const Symbol = (kind === "start" && entry && ENTRY_ICON[entry]) || KIND_ICON[kind];
  return (
    <Symbol
      size={size}
      stroke={stroke}
      color={tinted ? KIND_STYLE[kind].color : "currentColor"}
      style={{ flexShrink: 0 }}
      aria-hidden
    />
  );
}
