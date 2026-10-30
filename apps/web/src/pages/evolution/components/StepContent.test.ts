// @vitest-environment jsdom

import type {
  EvolutionStepAttribution,
  EvolutionStepClusters,
  EvolutionStepFinal,
  EvolutionStepInput,
  EvolutionStepSeverity,
} from "@harness/contracts";
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import StepAttribution from "./StepAttribution.vue";
import StepClusters from "./StepClusters.vue";
import StepFinal from "./StepFinal.vue";
import StepInput from "./StepInput.vue";
import StepSeverity from "./StepSeverity.vue";

const routerLink = {
  props: ["to"],
  template: "<a><slot /></a>",
};

const inputStep = {
  key: "input",
  index: 0,
  title: "筛选输入",
  status: "done",
  clickable: true,
  payload: {
    condition: {
      targetType: "skill",
      targetName: "deep-research",
      targetVersion: "v2.0.0",
      from: "2026-08-20",
      to: "2026-09-10",
      models: ["glm-5.3"],
    },
    hits: {
      sessions: 2,
      badCases: 2,
      analyzed: 2,
      unanalyzed: 0,
      probes: 2,
      usedProbes: 1,
      byAttributeType: { model: 0, prompt: 0, error: 1, nonIssue: 1 },
    },
    rows: [
      {
        probeId: "probe-1",
        sessionId: "session-1",
        sessionTitle: "研究",
        model: "glm-5.3",
        probeType: "skill",
        probeObject: "deep-research",
        objectVersion: "v2.0.0",
        attributeType: "error",
        description: "读取越界",
        sourceBadCaseIds: ["case-1"],
        clusterId: "C1",
      },
      {
        probeId: "probe-1",
        sessionId: "session-1",
        sessionTitle: "研究",
        model: "glm-5.3",
        probeType: "skill",
        probeObject: "deep-research",
        objectVersion: "v2.0.0",
        attributeType: "error",
        description: "读取越界",
        sourceBadCaseIds: ["case-1"],
        clusterId: "C1",
      },
    ],
  },
} satisfies EvolutionStepInput;

const clustersStep = {
  key: "clusters",
  index: 1,
  title: "问题聚类",
  status: "done",
  clickable: true,
  payload: {
    clusters: [
      {
        probeClusterId: "C1",
        title: "检索索引读取失败",
        description: "读取越界",
        type: "error",
        probeCount: 1,
        confidence: 0.82,
        severity: 7.4,
        probes: inputStep.payload.rows.slice(0, 1),
      },
    ],
  },
} satisfies EvolutionStepClusters;

const attributionStep = {
  key: "attribution",
  index: 2,
  title: "LLM 归因置信度",
  status: "done",
  clickable: true,
  payload: {
    items: [
      {
        clusterId: "C1",
        probeId: "probe-1",
        probeObject: "deep-research",
        objectVersion: "v2.0.0",
        attributeType: "error",
        description: "读取越界",
        analysis: "快照读取前缺少边界校验。",
        confidence: 0.82,
      },
    ],
  },
} satisfies EvolutionStepAttribution;

const severityStep = {
  key: "severity",
  index: 3,
  title: "严重程度排序",
  status: "done",
  clickable: true,
  payload: {
    rows: [
      {
        rank: 1,
        clusterId: "C1",
        title: "检索索引读取失败",
        type: "error",
        probeCount: 1,
        reportCount: 1,
        confidence: 0.82,
        impact: 9,
        severity: 7.4,
      },
    ],
  },
} satisfies EvolutionStepSeverity;

const finalStep = {
  key: "final",
  index: 5,
  title: "测试集终评",
  status: "done",
  clickable: true,
  payload: {
    verdict: true,
    bestVersion: "v2.0.1-best",
    bestScore: 88.9,
    initScore: 84.3,
    metrics: [
      { name: "任务完成率", init: 78, best: 84, delta: 6, unit: "%" },
    ],
    changes: [
      {
        commit: "cd34ab12ef56",
        clusterId: "C1",
        title: "检索索引读取失败",
        type: "error",
        reportCount: 1,
      },
    ],
  },
} satisfies EvolutionStepFinal;

const run = {
  runId: "run-1",
  title: "run",
  mode: "full" as const,
  target: { type: "skill" as const, name: "deep-research", version: "v2.0.0" },
  window: { from: "2026-08-20", to: "2026-09-10" },
  models: ["glm-5.3"],
  createdBy: "Ada",
  createdAt: "2026-10-19T18:42:00+08:00",
  status: "running" as const,
  error: null,
  stats: {
    sessions: 2,
    probes: 2,
    usedProbes: 1,
    clusters: 1,
    byAttributeType: { model: 0, prompt: 0, error: 1, nonIssue: 1 },
  },
  progress: {
    unit: 4,
    total: 5,
    percent: 80,
    stageLabel: "第 5/6 步 · 迭代优化 · 第 1/1 轮",
    doneRounds: 0,
    accepted: 0,
  },
  final: null,
};

