/*
 * The LocalServer API clients the stream camera UI adds
 * (rtsp-rtmp-stream-cameras Requirements 4.3, 11.6, 16.1, 16.2): their
 * endpoints, parameters, and how a registration that does not run
 * continuously reads.
 */
import axios from "axios";
import { APIList, Connection } from "config/Interface";
import {
  STREAM_CONNECTION_TEST_TIMEOUT_MS,
  getStreamHealth,
  testStreamConnection,
} from "./ImageSourceAPI";
import {
  getContinuousStatus,
  listRegistrationExecutions,
  pauseContinuousWorkflow,
  resumeContinuousWorkflow,
} from "./WorkflowRegistrationAPI";

const REGISTRATIONS = `${Connection.ENDPOINT}/workflows/registrations`;

let getSpy: jest.SpyInstance;
let postSpy: jest.SpyInstance;

beforeEach(() => {
  getSpy = jest.spyOn(axios, "get").mockResolvedValue({ data: {} });
  postSpy = jest.spyOn(axios, "post").mockResolvedValue({ data: {} });
});

afterEach(() => {
  getSpy.mockRestore();
  postSpy.mockRestore();
});

describe("the stream camera Image_Source clients", () => {
  it("runs a connection test with room for the 20 s device bound (4.3)", async () => {
    postSpy.mockResolvedValue({ data: { ok: true, category: null, message: "ok", streamHealth: {} } });
    const result = await testStreamConnection("src-1");
    expect(postSpy).toHaveBeenCalledWith(
      `${APIList.imageSourcesAPI}/src-1/test-connection`,
      undefined,
      { timeout: STREAM_CONNECTION_TEST_TIMEOUT_MS },
    );
    expect(STREAM_CONNECTION_TEST_TIMEOUT_MS).toBeGreaterThan(20_000);
    expect(result.ok).toBe(true);
  });

  it("reads a camera's Stream_Health (16.1)", async () => {
    getSpy.mockResolvedValue({ data: { state: "streaming", credentialsConfigured: true } });
    expect(await getStreamHealth("src-1")).toEqual({ state: "streaming", credentialsConfigured: true });
    expect(getSpy).toHaveBeenCalledWith(`${APIList.imageSourcesAPI}/src-1/stream-health`);
  });
});

describe("the continuous workflow clients", () => {
  it("reads the Continuous status (16.2)", async () => {
    getSpy.mockResolvedValue({ data: { registrationId: "reg-1", state: "running" } });
    expect(await getContinuousStatus("reg-1")).toEqual({ registrationId: "reg-1", state: "running" });
    expect(getSpy).toHaveBeenCalledWith(`${REGISTRATIONS}/reg-1/continuous`);
  });

  it("reads a registration that does not run continuously (404) as null", async () => {
    getSpy.mockRejectedValue({ response: { status: 404, data: { message: "not continuous" } } });
    await expect(getContinuousStatus("reg-1")).resolves.toBeNull();
  });

  it("passes any other failure on", async () => {
    const failure = { response: { status: 500, data: { message: "boom" } } };
    getSpy.mockRejectedValue(failure);
    await expect(getContinuousStatus("reg-1")).rejects.toBe(failure);
    getSpy.mockRejectedValue(new Error("Network Error"));
    await expect(getContinuousStatus("reg-1")).rejects.toThrow("Network Error");
  });

  it("pauses and resumes (11.6)", async () => {
    postSpy.mockResolvedValue({ data: { state: "paused" } });
    expect(await pauseContinuousWorkflow("reg-1")).toEqual({ state: "paused" });
    expect(postSpy).toHaveBeenLastCalledWith(`${REGISTRATIONS}/reg-1/continuous/pause`);
    postSpy.mockResolvedValue({ data: { state: "running" } });
    expect(await resumeContinuousWorkflow("reg-1")).toEqual({ state: "running" });
    expect(postSpy).toHaveBeenLastCalledWith(`${REGISTRATIONS}/reg-1/continuous/resume`);
  });

  it("lists the recent or notable executions of a registration (16.2)", async () => {
    getSpy.mockResolvedValue({ data: [] });
    await listRegistrationExecutions("reg-1", { limit: 50, notable: true });
    expect(getSpy).toHaveBeenLastCalledWith(`${REGISTRATIONS}/reg-1/executions`, {
      params: { limit: 50, notable: true },
    });
    await listRegistrationExecutions("reg-1", { limit: 50, notable: false });
    expect(getSpy).toHaveBeenLastCalledWith(`${REGISTRATIONS}/reg-1/executions`, {
      params: { limit: 50 },
    });
    await listRegistrationExecutions("reg-1");
    expect(getSpy).toHaveBeenLastCalledWith(`${REGISTRATIONS}/reg-1/executions`, { params: {} });
  });
});
