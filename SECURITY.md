# Security policy

## Supported versions

| Version | Gets security fixes |
|---|---|
| 2.4.x | Yes, in the newest 2.4 release |
| Older than 2.4.0 | No. Update to the newest release |

Fixes ship as a new signed release. `flowstate update` installs only a tag signed with a FlowState release key (see the README, "Upgrading").

## Report a vulnerability

Email [support@flowstatemcp.com](mailto:support@flowstatemcp.com). Tell us what you found and how to see it happen. Please don't send passwords, keys or tokens, and give us time to fix it before you make it public.

Please don't open a public GitHub issue for a security problem.

## Scope

In scope:

- FlowState, the MCP server in this repository, and its CLI, review pages and HTTP listener
- The relay agent in this repository that runs on your computer, and the relay protocol (docs/relay-protocol.md)
- The OAuth sign-in that AI apps use to reach your FlowState through the relay or your own tunnel
- The update path (`flowstate update`) and release signing

Out of scope here (report them to the same address anyway, we'll route them):

- The FlowState Relay server and flowstatemcp.com, whose code is not in this repository. How they are secured is described at https://flowstatemcp.com/security
- Third-party services and software, such as your AI apps, cloudflared, GitHub, Supabase or Cloudflare
- Problems that need someone who already controls your computer account. FlowState's database is a plain SQLite file that anyone with access to that account can read

Please test only against your own install or your own beta relay address, and ask before testing anything else.

## Outside checks

No outside firm has audited FlowState, the relay or flowstatemcp.com. A scan is not an audit.

- **OAuth auth scan, October 2026.** Auth-scanned by Leroy H. Mason using [mcp-authscan](https://github.com/LHMisme420/mcp-authscan), against his own beta relay address, with permission. Seven checks passed. It found that the token endpoint sent `Access-Control-Allow-Origin: *`, and it also noted that `/revoke` refused standard RFC 7009 requests and that the OAuth issuer had a trailing slash. All three are fixed in [FlowState 2.4.4](https://github.com/dialectforge/FlowState/releases/tag/v2.4.4).
