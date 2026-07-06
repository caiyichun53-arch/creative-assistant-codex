# Model Routing

The runtime uses a two-layer model configuration:

- `model_routes` defines purpose routes such as `daily_chat`, `business_analysis`, `engineering_execution`, and `writing_generation`.
- `model_providers` defines provider entries such as Mimo, GPT subscription via Codex CLI, or an OpenAI-compatible API gateway.

Workflow nodes bind only `route_id`. The `route_id` maps to `provider_ref` through `config/model_routes.yaml`, and the provider entry supplies only provider type plus auth/address references.

The current production-readiness mapping is Mimo-only:

- `daily_chat` -> Mimo
- `business_analysis` -> Mimo
- `engineering_execution` -> Mimo
- `writing_generation` -> Mimo

This preserves the current Phase 8 engineering handoff state: no GPT call, no
DeepSeek call, no fallback, and no automatic provider switch.

A future user-initiated GPT switch can use the multi-provider pattern in
`config/model_routes.example.multi_provider.yaml`:

- `daily_chat` -> Mimo
- `business_analysis` -> GPT subscription Codex CLI
- `engineering_execution` -> GPT subscription Codex CLI
- `writing_generation` -> GPT API gateway

This is configuration, not a hardcoded rule. All routes can be pointed to the
same provider by changing the config; see
`config/model_routes.example.single_provider.yaml`.

Writing-related workflow nodes use `writing_generation`, including title, hook, structure, draft, rewrite, polish, de-AI-style, platform copy, cover copy, publishing copy, and final copy review.

All route fallback values must be `none`. Provider failure must fail explicitly and must not switch to another provider automatically.
