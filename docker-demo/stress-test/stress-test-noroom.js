import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

const testData = JSON.parse(open("./test-data.json"));
const tenantNames = Object.keys(testData.tenants);

const loginDuration = new Trend("login_duration", true);
const syncDuration = new Trend("sync_duration", true);
const versionsDuration = new Trend("versions_duration", true);
const whoamiDuration = new Trend("whoami_duration", true);
const isolationOK = new Rate("isolation_check_passed");

export const options = {
  insecureSkipTLSVerify: true,
  stages: [
    { duration: "20s", target: 20 },
    { duration: "30s", target: 50 },
    { duration: "30s", target: 100 },
    { duration: "2m", target: 100 },
    { duration: "30s", target: 200 },
    { duration: "1m", target: 200 },
    { duration: "20s", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<5000"],
    http_req_failed: ["rate<0.15"],
    checks: ["rate>0.90"],
  },
};

const BASE_URL = __ENV.BASE_URL || "https://localhost";

let cachedToken = null;

function assignment() {
  const vuId = __VU - 1;
  const ti = vuId % tenantNames.length;
  const ui = Math.floor(vuId / tenantNames.length) % 10;
  const name = tenantNames[ti];
  return { name, user: testData.tenants[name].users[ui] };
}

function hdrs(tenant, token) {
  const h = { Host: tenant, "Content-Type": "application/json" };
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
}

function ensureLogin(tenant, user) {
  if (cachedToken) return cachedToken;
  const res = http.post(
    `${BASE_URL}/_matrix/client/v3/login`,
    JSON.stringify({
      type: "m.login.password",
      identifier: { type: "m.id.user", user: user.username },
      password: user.password,
    }),
    { headers: hdrs(tenant), tags: { name: "login", tenant } }
  );
  check(res, { "login 200": (r) => r.status === 200 });
  loginDuration.add(res.timings.duration, { tenant });
  if (res.status === 200) cachedToken = res.json().access_token;
  return cachedToken;
}

export default function () {
  const { name: tenant, user } = assignment();
  const token = ensureLogin(tenant, user);
  if (!token) {
    sleep(1);
    return;
  }

  // 1) hot path, no auth
  let r = http.get(`${BASE_URL}/_matrix/client/versions`, {
    headers: hdrs(tenant),
    tags: { name: "versions", tenant },
  });
  check(r, { "versions 200": (res) => res.status === 200 });
  versionsDuration.add(r.timings.duration, { tenant });

  // 2) short auth'd read — whoami
  r = http.get(`${BASE_URL}/_matrix/client/v3/account/whoami`, {
    headers: hdrs(tenant, token),
    tags: { name: "whoami", tenant },
  });
  check(r, {
    "whoami 200": (res) => res.status === 200,
    "whoami user matches tenant": (res) =>
      res.status === 200 && (res.json().user_id || "").endsWith(`:${tenant}`),
  });
  whoamiDuration.add(r.timings.duration, { tenant });

  // 3) heavier auth'd read — initial sync with short timeout
  r = http.get(`${BASE_URL}/_matrix/client/v3/sync?timeout=0`, {
    headers: hdrs(tenant, token),
    tags: { name: "sync", tenant },
  });
  check(r, { "sync 200": (res) => res.status === 200 });
  syncDuration.add(r.timings.duration, { tenant });

  // 4) Cross-tenant isolation probe every 10th iteration:
  //    present our token to a sibling tenant's Host header — must be rejected.
  if (__ITER % 10 === 9) {
    const other = tenantNames[(tenantNames.indexOf(tenant) + 1) % tenantNames.length];
    const iso = http.get(`${BASE_URL}/_matrix/client/v3/account/whoami`, {
      headers: hdrs(other, token),
      tags: { name: "isolation_check", tenant },
    });
    const rejected =
      iso.status === 401 || iso.status === 403 || iso.status === 404;
    check(iso, {
      "cross-tenant token rejected": () => rejected,
    });
    isolationOK.add(rejected ? 1 : 0);
  }

  sleep(1 + Math.random() * 2);
}
