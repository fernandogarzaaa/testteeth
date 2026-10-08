// Retry behaviour: timeouts are retried with backoff, declines are not, and we give up cleanly.
import { describe, expect, it, vi } from "vitest";
import { chargeWithRetry, GatewayTimeout, PaymentFailed } from "../src/client";

function fakeGateway(...outcomes: Array<string | Error>) {
  const charge = vi.fn(async (_txnId: string, _amount: number, _options: { timeoutMs: number }) => {
    const next = outcomes.shift();
    if (next instanceof Error) throw next;
    return next as string;
  });
  return { charge };
}

describe("chargeWithRetry", () => {
  it("success on the first attempt does not sleep", async () => {
    const gateway = fakeGateway("ch_1");
    const sleep = vi.fn(async (_ms: number) => {});
    await expect(chargeWithRetry(gateway, "t1", 9.5, 3, sleep)).resolves.toBe("ch_1");
    expect(gateway.charge).toHaveBeenCalledTimes(1);
    expect(gateway.charge).toHaveBeenCalledWith("t1", 9.5, { timeoutMs: 5000 });
    expect(sleep).not.toHaveBeenCalled();
  });

  it("timeouts are retried with exponential backoff", async () => {
    const gateway = fakeGateway(new GatewayTimeout(), new GatewayTimeout(), "ch_3");
    const sleep = vi.fn(async (_ms: number) => {});
    await expect(chargeWithRetry(gateway, "t1", 9.5, 3, sleep)).resolves.toBe("ch_3");
    expect(gateway.charge).toHaveBeenCalledTimes(3);
    expect(sleep.mock.calls).toEqual([[500], [1000]]);
  });

  it("gives up after all attempts without a final sleep", async () => {
    const timeout = new GatewayTimeout();
    const gateway = fakeGateway(timeout, new GatewayTimeout(), timeout);
    const sleep = vi.fn(async (_ms: number) => {});
    const error = await chargeWithRetry(gateway, "t1", 9.5, 3, sleep).catch((e) => e);
    expect(error).toBeInstanceOf(PaymentFailed);
    expect(error.message).toBe("t1: gave up after 3 attempts");
    expect(error.cause).toBe(timeout);
    expect(error.name).toBe("PaymentFailed");
    expect(gateway.charge).toHaveBeenCalledTimes(3);
    expect(sleep.mock.calls).toEqual([[500], [1000]]);
  });

  it("a single attempt means no retry", async () => {
    const gateway = fakeGateway(new GatewayTimeout());
    const sleep = vi.fn(async (_ms: number) => {});
    await expect(chargeWithRetry(gateway, "t1", 9.5, 1, sleep)).rejects.toThrow(PaymentFailed);
    expect(gateway.charge).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it("declines are not retried", async () => {
    const decline = new Error("card declined");
    const gateway = fakeGateway(decline, "never");
    await expect(chargeWithRetry(gateway, "t1", 9.5, 3, async () => {})).rejects.toBe(decline);
    expect(gateway.charge).toHaveBeenCalledTimes(1);
  });

  it("attempts must be positive", async () => {
    const gateway = fakeGateway();
    await expect(chargeWithRetry(gateway, "t1", 9.5, 0)).rejects.toThrow("attempts must be >= 1");
    expect(gateway.charge).not.toHaveBeenCalled();
  });

  it("the default sleep really waits between retries", async () => {
    vi.useFakeTimers();
    try {
      const gateway = fakeGateway(new GatewayTimeout(), "ch_2");
      const pending = chargeWithRetry(gateway, "t1", 9.5, 2);
      await vi.advanceTimersByTimeAsync(499);
      expect(gateway.charge).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(1);
      await expect(pending).resolves.toBe("ch_2");
      expect(gateway.charge).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("errors carry their names", () => {
    expect(new GatewayTimeout().name).toBe("GatewayTimeout");
    expect(new GatewayTimeout().message).toBe("gateway timed out");
  });
});
