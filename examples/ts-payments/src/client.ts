/** Charging a card through a flaky payment gateway, with retries on timeouts. */

export class GatewayTimeout extends Error {
  constructor() {
    super("gateway timed out");
    this.name = "GatewayTimeout";
  }
}

export class PaymentFailed extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = "PaymentFailed";
  }
}

export interface Gateway {
  charge(txnId: string, amount: number, options: { timeoutMs: number }): Promise<string>;
}

const realSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Charge `amount`; retry timeouts with exponential backoff (500ms, 1s, ...); declines are not retried. */
export async function chargeWithRetry(
  gateway: Gateway,
  txnId: string,
  amount: number,
  attempts = 3,
  sleep: (ms: number) => Promise<void> = realSleep,
): Promise<string> {
  if (attempts < 1) {
    throw new RangeError("attempts must be >= 1");
  }
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      return await gateway.charge(txnId, amount, { timeoutMs: 5000 });
    } catch (err) {
      if (!(err instanceof GatewayTimeout)) {
        throw err;
      }
      lastError = err;
      if (attempt < attempts - 1) {
        await sleep(500 * 2 ** attempt);
      }
    }
  }
  throw new PaymentFailed(`${txnId}: gave up after ${attempts} attempts`, lastError);
}
