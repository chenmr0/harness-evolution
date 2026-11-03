// @vitest-environment jsdom

import type { EvolutionRunCard } from "@harness/contracts";
import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import RunCard from "./RunCard.vue";

const run = (overrides: Partial<EvolutionRunCard> = {}): EvolutionRunCard => ({
  runId: "run-1",
  title: "deep-research v2.0.0 自进化",
  mode: "full",
  target: {
    type: "skill",
    name: "deep-research",
    version: "v2.0.0",
  },
  window: {
    from: "2026-08-20",
    to: "2026-09-10",
  },
  models: ["glm-5.3", "gpt-5"],
  createdBy: "Ada",
  createdAt: "2026-10-19T18:42:00+08:00",
  status: "running",
  error: null,
  stats: {
    sessions: 128,
    probes: 46,
    usedProbes: 28,
    clusters: 2,
    byAttributeType: {
      model: 1,
      prompt: 1,
      error: 0,
      nonIssue: 18,
    },
  },
  progress: {
    unit: 4,
    total: 7,
    percent: 57,
    stageLabel: "第 5/6 步 · 迭代优化 · 第 1/2 轮",
    doneRounds: 0,
    accepted: 1,
  },
  final: null,
  ...overrides,
});

describe("RunCard", () => {
  it("renders the preparing status explicitly", () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "preparing",
          progress: {
            unit: 0,
            total: 7,
            percent: 0,
            stageLabel: "第 1/6 步 · 筛选输入中…",
            doneRounds: 0,
            accepted: 0,
          },
        }),
      },
    });

    expect(wrapper.get(".badge").text()).toBe("准备中");
    expect(wrapper.get(".badge").classes()).toContain("running");
  });

  it("renders prototype metadata and uses backend progress verbatim", () => {
    const wrapper = mount(RunCard, {
      props: { run: run() },
    });

    expect(wrapper.get(".run-card").classes()).toContain("run-card");
    expect(wrapper.get(".title").text()).toBe("deep-research v2.0.0 自进化");
    expect(wrapper.get(".time").text()).toBe("Ada · 2026-10-19 18:42");
    expect(wrapper.get(".chip.kind").text()).toBe(
      "skill:deep-research@v2.0.0",
    );
    expect(wrapper.text()).toContain("2026-08-20 ~ 2026-09-10");
    expect(wrapper.findAll(".chip.model").map((chip) => chip.text())).toEqual([
      "glm-5.3",
      "gpt-5",
    ]);
    expect(wrapper.text()).toContain("128session");
    expect(wrapper.text()).toContain("46问题报告");
    expect(wrapper.text()).toContain("28用于进化");
    expect(wrapper.text()).toContain("2聚类");
    expect(wrapper.text()).toContain("0/2已跑轮次");
    expect(wrapper.text()).toContain("1已接纳");
    expect(wrapper.text()).toContain(
      "第 5/6 步 · 迭代优化 · 第 1/2 轮 · 57%",
    );
    expect(wrapper.get(".run-progress .fill").attributes("style")).toContain(
      "width: 57%",
    );
    expect(wrapper.get("[role='progressbar']").attributes()).toMatchObject({
      "aria-label": "第 5/6 步 · 迭代优化 · 第 1/2 轮",
      "aria-valuemax": "100",
      "aria-valuemin": "0",
      "aria-valuenow": "57",
    });
    expect(wrapper.get(".badge").classes()).toContain("running");
    expect(wrapper.get(".badge").text()).toContain("正在执行");
  });

  it("renders the effective history summary from backend values", () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "completed",
          progress: {
            unit: 7,
            total: 7,
            percent: 100,
            stageLabel: "第 6/6 步 · 测试集终评中…",
            doneRounds: 2,
            accepted: 1,
          },
          final: {
            bestVersion: "v2.0.1-best",
            branchName: "evo/run-1",
            pushStatus: "succeeded",
            verdict: true,
            bestScore: 88.9,
            initScore: 84.3,
          },
        }),
      },
    });

    expect(wrapper.get(".badge").text()).toBe("✓ 已完成 · 自进化有效");
    expect(wrapper.get(".final-line").text()).toContain(
      "best 88.90 vs 初始 84.30",
    );
    expect(wrapper.get(".final-line").text()).toContain("+4.60");
    expect(wrapper.get(".final-line").text()).toContain("接纳 1/2 个 patch");
    expect(wrapper.find("[role='progressbar']").exists()).toBe(false);
  });

  it("marks completed runs without improvement without claiming success", () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "completed",
          final: {
            bestVersion: "v2.0.0",
            branchName: "evo/run-1",
            pushStatus: "succeeded",
            verdict: false,
            bestScore: 80.1,
            initScore: 80.1,
          },
        }),
      },
    });

    expect(wrapper.get(".badge").text()).toBe("已完成 · 无收益");
    expect(wrapper.get(".final-line").text()).toContain("+0.0");
  });

  it("keeps a completed run with no final neutral instead of claiming no benefit", () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "completed",
          final: null,
        }),
      },
    });

    expect(wrapper.get(".badge").text()).toBe("已完成 · 终评未知");
    expect(wrapper.get(".badge").classes()).toContain("done-pending");
    expect(wrapper.text()).not.toContain("无收益");
    expect(wrapper.text()).toContain("终评数据暂不可用");
  });

  it("makes failed and interrupted error details available through a focusable tooltip", async () => {
    const failed = mount(RunCard, {
      props: {
        run: run({
          status: "failed",
          error: {
            stage: "eval",
            message: "评测执行失败：sandbox timeout",
            at: "2026-10-19T19:00:00+08:00",
          },
        }),
      },
    });
    const interrupted = mount(RunCard, {
      props: {
        run: run({
          status: "interrupted",
          error: {
            stage: "push",
            message: "推送连接中断",
            at: "2026-10-19T19:00:00+08:00",
          },
        }),
      },
    });

    expect(failed.get(".badge").classes()).toContain("failed");
    expect(failed.get(".badge").element.tagName).toBe("BUTTON");
    expect(failed.get(".badge").attributes("title")).toBe(
      "评测执行失败：sandbox timeout",
    );
    const failedTooltipId = failed.get(".badge").attributes("aria-describedby");
    expect(failedTooltipId).toMatch(/^run-error-/);
    expect(failed.get(".badge-tooltip").attributes("id")).toBe(failedTooltipId);
    expect(failed.get(".badge-tooltip").attributes("role")).toBe("tooltip");
    expect(failed.get(".badge-tooltip").classes()).not.toContain("is-open");
    await failed.get(".badge").trigger("focus");
    expect(failed.get(".badge-tooltip").classes()).toContain("is-open");
    expect(failed.get(".badge-tooltip").text()).toBe(
      "评测执行失败：sandbox timeout",
    );
    await failed.get(".badge").trigger("blur");
    expect(failed.get(".badge-tooltip").classes()).not.toContain("is-open");
    expect(failed.find(".final-line").exists()).toBe(false);
    expect(interrupted.get(".badge").classes()).toContain("interrupted");
    expect(interrupted.get(".badge").attributes("title")).toBe(
      "推送连接中断",
    );
    expect(interrupted.find(".final-line").exists()).toBe(false);
    expect(failed.find(".run-error").exists()).toBe(false);
    expect(interrupted.find(".run-error").exists()).toBe(false);
    expect(failed.text()).not.toContain("eval");
    expect(interrupted.text()).not.toContain("push");
  });

  it("marks blocked active runs with a focusable warning tooltip", async () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "blocked",
          error: {
            stage: "eval",
            message: "评测队列暂停",
            at: "2026-10-19T19:00:00+08:00",
          },
        }),
      },
    });

    expect(wrapper.get(".badge").classes()).toContain("blocked");
    expect(wrapper.text()).toContain("已阻塞");
    expect(wrapper.find("[role='progressbar']").exists()).toBe(true);
    expect(wrapper.get(".badge").attributes("title")).toBe("评测队列暂停");
    await wrapper.get(".badge").trigger("click");
    expect(wrapper.get(".badge-tooltip").classes()).toContain("is-open");
    expect(wrapper.get(".badge-tooltip").text()).toBe("评测队列暂停");
    expect(wrapper.find(".run-error").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("eval");
  });

  it("keeps status badges non-interactive when no error is present", () => {
    const wrapper = mount(RunCard, {
      props: { run: run() },
    });

    expect(wrapper.get(".badge").element.tagName).toBe("SPAN");
    expect(wrapper.find(".badge-tooltip").exists()).toBe(false);
  });

  it("opens the run detail with pointer and keyboard semantics", async () => {
    const wrapper = mount(RunCard, {
      props: { run: run() },
    });

    expect(wrapper.get(".run-card-link").attributes("href")).toBe(
      "/evolution/runs/run-1",
    );
    expect(wrapper.get(".run-card-link").attributes("aria-label")).toBe(
      "查看运行 deep-research v2.0.0 自进化",
    );
    await wrapper.get(".run-card-link").trigger("click");
    expect(wrapper.emitted("open")).toEqual([["/evolution/runs/run-1"]]);
  });

  it("routes cluster-only cards to the dedicated preview page", () => {
    const wrapper = mount(RunCard, {
      props: { run: run({ mode: "cluster_only" }) },
    });

    expect(wrapper.get(".run-card-link").attributes("href")).toBe(
      "/evolution/cluster/run-1",
    );
  });

  it("keeps the error tooltip button from opening the detail", async () => {
    const wrapper = mount(RunCard, {
      props: {
        run: run({
          status: "blocked",
          error: {
            stage: "eval",
            message: "评测队列暂停",
            at: "2026-10-19T19:00:00+08:00",
          },
        }),
      },
    });

    await wrapper.get(".error-trigger").trigger("click");
    expect(wrapper.emitted("open")).toBeUndefined();
  });
});
