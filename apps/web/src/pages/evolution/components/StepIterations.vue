<script setup lang="ts">
import type { EvolutionStepIteration, StepRound } from "@harness/contracts";
import { computed, ref } from "vue";

import DiffView from "./DiffView.vue";
import EvalCompare from "./EvalCompare.vue";
import ProbeSourceTable from "./ProbeSourceTable.vue";

const props = defineProps<{
  step: EvolutionStepIteration;
  blockedStage?: string | null;
}>();

const rounds = computed(() => props.step.payload.rounds);
const expanded = ref<Set<string>>(initialExpanded(rounds.value));

function initialExpanded(items: StepRound[]): Set<string> {
  const active = items.find((round) => !isTerminal(round));
  const first = active ?? items[0];
  return new Set(first ? [first.roundId] : []);
}

function isTerminal(round: StepRound): boolean {
  return (
    round.status === "accepted" ||
    round.status === "rejected" ||
    round.status === "skipped" ||
    round.status === "failed"
  );
}

function toggle(roundId: string): void {
  const next = new Set(expanded.value);
  if (next.has(roundId)) next.delete(roundId);
  else next.add(roundId);
  expanded.value = next;
}

function typeLabel(type: StepRound["type"]): string {
  return type === "prompt" ? "prompt" : type === "model" ? "模型" : "错误";
}

function delta(round: StepRound): number | null {
  if (round.eval.current === null || round.eval.candidate === null) return null;
  return round.eval.candidate - round.eval.current;
}