describe("evolution step content", () => {
  it("deduplicates repeated probe input while preserving source rows", () => {
    const wrapper = mount(StepInput, {
      props: {
        step: inputStep,
        condition: inputStep.payload.condition,
      },
      global: {
        stubs: { RouterLink: routerLink },
      },
    });

    expect(wrapper.findAll("tbody tr")).toHaveLength(1);
    expect(wrapper.text()).toContain("检测到 1 条重复 probe 输入");
    expect(wrapper.text()).toContain("probe-1");
    expect(wrapper.text()).toContain("case-1");
    expect(wrapper.text()).toContain("最终 1 份");
    expect(wrapper.findAll("thead th").map((heading) => heading.text())).toEqual([
      "报告 ID",
      "来源问题 ID",
      "问题现象",
      "模型",
      "归因类型",
      "归属簇",
    ]);
  });

  it("expands a cluster to reveal its source probes", async () => {
    const wrapper = mount(StepClusters, {
      props: { step: clustersStep },
      global: {
        stubs: { RouterLink: routerLink },
      },
    });

    expect(wrapper.text()).toContain("M=1 类");
    expect(wrapper.text()).not.toContain("probe-1");
    await wrapper.get(".cluster-head").trigger("click");
    expect(wrapper.get(".cluster-head").attributes("aria-expanded")).toBe("true");
    expect(wrapper.text()).toContain("probe-1");
  });

  it("highlights an addressed cluster and reports a missing target", async () => {
    const highlighted = mount(StepClusters, {
      props: { step: clustersStep, focusCluster: "C1" },
      global: {
        stubs: { RouterLink: routerLink },
      },
    });
    const missing = mount(StepClusters, {
      props: { step: clustersStep, focusCluster: "missing" },
      global: {
        stubs: { RouterLink: routerLink },
      },
    });
    await highlighted.vm.$nextTick();

    expect(highlighted.get("#cluster-C1").classes()).toContain("highlighted");
    expect(missing.text()).toContain("目标簇已不存在");
  });

  it("renders attribution evidence and confidence", () => {
    const wrapper = mount(StepAttribution, {
      props: { step: attributionStep, run },
    });

    expect(wrapper.text()).toContain("快照读取前缺少边界校验");
    expect(wrapper.text()).toContain("置信度 82%");
    expect(wrapper.text()).toContain("问题真实存在");
  });

  it("renders the severity rank and weighted score", () => {
    const wrapper = mount(StepSeverity, {
      props: { step: severityStep },
    });

    expect(wrapper.text()).toContain("#1");
    expect(wrapper.text()).toContain("82%");
    expect(wrapper.text()).toContain("9/10");
    expect(wrapper.text()).toContain("7.4");
  });

  it("renders the final verdict against the original target version", () => {
    const wrapper = mount(StepFinal, {
      props: {
        step: finalStep,
        targetVersion: "v2.0.0",
      },
    });

    expect(wrapper.text()).toContain("✓ 自进化有效");
    expect(wrapper.text()).toContain("初始 v2.0.0");
    expect(wrapper.text()).toContain("任务完成率");
    expect(wrapper.text()).toContain("cd34ab12ef56");
    expect(wrapper.text()).toContain("错误");
  });

  it("renders independent final mean and paired cases without mixing iteration scores", () => {
    const wrapper = mount(StepFinal, {
      props: {
        targetVersion: "v2.0.0",
        step: {
          ...finalStep,
          payload: {
            ...finalStep.payload,
            metrics: [
              { round: 1, currentScore: 90, candidateScore: 99, delta: 9 },
              {
                phase: "independent_final", currentScore: 5, candidateScore: 7,
                metrics: [{
                  kind: "case_scores",
                  current: { score: 5, caseScores: { case1: 3, case2: 7 } },
                  candidate: { score: 7, caseScores: { case2: 7, case1: 7 } },
                }],
              },
            ],
            changes: [{ ...finalStep.payload.changes[0]!, reportCount: 2 }],
          },
        },
      },
    });
    expect(wrapper.findAll("tbody tr").map((row) => row.findAll("td").map((cell) => cell.text())))
      .toEqual([
        ["total_score 均值", "5", "7", "+2.0"],
        ["用例 case1", "3", "7", "+4.0"],
        ["用例 case2", "7", "7", "+0.0"],
      ]);
    expect(wrapper.text()).not.toContain("终评指标暂不可用");
    expect(wrapper.text()).toContain("来源 2 个问题");
    expect(wrapper.text()).not.toContain("来源 2 份报告");
  });

  it.each([
    { case2: 7 },
    { case1: "7" },
    { case1: Number.NaN },
    {},
  ])("does not fabricate comparisons for invalid or mismatched case evidence %j", (caseScores) => {
    const wrapper = mount(StepFinal, {
      props: {
        targetVersion: "v2.0.0",
        step: {
          ...finalStep,
          payload: {
            ...finalStep.payload,
            metrics: [{
              phase: "independent_final", currentScore: 5, candidateScore: 7,
              metrics: [{
                kind: "case_scores",
                current: { caseScores: { case1: 5 } }, candidate: { caseScores },
              }],
            }],
          },
        },
      },
    });
    expect(wrapper.find("table.metrics").exists()).toBe(false);
    expect(wrapper.text()).toContain("终评指标暂不可用");
  });
});
