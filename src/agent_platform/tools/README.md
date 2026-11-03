# Application tools

Place reviewed application-specific tool implementations and bindings here when implemented.
The harness owns shared tool definitions, gateway policy and output enforcement. Runtime workers
own durable dispatch, attempt accounting and recovery. Application tools receive explicit clients
and must preserve separate restricted-output delivery metadata.

The existing arithmetic/report tools remain synthetic fixtures under `tests/validation/`.
This directory introduces no production banking integration or additional tool capability.
