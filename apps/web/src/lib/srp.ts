// SRP password proof — runs entirely in the browser.
//
// Uses one AuthenticationHelper instance for the whole exchange. The
// helper internally:
//   - on construction: kicks off async getLargeAValue → caches `a`, `A`
//   - on getPasswordAuthenticationKey: derives HKDF using poolName
//
// Constructing with the wrong pool name and trying to "transplant"
// state into a second helper races against the first helper's async
// init and causes BigInteger objects to be undefined when SRP math
// runs ("v.mod is not a function"). So: take pool_name as a
// constructor input, do everything on one helper.
//
// Cleartext password lives in `proof()` for the few ms of
// getPasswordAuthenticationKey, then is overwritten. Best-effort wipe.

// @ts-expect-error — internal lib path is not in the package types
import AuthenticationHelperImport from "amazon-cognito-identity-js/lib/AuthenticationHelper";
// @ts-expect-error — internal lib path is not in the package types
import BigIntegerImport from "amazon-cognito-identity-js/lib/BigInteger";

type AuthenticationHelperCtor = new (poolName: string) => any;
const AuthenticationHelper: AuthenticationHelperCtor =
  ((AuthenticationHelperImport as any).default ?? AuthenticationHelperImport) as AuthenticationHelperCtor;

type BigIntegerCtor = new (val: string, radix: number) => any;
const BigInteger: BigIntegerCtor =
  ((BigIntegerImport as any).default ?? BigIntegerImport) as BigIntegerCtor;

export type SrpProof = {
  password_proof: string;
  timestamp: string;
  secret_block: string;
};

function dateNowFormatted(): string {
  // Cognito SRP signature input requires this exact format:
  // "Day Mon DD HH:MM:SS UTC YYYY".
  const d = new Date();
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const day = days[d.getUTCDay()];
  const month = months[d.getUTCMonth()];
  const date = d.getUTCDate();
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  const year = d.getUTCFullYear();
  return `${day} ${month} ${date} ${hh}:${mm}:${ss} UTC ${year}`;
}

/**
 * Holds an in-progress SRP exchange across the two HTTP roundtrips.
 * Construct once per login attempt with the real pool_name.
 */
export class SrpExchange {
  private helper: any;

  /** poolName MUST be the real user-pool name (the part after `_` of
   *  the User Pool ID, e.g. "abcXYZ123" for "ap-northeast-1_abcXYZ123").
   *  This is NOT the User Pool ID and NOT a credential — it's just a
   *  namespace string the SRP HKDF info-bits need. */
  constructor(poolName: string) {
    this.helper = new AuthenticationHelper(poolName);
  }

  /** Returns the public ephemeral SRP_A as a hex string. */
  async largeA(): Promise<string> {
    return await new Promise<string>((resolve, reject) => {
      this.helper.getLargeAValue((err: unknown, a: { toString(radix?: number): string }) => {
        if (err) return reject(err as Error);
        resolve(a.toString(16));
      });
    });
  }

  /** Compute the SRP password proof. Cleartext password is wiped from
   *  this scope after use. */
  async proof(args: {
    username: string;
    password: string;
    srpB: string;     // hex string from Cognito
    salt: string;     // hex string from Cognito
    secretBlock: string;  // base64 from Cognito
  }): Promise<SrpProof> {
    const { username, srpB, salt, secretBlock } = args;
    let password = args.password;

    // SDK's getPasswordAuthenticationKey expects BigInteger instances
    // for SRP_B and salt (it calls .mod() on them). Cognito sends them
    // as hex strings; convert here.
    const srpBigB = new BigInteger(srpB, 16);
    const saltBig = new BigInteger(salt, 16);

    const timestamp = dateNowFormatted();
    const hkdf: Uint8Array = await new Promise<Uint8Array>((resolve, reject) => {
      this.helper.getPasswordAuthenticationKey(
        username,
        password,
        srpBigB,
        saltBig,
        (err: unknown, k: Uint8Array) => {
          if (err) return reject(err as Error);
          resolve(k);
        },
      );
    });

    // Best-effort wipe of cleartext password.
    password = "";
    void password;

    // Cognito SRP signature: HMAC-SHA256(hkdf,
    //   poolName ‖ username ‖ base64-decoded secretBlock ‖ timestamp).
    // Result is base64-encoded.
    const sig = await this.calculateSignatureBase64({
      hkdf,
      username,
      secretBlock,
      timestamp,
    });

    return {
      password_proof: sig,
      timestamp,
      secret_block: secretBlock,
    };
  }

  private async calculateSignatureBase64(args: {
    hkdf: Uint8Array;
    username: string;
    secretBlock: string;
    timestamp: string;
  }): Promise<string> {
    const enc = new TextEncoder();
    const poolNameBytes = enc.encode(this.helper.poolName as string);
    const usernameBytes = enc.encode(args.username);
    const secretBlockBytes = b64ToBytes(args.secretBlock);
    const timestampBytes = enc.encode(args.timestamp);

    const message = concat([
      poolNameBytes,
      usernameBytes,
      secretBlockBytes,
      timestampBytes,
    ]);

    // Some browsers reject Uint8Array directly as the key; copy into
    // a fresh ArrayBuffer.
    const keyBuf = new Uint8Array(args.hkdf).buffer;
    const cryptoKey = await crypto.subtle.importKey(
      "raw",
      keyBuf,
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const macBuf = await crypto.subtle.sign("HMAC", cryptoKey, message);
    return bytesToB64(new Uint8Array(macBuf));
  }
}

function concat(parts: Uint8Array[]): Uint8Array {
  let total = 0;
  for (const p of parts) total += p.length;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

function b64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]!);
  return btoa(s);
}
