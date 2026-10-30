// @vitest-environment jsdom

import type { ProbePreviewItem, ProbeQueryResult } from "@harness/contracts";
import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";
import { http, HttpResponse } from "msw";

import { server } from "../../../test/server";
import LaunchWizard from "./LaunchWizard.vue";

const wrappers: VueWrapper[] = [];

const probeItem: ProbePreviewItem = {
  probeId: "probe-synthetic-0001",
  sessionId: "session-1",
  sessionTitle: "研究 session",
  model: "glm-5.3",
  probeType: "skill",
  probeObject: "deep-research",
  objectVersion: "v2.0.0",
  attributeType: "error",
  description: "检索索引读取失败",
  sourceBadCaseIds: ["case-1"],
};

const preview: ProbeQueryResult = {
  summary: {
    sessions: 2,
    badCases: 3,
    analyzed: 2,
    unanalyzed: 0,
    probes: 2,
    usedProbes: 2,
    byAttributeType: { model: 0, prompt: 1, error: 1, nonIssue: 0 },
  },
  items: [probeItem],
  total: 1,
  page: 1,
  pageSize: 8,
};

async function mountWizard() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
  });
  await router.push("/");
  await router.isReady();
  const wrapper = mount(LaunchWizard, {
    global: {
      plugins: [
        [
          VueQueryPlugin,
          {
            queryClient: new QueryClient({
              defaultOptions: { queries: { gcTime: 0, retry: false } },
            }),
          },
        ],
        pinia,
        router,
      ],
    },
  });
  wrappers.push(wrapper);
  await flushPromises();
  return wrapper;
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount();
  server.resetHandlers();
});
afterAll(() => server.close());

function useCatalogHandlers(): void {
  server.use(
    http.get("/api/catalogs/targets", () =>
      HttpResponse.json({
        skill: [
          {
            id: "tgt-1",
            name: "deep-research",
            repoUrl: "https://git.example.com/skills.git",
            subDir: "skills/deep-research",
            defaultRef: "v2.0.0",
            versions: ["v2.0.0", "v1.9.0"],
          },
        ],
        agent: [],
      })),
    http.get("/api/catalogs/models", () =>
      HttpResponse.json({
        items: [{ model: "glm-5.3", sessionCount: 12, agentCount: 20 }],
      })),
    http.get("/api/evolution/targets/tgt-1/versions", () =>
      HttpResponse.json({ versions: ["v2.0.0", "v1.9.0"], defaultRef: "v2.0.0" })),
  );
}

