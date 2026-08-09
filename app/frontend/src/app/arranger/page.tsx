"use client";

import { useState, useCallback, useRef } from "react";
import {
  Layers,
  Upload,
  Download,
  Loader2,
  CheckCircle2,
  Sparkles,
  RotateCcw,
  FileAudio,
  Dice5,
} from "lucide-react";
import { api } from "@/hooks/useApi";
import { useToast } from "@/components/ToastProvider";
/* ── Types ───────────────────────────────────────────────────── */

interface ArrangeResult {
  status: string;
  output_filename: string;
  template_used: string;
  template_id: string;
  genre: string;
  tempo: number;
  patterns_detected: number;
  patterns_with_notes: number;
  roles_detected: Record<string, number>;
  sections_applied: number;
  patterns_moved: number;
  mixer_routing: Record<string, number>;
  had_existing_arrangement: boolean;
}

type Stage = "idle" | "uploading" | "done" | "error";

const ROLE_COLORS: Record<string, string> = {
  melody: "#38bdf8",
  drums: "#ff4444",
  bass: "#f5a623",
  keys: "#a78bfa",
  fx: "#e040fb",
  perc: "#f97316",
};

const GENRE_COLORS: Record<string, string> = {
  trap: "#ff4444",
  drill: "#9333ea",
  rnb: "#f472b6",
  melodic: "#38bdf8",
  dark_trap: "#6b21a8",
};

const ALL_TEMPLATES = [
  "drop_first",
  "trap_banger",
  "drill_beat",
  "melodic_rap",
  "dark_trap",
  "rnb_groove",
  "simple_beat",
];

/* ── Page ────────────────────────────────────────────────────── */

