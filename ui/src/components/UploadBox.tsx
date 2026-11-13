// Hand a document in from the shell: files, a typed note, a dictated one
// (browser speech recognition, no server) or a recorded voice note that
// the instance transcribes locally (ADR-0030). Lands in the chosen
// member's inbox with an envelope - rung 0 of the Vermittler ladder, the
// sender is the signed-in person. The mailroom is the right target when
// the person does not know (or does not care) which mandant it concerns.
import { Alert, Button, Card, FileButton, Group, Select, Stack, Text, Textarea, TextInput } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { type IntakeFile } from "../api/client";
import { useHousehold } from "../household";
import { LOCALES, useI18n } from "../i18n";

type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
};

function speechRecognition(): (new () => Recognition) | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export function UploadBox({ onDone, compact = false }: { onDone?: () => void; compact?: boolean }) {
  const { t, lang } = useI18n();
  const { members, selected } = useHousehold();
  const reachable = members.filter((m) => m.info);
  const mailroom = reachable.find((m) => m.member.type === "mailroom");
  const defaultTarget = selected !== "all" ? selected : (mailroom ?? reachable[0])?.member.id ?? null;
  const [target, setTarget] = useState<string | null>(defaultTarget);
  const [files, setFiles] = useState<File[]>([]);
  const [subject, setSubject] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const recognizer = useRef<Recognition | null>(null);
  const canDictate = speechRecognition() !== null;
  // Voice note: the browser records (MediaRecorder), the instance
  // transcribes with its local Whisper - the audio never leaves the house.
  const recorder = useRef<MediaRecorder | null>(null);
  const [recording, setRecording] = useState(false);
  const canRecord = typeof MediaRecorder !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);
  const instance = reachable.find((m) => m.member.id === target)?.info;
  const canTranscribe = Boolean(instance?.capabilities?.transcription);

  const record = async () => {
    if (recording) {
      recorder.current?.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const chunks: Blob[] = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
        const type = rec.mimeType || "audio/webm";
        const ext = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "m4a" : "weba";
        const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
        setFiles((prev) => [...prev, new File(chunks, `sprachnotiz-${stamp}.${ext}`, { type })]);
      };
      recorder.current = rec;
      setRecording(true);
      rec.start();
    } catch (err) {
      setMessage({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    }
  };

  useEffect(() => {
    if (!target && defaultTarget) setTarget(defaultTarget);
  }, [defaultTarget, target]);

  const dictate = () => {
    if (listening) {
      recognizer.current?.stop();
      return;
    }
    const Ctor = speechRecognition();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = LOCALES[lang];
    rec.interimResults = false;
    rec.continuous = true;
    rec.onresult = (e) => {
      const heard = Array.from(e.results).map((r) => r[0]?.transcript ?? "").join(" ").trim();
      if (heard) setText((prev) => (prev ? `${prev} ${heard}` : heard));
    };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    recognizer.current = rec;
    setListening(true);
    rec.start();
  };

  const send = async () => {
    const state = reachable.find((m) => m.member.id === target);
    if (!state) return;
    setBusy(true);
    setMessage(null);
    try {
      const encoded: IntakeFile[] = await Promise.all(
        files.map(async (f) => ({ name: f.name, content_base64: await toBase64(f) })),
      );
      const result = await state.client.upload({ subject, text, files: encoded });
      setMessage({ kind: "ok", text: t("upload.done", { count: result.accepted, member: state.member.title }) });
      setFiles([]);
      setSubject("");
      setText("");
      onDone?.();
    } catch (err) {
      setMessage({ kind: "err", text: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card withBorder padding={compact ? "sm" : "md"}>
      <Stack gap="xs">
        {!compact && <Text fw={600}>{t("upload.title")}</Text>}
        <Group grow align="flex-end">
          <Select
            label={t("upload.target")}
            value={target}
            onChange={setTarget}
            data={reachable.map((m) => ({
              value: m.member.id,
              label: m.member.type === "mailroom" ? t("upload.viaMailroom", { title: m.member.title }) : m.member.title,
            }))}
          />
          <TextInput label={t("upload.subject")} value={subject} onChange={(e) => setSubject(e.currentTarget.value)} />
        </Group>
        <Textarea
          label={t("upload.text")}
          autosize
          minRows={2}
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          placeholder={t("upload.textHint")}
        />
        <Group>
          <FileButton onChange={(picked) => setFiles((prev) => [...prev, ...picked])} multiple>
            {(props) => <Button variant="default" {...props}>{t("upload.pickFiles")}</Button>}
          </FileButton>
          {/* Photo intake (ADR-0028): on a phone this opens the camera; the
              picture goes through OCR like any scan. */}
          <FileButton onChange={(picked) => picked && setFiles((prev) => [...prev, picked])} accept="image/*" capture="environment">
            {(props) => <Button variant="default" {...props}>{t("upload.photo")}</Button>}
          </FileButton>
          {canDictate && (
            <Button variant={listening ? "filled" : "default"} color={listening ? "red" : undefined} onClick={dictate}>
              {listening ? t("upload.stopDictation") : t("upload.dictate")}
            </Button>
          )}
          {canRecord && canTranscribe && (
            <Button variant={recording ? "filled" : "default"} color={recording ? "red" : undefined} onClick={() => void record()}>
              {recording ? t("upload.stopRecording") : t("upload.record")}
            </Button>
          )}
          <Button loading={busy} disabled={!target || (!files.length && !text.trim())} onClick={() => void send()}>
            {t("upload.send")}
          </Button>
        </Group>
        {files.length > 0 && (
          <Text size="sm" c="dimmed">
            {files.map((f) => f.name).join(", ")}{" "}
            <Button variant="subtle" size="compact-xs" onClick={() => setFiles([])}>{t("upload.clear")}</Button>
          </Text>
        )}
        {message && <Alert color={message.kind === "ok" ? "green" : "red"}>{message.text}</Alert>}
      </Stack>
    </Card>
  );
}
