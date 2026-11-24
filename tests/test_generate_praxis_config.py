"""Tests for the praxis-local-net configuration generator."""

from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "generate_praxis_config.py"
SPEC = importlib.util.spec_from_file_location("generate_praxis_config", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


class ConfigTests(unittest.TestCase):
    def test_load_config_applies_defaults_and_normalizes(self) -> None:
        text = """
hosts = ["GPU-01.Local.", "192.168.1.50"]

[ports]
include = [8000]
ranges = ["9000-9002"]
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.toml"
            path.write_text(text, encoding="utf-8")
            config = generator.load_config(path)

        self.assertEqual(config.hosts, ("192.168.1.50", "gpu-01.local"))
        self.assertEqual(config.ports, (8000, 9000, 9001, 9002))
        self.assertEqual(config.scan.scheme, "auto")
        self.assertEqual(config.gateway.listen, "127.0.0.1:8080")
        self.assertTrue(config.gateway.aggregate_models)

    def test_example_config_is_valid(self) -> None:
        config = generator.load_config(ROOT / "example.toml")
        self.assertEqual(len(config.hosts), 3)
        self.assertIn(8000, config.ports)
        self.assertIn(30100, config.ports)

    def test_port_ranges_overlap_and_deduplicate(self) -> None:
        self.assertEqual(
            generator.expand_ports([8000, 8001], ["8001-8003", "8000-8000"]),
            (8000, 8001, 8002, 8003),
        )

    def test_invalid_port_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(generator.ConfigError, "START-END"):
            generator.expand_ports([], ["8000"])
        with self.assertRaisesRegex(generator.ConfigError, "must not exceed"):
            generator.expand_ports([], ["9000-8000"])
        with self.assertRaisesRegex(generator.ConfigError, "between 1 and 65535"):
            generator.expand_ports([0], [])

    def test_host_must_be_bare(self) -> None:
        with self.assertRaisesRegex(generator.ConfigError, "ports belong"):
            generator.normalize_host("gpu-01.local:8000")
        with self.assertRaisesRegex(generator.ConfigError, "bare hostname"):
            generator.normalize_host("http://gpu-01.local")
        with self.assertRaisesRegex(generator.ConfigError, "invalid host"):
            generator.normalize_host("bad_host")

    def test_ipv6_hosts_are_normalized(self) -> None:
        self.assertEqual(generator.normalize_host("[::1]"), "::1")
        self.assertEqual(generator.Endpoint("::1", 8000, "http").authority, "[::1]:8000")

    def test_unknown_keys_are_rejected(self) -> None:
        text = """
hosts = ["localhost"]

[ports]
include = [8000]

[scan]
timeout = 100
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(generator.ConfigError, "unknown key"):
                generator.load_config(path)

    def test_listen_validation_and_loopback_detection(self) -> None:
        self.assertEqual(generator.parse_listen("[::1]:8080"), ("::1", 8080))
        self.assertTrue(generator.is_loopback_listener("127.0.0.1:8080"))
        self.assertTrue(generator.is_loopback_listener("localhost:8080"))
        self.assertFalse(generator.is_loopback_listener("0.0.0.0:8080"))


class ParserTests(unittest.TestCase):
    def test_openai_models(self) -> None:
        payload = {"data": [{"id": "model-a"}, {"id": "model-a"}, {"id": ""}, {}, "bad"]}
        self.assertEqual(generator.parse_openai_models(payload), ("model-a",))

    def test_ollama_models(self) -> None:
        payload = {
            "models": [
                {"name": "llama3:8b"},
                {"model": "qwen2.5:7b"},
                {"name": "llama3:8b"},
            ]
        }
        self.assertEqual(
            generator.parse_ollama_models(payload),
            ("llama3:8b", "qwen2.5:7b"),
        )

    def test_tgi_models(self) -> None:
        self.assertEqual(generator.parse_tgi_models({"model_id": "tgi-model"}), ("tgi-model",))
        self.assertEqual(
            generator.parse_tgi_models({"served_model_name": ["a", "b"]}),
            ("a", "b"),
        )

    def test_model_names_reject_controls_and_empty_values(self) -> None:
        payload = {"data": [{"id": "line\nbreak"}, {"id": "   "}, {"id": 7}]}
        self.assertEqual(generator.parse_openai_models(payload), ())


class CatalogAndRenderTests(unittest.TestCase):
    def _catalog(self) -> dict[str, tuple[generator.Endpoint, ...]]:
        outcomes = (
            generator.ScanOutcome(
                host="gpu-01.local",
                port=8000,
                port_open=True,
                endpoint=generator.Endpoint("gpu-01.local", 8000, "http"),
                models=("model-a", "model-b"),
            ),
            generator.ScanOutcome(
                host="gpu-02.local",
                port=8000,
                port_open=True,
                endpoint=generator.Endpoint("gpu-02.local", 8000, "http"),
                models=("model-a",),
            ),
        )
        return generator.build_catalog(outcomes)

    def test_duplicate_models_share_one_cluster(self) -> None:
        catalog = self._catalog()
        self.assertEqual(len(catalog["model-a"]), 2)
        self.assertEqual(len(catalog["model-b"]), 1)

        text, warnings = generator.render_config(catalog, generator.GatewayOptions())
        self.assertEqual(warnings, ())
        self.assertIn('x-model: "model-a"', text)
        self.assertIn('"gpu-01.local:8000"', text)
        self.assertIn('"gpu-02.local:8000"', text)

    def test_rendered_yaml_structure(self) -> None:
        if yaml is None:
            self.skipTest("PyYAML is not installed")

        catalog = self._catalog()
        text, _ = generator.render_config(catalog, generator.GatewayOptions())
        document = yaml.safe_load(text)
        filters = document["filter_chains"][0]["filters"]

        self.assertEqual(filters[0]["filter"], "static_response")
        model_list = json.loads(filters[0]["body"])
        self.assertEqual([model["id"] for model in model_list["data"]], ["model-a", "model-b"])

        self.assertEqual(filters[1], {"filter": "model_to_header", "header": "X-Model"})

        router = filters[2]
        self.assertEqual(router["filter"], "router")
        routes = {route["headers"]["x-model"]: route["cluster"] for route in router["routes"]}
        self.assertEqual(set(routes), {"model-a", "model-b"})

        load_balancer = filters[3]
        self.assertEqual(load_balancer["filter"], "load_balancer")
        clusters = {cluster["name"]: cluster for cluster in load_balancer["clusters"]}
        self.assertEqual(
            clusters[routes["model-a"]]["endpoints"],
            ["gpu-01.local:8000", "gpu-02.local:8000"],
        )
        self.assertEqual(clusters[routes["model-b"]]["endpoints"], ["gpu-01.local:8000"])

    def test_https_cluster_uses_endpoint_authority(self) -> None:
        catalog = {
            "model-tls": (
                generator.Endpoint("gpu-a.local", 8443, "https"),
                generator.Endpoint("gpu-b.local", 8443, "https"),
            )
        }
        text, warnings = generator.render_config(catalog, generator.GatewayOptions())
        self.assertEqual(warnings, ())
        self.assertIn("authority:", text)
        self.assertIn("from: endpoint", text)
        self.assertIn("tls: {}", text)

        if yaml is not None:
            document = yaml.safe_load(text)
            cluster = document["filter_chains"][0]["filters"][-1]["clusters"][0]
            self.assertEqual(cluster["http"]["authority"], {"from": "endpoint"})
            self.assertEqual(cluster["tls"], {})

    def test_mixed_schemes_prefer_https_and_warn(self) -> None:
        catalog = {
            "mixed": (
                generator.Endpoint("gpu-a.local", 8000, "http"),
                generator.Endpoint("gpu-b.local", 8443, "https"),
            )
        }
        text, warnings = generator.render_config(catalog, generator.GatewayOptions())
        self.assertEqual(len(warnings), 1)
        self.assertIn("using HTTPS", warnings[0])
        self.assertNotIn('"gpu-a.local:8000"', text)
        self.assertIn('"gpu-b.local:8443"', text)

    def test_aggregate_models_can_be_disabled(self) -> None:
        catalog = self._catalog()
        text, _ = generator.render_config(
            catalog,
            generator.GatewayOptions(aggregate_models=False),
        )
        self.assertNotIn("static_response", text)
        self.assertIn("model_to_header", text)

    def test_render_is_deterministic(self) -> None:
        catalog = self._catalog()
        first, _ = generator.render_config(catalog, generator.GatewayOptions())
        second, _ = generator.render_config(catalog, generator.GatewayOptions())
        self.assertEqual(first, second)

    def test_output_file_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "praxis.yaml"
            path.write_text("old", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "use --force"):
                generator._write_output(path, "new", force=False)
            generator._write_output(path, "new", force=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "new")


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = self.server.payloads.get(self.path)  # type: ignore[attr-defined]
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _FixtureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, payloads: dict[str, Any]) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.payloads = payloads