export default function ArrangerPage() {
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const lastFileRef = useRef<File | null>(null);

  const [stage, setStage] = useState<Stage>("idle");
  const [fileName, setFileName] = useState<string>("");
  const [result, setResult] = useState<ArrangeResult | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [dragOver, setDragOver] = useState(false);
  const [rolling, setRolling] = useState(false);

  /* ── Arrange (optionally with a forced template) ── */
  const handleArrange = useCallback(
    async (file: File, forceTemplate?: string) => {
      lastFileRef.current = file;
      setFileName(file.name);
      setStage("uploading");
      setResult(null);
      setErrorMsg("");

      try {
        const formData = new FormData();
        formData.append("file", file);

        const url = forceTemplate
          ? `/arrangement/one-click?template_id=${encodeURIComponent(forceTemplate)}`
          : "/arrangement/one-click";

        const res = await api.upload<ArrangeResult>(url, formData);

        setResult(res);
        setStage("done");
        toast(
          `Arranged with ${res.template_used} — ${res.patterns_moved} patterns placed`,
          "success"
        );
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : "Arrangement failed";
        setErrorMsg(msg);
        setStage("error");
        toast(msg, "error");
      }
    },
    [toast]
  );

  /* ── Dice: re-arrange with a random different template ── */
  const handleDice = useCallback(async () => {
    const file = lastFileRef.current;
    if (!file || !result) return;

    setRolling(true);

    // Pick a random template that's different from the current one
    const others = ALL_TEMPLATES.filter((t) => t !== result.template_id);
    const pick = others[Math.floor(Math.random() * others.length)];

    await handleArrange(file, pick);
    setRolling(false);
  }, [result, handleArrange]);

  /* ── Download ── */
  const handleDownload = useCallback(async () => {
    if (!result?.output_filename) return;
    try {
      const token = localStorage.getItem("fy3-token");
      const res = await fetch(
        `/api/arrangement/download/${encodeURIComponent(result.output_filename)}`,
        {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        }
      );
      if (!res.ok) throw new Error("Download failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = result.output_filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast(`Downloaded ${result.output_filename}`, "success");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Download failed";
      toast(msg, "error");
    }
  }, [result, toast]);

  /* ── Reset ── */
  const handleReset = useCallback(() => {
    setStage("idle");
    setFileName("");
    setResult(null);
    setErrorMsg("");
    lastFileRef.current = null;
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  /* ── Drag & Drop ── */
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files?.[0];
      if (file && file.name.toLowerCase().endsWith(".flp")) {
        handleArrange(file);
      } else {
        toast("Only .flp files are accepted", "error");
      }
    },
    [handleArrange, toast]
  );

  const genreColor = result
    ? GENRE_COLORS[result.genre] ?? "var(--accent)"
    : "var(--accent)";

  return (
    <div className="animate-fade-in">
      {/* ── Header ── */}
      <div className="page-header">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2">
              <Layers size={20} className="text-accent" />
              Arranger
            </h1>
            <p className="page-subtitle">
              Drop an .flp — auto-detects patterns, picks the best template,
              arranges it
            </p>
          </div>
          {stage === "done" && (
            <button
              onClick={handleReset}
              className="flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold transition-all cursor-pointer"
              style={{
                background: "var(--bg-hover)",
                border: "1px solid var(--border)",
                color: "var(--text-secondary)",
              }}
            >
              <RotateCcw size={13} />
              New
            </button>
          )}
        </div>
      </div>

      {/* ── Drop Zone (idle state) ── */}
      {stage === "idle" && (
        <label
          className="flex flex-col items-center justify-center gap-4 p-16 rounded-2xl cursor-pointer transition-all"
          style={{
            background: dragOver ? "var(--accent)08" : "var(--bg-card)",
            backdropFilter: "blur(16px)",
            border: dragOver
              ? "2px solid var(--accent)"
              : "2px dashed var(--border)",
            ...(dragOver && { boxShadow: "0 0 40px var(--accent)15" }),
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <div
            className="w-16 h-16 rounded-2xl flex items-center justify-center"
            style={{
              background: "var(--accent)12",
              border: "1px solid var(--accent)30",
            }}
          >
            <Upload size={28} className="text-accent" />
          </div>
          <div className="text-center">
            <p className="text-sm font-bold text-foreground">
              Drop your .flp file here
            </p>
            <p className="text-xs text-text-tertiary mt-1">
              or click to browse — arrangement starts automatically
            </p>
          </div>
          <div className="flex gap-3 mt-2">
            {["Auto-detect roles", "Pick best template", "Smart layering", "Mixer routing"].map(
              (feat) => (
                <span
                  key={feat}
                  className="text-[10px] font-semibold px-2.5 py-1 rounded-full"
                  style={{
                    background: "var(--bg-hover)",
                    border: "1px solid var(--border)",
                    color: "var(--text-tertiary)",
                  }}
                >
                  {feat}
                </span>
              )
            )}
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".flp"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleArrange(f);
            }}
          />
        </label>
      )}

      {/* ── Processing State ── */}
      {stage === "uploading" && (
        <div
          className="flex flex-col items-center justify-center gap-6 p-16 rounded-2xl"
          style={{
            background: "var(--bg-card)",
            backdropFilter: "blur(16px)",
            border: "1px solid var(--glass-border)",
          }}
        >
          <div className="relative">
            <div
              className="w-20 h-20 rounded-2xl flex items-center justify-center"
              style={{
                background: "var(--accent)12",
                border: "1px solid var(--accent)30",
              }}
            >
              <Sparkles size={32} className="text-accent animate-pulse" />
            </div>
            <div
              className="absolute -bottom-1 -right-1 w-8 h-8 rounded-full flex items-center justify-center"
              style={{
                background: "var(--bg-card)",
                border: "2px solid var(--accent)",
              }}
            >
              <Loader2 size={14} className="animate-spin text-accent" />
            </div>
          </div>
          <div className="text-center">
            <p className="text-sm font-bold text-foreground">Arranging...</p>
            <p className="text-xs text-text-tertiary mt-1">
              <FileAudio size={10} className="inline mr-1" />
              {fileName}
            </p>
            <p className="text-[10px] text-text-tertiary mt-3 max-w-xs">
              Parsing FL Studio project, detecting pattern roles, picking the
              best template, applying smart layering, routing to mixer
            </p>
          </div>
        </div>
      )}

      {/* ── Error State ── */}
      {stage === "error" && (
        <div
          className="flex flex-col items-center justify-center gap-4 p-12 rounded-2xl"
          style={{
            background: "#ff444408",
            border: "1.5px solid #ff444430",
          }}
        >
          <p className="text-sm font-bold text-foreground">
            Arrangement failed
          </p>
          <p className="text-xs text-text-tertiary text-center max-w-md">
            {errorMsg}
          </p>
          <button
            onClick={handleReset}
            className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-xs font-bold cursor-pointer transition-all"
            style={{
              background: "var(--bg-hover)",
              border: "1px solid var(--border)",
              color: "var(--text-secondary)",
            }}
          >
            <RotateCcw size={13} />
            Try again
          </button>
        </div>
      )}

      {/* ── Success Result ── */}
      {stage === "done" && result && (
        <div className="space-y-4">
          {/* Main result card */}
          <div
            className="p-6 rounded-2xl"
            style={{
              background: `linear-gradient(135deg, ${genreColor}06, ${genreColor}03)`,
              border: `1.5px solid ${genreColor}30`,
            }}
          >
            <div className="flex items-start justify-between mb-5">
              <div className="flex items-center gap-3">
                <div
                  className="w-12 h-12 rounded-xl flex items-center justify-center"
                  style={{
                    background: `${genreColor}15`,
                    border: `1px solid ${genreColor}30`,
                  }}
                >
                  <CheckCircle2 size={24} style={{ color: "#00d362" }} />
                </div>
                <div>
                  <p className="text-sm font-bold text-foreground">
                    Arrangement complete
                  </p>
                  <p className="text-xs text-text-tertiary mt-0.5">
                    {result.output_filename}
                  </p>
                </div>
              </div>

              <div className="flex gap-2">
                {/* Dice button */}
                <button
                  onClick={handleDice}
                  disabled={rolling}
                  className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs font-bold transition-all cursor-pointer disabled:opacity-40"
                  style={{
                    background: "#6366f118",
                    color: "#6366f1",
                    border: "1.5px solid #6366f140",
                  }}
                  title="Re-arrange with a random template"
                >
                  {rolling ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : (
                    <Dice5 size={14} />
                  )}
                  Shuffle
                </button>

                {/* Download button */}
                <button
                  onClick={handleDownload}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-xs font-bold uppercase tracking-wider transition-all cursor-pointer"
                  style={{
                    background: "#00d36218",
                    color: "#00d362",
                    border: "1.5px solid #00d36240",
                  }}
                >
                  <Download size={14} />
                  Download .flp
                </button>
              </div>
            </div>

            {/* Stats row */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
              <div
                className="text-center p-3 rounded-xl"
                style={{ background: "var(--bg-hover)" }}
              >
                <p className="text-lg font-bold text-foreground">
                  {result.tempo}
                </p>
                <p className="text-[9px] text-text-tertiary uppercase">BPM</p>
              </div>
              <div
                className="text-center p-3 rounded-xl"
                style={{ background: "var(--bg-hover)" }}
              >
                <p className="text-lg font-bold text-foreground">
                  {result.patterns_with_notes}
                </p>
                <p className="text-[9px] text-text-tertiary uppercase">
                  Patterns
                </p>
              </div>
              <div
                className="text-center p-3 rounded-xl"
                style={{ background: "var(--bg-hover)" }}
              >
                <p className="text-lg font-bold text-foreground">
                  {result.sections_applied}
                </p>
                <p className="text-[9px] text-text-tertiary uppercase">
                  Sections
                </p>
              </div>
              <div
                className="text-center p-3 rounded-xl"
                style={{ background: "var(--bg-hover)" }}
              >
                <p className="text-lg font-bold text-foreground">
                  {result.patterns_moved}
                </p>
                <p className="text-[9px] text-text-tertiary uppercase">
                  Placed
                </p>
              </div>
            </div>

            {/* Template & genre info */}
            <div className="flex flex-wrap items-center gap-2 mb-4">
              <span
                className="text-[9px] font-bold px-2 py-0.5 rounded-full uppercase"
                style={{
                  background: `${genreColor}20`,
                  color: genreColor,
                }}
              >
                {result.genre.replace("_", " ")}
              </span>
              <span
                className="text-[10px] font-semibold px-2.5 py-0.5 rounded-lg"
                style={{
                  background: "var(--bg-hover)",
                  border: "1px solid var(--border)",
                  color: "var(--text-secondary)",
                }}
              >
                <Sparkles size={9} className="inline mr-1" />
                {result.template_used}
              </span>
              {result.had_existing_arrangement && (
                <span
                  className="text-[9px] font-bold px-2 py-0.5 rounded-full uppercase"
                  style={{
                    background: "#f5a62318",
                    color: "#f5a623",
                  }}
                >
                  re-arranged
                </span>
              )}
            </div>

            {/* Detected roles */}
            <div>
              <p className="text-[10px] font-bold uppercase tracking-wider text-text-tertiary mb-2">
                Auto-detected roles
              </p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(result.roles_detected).map(([role, count]) => {
                  const color = ROLE_COLORS[role] ?? "#6b7280";
                  return (
                    <span
                      key={role}
                      className="text-[10px] font-semibold px-2.5 py-1 rounded-lg flex items-center gap-1.5"
                      style={{
                        background: `${color}12`,
                        border: `1px solid ${color}40`,
                        color: "var(--text-secondary)",
                      }}
                    >
                      <span
                        className="text-[8px] font-bold uppercase px-1.5 py-0.5 rounded"
                        style={{
                          background: `${color}25`,
                          color,
                        }}
                      >
                        {role}
                      </span>
                      {count} {count === 1 ? "pattern" : "patterns"}
                    </span>
                  );
                })}
              </div>
            </div>

            {/* Mixer routing */}
            {result.mixer_routing &&
              Object.keys(result.mixer_routing).length > 0 && (
                <div className="mt-4">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-text-tertiary mb-2">
                    Mixer routing
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {Object.entries(result.mixer_routing).map(
                      ([channel, insert]) => (
                        <span
                          key={channel}
                          className="text-[10px] font-semibold px-2.5 py-1 rounded-lg flex items-center gap-1.5"
                          style={{
                            background: "#00d36210",
                            border: "1px solid #00d36230",
                            color: "var(--text-secondary)",
                          }}
                        >
                          <span
                            className="text-[8px] font-bold px-1.5 py-0.5 rounded"
                            style={{
                              background: "#00d36220",
                              color: "#00d362",
                            }}
                          >
                            INS {insert}
                          </span>
                          {channel}
                        </span>
                      )
                    )}
                  </div>
                </div>
              )}
          </div>

          {/* Arrange another */}
          <label
            className="flex items-center justify-center gap-3 p-4 rounded-2xl cursor-pointer transition-all"
            style={{
              background: "var(--bg-card)",
              border: "2px dashed var(--border)",
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              const file = e.dataTransfer.files?.[0];
              if (file && file.name.toLowerCase().endsWith(".flp")) {
                handleArrange(file);
              }
            }}
          >
            <Upload size={14} className="text-text-tertiary" />
            <span className="text-xs text-text-tertiary font-semibold">
              Drop another .flp to arrange
            </span>
            <input
              type="file"
              accept=".flp"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleArrange(f);
              }}
            />
          </label>
        </div>
      )}
    </div>
  );
}
