import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate, Counter } from "k6/metrics";

// ── Load test data ─────────────────────────────────────────────
const testData = JSON.parse(open("./test-data.json"));
const tenantNames = Object.keys(testData.tenants);

// ── Custom metrics ─────────────────────────────────────────────
const messageSendDuration = new Trend("message_send_duration", true);
const timelineReadDuration = new Trend("timeline_read_duration", true);
const isolationCheckRate = new Rate("isolation_check_passed");
const rateLimited = new Counter("rate_limited_responses");

// ── Options ────────────────────────────────────────────────────
// Ramp profile designed to find the connection pool saturation point
// (cp_max=10 in homeserver.yaml — challenges.md predicts starvation
// at moderate concurrency).
export const options = {
  insecureSkipTLSVerify: true,
  stages: [
    { duration: "20s", target: 20 },   // warm-up
    { duration: "30s", target: 50 },   // moderate — within pool budget
    { duration: "30s", target: 100 },  // push past cp_max=10
    { duration: "2m", target: 100 },   // sustained — expose pool starvation
    { duration: "30s", target: 200 },  // overload — find the breaking point
    { duration: "1m", target: 200 },   // sustained overload
    { duration: "20s", target: 0 },    // cool-down
  ],
  thresholds: {
    http_req_duration: ["p(95)<5000"],
    http_req_failed: ["rate<0.10"],
    checks: ["rate>0.90"],
  },
};

const BASE_URL = __ENV.BASE_URL || "https://localhost";

// ── Per-VU state ───────────────────────────────────────────────
let cachedToken = null;
let txnCounter = 0;

// ── VU assignment ──────────────────────────────────────────────
function getAssignment() {
  const vuId = __VU - 1;
  const tenantIndex = vuId % tenantNames.length;
  const userIndex = Math.floor(vuId / tenantNames.length) % 10;
  const tenantName = tenantNames[tenantIndex];
  const tenant = testData.tenants[tenantName];
  const user = tenant.users[userIndex];
  return { tenantName, tenant, user };
}

function hdrs(tenantName, token) {
  const h = {
    Host: tenantName,
    "Content-Type": "application/json",
  };
  if (token) {
    h["Authorization"] = `Bearer ${token}`;
  }
  return h;
}

// ── Login (cached per VU) ──────────────────────────────────────
function ensureLoggedIn(tenantName, user) {
  if (cachedToken) return cachedToken;

  const res = http.post(
    `${BASE_URL}/_matrix/client/v3/login`,
    JSON.stringify({
      type: "m.login.password",
      identifier: { type: "m.id.user", user: user.username },
      password: user.password,
    }),
    { headers: hdrs(tenantName), tags: { name: "login", tenant: tenantName } }
  );

  check(res, { "login succeeded": (r) => r.status === 200 });

  if (res.status === 200) {
    cachedToken = res.json().access_token;
  }
  return cachedToken;
}

// ── Send a single message ──────────────────────────────────────
function sendMessage(tenantName, token, roomId, body) {
  const txnId = `k6_${__VU}_${txnCounter++}`;
  const res = http.put(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txnId}`,
    JSON.stringify({ msgtype: "m.text", body: body }),
    { headers: hdrs(tenantName, token), tags: { name: "send_message", tenant: tenantName } }
  );

  check(res, { "message sent (200)": (r) => r.status === 200 });
  messageSendDuration.add(res.timings.duration, { tenant: tenantName });

  if (res.status === 429) {
    rateLimited.add(1, { tenant: tenantName });
  }
  return res;
}

// ── Read timeline ──────────────────────────────────────────────
function readTimeline(tenantName, token, roomId) {
  const res = http.get(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/messages?dir=b&limit=10`,
    { headers: hdrs(tenantName, token), tags: { name: "timeline", tenant: tenantName } }
  );

  check(res, { "timeline read (200)": (r) => r.status === 200 });
  timelineReadDuration.add(res.timings.duration, { tenant: tenantName });

  // Verify no cross-tenant leak in timeline
  if (res.status === 200) {
    const events = res.json().chunk || [];
    const allOwnTenant = events.every((evt) => {
      const sender = evt.sender || "";
      return sender.endsWith(`:${tenantName}`);
    });
    check(null, { "no cross-tenant leak in timeline": () => allOwnTenant });
  }
  return res;
}

// ── Main test function ─────────────────────────────────────────
// Realistic burst pattern from stress-test-suggestions.md:
//   send 3 messages quickly → wait → sync → read timeline → repeat
export default function () {
  const { tenantName, tenant, user } = getAssignment();
  const token = ensureLoggedIn(tenantName, user);

  if (!token) {
    console.error(`VU ${__VU}: login failed for ${user.username}@${tenantName}`);
    sleep(1);
    return;
  }

  const roomId = tenant.room_id;

  // ── Burst: send 3 messages quickly ─────────────────────────
  for (let i = 0; i < 3; i++) {
    sendMessage(
      tenantName, token, roomId,
      `burst msg ${i} from ${user.username}@${tenantName} iter=${__ITER}`
    );
  }

  // ── Wait (simulates user reading/typing) ───────────────────
  sleep(2 + Math.random() * 3); // 2-5s pause

  // ── Read timeline ──────────────────────────────────────────
  readTimeline(tenantName, token, roomId);

  // ── Isolation check (every 10th iteration) ─────────────────
  // Uses current user's token with a different tenant's Host header
  // to test actual schema-level token isolation.
  // Pick a RANDOM other tenant (not the deterministic next one) so the
  // probe covers the N² isolation surface as the tenant count scales.
  if (__ITER % 10 === 9) {
    const selfIdx = tenantNames.indexOf(tenantName);
    let otherIndex = Math.floor(Math.random() * tenantNames.length);
    if (otherIndex === selfIdx) otherIndex = (otherIndex + 1) % tenantNames.length;
    const otherTenantName = tenantNames[otherIndex];
    const otherRoomId = testData.tenants[otherTenantName].room_id;

    const isolationRes = http.get(
      `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(otherRoomId)}/messages?dir=b&limit=1`,
      { headers: hdrs(otherTenantName, token), tags: { name: "isolation_check", tenant: tenantName } }
    );

    const isolated = isolationRes.status === 401 || isolationRes.status === 403 || isolationRes.status === 404;
    check(isolationRes, {
      "cross-tenant token rejected (401/403/404)": (r) =>
        r.status === 401 || r.status === 403 || r.status === 404,
    });
    isolationCheckRate.add(isolated ? 1 : 0);
  }

  // ── Short pause before next burst ──────────────────────────
  sleep(1 + Math.random() * 2); // 1-3s
}
