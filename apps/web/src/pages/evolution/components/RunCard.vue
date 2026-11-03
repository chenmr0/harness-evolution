<script setup lang="ts">
import type { EvolutionRunCard } from "@harness/contracts";
import { computed, ref } from "vue";

const props = defineProps<{
  run: EvolutionRunCard;
}>();

const emit = defineEmits<{
  open: [path: string];
}>();

const isActive = computed(() =>
  ["preparing", "running", "blocked"].includes(props.run.status),
);
const errorOpen = ref(false);
const errorTooltipId = computed(() => `run-error-${props.run.runId}`);

const statusLabel = computed(() => {
  switch (props.run.status) {
    case "preparing":
      return "准备中";
    case "running":
      return "正在执行";
    case "blocked":
      return "已阻塞";
    case "completed":
      if (props.run.final === null) return "已完成 · 终评未知";
      return props.run.final.verdict
        ? "✓ 已完成 · 自进化有效"
        : "已完成 · 无收益";
    case "failed":
      return "失败";
    case "interrupted":
      return "已中断";
    default:
      return "未知状态";
  }
});

const statusClass = computed(() => {
  if (props.run.status === "completed") {
    if (props.run.final === null) return "done-pending";
    return props.run.final?.verdict ? "done-ok" : "done-no";
  }
  if (props.run.status === "blocked") return "blocked";
  if (props.run.status === "failed") return "failed";
  if (props.run.status === "interrupted") return "interrupted";
  return "running";
});

const createdMeta = computed(() => {
  const date = props.run.createdAt.replace("T", " ").slice(0, 16);
  return props.run.createdBy
    ? `${props.run.createdBy} · ${date}`
    : date;
});

const targetLabel = computed(() =>
  `${props.run.target.type}:${props.run.target.name}@${props.run.target.version}`,
);

const detailPath = computed(() =>
  props.run.mode === "cluster_only"
    ? `/evolution/cluster/${props.run.runId}`
    : `/evolution/runs/${props.run.runId}`,
);

const windowLabel = computed(() => {
  const { from, to } = props.run.window;
  if (from && to) return `${from} ~ ${to}`;
  if (from) return `${from} ~ 至今`;
  if (to) return `起始未知 ~ ${to}`;
  return "时间窗未设置";
});

const delta = computed(() => {
  if (!props.run.final) return null;
  const value = props.run.final.bestScore - props.run.final.initScore;
  return {
    label: `${value >= 0 ? "+" : ""}${value.toFixed(2)}`,
    positive: value >= 0,
  };
});

function openError(): void {
  errorOpen.value = true;
}

function closeError(): void {
  errorOpen.value = false;
}

</script>

<template>
  <article
    class="run-card"
    :class="`status-${run.status}`"
    :data-run-id="run.runId"
  >
    <a
      class="run-card-link"
      :href="detailPath"
      :aria-label="`查看运行 ${run.title}`"
      @click.prevent="emit('open', detailPath)"
    />
    <div class="run-title-row">
      <span class="title">{{ run.title }}</span>
      <span
        v-if="!run.error"
        class="badge"
        :class="statusClass"
      >
        <span
          v-if="isActive"
          class="pulse"
          aria-hidden="true"
        />
        {{ statusLabel }}
      </span>
      <span
        v-else
        class="error-status"
        @click.stop
        @keydown.stop
        @mouseenter="openError"
        @mouseleave="closeError"
      >
        <button
          class="badge error-trigger"
          :class="statusClass"
          type="button"
          :title="run.error.message"
          :aria-describedby="errorTooltipId"
          :aria-expanded="errorOpen ? 'true' : 'false'"
          @focus="openError"
          @blur="closeError"
          @click="openError"
          @keydown.esc="closeError"
        >
          <span
            v-if="isActive"
            class="pulse"
            aria-hidden="true"
          />
          {{ statusLabel }}
        </button>
        <span
          :id="errorTooltipId"
          class="badge-tooltip"
          :class="{ 'is-open': errorOpen }"
          role="tooltip"
        >
          {{ run.error.message }}
        </span>
      </span>
      <span class="time">{{ createdMeta }}</span>
    </div>

    <div class="run-meta">
      <span class="chip kind">{{ targetLabel }}</span>
      <span class="window mono">{{ windowLabel }}</span>
      <span
        v-for="model in run.models"
        :key="model"
        class="chip model"
      >
        {{ model }}
      </span>
    </div>

    <div class="run-stats">
      <span class="stat"><b>{{ run.stats.sessions }}</b>session</span>
      <span class="stat"><b>{{ run.stats.probes }}</b>问题报告</span>
      <span class="stat"><b>{{ run.stats.usedProbes }}</b>用于进化</span>
      <span class="stat"><b>{{ run.stats.clusters }}</b>聚类</span>
      <span class="stat">
        <b>{{ run.progress.doneRounds }}/{{ run.stats.clusters }}</b>已跑轮次
      </span>
      <span class="stat"><b>{{ run.progress.accepted }}</b>已接纳</span>
    </div>

    <div
      v-if="isActive"
      class="run-progress"
    >
      <div
        class="bar"
        role="progressbar"
        :aria-label="run.progress.stageLabel"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-valuenow="run.progress.percent"
      >
        <div
          class="fill"
          :style="{ width: `${run.progress.percent}%` }"
        />
      </div>
      <span class="progress-copy">
        {{ run.progress.stageLabel }} · {{ run.progress.percent }}%
      </span>
    </div>

    <div
      v-else-if="run.final && delta"
      class="final-line"
    >
      <span>
        best <b class="mono">{{ run.final.bestScore.toFixed(2) }}</b>
        vs 初始 <b class="mono">{{ run.final.initScore.toFixed(2) }}</b>
      </span>
      <b
        class="delta mono"
        :class="delta.positive ? 'delta-pos' : 'delta-neg'"
      >{{ delta.label }}</b>
      <span class="accepted-copy">
        · 接纳 {{ run.progress.accepted }}/{{ run.stats.clusters }} 个 patch
      </span>
    </div>
    <div
      v-else-if="run.status === 'completed'"
      class="final-line unavailable"
    >
      终评数据暂不可用
    </div>
  </article>
