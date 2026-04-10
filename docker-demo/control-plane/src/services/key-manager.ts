import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
} from "node:crypto";
import nacl from "tweetnacl";
import naclUtil from "tweetnacl-util";

/**
 * AES-256-GCM encryption for tenant signing keys.
 *
 * Wire format: IV (12 bytes) || ciphertext || GCM auth tag (16 bytes)
 *
 * This must produce output identical to the Python implementation in
 * synapse/crypto/tenant_key_encryption.py so that Synapse can decrypt
 * keys stored by the control plane.
 */

const IV_LENGTH = 12;
const TAG_LENGTH = 16;

export function encryptSigningKey(keyText: string, masterKey: Buffer): Buffer {
  const iv = randomBytes(IV_LENGTH);
  const cipher = createCipheriv("aes-256-gcm", masterKey, iv);
  const encrypted = Buffer.concat([
    cipher.update(keyText, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  // Wire format: IV || ciphertext || tag
  return Buffer.concat([iv, encrypted, tag]);
}

export function decryptSigningKey(encrypted: Buffer, masterKey: Buffer): string {
  const iv = encrypted.subarray(0, IV_LENGTH);
  const tag = encrypted.subarray(encrypted.length - TAG_LENGTH);
  const ciphertext = encrypted.subarray(IV_LENGTH, encrypted.length - TAG_LENGTH);

  const decipher = createDecipheriv("aes-256-gcm", masterKey, iv);
  decipher.setAuthTag(tag);
  const decrypted = Buffer.concat([
    decipher.update(ciphertext),
    decipher.final(),
  ]);
  return decrypted.toString("utf8");
}

/**
 * Generate an Ed25519 signing key in signedjson format.
 *
 * Returns the key text line like: "ed25519 a_acme <base64_unpadded_seed>"
 */
export function generateSigningKey(serverName: string): {
  keyText: string;
  keyId: string;
} {
  const keyPair = nacl.sign.keyPair();
  // signedjson uses the 32-byte seed (first half of the 64-byte secret key)
  const seed = keyPair.secretKey.subarray(0, 32);
  const seedB64 = naclUtil
    .encodeBase64(seed)
    .replace(/=+$/, ""); // unpadded base64

  // Key ID prefix from server name (first 4 chars, alphanumeric only)
  const prefix = serverName.replace(/[^a-zA-Z0-9]/g, "").slice(0, 4);
  const keyId = `a_${prefix}`;

  return {
    keyText: `ed25519 ${keyId} ${seedB64}`,
    keyId: `ed25519:${keyId}`,
  };
}

export function parseMasterKey(base64Key: string): Buffer {
  const key = Buffer.from(base64Key, "base64");
  if (key.length !== 32) {
    throw new Error(
      `TENANT_KEY_MASTER must decode to 32 bytes, got ${key.length}`
    );
  }
  return key;
}
