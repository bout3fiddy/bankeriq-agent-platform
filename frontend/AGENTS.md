# Frontend guidance

Read the root instructions, [README.md](README.md) and the backend application guide.

- Keep browser code and its build/dependencies here. Choose the framework when frontend
  implementation is authorised; this refactor adds documentation only.
- Use authenticated backend APIs. Never embed model/tool credentials or trust browser
  state for permissions, approvals, tenant membership or conversation ownership.
- Treat model text as untrusted content. Distinguish provisional streaming fragments from
  committed answers and handle reconnect gaps, obsolete attempts and access revocation.
- Preserve separate delivery metadata; protected report contents cannot enter model history.
- Passive SSE connections and status reads must not renew conversation activity.
