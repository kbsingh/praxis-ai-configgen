#!/usr/bin/env python3
"""Discover local AI model servers and generate a praxis-ai configuration."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_LISTEN = "127.0.0.1:8080"
DEFAULT_SCHEME = "auto"
DEFAULT_CONNECT_TIMEOUT_MS = 250
DEFAULT_REQUEST_TIMEOUT_MS = 1000
DEFAULT_CONCURRENCY = 256
DEFAULT_AGGREGATE_MODELS = True

MODEL_HEADER = "X-Model"
MODEL_HEADER_LOWER = "x-model"
AGGREGATE_PATH = "/v1/models"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODEL_NAME_BYTES = 256
USER_AGENT = "praxis-local-net-configgen/0.1"

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NO_MODELS = 3
EXIT_OUTPUT = 4

_SCHEMES = ("http", "https")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ConfigError(ValueError):
    """Raised when the generator input is invalid."""


class ProbeError(RuntimeError):
    """Raised when an endpoint probe cannot produce a JSON response."""


@dataclasses.dataclass(frozen=True)
class ScanOptions:
    scheme: str = DEFAULT_SCHEME
    connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    concurrency: int = DEFAULT_CONCURRENCY
    tls_skip_verify: bool = False


@dataclasses.dataclass(frozen=True)
class GatewayOptions:
    listen: str = DEFAULT_LISTEN
    aggregate_models: bool = DEFAULT_AGGREGATE_MODELS


@dataclasses.dataclass(frozen=True)
class GeneratorConfig:
    hosts: tuple[str, ...]
    ports: tuple[int, ...]
    scan: ScanOptions
    gateway: GatewayOptions


@dataclasses.dataclass(frozen=True, order=True)
class Endpoint:
    host: str
    port: int
    scheme: str

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.scheme}://{self.authority}{path}"


@dataclasses.dataclass(frozen=True)
class ScanOutcome:
    host: str
    port: int
    port_open: bool
    endpoint: Endpoint | None = None
    models: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class Probe:
    path: str
    parser: Callable[[Any], tuple[str, ...]]


def _require_table(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(value) - allowed
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"unknown key(s) in [{name}]: {keys}")
    return value


def _require_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be a string")
    return value


def normalize_host(raw: str) -> str:
    """Validate and normalize one hostname or IP address."""
    if not isinstance(raw, str):
        raise ConfigError("hosts entries must be strings")
    host = raw.strip()
    if not host:
        raise ConfigError("hosts entries must not be empty")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if any(token in host for token in ("://", "/", "?", "#")):
        raise ConfigError(f"invalid host '{raw}': use a bare hostname or IP address")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    if ":" in host:
        raise ConfigError(f"invalid host '{raw}': ports belong in [ports]")

    host = host.rstrip(".").lower()
    if not host or len(host) > 253:
        raise ConfigError(f"invalid host '{raw}'")
    for label in host.split("."):
        if not _HOST_LABEL.fullmatch(label):
            raise ConfigError(f"invalid host '{raw}'")
    return host


def parse_listen(value: str) -> tuple[str, int]:
    """Parse and validate a listener address, returning host and port."""
    address = _require_string(value, "gateway.listen").strip()
    if not address:
        raise ConfigError("gateway.listen must not be empty")

    if address.startswith("["):
        end = address.find("]")
        if end < 0 or not address[end + 1 :].startswith(":"):
            raise ConfigError(f"invalid gateway.listen '{value}'")
        host = address[1:end]
        port_text = address[end + 2 :]
    else:
        host, separator, port_text = address.rpartition(":")
        if not separator:
            raise ConfigError(f"gateway.listen '{value}' must be HOST:PORT")
        if ":" in host:
            raise ConfigError(f"invalid gateway.listen '{value}': bracket IPv6 addresses")

    host = normalize_host(host)
    port = _parse_port_text(port_text, "gateway.listen port")
    return host, port


def _parse_port_text(value: Any, name: str) -> int:
    text = _require_string(value, name).strip()
    if not text.isdigit():
        raise ConfigError(f"{name} must be an integer")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535")
    return port


def _validate_port(value: Any, name: str) -> int:
    port = _require_int(value, name)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} must be between 1 and 65535")
    return port


def expand_ports(include: Iterable[Any], ranges: Iterable[Any]) -> tuple[int, ...]:
    """Expand explicit ports and inclusive ranges into a sorted tuple."""
    ports: set[int] = set()
    for index, value in enumerate(include):
        ports.add(_validate_port(value, f"ports.include[{index}]"))

    for index, value in enumerate(ranges):
        name = f"ports.ranges[{index}]"
        text = _require_string(value, name).strip()
        parts = text.split("-")
        if len(parts) != 2:
            raise ConfigError(f"{name} must have the form START-END")
        start = _parse_port_text(parts[0], f"{name} start")
        end = _parse_port_text(parts[1], f"{name} end")
        if start > end:
            raise ConfigError(f"{name} start must not exceed end")
        ports.update(range(start, end + 1))

    if not ports:
        raise ConfigError("at least one port is required")
    return tuple(sorted(ports))


def _unique_hosts(values: Iterable[Any]) -> tuple[str, ...]:
    hosts: set[str] = set()
    for index, value in enumerate(values):
        hosts.add(normalize_host(_require_string(value, f"hosts[{index}]")))
    if not hosts:
        raise ConfigError("at least one host is required")
    return tuple(sorted(hosts))


def _normalize_listen(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def load_config(path: Path) -> GeneratorConfig:
    """Load and validate the TOML generator input."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("config root must be a table")
    unknown = set(data) - {"hosts", "ports", "scan", "gateway"}
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ConfigError(f"unknown top-level key(s): {keys}")

    if "hosts" not in data:
        raise ConfigError("missing required top-level key: hosts")
    raw_hosts = data["hosts"]
    if not isinstance(raw_hosts, list):
        raise ConfigError("hosts must be an array")
    hosts = _unique_hosts(raw_hosts)

    ports_section = _require_table(data.get("ports"), "ports", {"include", "ranges"})
    if not ports_section:
        raise ConfigError("missing required [ports] table")
    include = ports_section.get("include", [])
    ranges = ports_section.get("ranges", [])
    if not isinstance(include, list):
        raise ConfigError("ports.include must be an array")
    if not isinstance(ranges, list):
        raise ConfigError("ports.ranges must be an array")
    ports = expand_ports(include, ranges)

    scan_section = _require_table(
        data.get("scan"),
        "scan",
        {
            "scheme",
            "connect_timeout_ms",
            "request_timeout_ms",
            "concurrency",
            "tls_skip_verify",
        },
    )
    scheme = _require_string(scan_section.get("scheme", DEFAULT_SCHEME), "scan.scheme")
    if scheme not in {"auto", *_SCHEMES}:
        raise ConfigError("scan.scheme must be one of auto, http, https")
    connect_timeout_ms = _positive_int(
        scan_section.get("connect_timeout_ms", DEFAULT_CONNECT_TIMEOUT_MS),
        "scan.connect_timeout_ms",
    )
    request_timeout_ms = _positive_int(
        scan_section.get("request_timeout_ms", DEFAULT_REQUEST_TIMEOUT_MS),
        "scan.request_timeout_ms",
    )
    concurrency = _positive_int(
        scan_section.get("concurrency", DEFAULT_CONCURRENCY), "scan.concurrency"
    )
    tls_skip_verify = _require_bool(
        scan_section.get("tls_skip_verify", False),
        "scan.tls_skip_verify",
    )

    gateway_section = _require_table(
        data.get("gateway"),
        "gateway",
        {"listen", "aggregate_models"},
    )
    listen = _require_string(gateway_section.get("listen", DEFAULT_LISTEN), "gateway.listen")
    listen_host, listen_port = parse_listen(listen)
    aggregate_models = _require_bool(
        gateway_section.get("aggregate_models", DEFAULT_AGGREGATE_MODELS),
        "gateway.aggregate_models",
    )

    return GeneratorConfig(
        hosts=hosts,
        ports=ports,
        scan=ScanOptions(
            scheme=scheme,
            connect_timeout_ms=connect_timeout_ms,
            request_timeout_ms=request_timeout_ms,
            concurrency=concurrency,
            tls_skip_verify=tls_skip_verify,
        ),
        gateway=GatewayOptions(
            listen=_normalize_listen(listen_host, listen_port),
            aggregate_models=aggregate_models,
        ),
    )


