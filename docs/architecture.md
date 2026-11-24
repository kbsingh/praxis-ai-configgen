# Architecture

This document describes the design of the praxis-local-net configuration
generator. The generator is a standalone operator tool;

## 1. Problem statement

A local network may run several AI inference servers on different hosts and
ports. Clients should not need to know which host or port serves a particular
model. A locally running praxis-ai proxy can provide that indirection by
routing requests based on the model name in the request body.

The generator automates the otherwise manual work of:

1. Finding AI servers on the network.
2. Asking each server which models it serves.
3. Producing a praxis configuration that maps model names to the correct
   backends.

## 2. Goals

- Read a small TOML file containing hosts, ports, and scan options.
- Scan the host/port cartesian product efficiently and safely.
- Discover models from OpenAI-compatible, Ollama, and TGI endpoints.
- Generate a valid praxis YAML configuration.
- Load-balance a model across every compatible endpoint that serves it.
- Keep the generated proxy listener on loopback by default.
- Remain dependency-free at runtime apart from Python 3.11+.
- Keep the implementation small enough to audit and modify easily.

## 3. Non-goals

- Starting, stopping, or managing inference servers.
- Sending inference requests or validating model behavior.
- Mutating remote servers.
- Managing credentials for backend inference servers.
- Providing client-facing authentication in the generated proxy.
- Discovering non-HTTP model protocols.
- Acting as a general-purpose network inventory system.
- Replacing praxis-ai configuration validation.

## 4. System context

```text
+-------------------+
| Operator          |
| scan input (TOML) |
+---------+---------+
          |
          v
+-------------------+        read-only HTTP probes        +----------------------+
| config generator  | ----------------------------------> | AI inference servers |
|                   |                                     | vLLM, Ollama, TGI,   |
|                   | <---------------------------------- | llama.cpp, ...       |
+---------+---------+          model lists                +----------------------+
          |
          | generated praxis YAML
          v
+-------------------+        model-based routing         +----------------------+
| praxis-ai proxy   | ----------------------------------> | AI inference servers |
| local listener    |                                     | host:port endpoints  |
+---------+---------+                                     +----------------------+
          ^
          |
+---------+---------+
| API clients       |
| OpenAI/Anthropic  |
+-------------------+
```

The generator and the proxy are separate processes. The generator only writes
configuration; it does not stay in the request path.

## 5. Component design

The implementation is one Python module with separable functions.

```text
+---------------------+
| CLI / entry point   |
+----------+----------+
           |
           v
+---------------------+     +----------------------+
| Config loader       | --> | Scan plan builder    |
| TOML + validation   |     | hosts x ports        |
+---------------------+     +----------+-----------+
                                       |
                                       v
                            +----------------------+
                            | Concurrent scanner   |
                            | TCP + HTTP probes    |
                            +----------+-----------+
                                       |
                                       v
                            +----------------------+
                            | Probe adapters       |
                            | OpenAI/Ollama/TGI    |
                            +----------+-----------+
                                       |
                                       v
                            +----------------------+
                            | Model catalog        |
                            | model -> endpoints   |
                            +----------+-----------+
                                       |
                                       v
                            +----------------------+
                            | YAML renderer        |
                            | listeners, chains,   |
                            | routes, clusters     |
                            +----------+-----------+
                                       |
                                       v
                            +----------------------+
                            | stdout or output file|
                            +----------------------+
```

### 5.1 Config loader

Responsibilities:

- Read TOML with `tomllib`.
- Validate required sections and value types.
- Normalize hostnames.
- Expand explicit ports and inclusive ranges.
- Apply defaults for scan and gateway options.
- Reject invalid, empty, or contradictory input.

Validation rules:

- At least one host.
- At least one port after expansion.
- Ports are integers in `1..65535`.
- Ranges have the form `START-END` with `START <= END`.
- Timeouts and concurrency are positive integers.
- `scheme` is one of `auto`, `http`, or `https`.
- Gateway listen address is a valid `host:port` string.

### 5.2 Scan plan builder

The scan plan is the deduplicated cartesian product of hosts and ports.

```text
hosts = [h1, h2]
ports = [p1, p2, p3]
plan  = [(h1,p1), (h1,p2), (h1,p3), (h2,p1), (h2,p2), (h2,p3)]
```

The plan is ordered deterministically so output does not depend on thread
scheduling.

### 5.3 Concurrent scanner

Each worker owns one `host:port` task and performs:

1. A TCP prefilter using `socket.create_connection`.
2. One or more HTTP probes.
3. Model extraction through the probe adapters.
4. Return of either an endpoint result or a diagnostic.

