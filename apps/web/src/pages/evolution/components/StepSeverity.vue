<script setup lang="ts">
import type { EvolutionStepSeverity, StepSeverityRow } from "@harness/contracts";

defineProps<{
  step: EvolutionStepSeverity;
}>();

function typeLabel(type: StepSeverityRow["type"]): string {
  return type === "prompt" ? "prompt" : type === "model" ? "模型" : "错误";
}

function confidence(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(0)}%`;
}
</script>

<template>
  <article class="card panel">
    <h3 class="sec-title">
      第 4 步 · 严重程度排序
    </h3>
    <div
      v-if="step.payload.rows.length"
      class="table-scroll"
    >
      <table class="metrics">
        <thead>
          <tr>
            <th>迭代顺序</th>
            <th>聚类问题</th>
            <th>类型</th>
            <th>置信度</th>
            <th>影响面</th>
            <th>报告频次</th>
            <th>严重程度</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in step.payload.rows"
            :key="row.clusterId"
          >
            <td class="num">
              <span class="rank">#{{ row.rank ?? "—" }}</span>
            </td>
            <td><b>{{ row.clusterId }}</b> · {{ row.title }}</td>
            <td>
              <span
                class="type-chip"
                :class="`type-${row.type}`"
              >{{ typeLabel(row.type) }}</span>
            </td>
            <td class="num">
              {{ confidence(row.confidence) }}
            </td>
            <td class="num">
              未评估
            </td>
            <td class="num">
              {{ row.reportCount }} 份
            </td>
            <td>
              <span class="severity">
                <span class="bar">
                  <span
                    class="fill"
                    :style="{ width: `${((row.severity ?? 0) / 10) * 100}%` }"
                  />
                </span>
                <b>{{ row.severity === null ? "—" : row.severity.toFixed(1) }}</b>
              </span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p
      v-else
      class="waiting"
    >
      当前运行还没有严重程度排序结果。
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

td.num {
  font-family: var(--mono);
}

.rank {
  padding: 1px 7px;
  border-radius: 6px;
  background: var(--orange);
  color: white;
  font-family: var(--mono);
  font-size: 11px;
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

.severity {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  font-family: var(--mono);
}

.bar {
  width: 56px;
  height: 5px;
  overflow: hidden;
  border-radius: 3px;
  background: var(--density-track);
}

.fill {
  display: block;
  height: 100%;
  border-radius: 3px;
  background: var(--orange);
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