@contextlib.contextmanager
def running_server(payloads: dict[str, Any], tls: tuple[Path, Path] | None = None):
    server = _FixtureServer(payloads)
    if tls is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=tls[0], keyfile=tls[1])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_certificate(directory: Path) -> tuple[Path, Path]:
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return cert, key


class IntegrationTests(unittest.TestCase):
    def test_scan_discovers_openai_ollama_and_tgi(self) -> None:
        with (
            running_server({"/v1/models": {"data": [{"id": "shared-model"}]}}) as openai_port,
            running_server(
                {"/api/tags": {"models": [{"name": "shared-model"}, {"name": "ollama-model"}]}}
            ) as ollama_port,
            running_server({"/info": {"model_id": "tgi-model"}}) as tgi_port,
        ):
            config = generator.GeneratorConfig(
                hosts=("127.0.0.1",),
                ports=(openai_port, ollama_port, tgi_port),
                scan=generator.ScanOptions(
                    scheme="http",
                    connect_timeout_ms=100,
                    request_timeout_ms=500,
                    concurrency=4,
                ),
                gateway=generator.GatewayOptions(),
            )
            outcomes = generator.scan_targets(config)
            catalog = generator.build_catalog(outcomes)

        self.assertEqual(set(catalog), {"shared-model", "ollama-model", "tgi-model"})
        self.assertEqual(len(catalog["shared-model"]), 2)

    def test_non_ai_open_port_is_skipped(self) -> None:
        with running_server({}) as port:
            outcome = generator.discover_target(
                "127.0.0.1",
                port,
                generator.ScanOptions(
                    scheme="http",
                    connect_timeout_ms=100,
                    request_timeout_ms=500,
                ),
                generator.ProbeClient(
                    generator.ScanOptions(
                        scheme="http",
                        connect_timeout_ms=100,
                        request_timeout_ms=500,
                    )
                ),
            )
        self.assertTrue(outcome.port_open)
        self.assertEqual(outcome.models, ())
        self.assertIsNotNone(outcome.error)

    def test_main_returns_nonzero_when_no_models_found(self) -> None:
        port = closed_port()
        text = f"""
hosts = ["127.0.0.1"]

[ports]
include = [{port}]

[scan]
scheme = "http"
connect_timeout_ms = 100
request_timeout_ms = 100
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.toml"
            path.write_text(text, encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = generator.main(["-c", str(path)])
        self.assertEqual(result, generator.EXIT_NO_MODELS)
        self.assertIn("no models discovered", stderr.getvalue())

    def test_main_writes_generated_configuration_to_file(self) -> None:
        with running_server({"/v1/models": {"data": [{"id": "file-model"}]}}) as port:
            text = f"""
