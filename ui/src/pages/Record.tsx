// Alltag → Meeting aufnehmen (ADR-0050): the browser records a meeting
// that runs in another tab (Teams, Meet, Zoom in the web client) or in
// the room (microphone only), mixes tab audio and microphone into one
// Opus stream and hands the recording to the meeting-minutes process of
// the chosen member. The fallback when no bot joins: nothing leaves the
// browser except the finished recording, to the member's own instance.
import { Alert, Badge, Button, Card, Checkbox, Group, Progress, Select, Stack, Text, TextInput, Title } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useHousehold } from "../household";
import { useI18n } from "../i18n";

const PROCESS = "meeting-minutes";
const BITRATE = 32_000; // ~14 MB per hour, under the instance's 25 MB upload cap for ~1h45
const MAX_BYTES = 24 * 1024 * 1024;

function stamp(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 3600)).padStart(2, "0")}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function RecordPage() {
  const { t } = useI18n();
  const { members, selected } = useHousehold();
  const candidates = members.filter((m) => m.info && m.member.type !== "mailroom");
  const [target, setTarget] = useState<string | null>(null);
  const member = candidates.find((m) => m.member.id === (target ?? (selected !== "all" ? selected : candidates[0]?.member.id)));
  const [hasProcess, setHasProcess] = useState<boolean | null>(null);
  const [title, setTitle] = useState("");
  const [withTab, setWithTab] = useState(true);
  const [withMic, setWithMic] = useState(true);
  const [phase, setPhase] = useState<"idle" | "recording" | "uploading" | "done">("idle");
  const [seconds, setSeconds] = useState(0);
  const [bytes, setBytes] = useState(0);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const streams = useRef<MediaStream[]>([]);
  const ctx = useRef<AudioContext | null>(null);
  const timer = useRef<number | null>(null);

  const supported = typeof MediaRecorder !== "undefined" && Boolean(navigator.mediaDevices?.getUserMedia);
  const canTab = Boolean(navigator.mediaDevices?.getDisplayMedia);

  useEffect(() => {
    if (!member) return;
    member.client
      .templates()
      .then((rows) => setHasProcess(rows.some((r) => r.id === PROCESS)))
      .catch(() => setHasProcess(null));
  }, [member]);

  const cleanup = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    streams.current.forEach((s) => s.getTracks().forEach((tr) => tr.stop()));
    streams.current = [];
    ctx.current?.close().catch(() => undefined);
    ctx.current = null;
    recorder.current = null;
  };

  useEffect(() => cleanup, []);

  const start = async () => {
    setError(null);
    setCaseId(null);
    chunks.current = [];
    setBytes(0);
    setSeconds(0);
    try {
      const audio = new AudioContext();
      ctx.current = audio;
      const dest = audio.createMediaStreamDestination();
      const analyser = audio.createAnalyser();
      analyser.fftSize = 512;
      let sources = 0;
      if (withTab && canTab) {
        // The person picks the tab or window of the meeting; only its audio is kept.
        const display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
        display.getVideoTracks().forEach((tr) => tr.stop());
        if (display.getAudioTracks().length === 0) throw new Error(t("record.noTabAudio"));
        const src = audio.createMediaStreamSource(new MediaStream(display.getAudioTracks()));
        src.connect(dest);
        src.connect(analyser);
        streams.current.push(display);
        sources += 1;
        display.getAudioTracks()[0]?.addEventListener("ended", () => void stop());
      }
      if (withMic) {
        const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
        const src = audio.createMediaStreamSource(mic);
        src.connect(dest);
        src.connect(analyser);
        streams.current.push(mic);
        sources += 1;
      }
      if (sources === 0) throw new Error(t("record.pickSource"));
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find((m) => MediaRecorder.isTypeSupported(m));
      const rec = new MediaRecorder(dest.stream, mime ? { mimeType: mime, audioBitsPerSecond: BITRATE } : undefined);
      rec.ondataavailable = (e) => {
        if (e.data.size) {
          chunks.current.push(e.data);
          setBytes((b) => b + e.data.size);
        }
      };
      rec.onstop = () => void upload(rec.mimeType || mime || "audio/webm");
      rec.start(5000);
      recorder.current = rec;
      const buf = new Uint8Array(analyser.fftSize);
      const startedAt = Date.now();
      timer.current = window.setInterval(() => {
        setSeconds((Date.now() - startedAt) / 1000);
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = ((buf[i] ?? 128) - 128) / 128;
          sum += v * v;
        }
        setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 4));
      }, 500);
      setPhase("recording");
    } catch (err) {
      cleanup();
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const stop = async () => {
    const rec = recorder.current;
    if (rec && rec.state !== "inactive") rec.stop();
    else cleanup();
  };

  const upload = async (mime: string) => {
    setPhase("uploading");
    const blob = new Blob(chunks.current, { type: mime });
    cleanup();
    try {
      if (!member) throw new Error(t("record.noMember"));
      if (blob.size > MAX_BYTES) throw new Error(t("record.tooLarge"));
      const base64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(blob);
      });
      const ext = mime.includes("mp4") ? "m4a" : "webm";
      const name = `${(title.trim() || "Meeting").replace(/[^\w\- ]+/g, "-").slice(0, 60)}.${ext}`;
      const out = await member.client.startTemplate(PROCESS, { title: title.trim() || "Meeting" }, { name, content_base64: base64 });
      setCaseId(out.case_id);
      setPhase("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("idle");
    }
  };

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>{t("record.title")}</Title>
        <Text c="dimmed" size="sm">{t("record.intro")}</Text>
      </div>

      {!supported && <Alert color="yellow">{t("record.unsupported")}</Alert>}
      {hasProcess === false && <Alert color="yellow">{t("record.noProcess")}</Alert>}
      {error && <Alert color="red">{error}</Alert>}

      <Card withBorder>
        <Stack gap="sm">
          {candidates.length > 1 && (
            <Select label={t("household.members")} data={candidates.map((m) => ({ value: m.member.id, label: m.member.title }))} value={member?.member.id ?? null} onChange={setTarget} disabled={phase !== "idle"} />
          )}
          <TextInput label={t("record.meetingTitle")} placeholder={t("record.titleExample")} value={title} onChange={(e) => setTitle(e.currentTarget.value)} disabled={phase === "uploading"} />
          <Group>
            <Checkbox label={t("record.tabAudio")} checked={withTab && canTab} disabled={!canTab || phase !== "idle"} onChange={(e) => setWithTab(e.currentTarget.checked)} />
            <Checkbox label={t("record.mic")} checked={withMic} disabled={phase !== "idle"} onChange={(e) => setWithMic(e.currentTarget.checked)} />
          </Group>
          <Text size="xs" c="dimmed">{t("record.hint")}</Text>
          {phase === "recording" && (
            <Stack gap={4}>
              <Group gap="sm">
                <Badge color="red" variant="filled">● REC</Badge>
                <Text fw={600}>{stamp(seconds)}</Text>
                <Text size="sm" c="dimmed">{(bytes / 1024 / 1024).toFixed(1)} MB</Text>
              </Group>
              <Progress value={level * 100} size="sm" color={level > 0.02 ? "teal" : "gray"} />
            </Stack>
          )}
          <Group>
            {phase === "idle" && <Button disabled={!supported || !member || hasProcess === false} onClick={() => void start()}>{t("record.start")}</Button>}
            {phase === "recording" && <Button color="red" onClick={() => void stop()}>{t("record.stop")}</Button>}
            {phase === "uploading" && <Button loading>{t("record.uploading")}</Button>}
          </Group>
          {phase === "done" && caseId && member && (
            <Alert color="teal">
              {t("record.done")}{" "}
              <Link to={`/cases/${encodeURIComponent(member.member.id)}/${encodeURIComponent(caseId)}`}>{t("record.openCase")}</Link>
            </Alert>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
