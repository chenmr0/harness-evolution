<script setup lang="ts">
import type { EvolutionStepFinal } from "@harness/contracts";
import { computed } from "vue";

const props = defineProps<{
  step: EvolutionStepFinal;
  targetVersion: string;
  pushStatus?: string;
  pushedAt?: string | null;
}>();

type Metric = {
  name: string;
  init: number | string;
  best: number | string;
  delta: number;
  unit: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isScore(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function independentMetrics(item: Record<string, unknown>): Metric[] {
  if (!Array.isArray(item.metrics) || !isScore(item.currentScore) || !isScore(item.candidateScore)) {
    return [];
  }
  const evidence: unknown = item.metrics.find((entry: unknown) =>
    isRecord(entry) && entry.kind === "case_scores",
  );
  if (!isRecord(evidence) || !isRecord(evidence.current) || !isRecord(evidence.candidate)) {
    return [];
  }
  const baseline = evidence.current.caseScores;
  const candidate = evidence.candidate.caseScores;
  if (!isRecord(baseline) || !isRecord(candidate)) return [];
  const caseIds = Object.keys(baseline).sort();
  if (!caseIds.length || caseIds.length !== Object.keys(candidate).length) return [];
  const rows: Metric[] = [];
  for (const caseId of caseIds) {
    const init = baseline[caseId];
    const best = candidate[caseId];
    if (!Object.hasOwn(candidate, caseId) || !isScore(init) || !isScore(best) || !isScore(best - init)) {
      return [];
    }
    rows.push({ name: `用例 ${caseId}`, init, best, delta: best - init, unit: "" });
  }
  const delta = item.candidateScore - item.currentScore;
  if (!isScore(delta)) return [];
  const hard = evidence.current.scoreMetric === "hard" && evidence.candidate.scoreMetric === "hard";
  return [
    {
      name: hard ? "hard 均分" : "total_score 均值",
      init: hard ? item.currentScore.toFixed(2) : item.currentScore,
      best: hard ? item.candidateScore.toFixed(2) : item.candidateScore,
      delta,
      unit: "",
    },
    ...rows,
  ];
}

const metrics = computed(() =>
  props.step.payload.metrics.flatMap((item): Metric[] => {
    if (item.phase === "independent_final") return independentMetrics(item);
    const name = item.name;
    const init = item.init;
    const best = item.best;
    const delta = item.delta;
    if (
      typeof name !== "string" ||
      (typeof init !== "number" && typeof init !== "string") ||
      (typeof best !== "number" && typeof best !== "string") ||
      !isScore(delta)
    ) {
      return [];
    }
    return [{
      name,
      init,
      best,
      delta,
      unit: typeof item.unit === "string" ? item.unit : "",
    }];
  }),
);

function deltaLabel(delta: number, unit = ""): string {
  return `${delta >= 0 ? "+" : ""}${delta.toFixed(2)}${unit}`;
}

function score(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function typeLabel(type: string): string {
  if (type === "prompt") return "prompt";
  if (type === "model") return "模型";
  return "错误";
}

const pushLabel = computed(() => {
  switch (props.pushStatus) {
    case "succeeded":
      return "最终 push 成功";
    case "pushing":
      return "最终 push 进行中";
    case "failed":
      return "最终 push 失败";
    case "skipped":
      return "最终 push 已跳过";
    default:
      return "最终 push 待执行";
  }
});

const pushedAtLabel = computed(() =>
  props.pushedAt ? props.pushedAt.replace("T", " ").slice(0, 16) : "尚未推送",
);
</script>

<template>
  <article class="card panel">
    <h3 class="sec-title">
      第 6 步 · 测试集终评
      <span class="sub">全部迭代完成后的最终结果</span>
    </h3>
    <template v-if="step.payload.verdict !== null">
      <div
        class="verdict"
        :class="step.payload.verdict ? 'ok' : 'no'"
      >
        <span class="big">{{ step.payload.verdict ? "✓ 自进化有效" : "✗ 未产生收益" }}</span>
        <span class="desc">
          best skill <b class="mono">{{ step.payload.bestVersion ?? "—" }}</b>
          测试集得分 <b class="mono">{{ score(step.payload.bestScore) }}</b>
          vs 初始 <b class="mono">{{ score(step.payload.initScore) }}</b>
          <template v-if="step.payload.bestScore !== null && step.payload.initScore !== null">
            （{{ deltaLabel(step.payload.bestScore - step.payload.initScore) }}）
          </template>
        </span>
        <span class="final-push mono">{{ pushLabel }} · {{ pushedAtLabel }}</span>
      </div>

      <section class="cb-sec">
        <p class="cb-label">
          best skill 相对初始版本的修改点（{{ step.payload.changes.length }} 个已接纳 patch）
        </p>
        <div
          v-if="step.payload.changes.length"
          class="changes-list"
        >
          <div
            v-for="change in step.payload.changes"
            :key="change.commit"
            class="ch"
          >
            <span class="commit">{{ change.commit.slice(0, 12) }}</span>
            <span>[{{ change.clusterId }}] {{ change.title }}</span>
            <span
              class="type-chip"
              :class="`type-${change.type}`"
            >{{ typeLabel(change.type) }}</span>
            <span class="muted">来源 {{ change.reportCount }} 个问题</span>
          </div>
        </div>
        <p
          v-else
          class="muted"
        >
          没有已接纳的修改点。
        </p>
      </section>

      <section class="cb-sec">
        <p class="cb-label">
          测试集对比（best vs 初始）
        </p>
        <div
          v-if="metrics.length"
          class="table-scroll"
        >
          <table class="metrics">
            <thead>
              <tr>
                <th>指标</th>
                <th>初始 {{ targetVersion }}</th>
                <th>best</th>
                <th>Δ</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="metric in metrics"
                :key="metric.name"
              >
                <td>{{ metric.name }}</td>
                <td class="num">
                  {{ metric.init }}
                </td>
                <td class="num">
                  {{ metric.best }}
                </td>
                <td :class="metric.delta >= 0 ? 'delta-pos' : 'delta-neg'">
                  {{ deltaLabel(metric.delta, metric.unit) }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p
          v-else
          class="muted"
        >
          终评指标暂不可用。
        </p>
      </section>
    </template>
    <p
      v-else
      class="waiting"
    >
      等待全部迭代完成后，在测试集上评测 best skill 与初始 skill。
    </p>
  </article>
</template>

<style scoped>
.card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg-card);
  box-shadow: var(--shadow-card);
}

.panel {
  padding: 16px 18px;
}

.sec-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 10px;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 650;
}

.sub {
  color: var(--text-faint);
  font-size: 11.5px;
  font-weight: 400;
}

.verdict {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  padding: 13px 16px;
  border-radius: 10px;
  flex-wrap: wrap;
}

.verdict.ok {
  border: 1px solid color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

.verdict.no {
  border: 1px solid var(--border);
  background: color-mix(in srgb, var(--text-faint) 10%, transparent);
}

.big {
  color: var(--text);
  font-size: 15px;
  font-weight: 700;
}

.desc {
  color: var(--text-dim);
  font-size: 12px;
}

.final-push {
  width: 100%;
  color: var(--text-faint);
  font-size: 11px;
}

.mono,
.commit,
.num {
  font-family: var(--mono);
}

.cb-sec {
  margin-bottom: 14px;
}

.cb-sec:last-child {
  margin-bottom: 0;
}

.cb-label {
  margin: 0 0 6px;
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 600;
}

.changes-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12.5px;
}

.ch {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.commit {
  color: var(--green);
  font-weight: 700;
}

.type-chip {
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 10.5px;
}

.type-error {
  color: var(--red);
}

.type-prompt {
  color: var(--orange);
}

.type-model {
  color: var(--purple);
}

.muted {
  color: var(--text-faint);
  font-size: 11.5px;
}

.table-scroll {
  overflow-x: auto;
}

table.metrics {
  width: 100%;
  border-collapse: collapse;
  color: var(--text);
  font-size: 12.5px;
}

table.metrics th,
table.metrics td {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}

table.metrics th {
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 600;
}

.delta-pos {
  color: var(--green);
}

.delta-neg {
  color: var(--red);
}

.waiting {
  margin: 0;
  padding: 18px;
  border: 1px dashed var(--border);
  border-radius: 10px;
  color: var(--text-faint);
  font-size: 12.5px;
  text-align: center;
}
</style>
