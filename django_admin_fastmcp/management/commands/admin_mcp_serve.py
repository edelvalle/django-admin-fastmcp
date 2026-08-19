"""Run the FastMCP app under its own uvicorn with lifespan enabled (SPEC.md section 12).

Phase 1: a separate process next to the Django project. Nothing about the
host application's serving configuration changes. `stateless_http=True`, so
any instance behind a load balancer can serve any request.

The served path comes from ADMIN_FASTMCP["MCP_URL"], which defaults to
"/admin/mcp". One setting drives the path, the discovery metadata, and the
audience the verifier expects, so the three cannot drift apart.
"""

from urllib.parse import urlsplit

from django.core.management.base import BaseCommand

from django_admin_fastmcp import conf


class Command(BaseCommand):
    help = "Serve the admin MCP server over streamable HTTP."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Address to bind. Defaults to loopback: put a reverse proxy in front.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=None,
            help="Port to bind. Defaults to the port in MCP_URL, or 8765.",
        )

    def handle(self, *args, **options):
        import uvicorn

        from django_admin_fastmcp.server import BodySizeLimit, build_server

        path = conf.mcp_path()
        host = options["host"]
        port = options["port"] or urlsplit(conf.get("MCP_URL")).port or 8765

        app = build_server().http_app(stateless_http=True, path=path)
        self.stdout.write(
            f"Serving {conf.get('SERVER_NAME')} on http://{host}:{port}{path}\n"
            f"Clients must reach it as {conf.get('MCP_URL')}"
        )
        uvicorn.run(BodySizeLimit(app), host=host, port=port, lifespan="on")