describe("launch wizard", { timeout: 30000 }, () => {
  it("distinguishes reports from their many source bad cases", async () => {
    useCatalogHandlers();
    server.use(http.post("/api/evolution/probes/query", () => HttpResponse.json({
      ...preview,
      summary: { ...preview.summary, probes: 5, badCases: 7, analyzed: 7, unanalyzed: 0 },
    })));
    const wrapper = await mountWizard();
    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-10-24");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-10-25");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();
    const counters = wrapper.findAll(".wz-stats .st").map((item) => item.text());
    expect(counters).toContain("5问题报告");
    expect(counters).toContain("7来源问题");
    expect(counters).toContain("7已分析来源问题");
    expect(counters).not.toContain("7问题报告");
  });

  it("keeps unconfigured objects out of the candidate selector", async () => {
    useCatalogHandlers();
    const wrapper = await mountWizard();

    const options = wrapper
      .get("[data-testid='wizard-target']")
      .findAll("option")
      .map((option) => option.text());
    expect(options).toEqual(["选择对象", "deep-research"]);
    expect(
      wrapper.get("[data-testid='wizard-next']").attributes("disabled"),
    ).toBeUndefined();
  });

  it("links the hint line to the target config page without a gear button", async () => {
    useCatalogHandlers();
    const wrapper = await mountWizard();

    expect(wrapper.find(".gear").exists()).toBe(false);
    const configLink = wrapper.get("[data-testid='wizard-target-config-link']");
    expect(configLink.text()).toBe("配置页");
    const note = wrapper.get(".f-note").text();
    expect(note).toContain("未配置的进化对象不会出现在列表中");
    expect(note).toContain("前往");

    await configLink.trigger("click");
    await flushPromises();
    expect(wrapper.emitted("close")).toHaveLength(1);
    const router = wrapper.vm.$.appContext.config.globalProperties.$router;
    expect(router.currentRoute.value.path).toBe("/evolution/targets");
  });

  it("queries hits on the next step and shows the prototype statistics frame", async () => {
    useCatalogHandlers();
    server.use(
      http.post("/api/evolution/probes/query", () => HttpResponse.json(preview)),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-version']").setValue("v2.0.0");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    expect(wrapper.get(".wz-condstrip").text()).toContain("skill:deep-research@v2.0.0");
    expect(wrapper.get(".wz-stats").text()).toContain("「是问题」进入聚类");
    expect(wrapper.get(".wz-stats").text()).toContain("0未分析");
    expect(wrapper.get("table.rep").text()).toContain("prob…0001");

    const legend = wrapper.get(".wz-legend").text();
    expect(legend).toContain("是问题-错误");
    expect(legend).toContain("是问题-prompt缺失某些提示词");
    expect(legend).not.toContain("非问题");
    expect(legend).not.toContain("是问题-模型问题");
    expect(wrapper.find(".wz-waiting-note").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("等待外部归因");
  });

  it("renders only nonzero attribution types in the distribution and legend", async () => {
    useCatalogHandlers();
    const errorOnly: ProbeQueryResult = {
      ...preview,
      summary: {
        ...preview.summary,
        byAttributeType: { model: 0, prompt: 0, error: 1, nonIssue: 0 },
      },
    };
    server.use(
      http.post("/api/evolution/probes/query", () => HttpResponse.json(errorOnly)),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-version']").setValue("v2.0.0");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    const legend = wrapper.get(".wz-legend").text();
    expect(legend).toContain("是问题-错误");
    expect(legend).not.toContain("非问题");
    expect(legend).not.toContain("模型问题");
    expect(legend).not.toContain("prompt");
    expect(wrapper.get(".wz-typebar").findAll("span")).toHaveLength(1);
    expect(wrapper.get(".wz-stats").text()).toContain("0未分析");
  });

  it("exposes dialog semantics and closes on Escape", async () => {
    useCatalogHandlers();
    const wrapper = await mountWizard();

    const dialog = wrapper.get("[data-testid='launch-wizard']");
    expect(dialog.attributes("role")).toBe("dialog");
    expect(dialog.attributes("aria-modal")).toBe("true");
    const labelledBy = dialog.attributes("aria-labelledby") ?? "";
    expect(labelledBy).not.toBe("");
    expect(wrapper.get(`#${labelledBy}`).text()).toBe("启动自进化");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(wrapper.emitted("close")).toHaveLength(1);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    expect(wrapper.emitted("close")).toHaveLength(1);
  });

  it("selects the target defaultRef version as soon as versions load", async () => {
    useCatalogHandlers();
    server.use(
      http.get("/api/catalogs/targets", () =>
        HttpResponse.json({
          skill: [
            {
              id: "tgt-1",
              name: "deep-research",
              repoUrl: "https://git.example.com/skills.git",
              subDir: "skills/deep-research",
              defaultRef: "v1.9.0",
              versions: ["v1.9.0"],
            },
          ],
          agent: [],
        })),
      http.get("/api/evolution/targets/tgt-1/versions", () =>
        HttpResponse.json({
          versions: ["v1.9.0", "v2.0.0"],
          defaultRef: "v2.0.0",
        })),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();

    expect(wrapper.get<HTMLSelectElement>("[data-testid='wizard-version']").element.value).toBe(
      "v2.0.0",
    );
    expect(
      wrapper.get("[data-testid='wizard-next']").attributes("disabled"),
    ).toBeUndefined();
  });

  it("shows a retryable error when the version list fails and recovers after retry", async () => {
    useCatalogHandlers();
    let attempts = 0;
    server.use(
      http.get("/api/catalogs/targets", () =>
        HttpResponse.json({
          skill: [
            {
              id: "tgt-1",
              name: "deep-research",
              repoUrl: "https://git.example.com/skills.git",
              subDir: "skills/deep-research",
              defaultRef: "v2.0.0",
              versions: [],
            },
          ],
          agent: [],
        })),
      http.get("/api/evolution/targets/tgt-1/versions", () => {
        attempts += 1;
        return attempts === 1
          ? HttpResponse.json(
              {
                error: {
                  code: "REPO_INVALID",
                  message: "repository is not available",
                  details: {},
                },
              },
              { status: 422 },
            )
          : HttpResponse.json({
              versions: ["v2.0.0"],
              defaultRef: "v2.0.0",
            });
      }),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();

    expect(wrapper.get("[data-testid='wizard-version-load-error']").text()).toContain(
      "版本列表读取失败",
    );
    expect(wrapper.get("[data-testid='wizard-version-load-error']").text()).toContain(
      "repository is not available",
    );
    expect(
      wrapper.get("[data-testid='wizard-next']").attributes("disabled"),
    ).toBeUndefined();

    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();
    expect(wrapper.findAll("[data-testid='wizard-version-error']")).toHaveLength(1);
    expect(wrapper.get("[data-testid='wizard-version-error']").text()).toBe("请选择版本");
    expect(wrapper.get("[data-testid='wizard-version-load-error']").text()).toContain(
      "版本列表读取失败",
    );

    await wrapper.get("[data-testid='wizard-version-retry']").trigger("click");
    await flushPromises();

    expect(wrapper.find("[data-testid='wizard-version-error']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='wizard-version-load-error']").exists()).toBe(false);
    expect(wrapper.get<HTMLSelectElement>("[data-testid='wizard-version']").element.value).toBe(
      "v2.0.0",
    );
    expect(
      wrapper.get("[data-testid='wizard-next']").attributes("disabled"),
    ).toBeUndefined();
  });

  it("keeps different source problems distinguishable in the probe table", async () => {
    useCatalogHandlers();
    const twoSourcePreview: ProbeQueryResult = {
      ...preview,
      total: 2,
      items: [
        {
          ...probeItem,
          sourceBadCaseIds: ["badcase-synthetic-0001"],
        },
        {
          ...probeItem,
          probeId: "probe-synthetic-0002",
          sourceBadCaseIds: ["badcase-synthetic-0002"],
        },
      ],
    };
    server.use(
      http.post("/api/evolution/probes/query", () => HttpResponse.json(twoSourcePreview)),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    const sourceCells = wrapper
      .findAll("table.rep tbody tr")
      .map((row) => row.findAll("td")[5]?.text() ?? "");
    expect(sourceCells).toEqual(["badc…0001", "badc…0002"]);
  });

  it("always offers next and shows field errors directly under empty controls", async () => {
    useCatalogHandlers();
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/evolution/probes/query", async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json(preview);
      }),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    const next = wrapper.get("[data-testid='wizard-next']");
    expect(next.attributes("disabled")).toBeUndefined();
    for (const testId of ["wizard-target", "wizard-version", "wizard-from", "wizard-to"]) {
      const control = wrapper.get<HTMLSelectElement | HTMLInputElement>(
        `[data-testid='${testId}']`,
      );
      expect(control.classes()).toContain("input-error");
      expect(control.attributes("aria-invalid")).toBe("true");
      const error = control.element.nextElementSibling;
      expect(error?.classList.contains("field-error")).toBe(true);
    }
    expect(wrapper.get("[data-testid='wizard-target-error']").text()).toBe(
      "请选择进化对象",
    );
    expect(wrapper.get("[data-testid='wizard-version-error']").text()).toBe(
      "请选择版本",
    );
    expect(wrapper.get("[data-testid='wizard-from-error']").text()).toBe(
      "请选择开始时间",
    );
    expect(wrapper.get("[data-testid='wizard-to-error']").text()).toBe(
      "请选择结束时间",
    );
    expect(bodies).toEqual([]);
    expect(wrapper.find(".wz-stats").exists()).toBe(false);

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    expect(wrapper.find("[data-testid='wizard-target-error']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='wizard-version-error']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='wizard-from-error']").exists()).toBe(true);

    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    expect(wrapper.find("[data-testid='wizard-from-error']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='wizard-to-error']").exists()).toBe(true);
    expect(bodies).toEqual([]);

    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    expect(bodies).toEqual([]);
    expect(wrapper.find("[data-testid='wizard-target-error']").exists()).toBe(false);
    expect(wrapper.find("[data-testid='wizard-version-error']").exists()).toBe(false);
    expect(wrapper.get("[data-testid='wizard-from-error']").text()).toBe(
      "起始时间不能晚于结束时间",
    );
    expect(wrapper.get("[data-testid='wizard-to-error']").text()).toBe(
      "起始时间不能晚于结束时间",
    );
  });

  it("maps no matching probes to a disabled step 2 empty state", async () => {
    useCatalogHandlers();
    server.use(
      http.post("/api/evolution/probes/query", () =>
        HttpResponse.json(
          { error: { code: "NO_PROBES_MATCHED", message: "no hits", details: {} } },
          { status: 409 },
        ),
      ),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-version']").setValue("v2.0.0");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();

    expect(wrapper.get(".wz-condstrip").text()).toContain("skill:deep-research@v2.0.0");
    expect(wrapper.get("[data-testid='wizard-empty']").text()).toContain(
      "未找到匹配的问题报告",
    );
    expect(wrapper.get("[data-testid='wizard-cluster']").attributes("disabled")).toBe("");
    expect(wrapper.get("[data-testid='wizard-start']").attributes("disabled")).toBe("");
  });

  it("launches a cluster-only run and reports the accepted run id", async () => {
    useCatalogHandlers();
    const bodies: Record<string, unknown>[] = [];
    server.use(
      http.post("/api/evolution/probes/query", () => HttpResponse.json(preview)),
      http.post("/api/evolution/cluster-previews", async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          runId: "evo-preview-1",
          jobId: "job-1",
          status: "preparing",
        });
      }),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-version']").setValue("v2.0.0");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-cluster']").trigger("click");
    await flushPromises();

    expect(bodies[0]).toMatchObject({ mode: "cluster_only", targetName: "deep-research" });
    expect(wrapper.emitted("launched")).toEqual([["cluster_only", "evo-preview-1"]]);
  });

  it("launches a full run for the primary action", async () => {
    useCatalogHandlers();
    server.use(
      http.post("/api/evolution/probes/query", () => HttpResponse.json(preview)),
      http.post("/api/evolution/runs", () =>
        HttpResponse.json(
          { runId: "evo-full-1", jobId: "job-2", status: "preparing" },
          { status: 202 },
        ),
      ),
    );
    const wrapper = await mountWizard();

    await wrapper.get("[data-testid='wizard-target']").setValue("tgt-1");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-version']").setValue("v2.0.0");
    await wrapper.get("[data-testid='wizard-from']").setValue("2026-08-01");
    await wrapper.get("[data-testid='wizard-to']").setValue("2026-08-31");
    await wrapper.get("[data-testid='wizard-next']").trigger("click");
    await flushPromises();
    await wrapper.get("[data-testid='wizard-start']").trigger("click");
    await flushPromises();

    expect(wrapper.emitted("launched")).toEqual([["full", "evo-full-1"]]);
  });
});
