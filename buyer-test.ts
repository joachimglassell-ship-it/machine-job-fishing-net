import { CdpClient } from "@coinbase/cdp-sdk";
import {
  fromCdpEvmAccount,
  applySpendControls,
} from "@coinbase/cdp-sdk/x402";
import { x402Client } from "@x402/core/client";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";

const API_URL =
  "https://machine-job-fishing-net.onrender.com/v1/find-official-source";

const USDC_BASE_SEPOLIA =
  "0x036cbd53842c5426634e7929541ec2318f3dcf7e";

async function main() {
  const cdp = new CdpClient();

  const account = await cdp.evm.getOrCreateAccount({
    name: "machine-job-buyer",
  });

  console.log("Buyer wallet:", account.address);

  const client = new x402Client();

  registerExactEvmScheme(client, {
    signer: fromCdpEvmAccount(account),
  });

  applySpendControls(client, {
    maxAmountPerPayment: {
      atomic: 10_000n,
      asset: USDC_BASE_SEPOLIA,
    },
    maxCumulativeSpend: {
      atomic: 10_000n,
      asset: USDC_BASE_SEPOLIA,
    },
    maxCumulativeSpendWindow: "24h",
    allowedNetworks: ["eip155:84532"],
  });

  const fetchWithPayment = wrapFetchWithPayment(
    globalThis.fetch,
    client,
  );

  console.log("Calling paid endpoint...");

  const response = await fetchWithPayment(API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query: "Ericsson annual report 2025",
    }),
  });

  console.log("HTTP status:", response.status);

  const text = await response.text();

  console.log("Response:");
  console.log(text);

  if (!response.ok) {
    throw new Error(
      `Paid request failed: ${response.status} ${response.statusText}`,
    );
  }
}

main().catch((error) => {
  console.error("Buyer test failed:");
  console.error(error);
  process.exit(1);
});
