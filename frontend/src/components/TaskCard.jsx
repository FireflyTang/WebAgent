import { useState } from "react";
import {
  CaretRight,
  CheckCircle,
  Circle,
  SpinnerGap,
  Stop,
  WarningCircle,
} from "@phosphor-icons/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const elapsed = (started, ended, now = Date.now()) =>
  started ? Math.max(0, ((ended || now) - started) / 1000) : 0;

const clock = (value) => {
  const total = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};

export function MarkdownContent({
  content,
  streaming = false,
  className = "",
}) {
  return (
    <div className={`markdown-content ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      {streaming && <i className="cursor" />}
    </div>
  );
}

function NarrationRow({ item }) {
  return (
    <div className="narration-row" data-kind="narration">
      <span className="narration-node" aria-hidden="true" />
      <MarkdownContent content={item.content} />
    </div>
  );
}

function StepRow({ step, now }) {
  const [open, setOpen] = useState(false);
  const status =
    step.status === "completed"
      ? "完成"
      : step.status === "failed"
        ? "失败"
        : step.status === "aborted"
          ? "已中止"
          : step.status === "stopped"
            ? "已停止"
            : step.status === "pending"
              ? "等待中"
              : "进行中";
  const Icon =
    step.status === "completed"
      ? CheckCircle
      : ["failed", "stopped", "aborted"].includes(step.status)
        ? WarningCircle
        : step.status === "pending"
          ? Circle
          : SpinnerGap;
  return (
    <div className={`step-wrap phase-${step.phase || "other"}`}>
      <div className="step-row" data-status={step.status}>
        <Icon
          className={step.status === "running" ? "spin" : ""}
          weight={step.status === "completed" ? "fill" : "regular"}
        />
        <strong>{step.label}</strong>
        <span>
          {status}
          {step.startedAt
            ? ` · ${clock(elapsed(step.startedAt, step.endedAt, now))}`
            : ""}
        </span>
        {step.activities.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            活动 {step.activities.length}
            <CaretRight className={open ? "rotated-right" : ""} />
          </button>
        )}
      </div>
      {open && (
        <ul className="activities">
          {step.activities.map((activity) => (
            <li key={activity.id}>
              {activity.label}
              {activity.detail && <small>{activity.detail}</small>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TaskTimeline({ steps, now }) {
  const [expanded, setExpanded] = useState(() => new Set());
  const rows = [];
  const renderStep = (item) =>
    item.kind === "narration" ? (
      <NarrationRow key={item.id} item={item} />
    ) : (
      <StepRow key={item.id} step={item} now={now} />
    );

  for (let index = 0; index < steps.length; ) {
    const item = steps[index];
    const foldable =
      item.kind !== "narration" &&
      item.status === "completed" &&
      ["tool", "task"].includes(item.phase);
    if (!foldable) {
      rows.push(renderStep(item));
      index += 1;
      continue;
    }
    let end = index + 1;
    while (end < steps.length) {
      const candidate = steps[end];
      if (
        candidate.kind === "narration" ||
        candidate.status !== "completed" ||
        !["tool", "task"].includes(candidate.phase)
      ) {
        break;
      }
      end += 1;
    }
    const segment = steps.slice(index, end);
    if (segment.length <= 2) {
      segment.forEach((step) => rows.push(renderStep(step)));
      index = end;
      continue;
    }
    const hidden = segment.length - 2;
    const key = `${segment[0].id}:${segment.at(-1).id}`;
    const open = expanded.has(key);
    if (open) {
      segment.forEach((step) => rows.push(renderStep(step)));
      rows.push(
        <button
          type="button"
          className="step-fold-row"
          aria-expanded="true"
          key={`fold-${key}`}
          onClick={() =>
            setExpanded((current) => {
              const next = new Set(current);
              next.delete(key);
              return next;
            })
          }
        >
          收起 {hidden} 个
        </button>,
      );
    } else {
      rows.push(renderStep(segment[0]));
      rows.push(
        <button
          type="button"
          className="step-fold-row"
          aria-expanded="false"
          key={`fold-${key}`}
          onClick={() => setExpanded((current) => new Set(current).add(key))}
        >
          ……已折叠 {hidden} 个……
        </button>,
      );
      rows.push(renderStep(segment.at(-1)));
    }
    index = end;
  }
  return <div className="steps">{rows}</div>;
}

export default function TaskCard({
  task,
  now,
  onStop,
  stopDisabled = false,
  stopHint = "",
}) {
  if (!task) return null;
  const stoppable = ["starting", "running"].includes(task.status);
  const label =
    task.status === "completed"
      ? "任务完成"
      : task.status === "aborted"
        ? "已中止"
        : task.status === "stopped"
          ? "已停止"
          : task.status === "failed"
            ? "执行失败"
            : task.status === "stopping"
              ? "正在停止"
              : task.status === "finishing"
                ? "正在完成"
                : "正在工作";
  return (
    <section className={`task-card ${task.status}`}>
      <header>
        <strong>
          {label} · {clock(elapsed(task.startedAt, task.endedAt, now))}
        </strong>
        {stoppable && (
          <button
            type="button"
            className="stop-inline"
            onClick={onStop}
            disabled={stopDisabled}
            title={stopDisabled ? stopHint : undefined}
          >
            <Stop weight="fill" />
            停止
          </button>
        )}
      </header>
      {stoppable && stopDisabled && (
        <p className="stop-hint" role="status">
          {stopHint}
        </p>
      )}
      <TaskTimeline steps={task.steps} now={now} />
      {task.usage && (
        <footer className="activity-summary">
          tokens {task.usage.input_tokens || 0}/{task.usage.output_tokens || 0}
        </footer>
      )}
    </section>
  );
}
