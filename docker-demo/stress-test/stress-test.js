import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

// ── Load test data ─────────────────────────────────────────────
const testData = JSON.parse(open("./test-data.json"));
const tenantNames = Object.keys(testData.tenants);

// ── Custom metrics ─────────────────────────────────────────────
const messageSendDuration = new Trend("message_send_duration", true);
const timelineReadDuration = new Trend("timeline_read_duration", true);
const isolationCheckRate = new Rate("isolation_check_passed");

// ── Options ────────────────────────────────────────────────────
export const options = {
  stages: [
    { duration: "30s", target: 10 },  // warm-up
    { duration: "1m", target: 40 },   // ramp to full load
    { duration: "2m", target: 40 },   // sustained peak
    { duration: "30s", target: 0 },   // cool-down
  ],
  thresholds: {
    http_req_duration: ["p(95)<2000"],
    http_req_failed: ["rate<0.05"],
    checks: ["rate>0.95"],
  },
};

const BASE_URL = __ENV.BASE_URL || "http://localhost";

// ── Per-VU state ───────────────────────────────────────────────
let cachedToken = null;
let txnCounter = 0;

// ── VU assignment ──────────────────────────────────────────────
function getAssignment() {
  // VU IDs are 1-based in k6
  const vuId = __VU - 1;
  const tenantIndex = vuId % tenantNames.length;
  const userIndex = Math.floor(vuId / tenantNames.length) % 10;
  const tenantName = tenantNames[tenantIndex];
  const tenant = testData.tenants[tenantName];
  const user = tenant.users[userIndex];
  return { tenantName, tenant, user };
}

function headers(tenantName, token) {
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
    { headers: headers(tenantName) }
  );

  check(res, {
    "login succeeded": (r) => r.status === 200,
  });

  if (res.status === 200) {
    cachedToken = res.json().access_token;
  }
  return cachedToken;
}

// ── Main test function ─────────────────────────────────────────
export default function () {
  const { tenantName, tenant, user } = getAssignment();
  const token = ensureLoggedIn(tenantName, user);

  if (!token) {
    console.error(`VU ${__VU}: login failed for ${user.username}@${tenantName}`);
    sleep(1);
    return;
  }

  const roomId = tenant.room_id;
  const txnId = `k6_${__VU}_${txnCounter++}`;

  // ── Send message ───────────────────────────────────────────
  const sendRes = http.put(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/send/m.room.message/${txnId}`,
    JSON.stringify({
      msgtype: "m.text",
      body: `stress test msg from ${user.username}@${tenantName} iter=${__ITER}`,
    }),
    { headers: headers(tenantName, token) }
  );

  check(sendRes, {
    "message sent (200)": (r) => r.status === 200,
  });
  messageSendDuration.add(sendRes.timings.duration, { tenant: tenantName });

  // ── Read timeline ──────────────────────────────────────────
  const timelineRes = http.get(
    `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(roomId)}/messages?dir=b&limit=10`,
    { headers: headers(tenantName, token) }
  );

  check(timelineRes, {
    "timeline read (200)": (r) => r.status === 200,
  });
  timelineReadDuration.add(timelineRes.timings.duration, { tenant: tenantName });

  // ── Verify no cross-tenant leak in timeline ────────────────
  if (timelineRes.status === 200) {
    const events = timelineRes.json().chunk || [];
    const allOwnTenant = events.every((evt) => {
      // sender format: @username:server_name
      const sender = evt.sender || "";
      return sender.endsWith(`:${tenantName}`);
    });
    check(null, {
      "no cross-tenant leak in timeline": () => allOwnTenant,
    });
  }

  // ── Isolation check (every 10th iteration) ─────────────────
  // Tests actual schema isolation: use current user's token but send the
  // request with a *different* tenant's Host header. If schema isolation
  // is working, the token is invalid for the other tenant's key namespace
  // and Synapse returns 401. If isolation is broken (search_path bleed),
  // the token would authenticate against the wrong schema.
  if (__ITER % 10 === 9) {
    const otherIndex = (tenantNames.indexOf(tenantName) + 1) % tenantNames.length;
    const otherTenantName = tenantNames[otherIndex];
    const otherRoomId = testData.tenants[otherTenantName].room_id;

    const isolationRes = http.get(
      `${BASE_URL}/_matrix/client/v3/rooms/${encodeURIComponent(otherRoomId)}/messages?dir=b&limit=1`,
      { headers: headers(otherTenantName, token) }
    );

    const isolated = isolationRes.status === 401 || isolationRes.status === 403 || isolationRes.status === 404;
    check(isolationRes, {
      "cross-tenant token rejected (401/403/404)": (r) =>
        r.status === 401 || r.status === 403 || r.status === 404,
    });
    isolationCheckRate.add(isolated ? 1 : 0);
  }

  sleep(0.5 + Math.random() * 0.5); // 0.5–1s think time
}