hosts = ["127.0.0.1"]

[ports]
include = [{port}]

[scan]
scheme = "http"
connect_timeout_ms = 100
request_timeout_ms = 500
"""
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "scan.toml"
                output_path = Path(directory) / "praxis.yaml"
                config_path.write_text(text, encoding="utf-8")
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = generator.main(["-c", str(config_path), "-o", str(output_path)])
                generated = output_path.read_text(encoding="utf-8")

        self.assertEqual(result, generator.EXIT_OK)
        self.assertIn("file-model", generated)
        self.assertIn("scanned 1 target(s)", stderr.getvalue())

    def test_main_warns_when_tls_skip_verify_is_enabled(self) -> None:
        with running_server({"/v1/models": {"data": [{"id": "model-a"}]}}) as port:
            text = f"""
hosts = ["127.0.0.1"]

[ports]
include = [{port}]

[scan]
scheme = "http"
connect_timeout_ms = 100
request_timeout_ms = 500
tls_skip_verify = true
"""
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "scan.toml"
                path.write_text(text, encoding="utf-8")
                stderr = io.StringIO()
                stdout = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    contextlib.redirect_stdout(stdout),
                ):
                    result = generator.main(["-c", str(path)])

        self.assertEqual(result, generator.EXIT_OK)
        self.assertIn("tls_skip_verify is enabled", stderr.getvalue())
        self.assertIn("model-a", stdout.getvalue())

    @unittest.skipUnless(os.environ.get("PRAXIS_AI_BIN"), "PRAXIS_AI_BIN is not set")
    def test_generated_config_passes_praxis_validation(self) -> None:
        with running_server({"/v1/models": {"data": [{"id": "validated-model"}]}}) as port:
            config = generator.GeneratorConfig(
                hosts=("127.0.0.1",),
                ports=(port,),
                scan=generator.ScanOptions(
                    scheme="http",
                    connect_timeout_ms=100,
                    request_timeout_ms=500,
                ),
                gateway=generator.GatewayOptions(),
            )
            text, _ = generator.render_config(
                generator.build_catalog(generator.scan_targets(config)),
                config.gateway,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "praxis.yaml"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [os.environ["PRAXIS_AI_BIN"], "--validate", "-c", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is required for the TLS test")
    def test_scan_discovers_https_endpoint_in_auto_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_certificate(Path(directory))
            with running_server(
                {"/v1/models": {"data": [{"id": "tls-model"}]}}, tls=(cert, key)
            ) as port:
                config = generator.GeneratorConfig(
                    hosts=("127.0.0.1",),
                    ports=(port,),
                    scan=generator.ScanOptions(
                        scheme="auto",
                        connect_timeout_ms=100,
                        request_timeout_ms=1000,
                        concurrency=1,
                        tls_skip_verify=True,
                    ),
                    gateway=generator.GatewayOptions(),
                )
                outcomes = generator.scan_targets(config)
                catalog = generator.build_catalog(outcomes)

        self.assertEqual(set(catalog), {"tls-model"})
        self.assertEqual(catalog["tls-model"][0].scheme, "https")


if __name__ == "__main__":
    unittest.main()