Concurrency is bounded by `scan.concurrency`. A thread pool is sufficient
because probes are I/O-bound and the expected problem size is modest.

Each task is independent. A failure on one endpoint never aborts the scan.

### 5.4 Probe adapters

Probes are attempted in a fixed order:

| Order | Path | Typical server | Model fields |
| --- | --- | --- | --- |
| 1 | `/v1/models` | vLLM, llama.cpp, OpenAI-compatible | `data[].id` |
| 2 | `/api/tags` | Ollama | `models[].name`, `models[].model` |
| 3 | `/info` | TGI | `model_id`, `model`, `served_model_name` |

A probe succeeds only when:

- The HTTP status is 2xx.
- The response is valid JSON.
- At least one valid model identifier is extracted.

If a probe returns a 2xx response with no models, the scanner continues to the
next probe. If all probes fail, the endpoint produces no catalog entry.

Redirects are not followed. This prevents a scanned server from redirecting
the generator to an unrelated host.

In `auto` scheme mode, the scanner tries HTTP before HTTPS for each probe. The
TCP prefilter is shared by both attempts.

### 5.5 Model catalog

The catalog is a mapping from exact model name to a set of endpoints.

```text
model-a -> [
  { scheme: http, host: gpu-01.local, port: 8000 },
  { scheme: http, host: gpu-02.local, port: 8000 },
]
model-b -> [
  { scheme: http, host: gpu-01.local, port: 8001 },
]
```

Catalog rules:

- Model names are preserved exactly as served, including tags such as
  `llama3:8b`.
- Empty or control-character model names are rejected.
- Duplicate discoveries of the same endpoint/model pair collapse to one entry.
- Host ordering and model ordering are deterministic.

### 5.6 YAML renderer

The renderer converts the catalog into praxis configuration. It emits YAML
using JSON-quoted scalars, so the script does not require a YAML library at
runtime.

Generated pipeline:

```text
GET /v1/models
      |
      v
static_response (optional merged model list)

other requests
      |
      v
model_to_header
      |
      v
router (exact model name -> cluster)
      |
      v
load_balancer (cluster -> endpoints)
```

#### Listener

One listener is generated with the configured address. The default is
`127.0.0.1:8080`. The generator warns when the address is not a loopback
address because the first version has no client authentication.

#### Model extraction

`model_to_header` is configured with `header: X-Model`. It reads the JSON
request body, extracts the `model` field, and removes any client-supplied copy
of the header so clients cannot spoof the routing decision.

#### Router

One route is generated per model:

```yaml
- path_prefix: "/"
  headers:
    x-model: "MODEL_NAME"
  cluster: "CLUSTER_NAME"
```

A miss returns a router error rather than silently forwarding to an arbitrary
backend. There is no implicit catch-all cluster.

#### Clusters

Each cluster contains every endpoint that serves the model. For HTTP endpoints,
all discovered endpoints can share one cluster.

Cluster names are generated deterministically, for example `model-0001`.
Original model names are retained in comments and in route header values.

#### Merged model discovery

When `aggregate_models` is enabled, a `static_response` filter is inserted
before model extraction. It matches `GET /v1/models` and returns one
OpenAI-compatible list containing every discovered model exactly once. This
lets clients discover the merged catalog without querying individual servers.

### 5.7 Output handling

- YAML is written to stdout by default.
- `-o FILE` writes to a file.
- Existing files are rejected unless `--force` is supplied.
- Diagnostics and scan summaries are written to stderr.
- If no models are discovered, no configuration is written and the process
  exits non-zero.

## 6. Input schema

```toml
hosts = ["gpu-01.local", "gpu-02.local", "192.168.1.50"]

[ports]
include = [8000, 11434]
ranges = ["30000-30100", "9000-9010"]

[scan]
scheme = "auto"
connect_timeout_ms = 250
request_timeout_ms = 1000
concurrency = 256
tls_skip_verify = false

[gateway]
listen = "127.0.0.1:8080"
aggregate_models = true
```

Defaults:

| Key | Default |
| --- | --- |
| `scheme` | `auto` |
| `connect_timeout_ms` | `250` |
| `request_timeout_ms` | `1000` |
| `concurrency` | `256` |
| `tls_skip_verify` | `false` |
| `listen` | `127.0.0.1:8080` |
| `aggregate_models` | `true` |

## 7. Generated praxis configuration

The output uses only standard praxis filters available in a default praxis-ai
build:

- `static_response`
- `model_to_header`
- `router`
- `load_balancer`

