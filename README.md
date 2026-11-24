# praxis-local-net config generator

A small, standalone configuration generator for a locally running
[praxis-ai](https://github.com/praxis-proxy/ai) gateway.

It scans a set of hosts and ports on the local network, discovers the AI
models being served by OpenAI-compatible, Ollama, and TGI endpoints, and
emits a praxis YAML configuration. The resulting proxy lets clients use any
discovered model by name without knowing which host or port serves it.

## Status

| Area | State |
| --- | --- |
| Architecture | Documented in [docs/architecture.md](docs/architecture.md) |
| Generator implementation | Implemented |
| Tests | Implemented |

The implementation is a single Python 3.11+ script with no runtime
dependencies outside the standard library.

## Workflow

```text
scan input (TOML)
      |
      v
expand hosts x ports
      |
      v
probe each endpoint (/v1/models, /api/tags, /info)
      |
      v
build model -> endpoints catalog
      |
      v
render praxis YAML
      |
      v
run: praxis-ai -c generated.yaml
```

## Layout

```text
configgen/
  README.md
  docs/
    architecture.md
    milestones.md
    next-steps.md
  example.toml
  generate_praxis_config.py
  pyproject.toml
  tests/
    test_generate_praxis_config.py
```

## Input configuration

The generator reads a TOML file containing a host list and a separate port
section.

```toml
hosts = ["gpu-01.local", "gpu-02.local", "192.168.1.50"]

[ports]
include = [8000, 11434]
ranges = ["30000-30100", "9000-9010"]

[scan]
scheme = "auto"              # auto | http | https
connect_timeout_ms = 250
request_timeout_ms = 1000
concurrency = 256
tls_skip_verify = false

[gateway]
listen = "127.0.0.1:8080"
aggregate_models = true
```

### `[ports]`

- `include`: explicit ports to scan.
- `ranges`: inclusive port ranges in `START-END` form.
- Every host is evaluated against every port in the union of both lists.

### `[scan]`

| Key | Default | Meaning |
| --- | --- | --- |
| `scheme` | `auto` | Try HTTP first, then HTTPS, or restrict to one scheme |
| `connect_timeout_ms` | `250` | TCP prefilter timeout |
| `request_timeout_ms` | `1000` | Per-probe HTTP timeout |
| `concurrency` | `256` | Number of concurrent scan workers |
| `tls_skip_verify` | `false` | Skip certificate verification during discovery only |

### `[gateway]`

| Key | Default | Meaning |
| --- | --- | --- |
| `listen` | `127.0.0.1:8080` | Generated praxis listener address |
| `aggregate_models` | `true` | Serve a merged `GET /v1/models` list |

## Discovery

For each `host:port`, the generator:

1. Optionally verify that the TCP port is open.
2. Probe these paths in order:
   - `/v1/models` for OpenAI-compatible servers such as vLLM or llama.cpp
   - `/api/tags` for Ollama
   - `/info` for TGI
3. Extract model identifiers from the first successful probe.
4. Record the endpoint against each model it serves.

The scan is read-only. 

## Generated configuration

The output is praxis YAML built from standard filters:

- `model_to_header` extracts the request body `model` field into `X-Model`.
- `router` maps an exact model name to a generated cluster.
- `load_balancer` forwards to every endpoint serving that model.
- `static_response` optionally serves one merged model list on `GET /v1/models`.

At a high level:

```yaml
listeners:
  - name: praxis-local-models
    address: "127.0.0.1:8080"
    filter_chains: [local-models]

filter_chains:
  - name: local-models
    filters:
      - filter: model_to_header
        header: X-Model
      - filter: router
        routes:
          - path_prefix: "/"
            headers:
              x-model: "model-a"
            cluster: "model-0001"
      - filter: load_balancer
        clusters:
          - name: "model-0001"
            endpoints:
              - "gpu-01.local:8000"
              - "gpu-02.local:8000"

insecure_options:
  allow_private_endpoints: true
  allow_private_upstreams: true
```

HTTPS clusters use `http.authority: {from: endpoint}` with `tls: {}`, so one
cluster can safely load-balance across endpoints with different hostnames and
certificate names.

The generated listener binds to loopback by default. If it is changed to a
non-loopback address, the generator warns that the first version has no
gateway authentication.

## CLI

```console
python3 configgen/generate_praxis_config.py -c configgen/example.toml
python3 configgen/generate_praxis_config.py -c configgen/example.toml -o praxis.yaml
python3 configgen/generate_praxis_config.py -c configgen/example.toml --verbose
```

- YAML goes to stdout unless `-o FILE` is supplied.
- Diagnostics go to stderr.
- Existing output files are not overwritten without `--force`.
- The command exits non-zero when no models are discovered.

## Scope and limitations

The version is deliberately small.

- No backend bearer-token or API-key support.
- No gateway authentication.
- A model discovered on both HTTP and HTTPS endpoints is routed over HTTPS;
  the generator warns and omits the HTTP endpoints.
- No API-path capability detection. A discovered model is assumed to support
  every inference path routed to its cluster.
- No custom backend CA configuration.
- Large port ranges can take time; tune `concurrency` and timeouts.

See [docs/architecture.md](docs/architecture.md) for the full design.

## Development

Run the tests and lint checks with:

```console
python3 -m unittest discover -s configgen/tests
ruff check configgen
ruff format --check configgen
```

The suite uses local fake HTTP backends and optionally HTTPS, so it does not
require GPU servers or a working local model deployment. When
`PRAXIS_AI_BIN` points to a `praxis-ai` binary, the generated configuration can
also be validated with `praxis-ai --validate`.
