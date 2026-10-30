// @vitest-environment jsdom

import type {
  EvolutionStepAttribution,
  EvolutionStepClusters,
  EvolutionStepFinal,
  EvolutionStepIteration,
  StepRound,
} from "@harness/contracts";
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import DiffView from "./DiffView.vue";
import EvalCompare from "./EvalCompare.vue";
import StepAttribution from "./StepAttribution.vue";
import StepClusters from "./StepClusters.vue";
import StepFinal from "./StepFinal.vue";
import StepIterations from "./StepIterations.vue";
import ProbeSourceTable from "./ProbeSourceTable.vue";

const RouterLinkStub = {
  props: ["to"],
  template: "<a :data-to='JSON.stringify(to)'><slot /></a>",
};

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

const probe = {
  probeId: "probe-1",
  sessionId: "session-1",
  sessionTitle: "研究运行",
  model: "glm-5.3",
  probeType: "skill",
  probeObject: "deep-research",
  objectVersion: "v2.0.0",
  attributeType: "error" as const,
  description: "读取越界",
  sourceBadCaseIds: ["case-1"],
};

const clusterStep = {
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
        probes: [probe],
      },
    ],
  },
} satisfies EvolutionStepClusters;

function round(overrides: Partial<StepRound> = {}): StepRound {
  return {
    roundId: "round-1",
    clusterId: "C1",
    title: "检索索引读取失败",
    type: "error",
    probeCount: 1,
    confidence: 0.82,
    severity: 7.4,
    probes: [probe],
    status: "pending",
    candidate: null,
    patch: null,
    push: { status: "pending", attempts: 0, error: null },
    eval: { status: "pending", current: 84.6, candidate: null, nextPollAt: null },
    accepted: false,
    finalCommit: null,
    ...overrides,
  };
}

function iterationStep(rounds: StepRound[]): EvolutionStepIteration {
  return {
    key: "iteration",
    index: 4,
    title: "迭代优化",
    status: "active",
    clickable: true,
    payload: { rounds },
  } as EvolutionStepIteration;
}

