// The document behind a case: a link that opens the file itself (same
// host, session cookie travels), a player for recordings (ADR-0030) and an
// inline text preview, so a gate can be decided without leaving the page.
// Nothing here leaves the instance.
import { Anchor, Button, Code, Group, Text } from "@mantine/core";
import { useState } from "react";
import { type DocumentText, type InstanceClient } from "../api/client";
import { useI18n } from "../i18n";

const AUDIO = /\.(mp3|m4a|aac|wav|flac|ogg|oga|opus|weba)$/i;
const VIDEO = /\.(mp4|m4v|mov|mkv|webm|avi)$/i;

export function DocumentView({ client, caseId, hasDocument, name }: { client: InstanceClient; caseId: string; hasDocument: boolean; name?: string | null }) {
  const { t } = useI18n();
  const isAudio = Boolean(name && AUDIO.test(name));
  const isVideo = Boolean(name && VIDEO.test(name));
  const [preview, setPreview] = useState<DocumentText | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!hasDocument) return null;

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (preview) return;
    setBusy(true);
    setError(null);
    try {
      setPreview(await client.documentText(caseId));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Group gap="sm" mt={4}>
        <Anchor href={client.documentUrl(caseId)} target="_blank" rel="noopener" size="sm">
          {t("document.open")}
        </Anchor>
        <Button variant="subtle" size="compact-xs" loading={busy} onClick={() => void toggle()}>
          {open ? t("document.hidePreview") : t("document.preview")}
        </Button>
      </Group>
      {isAudio && <audio controls preload="none" src={client.documentUrl(caseId)} style={{ display: "block", marginTop: 6, maxWidth: 480 }} />}
      {isVideo && <video controls preload="metadata" src={client.documentUrl(caseId)} style={{ display: "block", marginTop: 6, maxWidth: 480, maxHeight: 320 }} />}
      {open && error && <Text size="sm" c="red">{t("app.error", { message: error })}</Text>}
      {open && preview && (
        <div>
          {preview.pending ? (
            <Text size="sm" c="dimmed" mt={4}>{t("document.pending")}</Text>
          ) : preview.text ? (
            <Code block mt={4} style={{ whiteSpace: "pre-wrap", maxHeight: 320, overflow: "auto" }}>
              {preview.text}
            </Code>
          ) : (
            <Text size="sm" c="dimmed" mt={4}>{t("document.noText", { reason: preview.error ?? "" })}</Text>
          )}
          {preview.truncated && <Text size="xs" c="dimmed">{t("document.truncated")}</Text>}
        </div>
      )}
    </div>
  );
}
