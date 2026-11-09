# ${{ values.name }}

Minimal authenticated gateway client example, not a deployable golden path.
Set `LLM_GATEWAY_API_KEY` to an accepted gateway key through a Secret. Set
`LLM_GATEWAY_URL` when running outside Kubernetes (the default is
`http://llm-gateway.apps.svc/v1`). The configured model must match a model served
by the gateway backend. The example deliberately avoids logging prompt text.

This skeleton has no image build, manifests, streaming, or release workflow.
Use the New Microservice template for those delivery foundations, then integrate
this client and review its dependency pins, lifecycle, retry and timeout policy.