describe("run detail conformance", () => {
  it("opens source report and problem IDs in safe new-window deep links", () => {
    const fullProbeId = "11111111-1111-4111-8111-111111111111";
    const fullBadCaseId = "22222222-2222-4222-8222-222222222222";
    const wrapper = mount(ProbeSourceTable, {
      props: {
        rows: [
          {
            ...probe,
            probeId: fullProbeId,
            sourceBadCaseIds: [fullBadCaseId],
          },
        ],
        showCluster: false,
      },
    });

    const links = wrapper.findAll("a");
    expect(links).toHaveLength(2);
    expect(links.map((link) => link.text())).toEqual(["11111111", "22222222"]);

    for (const link of links) {
      expect(link.attributes("target")).toBe("_blank");
      expect(link.attributes("rel")).toBe("noopener noreferrer");
    }

    expect(links[0]?.attributes()).toMatchObject({
      href: `/sessions/session-1/errors?case=${fullBadCaseId}&tab=attr`,
      title: fullProbeId,
      "aria-label": "归因报告 11111111，在新窗口打开归因分析",
    });
    expect(links[1]?.attributes()).toMatchObject({
      href: `/sessions/session-1/errors?case=${fullBadCaseId}&tab=desc`,
      title: fullBadCaseId,
      "aria-label": "来源问题 22222222，在新窗口打开问题描述",
    });
  });

  it("renders the prototype source-problem table with report and issue deep links", async () => {
    const clusters = mount(StepClusters, {
      props: { step: clusterStep, focusCluster: "C1" },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });
    await clusters.vm.$nextTick();

    for (const heading of [
      "报告 ID",
      "来源问题 ID",
      "问题现象",
      "来源 session",
      "模型",
      "归因类型",
    ]) {
      expect(clusters.text()).toContain(heading);
    }
    expect(clusters.text()).toContain("研究运行");
    expect(clusters.text()).toContain("glm-5.3");
    expect(clusters.text()).toContain("错误");

    const links = clusters.findAll("a");
    expect(links.some((link) => link.attributes("href")?.includes("attr"))).toBe(
      true,
    );
    expect(links.some((link) => link.attributes("href")?.includes("desc"))).toBe(
      true,
    );

    const iterations = mount(StepIterations, {
      props: { step: iterationStep([round({ status: "evaluating" })]) },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });
    expect(iterations.text()).toContain("来源 session");
    expect(iterations.text()).toContain("probe-1");
    expect(iterations.text()).toContain("case-1");
  });

  it("expands a valid focused cluster and emits a cleanup request for a missing one", async () => {
    const present = mount(StepClusters, {
      props: { step: clusterStep, focusCluster: "C1" },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });
    await present.vm.$nextTick();
    expect(present.get(".cluster-head").attributes("aria-expanded")).toBe("true");

    const missing = mount(StepClusters, {
      props: { step: clusterStep, focusCluster: "missing" },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });
    await missing.vm.$nextTick();
    expect(missing.emitted("missing-focus")).toHaveLength(1);
  });

  it.each([
    ["pending", "待执行"],
    ["patching", "生成 patch 中"],
    ["pushing", "推送中"],
    ["evaluating", "评测中"],
    ["deciding", "决策中"],
    ["accepted", "已接纳"],
    ["rejected", "未接纳"],
    ["failed", "失败"],
  ])("maps round status %s to %s", (status, label) => {
    const wrapper = mount(StepIterations, {
      props: {
        step: iterationStep([
          round({
            status: status as StepRound["status"],
            candidate:
              status === "pending" || status === "patching"
                ? null
                : {
                    ref: "refs/candidate",
                    commit: "abc123",
                    message: "candidate",
                  },
            push:
              status === "pushing"
                ? { status: "pushing", attempts: 1, error: null }
                : { status: "succeeded", attempts: 1, error: null },
            eval:
              status === "evaluating" || status === "deciding"
                ? {
                    status: status === "evaluating" ? "evaluating" : "completed",
                    current: 84.6,
                    candidate: status === "deciding" ? 85.1 : null,
                    nextPollAt: null,
                  }
                : { status: "pending", current: null, candidate: null, nextPollAt: null },
          }),
        ]),
      },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });

    expect(wrapper.text()).toContain(label);
  });

  it.each([
    [
      "push",
      "pushing",
      { status: "failed" as const, attempts: 1, error: "远端拒绝" },
      { status: "pending" as const, current: 84.6, candidate: null, nextPollAt: null },
      "推送失败",
      "failed",
    ],
    [
      "eval",
      "evaluating",
      { status: "succeeded" as const, attempts: 1, error: null },
      { status: "failed" as const, current: 84.6, candidate: null, nextPollAt: null },
      "评测失败",
      "blocked",
    ],
    [
      "patch",
      "patching",
      { status: "pending" as const, attempts: 0, error: null },
      { status: "pending" as const, current: 84.6, candidate: null, nextPollAt: null },
      "patch 已阻塞",
      "blocked",
    ],
  ])(
    "uses run blocked stage %s before pending intermediate labels",
    (_stage, status, push, evaluation, label, className) => {
      const wrapper = mount(StepIterations, {
        props: {
          step: iterationStep([
            round({
              status: status as StepRound["status"],
              push,
              eval: evaluation,
            }),
          ]),
          blockedStage: _stage === "patch" ? _stage : null,
        },
        global: { stubs: { RouterLink: RouterLinkStub } },
      });

      const badge = wrapper.get(".badge");
      expect(badge.text()).toBe(label);
      expect(badge.classes()).toContain(className);
      expect(badge.text()).not.toBe("推送中");
      expect(badge.text()).not.toBe("评测中");
    },
  );

  it("explains that patching has not created a candidate ref yet", () => {
    const wrapper = mount(StepIterations, {
      props: { step: iterationStep([round({ status: "patching" })]) },
      global: { stubs: { RouterLink: RouterLinkStub } },
    });

    expect(wrapper.text()).toContain("正在生成并应用 patch");
    expect(wrapper.text()).not.toContain("尚未执行");
  });

  it("renders local-only candidates as skipped instead of remote push success", () => {
    const wrapper = mount(StepIterations, {
      props: {
        step: iterationStep([
          round({
            status: "skipped",
            candidate: {
              ref: "evo/run/candidate/r1",
              commit: "abcdef1234567890",
              message: "candidate",
            },
            push: { status: "skipped", attempts: 0, error: null },
            eval: {
              status: "pending",
              current: null,
              candidate: null,
              nextPollAt: null,
            },
          }),
        ]),
      },
    });

    expect(wrapper.text()).toContain("本地候选（未推送）");
    expect(wrapper.text()).toContain("已跳过（本地模式）");
    expect(wrapper.text()).toContain("candidate 已生成 · 未推送");
    expect(wrapper.text()).not.toContain("candidate 已 push");
    expect(wrapper.text()).not.toContain("推送成功");
  });

  it("renders an explicit empty state when no iteration round exists", () => {
    const wrapper = mount(StepIterations, {
      props: { step: iterationStep([]) },
    });

    expect(wrapper.text()).toContain("当前还没有进入迭代轮次");
  });

  it("uses acceptance rather than score direction for the evaluation outcome", () => {
    const rejected = mount(EvalCompare, {
      props: {
        evaluation: {
          status: "completed",
          current: 84.3,
          candidate: 88.9,
          nextPollAt: null,
        },
        accepted: false,
      },
    });
    expect(rejected.text()).toContain("未接纳");
    expect(rejected.text()).not.toContain("已提升为 current");

    const accepted = mount(EvalCompare, {
      props: {
        evaluation: {
          status: "completed",
          current: 84.3,
          candidate: 88.9,
          nextPollAt: null,
        },
        accepted: true,
      },
    });
    expect(accepted.text()).toContain("已提升为 current");

    const failed = mount(EvalCompare, {
      props: {
        evaluation: {
          status: "failed",
          current: 84.3,
          candidate: null,
          nextPollAt: null,
        },
        accepted: false,
      },
    });
    expect(failed.text()).toContain("评测失败");
    expect(failed.text()).not.toContain("无收益");

    const timeout = mount(EvalCompare, {
      props: {
        evaluation: {
          status: "timeout",
          current: 84.3,
          candidate: null,
          nextPollAt: null,
        },
        accepted: false,
      },
    });
    expect(timeout.text()).toContain("评测超时");
  });

  it("shows skipped evaluation as not run rather than pending", () => {
    const skipped = mount(EvalCompare, {
      props: {
        evaluation: { status: "skipped", current: null, candidate: null, nextPollAt: null },
        accepted: false,
        roundStatus: "rejected",
      },
    });
    expect(skipped.text()).toContain("未执行评测");
    expect(skipped.text()).not.toContain("等待评测启动");
    expect(skipped.text()).not.toContain("评测完成");
  });

  it("labels the evaluated candidate commit on an accepted round", () => {
    const evaluatedCommit = "abcdef1234567890";
    const finalCommit = "fedcba0987654321";
    const wrapper = mount(StepIterations, {
      props: {
        step: iterationStep([
          round({
            status: "accepted",
            candidate: {
              ref: "refs/candidate",
              commit: evaluatedCommit,
              message: "candidate",
            },
            push: { status: "succeeded", attempts: 1, error: null },
            eval: {
              status: "completed",
              current: 84.6,
              candidate: 85.1,
              nextPollAt: null,
            },
            accepted: true,
            finalCommit: {
              hash: finalCommit,
              message: "accepted candidate",
            },
          }),
        ]),
      },
    });

    expect(wrapper.text()).toContain("Harness-Evaluated-Commit");
    expect(wrapper.text()).toContain(evaluatedCommit.slice(0, 12));
    expect(wrapper.text()).toContain(finalCommit.slice(0, 12));
  });

  it("colors added, deleted, hunk and context diff lines", () => {
    const wrapper = mount(DiffView, {
      props: {
        patch: {
          file: "SKILL.md",
          lines: ["@@ -1 +1 @@", "-old", "+new", " context"],
        },
      },
    });

    const lines = wrapper.findAll(".dline");
    expect(lines[0]?.classes()).toContain("hunk");
    expect(lines[1]?.classes()).toContain("del");
    expect(lines[2]?.classes()).toContain("add");
    expect(lines[3]?.classes()).toContain("ctx");
  });

  it("renders attribution analysis as safe structural Markdown", () => {
    const attribution = {
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
            analysis: "**确认问题**\n\n- 索引未校验\n\n`read()`",
            confidence: 0.82,
          },
        ],
      },
    } satisfies EvolutionStepAttribution;
    const wrapper = mount(StepAttribution, {
      props: { step: attribution, run },
    });

    expect(wrapper.find(".markdown strong").text()).toBe("确认问题");
    expect(wrapper.find(".markdown li").text()).toBe("索引未校验");
    expect(wrapper.find(".markdown code").text()).toBe("read()");
  });

  it("shows the final push status and timestamp", () => {
    const final = {
      key: "final",
      index: 5,
      title: "测试集终评",
      status: "done",
      clickable: true,
      payload: {
        verdict: true,
        bestVersion: "v2.0.1",
        bestScore: 88.9,
        initScore: 84.3,
        metrics: [],
        changes: [],
      },
    } satisfies EvolutionStepFinal;
    const wrapper = mount(StepFinal, {
      props: {
        step: final,
        targetVersion: "v2.0.0",
        pushStatus: "succeeded",
        pushedAt: "2026-10-27T12:00:00+08:00",
      },
    });

    expect(wrapper.text()).toContain("最终 push 成功");
    expect(wrapper.text()).toContain("2026-10-27 12:00");
  });
});
