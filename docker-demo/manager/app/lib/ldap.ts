/**
 * Manager-side LDAP client for dynamic tenant-branch + user seeding.
 * Used exclusively by the provisioning SSE stream.
 *
 * NOT a "use server" module — imported by the stream route which is
 * server-side by construction. "use server" would expose every export
 * as a client-callable Server Action, which we don't want.
 */
import ldap from "ldapjs";

const LDAP_URL = process.env.LDAP_URL ?? "ldap://openldap:389";
const LDAP_ADMIN_DN = process.env.LDAP_ADMIN_DN ?? "cn=admin,dc=demo,dc=local";
const LDAP_ADMIN_PW = process.env.LDAP_ADMIN_PW ?? "admin";
const DEMO_USER_SSHA = process.env.DEMO_USER_SSHA ?? ""; // SSHA hash of password "demo"

function bindClient(): Promise<ldap.Client> {
  return new Promise((resolve, reject) => {
    const client = ldap.createClient({ url: LDAP_URL });
    client.on("error", reject);
    client.bind(LDAP_ADMIN_DN, LDAP_ADMIN_PW, (err) => {
      if (err) reject(err);
      else resolve(client);
    });
  });
}

function add(client: ldap.Client, dn: string, entry: Record<string, unknown>): Promise<void> {
  return new Promise((resolve, reject) => {
    client.add(dn, entry, (err) => {
      if (err && err.name === "EntryAlreadyExistsError") return resolve();
      if (err) return reject(err);
      resolve();
    });
  });
}

export type SeedResult = {
  branch_dn: string;
  users: Array<{ uid: string; dn: string; mail: string }>;
};

export async function seedTenantBranch(tenantServerName: string): Promise<SeedResult> {
  if (!DEMO_USER_SSHA) {
    throw new Error(
      "DEMO_USER_SSHA env is required (pre-hashed SSHA of password 'demo')"
    );
  }
  const org = tenantServerName.replace(/\..*$/, ""); // "acme" from "acme.localhost"
  const client = await bindClient();
  try {
    const orgDN = `o=${org},ou=organizations,dc=demo,dc=local`;
    const usersDN = `ou=users,${orgDN}`;

    await add(client, orgDN, {
      objectClass: ["top", "organization"],
      o: org,
      description: `Tenant branch for ${tenantServerName}`,
    });

    await add(client, usersDN, {
      objectClass: ["top", "organizationalUnit"],
      ou: "users",
    });

    const seeded: Array<{ uid: string; dn: string; mail: string }> = [];
    for (const uid of ["alice", "bob", "charlie"]) {
      const userDN = `uid=${uid},${usersDN}`;
      const mail = `${uid}@${tenantServerName}`;
      await add(client, userDN, {
        objectClass: ["top", "person", "organizationalPerson", "inetOrgPerson"],
        uid,
        cn: uid.charAt(0).toUpperCase() + uid.slice(1),
        sn: uid,
        mail,
        userPassword: DEMO_USER_SSHA,
      });
      seeded.push({ uid, dn: userDN, mail });
    }
    return { branch_dn: orgDN, users: seeded };
  } finally {
    client.unbind();
  }
}