</template>

<style scoped>
.run-card {
  position: relative;
  min-width: 0;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-card);
  box-shadow: var(--shadow-card);
  cursor: pointer;
  transition: border-color 0.12s;
}

.run-card:hover,
.run-card:has(.run-card-link:focus-visible) {
  border-color: var(--blue);
}

.run-card-link {
  position: absolute;
  z-index: 1;
  inset: 0;
  border-radius: 12px;
}

.run-card-link:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}

.run-card > :not(.run-card-link) {
  position: relative;
  z-index: 2;
  pointer-events: none;
}

.error-status,
.error-trigger {
  pointer-events: auto;
}

.run-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.title {
  color: var(--text);
  font-size: 14px;
  font-weight: 650;
}

.time {
  margin-left: auto;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11.5px;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.badge.running {
  color: var(--blue);
  background: color-mix(in srgb, var(--blue) 13%, transparent);
}

.badge.done-ok {
  color: var(--green);
  background: color-mix(in srgb, var(--green) 13%, transparent);
}

.badge.done-no,
.badge.done-pending {
  color: var(--text-dim);
  background: color-mix(in srgb, var(--text-faint) 22%, transparent);
}

.badge.blocked,
.badge.interrupted {
  color: var(--orange);
  background: color-mix(in srgb, var(--orange) 12%, transparent);
}

.badge.failed {
  color: var(--red);
  background: color-mix(in srgb, var(--red) 11%, transparent);
}

.error-status {
  position: relative;
  display: inline-flex;
}

.error-trigger {
  border: 0;
  cursor: pointer;
  font: inherit;
}

.badge-tooltip {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 20;
  width: max-content;
  max-width: min(320px, calc(100vw - 64px));
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg-card);
  box-shadow: var(--shadow-popover);
  color: var(--text-dim);
  font-size: 11px;
  line-height: 1.5;
  opacity: 0;
  pointer-events: none;
  transform: translateY(-2px);
  visibility: hidden;
  white-space: normal;
}

.badge-tooltip.is-open {
  opacity: 1;
  transform: translateY(0);
  visibility: visible;
}

.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--blue);
  animation: pulse 1.4s infinite;
}

.run-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin: 8px 0;
  color: var(--text-dim);
  font-size: 11.5px;
}

.chip {
  display: inline-block;
  padding: 1px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-card);
  color: var(--text-dim);
  white-space: nowrap;
}

.chip.kind {
  color: var(--purple);
  border-color: color-mix(in srgb, var(--purple) 35%, transparent);
  background: color-mix(in srgb, var(--purple) 9%, transparent);
  font-family: var(--mono);
  font-size: 10.5px;
}

.chip.model {
  color: var(--cyan);
  border-color: color-mix(in srgb, var(--cyan) 35%, transparent);
  background: color-mix(in srgb, var(--cyan) 9%, transparent);
  font-family: var(--mono);
  font-size: 10.5px;
}

.window {
  font-size: 11px;
}

.run-stats {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
}

.stat {
  color: var(--text-faint);
  font-size: 11px;
}

.stat b {
  margin-right: 3px;
  color: var(--text);
  font-family: var(--mono);
  font-weight: 600;
}

.run-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
}

.bar {
  flex: 1;
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--density-track);
}

.fill {
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--blue) 55%, white),
    var(--blue)
  );
  transition: width 0.4s;
}

.progress-copy {
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11px;
  white-space: nowrap;
}

.final-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
  color: var(--text-dim);
  font-size: 12px;
}

.delta {
  font-weight: 600;
}

.delta-pos {
  color: var(--green);
}

.delta-neg {
  color: var(--red);
}

.accepted-copy,
.unavailable {
  color: var(--text-faint);
}

@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }

  50% {
    opacity: 0.25;
  }
}

@media (max-width: 640px) {
  .run-title-row {
    align-items: flex-start;
  }

  .time {
    width: 100%;
    margin-left: 0;
  }

  .run-progress {
    align-items: flex-start;
    flex-direction: column;
  }

  .progress-copy {
    white-space: normal;
  }
}
</style>
