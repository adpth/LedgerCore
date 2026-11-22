import http from "k6/http";
import { check } from "k6";
import exec from "k6/execution";

const base = __ENV.LEDGER_URL || "http://localhost:8000";
const pairs = JSON.parse(__ENV.LEDGER_ACCOUNT_PAIRS || "[]");
const replays = __ENV.LEDGER_MODE === "replay";
const run = __ENV.LEDGER_RUN_ID;
const rate = Number(__ENV.LEDGER_RATE || 1200);

export const options = {
  scenarios: {transfers: replays
    ? {executor: "shared-iterations", vus: pairs.length || 1, iterations: 50000, maxDuration: "20m"}
    : {executor: "constant-arrival-rate", rate, timeUnit: "1s", duration: "30s",
       preAllocatedVUs: pairs.length || 1}},
  thresholds: {checks: ["rate==1"], http_req_failed: ["rate==0"], ...(replays ? {iterations: ["count==50000"]} : {
    dropped_iterations: ["count==0"], http_req_duration: ["p(95)<45"],
  })},
};

export function setup() {
  if (!/^[!-~]{1,100}$/.test(run || "") || !__ENV.LEDGER_TOKEN || !pairs.length) {
    throw new Error("Set LEDGER_RUN_ID, LEDGER_TOKEN and LEDGER_ACCOUNT_PAIRS");
  }
  if (!pairs.every(pair => Array.isArray(pair) && pair.length === 2 &&
      pair.every(id => Number.isSafeInteger(id) && id > 0)) ||
      new Set(pairs.flat()).size !== pairs.length * 2) {
    throw new Error("Use two distinct positive safe-integer account IDs per pair");
  }
}

export default function () {
  const pair = pairs[exec.vu.idInTest - 1];
  const [source, destination] = exec.vu.iterationInScenario % 2 ? [...pair].reverse() : pair;
  const key = run + "-" + exec.scenario.iterationInTest;
  const request = {source, destination, amount_minor: 1};
  const params = {headers: {Authorization: "Bearer " + __ENV.LEDGER_TOKEN,
    "Content-Type": "application/json", "Idempotency-Key": key}};
  const first = http.post(base + "/api/movements/transfer", JSON.stringify(request), params);
  const entry = first.status === 201 ? first.json("id") : null;
  check(first, {"transfer committed": response => response.status === 201 &&
    typeof entry === "string" && entry.length > 0});
  if (replays) {
    const retry = http.post(base + "/api/movements/transfer", JSON.stringify(request), params);
    check(retry, {"replay response matches": response =>
      typeof entry === "string" && entry.length > 0 &&
      response.status === 201 && response.json("id") === entry});
  }
}