The generated configuration includes:

```yaml
insecure_options:
  allow_private_endpoints: true
  allow_private_upstreams: true
```

These options are required to proxy to private or local network backends.
They intentionally relax praxis defaults and are appropriate only for the
local-network use case this tool targets.

## 8. TLS handling

Discovery supports HTTP and HTTPS.

For generated HTTPS clusters:

- The cluster sets `http.authority: {from: endpoint}` and `tls: {}`. Praxis
  derives the authority and TLS name from the endpoint selected for each
  attempt, so one cluster can safely front endpoints with different hostnames
  or IP addresses.
- The generated configuration does not include custom CA paths. The praxis
  process must already trust the backend certificate.
- The generator's discovery TLS verification is independent from praxis
  backend verification. If a self-signed certificate is accepted during
  discovery, praxis still needs a trusted CA or the connection will fail.

### Mixed HTTP and HTTPS endpoints

A praxis cluster applies TLS settings to all of its endpoints. If the same
model is discovered on both HTTP and HTTPS endpoints, one cluster cannot serve
both correctly.

First-version policy:

1. Prefer HTTPS when a model appears on both schemes.
2. Omit the HTTP endpoints.
3. Warn about the dropped endpoints.

## 9. Concurrency and timeouts

The scan has two timeout layers:

- `connect_timeout_ms` for the TCP prefilter.
- `request_timeout_ms` for each HTTP probe.

Workers are bounded by `concurrency`. Every task returns a result or a
diagnostic; panics and unexpected exceptions are isolated per task.

Large ranges multiply quickly:

```text
hosts x ports
```

Start with narrow ranges and increase concurrency only after confirming the
expected network load.

## 10. Determinism

Generated output must be stable across runs with the same discovery result.

- Hosts are normalized and sorted.
- Ports are expanded, deduplicated, and sorted.
- Models are sorted by name.
- Endpoints within a cluster are sorted by host, then port, then scheme.
- Cluster IDs are assigned after sorting.
- No timestamps are written to the generated YAML.

## 11. Security considerations

- The scan is read-only and sends only model-list requests.
- Redirects are disabled to avoid unexpected cross-host requests.
- The generated listener binds to loopback by default.
- The first version has no gateway authentication; exposing the listener on a
  non-loopback interface is a documented risk.
- Generated `insecure_options` relax private-address protections. This is
  required for local backends but should be reviewed before reuse elsewhere.
- Logs include host, port, status, and model counts by default. They do not
  include response bodies.
- `--verbose` may include probe URLs and parse failures, but still avoids
  dumping full response bodies.

## 12. Error handling

| Condition | Behavior |
| --- | --- |
| Invalid TOML | Print parse error, exit non-zero |
| Invalid port or range | Print validation error, exit non-zero |
| Closed port | Skip silently; count in summary |
| Open non-AI port | Skip after probes fail; count in summary |
| Authorization failure | Skip; report with `--verbose` |
| TLS or certificate failure | Skip; report with `--verbose` |
| No models discovered | Print summary, exit non-zero |
| Output file exists | Refuse unless `--force` |
| Some hosts fail | Continue scanning, exit success if at least one model found |

## 13. Testing strategy

Unit tests:

- TOML parsing and validation.
- Port and range expansion.
- Response parsing for each probe shape.
- Catalog aggregation and deduplication.
- Deterministic YAML rendering.
- Mixed-scheme warnings and HTTPS endpoint authority generation.
- Output-file and no-model exit behavior.

Integration tests:

- Fake HTTP servers implementing OpenAI, Ollama, and TGI response shapes.
- Duplicate model served by multiple fake endpoints.
- End-to-end generation against loopback servers.
- HTTPS probe against a temporary self-signed certificate when `openssl` is
  available.

Optional validation:

- If `PRAXIS_AI_BIN` points to a `praxis-ai` binary, run
  `praxis-ai --validate -c <generated>` as part of the test suite.
- This check is skipped when the binary is unavailable.

## 14. Future extensions

Potential follow-up work, not part of the first milestone set:

- Optional backend bearer-token or API-key support.
- Client authentication for non-loopback listeners.
- Qualified model aliases for mixed HTTP/HTTPS endpoint groups.
- Custom CA paths for generated clusters.
- Capability probing per model (`/v1/chat/completions`, `/v1/messages`,
  `/v1/responses`, embeddings).
- Watch mode that regenerates configuration when the catalog changes.
- JSON output mode for automation.
- Overlay-file output for praxis `intelligent_route` round-robin selection.