function deltaLabel(round: StepRound): string {
  const value = delta(round);
  if (value === null) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}`;
}

function statusLabel(round: StepRound): string {
  if (props.blockedStage === "patch" && round.status === "patching") {
    return "patch 已阻塞";
  }
  if (round.push.status === "failed") {
    return "推送失败";
  }
  if (round.push.status === "skipped") {
    return "本地候选（未推送）";
  }
  if (round.eval.status === "skipped") {
    return round.status === "rejected" ? "未接纳（未执行评测）" : "已跳过（未执行评测）";
  }
  if (round.eval.status === "timeout") {
    return "评测超时";
  }
  if (round.eval.status === "failed") {
    return "评测失败";
  }
  switch (round.status) {
    case "patching":
      return "生成 patch 中";
    case "pushing":
      return "推送中";
    case "evaluating":
      return "评测中";
    case "deciding":
      return "决策中";
    case "accepted":
      return `已接纳 ${deltaLabel(round)}`;
    case "rejected":
      return `未接纳 ${deltaLabel(round)}`;
    case "skipped":
      return "本地候选（未推送）";
    case "failed":
      return "失败";
    default:
      return "待执行";
  }
}

function statusClass(round: StepRound): string {
  if (round.status === "accepted") return "accepted";
  if (round.status === "rejected") return "rejected";
  if (round.status === "failed" || round.push.status === "failed") {
    return "failed";
  }
  if (
    ["failed", "timeout"].includes(round.eval.status) ||
    props.blockedStage === "patch"
  ) {
    return "blocked";
  }
  if (!isTerminal(round)) return "running";
  return "pending";
}

function candidateText(round: StepRound): string {
  if (round.candidate) {
    return `${round.candidate.ref} · ${round.candidate.commit.slice(0, 12)}`;
  }
  if (round.status === "patching") {
    return "正在生成并应用 patch，候选 ref 尚未创建";
  }
  if (round.status === "failed") return "本轮失败，未生成 candidate";
  return "尚未生成 candidate";
}

function candidateLabel(round: StepRound): string {
  if (round.status === "patching") return "candidate 生成中";
  if (round.push.status === "succeeded") return "candidate 已 push · ref / commit";
  if (round.push.status === "failed") return "candidate push 失败 · ref / commit";
  if (round.push.status === "skipped") return "candidate 已生成 · 未推送 · ref / commit";
  return "candidate ref / commit";
}

function pushLabel(status: string): string {
  return (
    {
      pending: "待推送",
      pushing: "推送中",
      succeeded: "推送成功",
      failed: "推送失败",
      skipped: "已跳过（本地模式）",
    }[status] ?? status
  );
}
</script>

<template>
  <article class="card panel">
    <h3 class="sec-title">
      第 5 步 · 迭代优化
      <span class="sub">按严重程度降序逐轮执行 · patch → push → 评测 → 接纳 / 拒绝</span>
    </h3>
    <section
      v-for="(round, index) in rounds"
      :id="`cluster-${round.clusterId}`"
      :key="round.roundId"
      class="cluster"
      :class="{ open: expanded.has(round.roundId) }"
      :data-round-id="round.roundId"
    >
      <button
        class="cluster-head"
        type="button"
        :aria-expanded="expanded.has(round.roundId)"
        @click="toggle(round.roundId)"
      >
        <span class="rank">#{{ index + 1 }}</span>
        <span class="cid">{{ round.clusterId }}</span>
        <span class="ctitle">{{ round.title }}</span>
        <span
          class="type-chip"
          :class="`type-${round.type}`"
        >{{ typeLabel(round.type) }}</span>
        <span class="meta">probe ×{{ round.probeCount }}</span>
        <span
          v-if="round.severity !== null"
          class="meta"
        >严重程度 {{ round.severity.toFixed(1) }}</span>
        <span class="tail">
          <span
            class="badge"
            :class="statusClass(round)"
          >{{ statusLabel(round) }}</span>
          <span class="caret">▶</span>
        </span>
      </button>

      <div
        v-if="expanded.has(round.roundId)"
        class="cluster-body"
      >
        <section class="cb-sec">
          <p class="cb-label">
            来源问题报告（{{ round.probes.length }} 份 · 由归因结论聚类而来）
          </p>
          <ProbeSourceTable :rows="round.probes" />
        </section>

        <section
          v-if="round.patch"
          class="cb-sec"
        >
          <p class="cb-label">
            生成的 patch（current → candidate）
          </p>
          <DiffView :patch="round.patch" />
        </section>

        <section class="cb-sec">
          <p class="cb-label">
            {{ candidateLabel(round) }}
          </p>
          <div
            class="candidate"
            :class="{ empty: !round.candidate }"
          >
            {{ candidateText(round) }}
          </div>
        </section>

        <section class="cb-sec">
          <p class="cb-label">
            推送状态
          </p>
          <p class="phase-line">
            <b>{{ pushLabel(round.push.status) }}</b>
            · 尝试 {{ round.push.attempts }} 次
            <span
              v-if="round.push.error"
              class="error-copy"
            > · {{ round.push.error }}</span>
          </p>
        </section>

        <section class="cb-sec">
          <p class="cb-label">
            评测对比
          </p>
          <EvalCompare
            :evaluation="round.eval"
            :accepted="round.status === 'accepted'"
            :round-status="round.status"
          />
          <p
            v-if="round.eval.nextPollAt"
            class="muted"
          >
            下一次轮询：{{ round.eval.nextPollAt }}
          </p>
        </section>

        <section
          v-if="round.status === 'accepted' && round.finalCommit"
          class="cb-sec"
        >
          <p class="cb-label">
            已接纳 · amend 后的 run 分支 commit
          </p>
          <div
            v-if="round.candidate"
            class="evaluated-commit"
            :title="round.candidate.commit"
          >
            <span class="commit-label">Harness-Evaluated-Commit</span>
            <span class="hash">{{ round.candidate.commit.slice(0, 12) }}</span>
          </div>
          <div class="commit">
            <span class="hash">{{ round.finalCommit.hash.slice(0, 12) }}</span>
            <span>{{ round.finalCommit.message }}</span>
          </div>
        </section>

        <section
          v-else-if="round.status === 'rejected'"
          class="cb-sec"
        >
          <p class="reject-note">
            未接纳：candidate 已删除，run 分支保持不变。
          </p>
        </section>
      </div>
    </section>

    <p
      v-if="rounds.length === 0"
      class="waiting"
    >
      当前还没有进入迭代轮次。
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

.cluster {
  overflow: hidden;
  margin-bottom: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-card);
}

.cluster-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 11px 14px;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-align: left;
  flex-wrap: wrap;
  cursor: pointer;
}

.cluster-head:hover {
  background: var(--bg-panel);
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

.cid,
.meta {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11px;
}

.ctitle {
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}

.type-chip,
.badge {
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

.badge.accepted {
  border-color: color-mix(in srgb, var(--green) 32%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
  color: var(--green);
}

.badge.rejected {
  background: color-mix(in srgb, var(--red) 11%, transparent);
  color: var(--red);
}

.badge.running {
  background: color-mix(in srgb, var(--blue) 12%, transparent);
  color: var(--blue);
}

.badge.blocked {
  background: color-mix(in srgb, var(--orange) 12%, transparent);
  color: var(--orange);
}

.badge.failed {
  background: color-mix(in srgb, var(--red) 11%, transparent);
  color: var(--red);
}

.badge.pending {
  color: var(--text-faint);
}

.tail {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
}

.caret {
  color: var(--text-faint);
  font-size: 11px;
  transition: transform 0.15s;
}

.cluster.open .caret {
  transform: rotate(90deg);
}

.cluster-body {
  padding: 14px 16px;
  border-top: 1px solid var(--border);
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

.report-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.report-chip {
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg-panel);
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 10.5px;
  text-decoration: none;
}

.candidate,
.commit {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-panel);
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11.5px;
  word-break: break-all;
}

.candidate.empty {
  border-style: dashed;
  color: var(--text-faint);
}

.phase-line {
  margin: 0;
  color: var(--text-dim);
  font-size: 12px;
}

.phase-line b {
  color: var(--text);
  font-family: var(--mono);
}

.error-copy {
  color: var(--red);
}

.commit {
  display: grid;
  gap: 6px;
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 6%, transparent);
}

.evaluated-commit {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 6px;
  padding: 6px 9px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: var(--bg-panel);
  font-family: var(--mono);
  font-size: 11.5px;
}

.commit-label {
  color: var(--text-faint);
}

.hash {
  color: var(--green);
  font-weight: 700;
}

.reject-note {
  margin: 0;
  padding: 9px 12px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  color: var(--text-dim);
  font-size: 12px;
}

.muted {
  color: var(--text-faint);
  font-size: 11.5px;
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