def _positive_int(value: Any, name: str) -> int:
    number = _require_int(value, name)
    if number <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return number


def is_loopback_listener(listen: str) -> bool:
    host, _ = parse_listen(listen)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_model_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name.encode("utf-8")) > MAX_MODEL_NAME_BYTES:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        return None
    return name


def _models_from_values(values: Iterable[Any]) -> tuple[str, ...]:
    models: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = _valid_model_name(value)
        if name is not None and name not in seen:
            seen.add(name)
            models.append(name)
    return tuple(models)


def parse_openai_models(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    data = payload.get("data")
    if not isinstance(data, list):
        return ()
    return _models_from_values(item.get("id") for item in data if isinstance(item, dict))


def parse_ollama_models(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    values: list[Any] = []
    for item in models:
        if isinstance(item, dict):
            values.append(item.get("name") or item.get("model"))
    return _models_from_values(values)


def parse_tgi_models(payload: Any) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    values: list[Any] = []
    for key in ("model_id", "model", "served_model_name"):
        value = payload.get(key)
        if isinstance(value, list):
            values.extend(value)
        elif value is not None:
            values.append(value)
    return _models_from_values(values)


PROBES: tuple[Probe, ...] = (
    Probe("/v1/models", parse_openai_models),
    Probe("/api/tags", parse_ollama_models),
    Probe("/info", parse_tgi_models),
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class ProbeClient:
    """HTTP client for model discovery probes."""

    def __init__(self, options: ScanOptions) -> None:
        self._timeout = options.request_timeout_ms / 1000
        self._contexts = {
            "http": None,
            "https": self._build_ssl_context(options.tls_skip_verify),
        }

    @staticmethod
    def _build_ssl_context(skip_verify: bool) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if skip_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _opener(self, scheme: str) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [_NoRedirectHandler()]
        context = self._contexts[scheme]
        if context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def get_json(self, endpoint: Endpoint, path: str) -> Any:
        request = urllib.request.Request(
            endpoint.url(path),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        try:
            with self._opener(endpoint.scheme).open(request, timeout=self._timeout) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise ProbeError(f"HTTP {status}")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ProbeError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProbeError(_describe_network_error(exc)) from exc

        if len(body) > MAX_RESPONSE_BYTES:
            raise ProbeError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProbeError("response is not valid JSON") from exc


def _describe_network_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and exc.reason:
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__


def _port_is_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _candidate_schemes(scheme: str) -> tuple[str, ...]:
    if scheme == "auto":
        return _SCHEMES
    return (scheme,)


def discover_target(host: str, port: int, options: ScanOptions, client: ProbeClient) -> ScanOutcome:
    notes: list[str] = []
    if not _port_is_open(host, port, options.connect_timeout_ms / 1000):
        return ScanOutcome(host=host, port=port, port_open=False)

    for probe in PROBES:
        for scheme in _candidate_schemes(options.scheme):
            endpoint = Endpoint(host=host, port=port, scheme=scheme)
            try:
                payload = client.get_json(endpoint, probe.path)
            except ProbeError as exc:
                notes.append(f"{scheme} {probe.path}: {exc}")
                continue
            models = probe.parser(payload)
            if models:
                return ScanOutcome(
                    host=host,
                    port=port,
                    port_open=True,
                    endpoint=endpoint,
                    models=models,
                    notes=tuple(notes),
                )
            notes.append(f"{scheme} {probe.path}: no models")

    return ScanOutcome(
        host=host,
        port=port,
        port_open=True,
        notes=tuple(notes),
        error="no supported model endpoint found",
    )


def scan_targets(config: GeneratorConfig) -> tuple[ScanOutcome, ...]:
    targets = [(host, port) for host in config.hosts for port in config.ports]
    client = ProbeClient(config.scan)
    outcomes: list[ScanOutcome] = []
    workers = min(config.scan.concurrency, max(1, len(targets)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(discover_target, host, port, config.scan, client): (
                host,
                port,
            )
            for host, port in targets
        }
        for future in concurrent.futures.as_completed(futures):
            host, port = futures[future]
            exception = future.exception()
            if exception is None:
                outcomes.append(future.result())
            elif isinstance(exception, (KeyboardInterrupt, SystemExit)):
                raise exception
            else:
                outcomes.append(
                    ScanOutcome(
                        host=host,
                        port=port,
                        port_open=False,
                        error=f"unexpected scan failure: {exception}",
                    )
                )

    outcomes.sort(key=lambda outcome: (outcome.host, outcome.port))
    return tuple(outcomes)


def build_catalog(outcomes: Iterable[ScanOutcome]) -> dict[str, tuple[Endpoint, ...]]:
    catalog: dict[str, set[Endpoint]] = defaultdict(set)
    for outcome in outcomes:
        if outcome.endpoint is None:
            continue
        for model in outcome.models:
            catalog[model].add(outcome.endpoint)
    return {model: tuple(sorted(endpoints)) for model, endpoints in sorted(catalog.items())}


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _select_endpoints(
    model: str,
    endpoints: Sequence[Endpoint],
) -> tuple[tuple[Endpoint, ...], tuple[str, ...]]:
    http_endpoints = tuple(endpoint for endpoint in endpoints if endpoint.scheme == "http")
    https_endpoints = tuple(endpoint for endpoint in endpoints if endpoint.scheme == "https")
    if http_endpoints and https_endpoints:
        warning = (
            f"model '{model}' has both HTTP and HTTPS endpoints; "
            f"using HTTPS and omitting {len(http_endpoints)} HTTP endpoint(s)"
        )
        return tuple(sorted(https_endpoints)), (warning,)
    return tuple(sorted(endpoints)), ()


def render_config(
    catalog: dict[str, tuple[Endpoint, ...]],
    gateway: GatewayOptions,
) -> tuple[str, tuple[str, ...]]:
    models = sorted(catalog)
    warnings: list[str] = []
    selections: list[tuple[str, str, tuple[Endpoint, ...]]] = []

    for index, model in enumerate(models, start=1):
        endpoints, model_warnings = _select_endpoints(model, catalog[model])
        warnings.extend(model_warnings)
        selections.append((model, f"model-{index:04d}", endpoints))

    lines = [
        "# Generated by praxis-local-net configgen.",
        "# Review before use; see README.md for scope and limitations.",
        "listeners:",
        "  - name: praxis-local-models",
        f"    address: {_quote(gateway.listen)}",
        "    filter_chains:",
        "      - local-models",
        "",
        "filter_chains:",
        "  - name: local-models",
        "    filters:",
    ]

    if gateway.aggregate_models:
        model_list = {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "praxis-local-net",
                }
                for model in models
            ],
        }
        body = json.dumps(model_list, separators=(",", ":"), ensure_ascii=False)
        lines.extend(
            [
                "      - filter: static_response",
                "        status: 200",
                "        headers:",
                "          - name: Content-Type",
                "            value: application/json",
                f"        body: {_quote(body)}",
                "        conditions:",
                "          - when:",
                "              methods: [GET]",
                f"              path: {_quote(AGGREGATE_PATH)}",
            ]
        )

    lines.extend(
        [
            "      - filter: model_to_header",
            f"        header: {MODEL_HEADER}",
            "      - filter: router",
            "        routes:",
        ]
    )

    for model, cluster, _ in selections:
        lines.extend(
            [
                '          - path_prefix: "/"',
                "            headers:",
                f"              {MODEL_HEADER_LOWER}: {_quote(model)}",
                f"            cluster: {_quote(cluster)}",
            ]
        )

    lines.extend(
        [
            "      - filter: load_balancer",
            "        clusters:",
        ]
    )

    for _, cluster, endpoints in selections:
        lines.extend(
            [
                f"          - name: {_quote(cluster)}",
                "            endpoints:",
            ]
        )
        for endpoint in endpoints:
            lines.append(f"              - {_quote(endpoint.authority)}")
        if endpoints and all(endpoint.scheme == "https" for endpoint in endpoints):
            lines.extend(
                [
                    "            http:",
                    "              authority:",
                    "                from: endpoint",
                    "            tls: {}",
                ]
            )

    lines.extend(
        [
            "",
            "insecure_options:",
            "  allow_private_endpoints: true",
            "  allow_private_upstreams: true",
        ]
    )

    return "\n".join(lines) + "\n", tuple(warnings)


def _summary(config: GeneratorConfig, outcomes: Sequence[ScanOutcome], elapsed: float) -> str:
    open_count = sum(1 for outcome in outcomes if outcome.port_open)
    endpoint_count = sum(1 for outcome in outcomes if outcome.models)
    model_count = len({model for outcome in outcomes for model in outcome.models})
    targets = len(config.hosts) * len(config.ports)
    return (
        f"scanned {targets} target(s) across {len(config.hosts)} host(s) "
        f"and {len(config.ports)} port(s); {open_count} open, "
        f"{endpoint_count} model endpoint(s), {model_count} model(s), "
        f"{elapsed:.2f}s"
    )


def _write_output(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise OSError(f"output file exists: {path} (use --force to overwrite)")
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_praxis_config.py",
        description="Discover local AI model servers and generate praxis-ai YAML.",
    )
    parser.add_argument("-c", "--config", required=True, type=Path, help="TOML scan input")
    parser.add_argument(
        "-o", "--output", type=Path, help="write YAML to this file instead of stdout"
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    parser.add_argument("--verbose", action="store_true", help="print per-probe diagnostics")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if not is_loopback_listener(config.gateway.listen):
        print(
            f"warning: listener {config.gateway.listen} is not loopback-bound; "
            "the generated gateway has no client authentication",
            file=sys.stderr,
        )
    if config.scan.tls_skip_verify:
        print(
            "warning: scan.tls_skip_verify is enabled; "
            "certificate verification is disabled during discovery",
            file=sys.stderr,
        )

    started = time.monotonic()
    outcomes = scan_targets(config)
    elapsed = time.monotonic() - started

    if args.verbose:
        for outcome in outcomes:
            for note in outcome.notes:
                print(f"{outcome.host}:{outcome.port} {note}", file=sys.stderr)
            if outcome.error:
                print(f"{outcome.host}:{outcome.port} {outcome.error}", file=sys.stderr)

    print(_summary(config, outcomes, elapsed), file=sys.stderr)
    catalog = build_catalog(outcomes)
    if not catalog:
        print("no models discovered; no configuration written", file=sys.stderr)
        return EXIT_NO_MODELS

    text, warnings = render_config(catalog, config.gateway)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.output is None:
        sys.stdout.write(text)
        return EXIT_OK

    try:
        _write_output(args.output, text, args.force)
    except OSError as exc:
        print(f"output error: {exc}", file=sys.stderr)
        return EXIT_OUTPUT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
